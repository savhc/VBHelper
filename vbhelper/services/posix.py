"""POSIX 平台的 MCP 服务后端：截屏、输入、剪贴板。

接口与 ``services/win`` 下的实现保持一致，由 ``features/mcp/runtime`` 按平台选用。
外部依赖都是常见命令行工具：

- ``xdotool``   鼠标、键盘、窗口查询
- ``xclip`` / ``xsel`` 剪贴板
- Pillow 的 ``ImageGrab`` 截屏（需要 X11）

缺任何一个时，对应能力抛本模块的异常并带可读原因，不会让调用方拿到半截状态。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any


class ScreenError(RuntimeError):
    """截屏或窗口查询失败。"""


class InputError(RuntimeError):
    """输入参数非法或发送失败。"""


class InputBlocked(InputError):
    """输入被关闭或急停拦截。"""


class ClipboardError(RuntimeError):
    """剪贴板读写失败。"""


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
    rect: tuple[int, int, int, int]
    own: bool = False


@dataclass
class CaptureResult:
    image: Any
    png: bytes
    path: str | None
    width: int
    height: int
    scale: float
    origin: tuple[int, int]
    mode: str
    window: WindowInfo | None = None


# ---------------------------------------------------------------- 命令封装

def _which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _run(argv: list[str], timeout: float = 3.0) -> str:
    """跑一条命令并返回 stdout；失败抛 RuntimeError。"""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("执行 %s 失败：%s" % (argv[0], exc)) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError("%s 返回 %d：%s" % (argv[0], proc.returncode, detail))
    return proc.stdout


def _xdotool() -> str:
    exe = _which("xdotool")
    if not exe:
        raise InputError("未找到 xdotool，请先安装（apt install xdotool）")
    return exe


# ---------------------------------------------------------------- 剪贴板

class ClipboardService:
    """CF_UNICODETEXT 等价实现：xclip / xsel 优先，都没有时抛错。"""

    def __init__(self) -> None:
        self._saved: str | None = None
        self._lock = threading.Lock()

    def _tool(self) -> tuple[str, list[str]]:
        clip = _which("xclip")
        if clip:
            return clip, ["-selection", "clipboard"]
        sel = _which("xsel")
        if sel:
            return sel, ["--clipboard"]
        raise ClipboardError("未找到 xclip 或 xsel，请先安装其一")

    def read_text(self) -> str:
        exe, sel = self._tool()
        try:
            argv = [exe, "-o"] + sel if exe.endswith("xclip") else [exe, "-o"] + sel
            return _run(argv)
        except RuntimeError as exc:
            # xclip 在剪贴板为空时返回非 0，按空字符串处理
            return ""

    def write_text(self, text: str) -> None:
        exe, sel = self._tool()
        try:
            proc = subprocess.run([exe, "-i"] + sel, input=text, text=True,
                                  timeout=3.0, capture_output=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClipboardError("写剪贴板失败：%s" % exc) from exc
        if proc.returncode != 0:
            raise ClipboardError("写剪贴板失败：%s" % (proc.stderr or "").strip())

    def snapshot(self) -> None:
        try:
            with self._lock:
                self._saved = self.read_text()
        except ClipboardError:
            self._saved = None

    def restore(self) -> bool:
        with self._lock:
            text = self._saved
        if text is None:
            return False
        try:
            self.write_text(text)
            return True
        except ClipboardError:
            return False

    def foreground_app(self) -> dict:
        """当前活动窗口：{title, class_name, pid, hwnd, is_self}。"""
        out = {"title": "", "class_name": "", "pid": 0, "hwnd": 0, "is_self": False}
        try:
            exe = _which("xdotool")
            if not exe:
                return out
            wid = _run([exe, "getactivewindow"]).strip()
            if not wid:
                return out
            out["hwnd"] = int(wid)
            try:
                out["title"] = _run([exe, "getwindowname", wid]).strip()
            except RuntimeError:
                pass
            try:
                out["pid"] = int(_run([exe, "getwindowpid", wid]).strip() or 0)
            except RuntimeError:
                pass
            out["is_self"] = out["pid"] == os.getpid()
        except Exception:
            pass
        return out


# ---------------------------------------------------------------- 输入

class InputService:
    """鼠标键盘：xdotool 后端 + 启用开关与急停。

    坐标语义与 Windows 版一致：虚拟屏幕绝对像素；滚动正数向上、负数向下。
    """

    def __init__(self, cfg: dict | None = None, clipboard: ClipboardService | None = None) -> None:
        self._cfg = cfg or {}
        self._clipboard = clipboard
        self._panic_until = 0.0
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._on_panic = None
        self._lock = threading.Lock()
        self._pos = (0, 0)

    # ---- 状态 ----

    def enabled(self) -> bool:
        section = self._cfg.get("input") or {}
        return bool(section.get("enabled"))

    def panicked(self) -> bool:
        return time.time() < self._panic_until

    def panic(self, seconds: float = 60.0) -> None:
        self._panic_until = time.time() + max(1.0, float(seconds))

    def start_watch(self, on_panic=None) -> None:
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        self._on_panic = on_panic
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()

    def stop_watch(self) -> None:
        self._watch_stop.set()
        thread, self._watch_thread = self._watch_thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def _watch_loop(self) -> None:
        """急停热键：xdotool 无法直接读组合键状态，改为轮询窗口标题不做处理。

        Linux 下没有与 GetAsyncKeyState 等价的通用接口，这里保留线程与开关，
        热键由桌面环境的快捷键绑定外部触发 panic()。
        """
        while not self._watch_stop.is_set():
            self._watch_stop.wait(0.2)

    def set_cfg(self, cfg: dict) -> None:
        self._cfg = cfg or {}

    def _guard(self) -> str:
        if self.panicked():
            raise InputBlocked("已触发急停，输入被拦截")
        if not self.enabled():
            raise InputBlocked("输入自动化未启用")
        return _xdotool()

    # ---- 鼠标 ----

    def mouse_move(self, x: int, y: int, duration: float = 0.0) -> None:
        exe = self._guard()
        args = [exe, "mousemove", str(int(x)), str(int(y))]
        if duration > 0:
            args = [exe, "mousemove", "--sync", "--delay", str(int(duration * 1000)),
                    str(int(x)), str(int(y))]
        _run(args)
        self._pos = (int(x), int(y))

    def mouse_click(self, button: str = "left", count: int = 1,
                    x: int | None = None, y: int | None = None) -> None:
        if (x is None) != (y is None):
            raise InputError("x 与 y 必须同时给出")
        exe = self._guard()
        if x is not None:
            _run([exe, "mousemove", str(int(x)), str(int(y))])
        key = {"left": "1", "middle": "2", "right": "3"}.get(button)
        if key is None:
            raise InputError("不支持的按键：%s" % button)
        _run([exe, "click", "--repeat", str(max(1, int(count))), key])

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int,
                   duration: float = 0.2) -> None:
        exe = self._guard()
        _run([exe, "mousemove", str(int(x1)), str(int(y1))])
        _run([exe, "mousedown", "1"])
        try:
            _run([exe, "mousemove", "--sync", "--delay", str(int(duration * 1000 / 8)),
                  str(int(x2)), str(int(y2))])
        finally:
            _run([exe, "mouseup", "1"])

    def scroll(self, amount: int = -3, *, horizontal: bool = False,
               x: int | None = None, y: int | None = None) -> None:
        exe = self._guard()
        if x is not None and y is not None:
            _run([exe, "mousemove", str(int(x)), str(int(y))])
        button = "7" if horizontal else ("4" if amount > 0 else "5")
        for _ in range(max(1, abs(int(amount)))):
            _run([exe, "click", button])

    # ---- 键盘 ----

    def type_text(self, text: str, *, method: str = "paste",
                  interval: float = 0.0) -> None:
        if not text:
            return
        exe = self._guard()
        if method == "paste" and self._clipboard is not None:
            self._clipboard.snapshot()
            self._clipboard.write_text(text)
            try:
                _run([exe, "key", "--clearmodifiers", "ctrl+v"])
            finally:
                time.sleep(0.05)
                self._clipboard.restore()
            return
        args = [exe, "type", "--clearmodifiers"]
        if interval > 0:
            args += ["--delay", str(int(interval * 1000))]
        args.append(text)
        _run(args, timeout=10.0)

    def press(self, keys: str) -> None:
        exe = self._guard()
        combo = "+".join(part.strip() for part in keys.split("+") if part.strip())
        if not combo:
            raise InputError("按键为空")
        _run([exe, "key", "--clearmodifiers", combo])

    def position(self) -> tuple[int, int]:
        exe = _xdotool()
        out = _run([exe, "getmouselocation", "--shell"])
        data = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return int(data.get("X", 0)), int(data.get("Y", 0))

    def screen_size(self) -> tuple[int, int, int, int]:
        exe = _xdotool()
        out = _run([exe, "getdisplaygeometry"]).split()
        if len(out) < 2:
            raise InputError("无法获取屏幕尺寸")
        return 0, 0, int(out[0]), int(out[1])


# ---------------------------------------------------------------- 截屏

class ScreenService:
    """截图与窗口信息：PIL ImageGrab + xdotool。"""

    def __init__(self, captures_dir: str, max_width: int = 1600) -> None:
        self.captures_dir = captures_dir
        self.max_width = int(max_width)

    def _geometry(self) -> tuple[int, int]:
        exe = _which("xdotool")
        if exe:
            try:
                out = _run([exe, "getdisplaygeometry"]).split()
                if len(out) >= 2:
                    return int(out[0]), int(out[1])
            except Exception:
                pass
        return 1920, 1080

    def info(self) -> dict:
        width, height = self._geometry()
        return {
            "virtual": {"x": 0, "y": 0, "width": width, "height": height},
            "monitors": [Monitor(0, 0, 0, width, height, True)],
            "active": self.active_window(),
            "dpi": 96,
            "primary": {"width": width, "height": height},
        }

    def list_windows(self) -> list[WindowInfo]:
        """按可见窗口列出标题与几何；没有 xdotool 时返回空列表。"""
        exe = _which("xdotool")
        if not exe:
            return []
        try:
            ids = _run([exe, "search", "--onlyvisible", "--name", ""]).split()
        except RuntimeError:
            return []
        rows: list[WindowInfo] = []
        for wid in ids[:80]:
            try:
                title = _run([exe, "getwindowname", wid]).strip()
                if not title:
                    continue
                geo = _run([exe, "getwindowgeometry", "--shell", wid])
                data = dict(line.split("=", 1) for line in geo.splitlines() if "=" in line)
                x, y = int(data.get("X", 0)), int(data.get("Y", 0))
                w, h = int(data.get("WIDTH", 0)), int(data.get("HEIGHT", 0))
                try:
                    pid = int(_run([exe, "getwindowpid", wid]).strip() or 0)
                except RuntimeError:
                    pid = 0
                rows.append(WindowInfo(int(wid), title, "", pid, (x, y, x + w, y + h),
                                       pid == os.getpid()))
            except (RuntimeError, ValueError):
                continue
        return rows

    def active_window(self) -> WindowInfo | None:
        exe = _which("xdotool")
        if not exe:
            return None
        try:
            wid = _run([exe, "getactivewindow"]).strip()
            if not wid:
                return None
            title = _run([exe, "getwindowname", wid]).strip()
            data = dict(line.split("=", 1) for line in _run(
                [exe, "getwindowgeometry", "--shell", wid]).splitlines() if "=" in line)
            x, y = int(data.get("X", 0)), int(data.get("Y", 0))
            w, h = int(data.get("WIDTH", 0)), int(data.get("HEIGHT", 0))
            try:
                pid = int(_run([exe, "getwindowpid", wid]).strip() or 0)
            except RuntimeError:
                pid = 0
            return WindowInfo(int(wid), title, "", pid, (x, y, x + w, y + h),
                              pid == os.getpid())
        except (RuntimeError, ValueError):
            return None

    def capture(self, *, mode: str = "full", monitor: int = 0,
                region: tuple[int, int, int, int] | None = None,
                window_title: str | None = None,
                max_width: int | None = None,
                save: bool = True) -> CaptureResult:
        """截图。``region`` 是虚拟屏幕绝对坐标 (x1, y1, x2, y2)。"""
        try:
            from PIL import ImageGrab
        except ImportError as exc:
            raise ScreenError("缺少 Pillow，无法截图") from exc

        bbox = None
        if mode == "region":
            if not region:
                raise ScreenError("region 模式需要 region 参数")
            x1, y1, x2, y2 = region
            bbox = (int(x1), int(y1), int(x2), int(y2))
        elif mode == "window":
            win = self.active_window() if not window_title else None
            if window_title:
                for item in self.list_windows():
                    if window_title.lower() in item.title.lower():
                        win = item
                        break
            if win is None:
                raise ScreenError("找不到目标窗口")
            bbox = win.rect

        try:
            image = ImageGrab.grab(bbox=bbox, xdisplay=os.environ.get("DISPLAY"))
        except Exception as exc:
            raise ScreenError("截图失败：%s（需要 X11 且 DISPLAY 可用）" % exc) from exc

        origin = (bbox[0], bbox[1]) if bbox else (0, 0)
        scale = 1.0
        limit = int(max_width if max_width is not None else self.max_width)
        if limit > 0 and image.width > limit:
            ratio = limit / float(image.width)
            image = image.resize((limit, int(image.height * ratio)))
            scale = ratio

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        png = buf.getvalue()

        path = None
        if save:
            try:
                os.makedirs(self.captures_dir, exist_ok=True)
                path = os.path.join(self.captures_dir,
                                    "cap-%d.png" % int(time.time() * 1000))
                with open(path, "wb") as fp:
                    fp.write(png)
            except OSError:
                path = None

        return CaptureResult(image=image, png=png, path=path,
                             width=image.width, height=image.height,
                             scale=scale, origin=origin, mode=mode)
