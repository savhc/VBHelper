# -*- coding: utf-8 -*-
"""截屏服务：虚拟屏幕信息、窗口枚举、全屏/区域/窗口截图。

设计要点（见 docs/plan/impl/mcp-block.md §3.1）：
- 坐标一律是**虚拟屏幕绝对像素**（多显示器时左上角可负）；截图结果带
  ``origin``，调用方用 ``screen_x = origin_x + img_x / scale`` 换算回屏幕坐标；
- 全屏/区域截图用 PIL ``ImageGrab.grab(all_screens=True)``，再按虚拟屏幕原点
  自己裁剪（不依赖 PIL 的 bbox 语义，避免不同 Pillow 版本行为差异）；
- 窗口截图优先 ``PrintWindow(PW_RENDERFULLCONTENT)``，失败回退屏幕区域裁剪；
- 只缩不放：``max_width<=0`` 不缩放；缩放不改变 ``origin``；
- 纯 ctypes/PIL，不 import Tk，可在任意线程调用；
- **不改进程 DPI 设置**：宿主（vbhelper/app.py）已 ``SetProcessDpiAwareness(2)``，
  本模块直接读取感知后的 GetSystemMetrics。若宿主是 DPI-unaware 进程，
  GetSystemMetrics 与 ImageGrab 的物理像素可能不一致，此时请由宿主先设 DPI。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import io
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from PIL import Image, ImageGrab

# ---------------- Win32 常量 ----------------

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
PW_RENDERFULLCONTENT = 0x00000002
MONITORINFOF_PRIMARY = 0x00000001
DIB_RGB_COLORS = 0
BI_RGB = 0

# 这些窗口类即使可见且非工具窗口也不是用户想操作的目标
_SKIP_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "DV2ControlHost", "MsgrIMEWindowClass", "SysShadow", "Button",
}


class ScreenError(RuntimeError):
    """截屏/窗口操作失败（带中文原因，UI 可直接显示）。"""


# ---------------- ctypes 声明 ----------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int

_user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
_user32.EnumWindows.restype = wt.BOOL
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.IsWindowVisible.restype = wt.BOOL
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wt.LONG
_user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.GetWindowRect.restype = wt.BOOL
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wt.HWND
_user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p,
                                        ctypes.c_void_p, wt.LPARAM]
_user32.EnumDisplayMonitors.restype = wt.BOOL
_user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
_user32.PrintWindow.restype = wt.BOOL
_user32.GetDC.argtypes = [wt.HWND]
_user32.GetDC.restype = wt.HDC
_user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_user32.ReleaseDC.restype = ctypes.c_int

_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
_gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_gdi32.SelectObject.restype = wt.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
_gdi32.DeleteObject.restype = wt.BOOL
_gdi32.DeleteDC.argtypes = [wt.HDC]
_gdi32.DeleteDC.restype = wt.BOOL
_gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                             ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
_gdi32.GetDIBits.restype = ctypes.c_int

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                                     ctypes.POINTER(wt.RECT), wt.LPARAM)


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# ---------------- 数据模型 ----------------

@dataclass
class Monitor:
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    rect: tuple[int, int, int, int]          # 虚拟屏幕坐标 l,t,r,b
    own: bool = False                        # 是否本进程（VBHelper 自己的窗口）


@dataclass
class CaptureResult:
    image: Any                               # PIL.Image.Image
    png: bytes
    path: str | None
    width: int
    height: int
    scale: float                             # 图片/屏幕像素；1.0 = 未缩放
    origin: tuple[int, int]                  # 截图左上角的虚拟屏幕坐标
    mode: str
    window: WindowInfo | None = None


# ---------------- 底层工具 ----------------

def _get_dpi() -> int:
    """GetDpiForSystem（Win10 1607+），失败回退 96。"""
    try:
        fn = _user32.GetDpiForSystem
        fn.argtypes = []
        fn.restype = wt.UINT
        value = int(fn())
        return value if value > 0 else 96
    except (AttributeError, OSError):
        return 96


def virtual_screen() -> tuple[int, int, int, int]:
    """虚拟屏幕矩形 (x, y, width, height)，多显示器左上角可为负。"""
    x = int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    w = int(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    h = int(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if w <= 0 or h <= 0:
        raise ScreenError("无法读取虚拟屏幕尺寸")
    return x, y, w, h


def _window_text(hwnd: int) -> str:
    length = int(_user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ScreenError("读取窗口位置失败（hwnd=%s）" % hwnd)
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _window_info(hwnd: int) -> WindowInfo:
    return WindowInfo(hwnd=int(hwnd) if hwnd else 0,
                      title=_window_text(hwnd),
                      class_name=_class_name(hwnd),
                      pid=_window_pid(hwnd),
                      rect=_window_rect(hwnd))


def _enum_windows(*, visible_only: bool = True) -> list[WindowInfo]:
    """按 Z 顺序（顶层在前）枚举有标题、非工具窗口的顶层窗口。

    不过滤自身进程：窗口截图允许抓本应用自己的窗口；list_windows 再过滤。
    """
    result: list[WindowInfo] = []

    def _callback(hwnd: int, _lparam: int) -> bool:
        try:
            if visible_only and not _user32.IsWindowVisible(hwnd):
                return True
            title = _window_text(hwnd)
            if not title.strip():
                return True
            class_name = _class_name(hwnd)
            if class_name in _SKIP_CLASSES:
                return True
            if int(_user32.GetWindowLongW(hwnd, GWL_EXSTYLE)) & WS_EX_TOOLWINDOW:
                return True
            try:
                rect = _window_rect(hwnd)
            except ScreenError:
                return True
            if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
                return True
            result.append(WindowInfo(hwnd=int(hwnd), title=title,
                                     class_name=class_name,
                                     pid=_window_pid(hwnd), rect=rect))
        except Exception:
            pass                                  # 单个窗口失败不影响枚举
        return True

    callback = WNDENUMPROC(_callback)
    if not _user32.EnumWindows(callback, 0):
        # EnumWindows 返回 0 也可能只是回调提前终止；有结果就当成功
        if not result:
            raise ScreenError("窗口枚举失败")
    return result


def _find_window(title: str) -> WindowInfo:
    """按标题子串查找窗口（不区分大小写），返回 Z 序最靠前的一个。"""
    needle = (title or "").strip().lower()
    if not needle:
        raise ScreenError("未指定窗口标题")
    windows = _enum_windows()
    for info in windows:
        if needle in info.title.lower():
            return info
    raise ScreenError("找不到标题包含「%s」的可见窗口" % title)


def _monitors() -> list[Monitor]:
    """EnumDisplayMonitors + GetMonitorInfoW，按屏幕坐标排序。"""
    raw: list[Monitor] = []

    def _callback(hmonitor: int, _hdc: int, _rect: Any, _lparam: int) -> bool:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r = info.rcMonitor
            raw.append(Monitor(index=0, x=int(r.left), y=int(r.top),
                               width=int(r.right - r.left),
                               height=int(r.bottom - r.top),
                               primary=bool(info.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    callback = MONITORENUMPROC(_callback)
    _user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.c_void_p]
    _user32.GetMonitorInfoW.restype = wt.BOOL
    _user32.EnumDisplayMonitors(None, None, callback, 0)
    if not raw:
        # 枚举失败时至少给出主屏，保证 info() 可用
        x, y, w, h = virtual_screen()
        return [Monitor(index=0, x=x, y=y, width=w, height=h, primary=True)]
    raw.sort(key=lambda m: (m.y, m.x))
    for i, mon in enumerate(raw):
        mon.index = i
    return raw


def _grab_virtual() -> tuple[Image.Image, tuple[int, int]]:
    """抓整块虚拟屏幕，返回 (图像, 虚拟屏幕左上角)。

    Pillow 的 all_screens=True 返回包含所有显示器的整屏，其像素原点就是
    虚拟屏幕原点（多显示器时可能是负数）。这里显式记录 origin，后续裁剪
    一律用 ``坐标 - origin`` 的相对像素，避免依赖 Pillow bbox 语义。
    """
    try:
        image = ImageGrab.grab(all_screens=True)
    except Exception as exc:                       # OSError / 平台错误
        raise ScreenError("截屏失败：%s" % exc) from exc
    image.load()
    vx, vy, _vw, _vh = virtual_screen()
    return image, (vx, vy)


def _crop_relative(image: Image.Image, origin: tuple[int, int],
                   box: tuple[int, int, int, int]) -> Image.Image:
    """按虚拟屏幕坐标裁剪整屏图像；超出部分夹到图像内。"""
    x0, y0 = origin
    left = max(0, box[0] - x0)
    top = max(0, box[1] - y0)
    right = min(image.width, box[2] - x0)
    bottom = min(image.height, box[3] - y0)
    if right <= left or bottom <= top:
        raise ScreenError("截图区域不在屏幕范围内：%s" % (box,))
    return image.crop((left, top, right, bottom))


def _print_window(hwnd: int, width: int, height: int) -> Image.Image | None:
    """PrintWindow(PW_RENDERFULLCONTENT) 抓窗口位图；失败返回 None。"""
    if width <= 0 or height <= 0:
        return None
    hdc_screen = _user32.GetDC(None)
    if not hdc_screen:
        return None
    hdc_mem = None
    hbmp = None
    old = None
    try:
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            return None
        hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        if not hbmp:
            return None
        old = _gdi32.SelectObject(hdc_mem, hbmp)
        if not _user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT):
            return None
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height                 # 负高度 = 自上而下的 DIB
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        info = _BITMAPINFO()
        info.bmiHeader = header
        buf = ctypes.create_string_buffer(width * height * 4)
        copied = _gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf,
                                  ctypes.byref(info), DIB_RGB_COLORS)
        if copied != height:
            return None
        # 32bpp BGRA（BGRX）逐行无 padding，直接包装成 RGB
        image = Image.frombuffer("RGB", (width, height), buf,
                                 "raw", "BGRX", 0, 1)
        image = image.copy()
        if image.getbbox() is None:               # 全黑：最小化/受保护窗口
            return None
        return image
    except Exception:
        return None
    finally:
        if old:
            try:
                _gdi32.SelectObject(hdc_mem, old)
            except Exception:
                pass
        if hbmp:
            _gdi32.DeleteObject(hbmp)
        if hdc_mem:
            _gdi32.DeleteDC(hdc_mem)
        if hdc_screen:
            _user32.ReleaseDC(None, hdc_screen)


def _scale_image(image: Image.Image,
                 max_width: int) -> tuple[Image.Image, float]:
    """只缩不放：宽超过 max_width 时等比缩放，返回 (图像, scale)。"""
    if max_width <= 0 or image.width <= max_width:
        return image, 1.0
    new_w = int(max_width)
    new_h = max(1, int(round(image.height * new_w / image.width)))
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    return resized, new_w / image.width


def _png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ---------------- 服务 ----------------

class ScreenService:
    """截屏 + 虚拟屏幕/窗口信息（无 GUI、无线程状态）。"""

    def __init__(self, save_dir: str, *, max_width: int = 1600) -> None:
        self.save_dir = save_dir
        self.max_width = int(max_width)

    # -------- 信息 --------

    def info(self) -> dict:
        """虚拟屏幕、显示器列表、活动窗口、系统 DPI、主屏尺寸。"""
        vx, vy, vw, vh = virtual_screen()
        monitors = _monitors()
        screen_w = int(_user32.GetSystemMetrics(SM_CXSCREEN))
        screen_h = int(_user32.GetSystemMetrics(SM_CYSCREEN))
        active = self.active_window()
        return {
            "virtual_screen": {"x": vx, "y": vy, "w": vw, "h": vh},
            "monitors": [asdict(m) for m in monitors],
            "active_window": asdict(active) if active else None,
            "dpi": _get_dpi(),
            "primary_size": [screen_w, screen_h],
        }

    def list_windows(self, limit: int = 50,
                     visible_only: bool = True,
                     include_self: bool = False) -> list[WindowInfo]:
        """按 Z 顺序列出窗口；默认过滤自身进程。

        ``include_self=True`` 时保留 VBHelper 自己的窗口并标 ``own=True``，
        方便 AI 确认「应用在后台运行」或定位自己的面板。
        """
        own_pid = os.getpid()
        out: list[WindowInfo] = []
        for info in _enum_windows(visible_only=visible_only):
            info.own = info.pid == own_pid
            if info.own and not include_self:
                continue
            out.append(info)
            if 0 < limit <= len(out):
                break
        return out

    def active_window(self) -> WindowInfo | None:
        hwnd = int(_user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        try:
            info = _window_info(hwnd)
            info.own = info.pid == os.getpid()
            return info
        except ScreenError:
            return None

    # -------- 截图 --------

    def capture(self, *, mode: str = "full", monitor: int = 0,
                region: tuple[int, int, int, int] | None = None,
                window_title: str | None = None,
                max_width: int | None = None,
                save: bool = True) -> CaptureResult:
        """截图。

        - mode="full"：整块虚拟屏幕；``monitor>0`` 时改抓 monitor.index 对应
          显示器（Monitor.index 从 0 开始，0 表示不选显示器 = 整屏）；
        - mode="monitor"：按 ``monitor``（0 起）抓单个显示器；
        - mode="region"：``region=(x1,y1,x2,y2)`` 虚拟屏幕绝对坐标；
        - mode="window"：``window_title`` 子串匹配（不区分大小写）；
        - max_width：None 用构造参数，<=0 不缩放；只缩不放。
        """
        mode = (mode or "full").strip().lower()
        limit = self.max_width if max_width is None else int(max_width)
        window: WindowInfo | None = None

        if mode == "window":
            if not window_title:
                raise ScreenError("window 模式必须提供 window_title")
            window = _find_window(window_title)
            image = self._capture_window(window)
            origin = (window.rect[0], window.rect[1])
            mode = "window"
        elif mode in ("full", "monitor"):
            index: int | None = None
            if mode == "monitor":
                index = int(monitor or 0)
            elif monitor and int(monitor) > 0:
                index = int(monitor)
            if index is not None:
                monitors = _monitors()
                match = next((m for m in monitors if m.index == index), None)
                if match is None:
                    raise ScreenError("没有编号为 %d 的显示器" % index)
                full, _full_origin = _grab_virtual()
                image = _crop_relative(full, _full_origin,
                                       (match.x, match.y,
                                        match.x + match.width,
                                        match.y + match.height))
                # 显示器截图左上角就是该显示器原点（裁剪不改变坐标）
                origin = (match.x, match.y)
                mode = "monitor"
            else:
                image, origin = _grab_virtual()
                mode = "full"
        elif mode == "region":
            if region is None:
                raise ScreenError("region 模式必须提供 region=(x1,y1,x2,y2)")
            x1, y1, x2, y2 = (int(v) for v in region)
            if x2 <= x1 or y2 <= y1:
                raise ScreenError("region 需要 x2>x1 且 y2>y1")
            full, full_origin = _grab_virtual()
            image = _crop_relative(full, full_origin, (x1, y1, x2, y2))
            # 裁剪可能夹边：以实际图像大小回推真实左上角
            left = max(0, x1 - full_origin[0]) + full_origin[0]
            top = max(0, y1 - full_origin[1]) + full_origin[1]
            origin = (left, top)
            mode = "region"
        else:
            raise ScreenError("未知截图模式：%s（full/region/window）" % mode)

        image, scale = _scale_image(image, limit)
        png = _png_bytes(image)
        path = self._save_png(png) if save else None
        return CaptureResult(image=image, png=png, path=path,
                             width=image.width, height=image.height,
                             scale=scale, origin=origin, mode=mode,
                             window=window)

    # -------- 内部 --------

    def _capture_window(self, window: WindowInfo) -> Image.Image:
        """PrintWindow 优先；失败回退屏幕区域（窗口需在虚拟屏幕内）。"""
        left, top, right, bottom = window.rect
        width, height = right - left, bottom - top
        image = _print_window(window.hwnd, width, height)
        if image is not None:
            return image
        vx, vy, vw, vh = virtual_screen()
        if right <= vx or bottom <= vy or left >= vx + vw or top >= vy + vh:
            raise ScreenError("窗口「%s」不在屏幕内（可能已最小化）"
                              % window.title)
        full, origin = _grab_virtual()
        return _crop_relative(full, origin, window.rect)

    def _save_png(self, png: bytes) -> str:
        """写 save_dir/capture_YYYYmmdd_HHMMSS_mmm.png，自动建目录。"""
        try:
            os.makedirs(self.save_dir, exist_ok=True)
        except OSError as exc:
            raise ScreenError("创建截图目录失败：%s" % exc) from exc
        stamp = time.strftime("%Y%m%d_%H%M%S") + "_%03d" % (
            int(time.time() * 1000) % 1000)
        base = "capture_%s" % stamp
        path = os.path.join(self.save_dir, base + ".png")
        suffix = 1
        while os.path.exists(path):
            path = os.path.join(self.save_dir, "%s_%d.png" % (base, suffix))
            suffix += 1
        try:
            with open(path, "wb") as f:
                f.write(png)
        except OSError as exc:
            raise ScreenError("保存截图失败：%s" % exc) from exc
        return path
