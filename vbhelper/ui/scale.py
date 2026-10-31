# -*- coding: utf-8 -*-
"""DPI 缩放与字号/间距工具。

所有新 UI 的物理像素都从这里换算，禁止写死像素。
Windows 用 GetDpiForWindow 取窗口 DPI；其它平台回落到 Tk 的 fpixels("1i")。
"""
import ctypes
import math
import tkinter as tk
from tkinter import font as tkfont

from .. import platform as _plat

# 全局状态：init() 后有效
_root: tk.Misc | None = None
_dpi: int = 96
_scale: float = 1.0

# 界面字体候选，按平台取值（与 core.pick_font 同源）
_FONT_FAMILIES = _plat.ui_font_candidates()

# 字体缓存：(family, size_px, weight) -> Font，避免重复注册具名字体
_font_cache: dict[tuple[str, int, str], tkfont.Font] = {}
_font_family: str | None = None


def _get_dpi_for_window(root: tk.Misc) -> int | None:
    """取窗口 DPI：Windows 用 GetDpiForWindow，其它平台返回 None 走回落路径。"""
    if not _plat.IS_WINDOWS:
        return None
    try:
        hwnd = int(root.winfo_id())
        if hwnd:
            val = ctypes.windll.user32.GetDpiForWindow(hwnd)
            if val and val > 0:
                return int(val)
    except Exception:
        pass
    return None


def _dpi_from_fpixels(root: tk.Misc) -> int:
    """回退方案：1 英寸对应的像素数即 DPI。"""
    try:
        fp = float(root.winfo_fpixels("1i"))
        if fp > 0:
            return int(round(fp))
    except Exception:
        pass
    return 96


def init(root: tk.Misc) -> None:
    """绑定 Tk 根窗口并测量当前 DPI；窗口换屏幕重开时可再调一次。"""
    global _root, _dpi, _scale
    _root = root
    try:
        root.winfo_id()   # 确保窗口句柄已生成
    except Exception:
        pass
    dpi = _get_dpi_for_window(root)
    if dpi is None:
        dpi = _dpi_from_fpixels(root)
    _dpi = int(dpi)
    _scale = _dpi / 96.0


def dpi() -> int:
    """当前 DPI（96/120/144/192…）。未 init 时返回 96。"""
    return _dpi


def scale() -> float:
    """缩放系数 = dpi / 96。"""
    return _scale


def s(n: float) -> int:
    """逻辑像素 -> 物理像素，四舍五入（半数向上），最小 1。"""
    v = int(math.floor(n * _scale + 0.5))
    return v if v >= 1 else 1


def _default_family() -> str:
    """挑选可用中文字体族；只在第一次取 font 时探测一次。"""
    global _font_family
    if _font_family is not None:
        return _font_family
    fam = "TkDefaultFont"
    try:
        fams = set(tkfont.families(_root))
        for f in _FONT_FAMILIES:
            if f in fams:
                fam = f
                break
    except Exception:
        pass
    _font_family = fam
    return fam


def font(size: int, weight: str = "normal",
         family: str | None = None) -> tkfont.Font:
    """按逻辑字号创建 tkfont.Font。

    size 用负数（Tk 像素字号），字号随 DPI 一起缩放，行高同步。

    示例::

        f = scale.font(13, weight="bold")
        tk.Label(parent, text="标题", font=f)
    """
    fam = family if family else _default_family()
    px = s(size)
    key = (fam, px, weight)
    f = _font_cache.get(key)
    if f is None:
        f = tkfont.Font(root=_root, family=fam, size=-px, weight=weight)
        _font_cache[key] = f
    return f


def reset_cache() -> None:
    """DPI 变化或换根窗口时清空字体缓存。"""
    global _font_family
    _font_cache.clear()
    _font_family = None


def window_size(w: int, h: int) -> tuple[int, int]:
    """按逻辑尺寸换算窗口物理尺寸。"""
    return s(w), s(h)


def _work_area() -> tuple[int, int, int, int]:
    """工作区 (left, top, right, bottom)，自动排除任务栏。"""
    try:
        import ctypes.wintypes as wt

        class RECT(ctypes.Structure):
            _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                        ("right", wt.LONG), ("bottom", wt.LONG)]

        r = RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        if r.right > r.left and r.bottom > r.top:
            return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:
        pass
    return 0, 0, 1920, 1080


def clamp_to_work_area(win, w: int, h: int) -> tuple[int, int]:
    """把 (w, h) 钳制到工作区范围内（小屏自动缩小）。"""
    left, top, right, bottom = _work_area()
    wa_w, wa_h = right - left, bottom - top
    return min(max(w, 1), wa_w), min(max(h, 1), wa_h)
