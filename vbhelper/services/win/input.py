# -*- coding: utf-8 -*-
"""鼠标/键盘服务：SendInput 后端 + 策略/急停层（见 mcp-block.md §3.2）。

坐标约定
--------
- 所有公开方法使用**虚拟屏幕绝对像素**（多显示器左上角可为负）；
- 绝对移动用 ``MOUSEEVENTF_MOVE|ABSOLUTE|VIRTUALDESK``，按虚拟屏幕
  ``0..65535`` 归一化（分母是 w-1 / h-1，所以最右/最下恰好是 65535）；
- ``scroll(amount)``：amount 以“格”为单位，**负数向下**、正数向上
  （每格 = WHEEL_DELTA = 120）；``horizontal=True`` 时正数向右。

INPUT 结构（x64）
----------------
``INPUT`` 必须是 40 字节：``type``(4) + 4 字节对齐 + union(32)。
union 里最大成员 ``MOUSEINPUT``（4+4+4+4+4+8=28，对齐到 32）。
结构错了 ``SendInput`` 会静默失败，构造时自检 ``ctypes.sizeof(INPUT)==40``。

急停
----
``start_watch`` 起 daemon 线程每 100ms 轮询 ``GetAsyncKeyState``，
``Ctrl+Alt+Shift+Esc`` 同时按下触发 ``panic()``；panic 期间所有操作抛
``InputBlocked``。``stop_watch`` 用 Event 停止，可重复调用。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Any

# ---------------- Win32 常量 ----------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MAPVK_VK_TO_VSC = 0
WHEEL_DELTA = 120
MAX_INPUTS_PER_BATCH = 256

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_TAB = 0x09

# 需要 KEYEVENTF_EXTENDEDKEY 的键（契约要求的方向/编辑键 + 常见扩展键）
_EXTENDED_VKS = frozenset({
    0x25, 0x26, 0x27, 0x28,              # Left Up Right Down
    0x24, 0x23,                          # Home End
    0x21, 0x22,                          # PgUp PgDn
    0x2D, 0x2E,                          # Insert Delete
    0x2C,                                # PrintScreen
    0x5C, 0x5D,                          # RWin Apps
    0x90,                                # NumLock
    0x6F,                                # 小键盘 /
})


class InputError(RuntimeError):
    """输入参数/发送失败。"""


class InputBlocked(InputError):
    """未启用或急停中：不允许任何真实输入。"""


# ---------------- INPUT 结构 ----------------

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),                 # 4
        ("dy", wt.LONG),                 # 4
        ("mouseData", wt.DWORD),         # 4
        ("dwFlags", wt.DWORD),           # 4
        ("time", wt.DWORD),              # 4
        ("dwExtraInfo", ctypes.c_size_t),  # 8 (ULONG_PTR)
    ]                                    # 合计 28 -> 对齐 32


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),                # 2
        ("wScan", wt.WORD),              # 2
        ("dwFlags", wt.DWORD),           # 4
        ("time", wt.DWORD),              # 4
        ("dwExtraInfo", ctypes.c_size_t),  # 8
    ]                                    # 合计 24


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]                                    # 8


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]
    # 4 + 4(padding) + 32 = 40 on x64


INPUT_SIZE = ctypes.sizeof(INPUT)


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wt.UINT
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetCursorPos.restype = wt.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_user32.GetCursorPos.restype = wt.BOOL
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
_user32.MapVirtualKeyW.restype = wt.UINT


def _virtual_screen() -> tuple[int, int, int, int]:
    x = int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    w = int(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    h = int(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if w <= 0 or h <= 0:
        raise InputError("无法读取虚拟屏幕尺寸")
    return x, y, w, h


def normalize_point(x: int, y: int) -> tuple[int, int]:
    """虚拟屏幕绝对坐标 -> SendInput 的 0..65535 归一化坐标。

    分母用 ``w-1``，保证最右/最下像素正好归一化成 65535。
    """
    vx, vy, vw, vh = _virtual_screen()
    nx = 0 if vw <= 1 else int(round((int(x) - vx) * 65535 / (vw - 1)))
    ny = 0 if vh <= 1 else int(round((int(y) - vy) * 65535 / (vh - 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


# ---------------- VK 表 ----------------

def _build_keys() -> dict[str, int]:
    keys: dict[str, int] = {}
    for ch in "abcdefghijklmnopqrstuvwxyz":
        keys[ch] = ord(ch.upper())
    for ch in "0123456789":
        keys[ch] = ord(ch)
    for i in range(1, 25):
        keys["f%d" % i] = 0x6F + i          # F1=0x70 ... F24=0x87
    keys.update({
        "ctrl": 0x11, "control": 0x11,
        "alt": 0x12, "menu": 0x12,
        "shift": 0x10,
        "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
        "super": 0x5B, "meta": 0x5B, "cmd": 0x5B,
        "enter": VK_RETURN, "return": VK_RETURN,
        "tab": VK_TAB,
        "esc": VK_ESCAPE, "escape": VK_ESCAPE,
        "space": 0x20,
        "backspace": 0x08, "back": 0x08,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pgup": 0x21,
        "pagedown": 0x22, "pgdn": 0x22,
        "insert": 0x2D, "ins": 0x2D,
        "delete": 0x2E, "del": 0x2E,
        "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
        "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13,
        "apps": 0x5D, "sleep": 0x5F,
        "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
        "media_next": 0xB0, "media_prev": 0xB1, "media_play": 0xB3,
        # 常见符号（OEM 键）；需要上档字符时请显式写 shift，例如 "shift+1"
        "`": 0xC0, "-": 0xBD, "=": 0xBB,
        "[": 0xDB, "]": 0xDD, "\\": 0xDC,
        ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
        "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62,
        "numpad3": 0x63, "numpad4": 0x64, "numpad5": 0x65,
        "numpad6": 0x66, "numpad7": 0x67, "numpad8": 0x68,
        "numpad9": 0x69, "numpad*": 0x6A, "numpad+": 0x6B,
        "numpad-": 0x6D, "numpad.": 0x6E, "numpad/": 0x6F,
    })
    return keys


_VK_TABLE = _build_keys()


def parse_keys(keys: str) -> list[tuple[int, bool]]:
    """把 "ctrl+shift+t" / "up" / "f12" 解析成 [(vk, extended), ...]。

    大小写不敏感；按下顺序从左到右，抬起由调用方逆序。
    注意 ``+`` 是组合分隔符，单个加号键请用 ``shift+=``（OEM_PLUS 本身无法
    不经组合表达）。
    """
    if not isinstance(keys, str) or not keys.strip():
        raise InputError("按键不能为空")
    out: list[tuple[int, bool]] = []
    for part in keys.split("+"):
        name = part.strip().lower()
        if not name:
            raise InputError("按键格式错误：%s" % keys)
        vk = _VK_TABLE.get(name)
        if vk is None:
            raise InputError("未知按键：%s" % part.strip())
        out.append((vk, vk in _EXTENDED_VKS))
    return out


# ---------------- 后端 ----------------

class InputBackend:
    """输入后端接口（测试可注入/子类化，不碰真实设备）。"""

    def move(self, x: int, y: int) -> None:
        raise NotImplementedError

    def click(self, button: str, count: int, interval: float) -> None:
        raise NotImplementedError

    def scroll(self, amount: int, horizontal: bool) -> None:
        raise NotImplementedError

    def key(self, vk: int, down: bool, extended: bool = False) -> None:
        raise NotImplementedError

    def unicode(self, text: str) -> None:
        raise NotImplementedError

    # 扩展接口：拖拽需要按住在两个坐标间移动
    def button(self, button: str, down: bool) -> None:
        raise NotImplementedError


class SendInputBackend(InputBackend):
    """真实 SendInput 后端。自检脚本只允许用它的子类记录事件，不真发输入。"""

    _BUTTONS = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }

    def __init__(self) -> None:
        if INPUT_SIZE != 40:
            raise InputError(
                "INPUT 结构大小异常：%d（x64 应为 40），SendInput 会静默失败"
                % INPUT_SIZE)

    # ---- 发送 ----

    def _send(self, events: list[INPUT]) -> None:
        """分批发送（每批 <=256）。子类可覆盖本方法做记录。"""
        for start in range(0, len(events), MAX_INPUTS_PER_BATCH):
            batch = events[start:start + MAX_INPUTS_PER_BATCH]
            array = (INPUT * len(batch))(*batch)
            sent = int(_user32.SendInput(len(batch), array, INPUT_SIZE))
            if sent != len(batch):
                err = ctypes.get_last_error()
                raise InputError("SendInput 只发送了 %d/%d 个事件（错误码 %d）"
                                 % (sent, len(batch), err))

    @staticmethod
    def _mouse(dx: int, dy: int, flags: int, data: int = 0) -> INPUT:
        item = INPUT(type=INPUT_MOUSE)
        item.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data,
                             dwFlags=flags, time=0, dwExtraInfo=0)
        return item

    @staticmethod
    def _key_event(vk: int, scan: int, flags: int, data: int = 0) -> INPUT:
        item = INPUT(type=INPUT_KEYBOARD)
        item.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                             time=0, dwExtraInfo=0)
        return item

    # ---- 鼠标 ----

    def move(self, x: int, y: int) -> None:
        nx, ny = normalize_point(x, y)
        self._send([self._mouse(nx, ny,
                                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                                | MOUSEEVENTF_VIRTUALDESK)])

    def button(self, button: str, down: bool) -> None:
        pair = self._BUTTONS.get(str(button).lower())
        if pair is None:
            raise InputError("不支持的鼠标按键：%s（left/right/middle）" % button)
        self._send([self._mouse(0, 0, pair[0] if down else pair[1])])

    def click(self, button: str, count: int, interval: float) -> None:
        pair = self._BUTTONS.get(str(button).lower())
        if pair is None:
            raise InputError("不支持的鼠标按键：%s（left/right/middle）" % button)
        for i in range(max(1, int(count))):
            self._send([self._mouse(0, 0, pair[0]),
                        self._mouse(0, 0, pair[1])])
            if interval > 0:
                time.sleep(interval)

    def scroll(self, amount: int, horizontal: bool) -> None:
        delta = int(amount) * WHEEL_DELTA
        flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        self._send([self._mouse(0, 0, flags, delta & 0xFFFFFFFF)])

    # ---- 键盘 ----

    def key(self, vk: int, down: bool, extended: bool = False) -> None:
        scan = int(_user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC))
        flags = 0 if down else KEYEVENTF_KEYUP
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        self._send([self._key_event(int(vk), scan, flags)])

    def unicode(self, text: str) -> None:
        """逐 UTF-16 code unit 发送（emoji 代理对会分成两个事件）。

        按 128 个字符一批构造，配合 _send 的 256 事件上限。
        """
        units: list[int] = []
        for ch in text:
            encoded = ch.encode("utf-16-le", errors="surrogatepass")
            for i in range(0, len(encoded), 2):
                units.append(encoded[i] | (encoded[i + 1] << 8))
        events: list[INPUT] = []
        for unit in units:
            events.append(self._key_event(0, unit, KEYEVENTF_UNICODE))
            events.append(self._key_event(0, unit,
                                          KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        self._send(events)


# ---------------- 服务 ----------------

class InputService:
    """策略层：启用开关 + 急停 + 组合键/文本/paste 回退。

    ``cfg`` 是同一个配置 dict 的引用（``cfg["input"]``）；
    ``set_cfg`` 只更新引用，不复制。service 不做确认弹窗（确认在 MCP 层）。
    """

    def __init__(self, cfg: dict, *, clipboard: Any = None,
                 backend: InputBackend | None = None) -> None:
        self._cfg = cfg
        self._clipboard = clipboard
        self._backend = backend or SendInputBackend()
        self._panic_until = 0.0
        self._on_panic: Any = None
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self._watch_lock = threading.Lock()

    # -------- 状态 --------

    @property
    def enabled(self) -> bool:
        """读 cfg["input"]["enabled"]；缺省视为关闭（安全优先）。"""
        try:
            return bool(self._cfg.get("input", {}).get("enabled", False))
        except Exception:
            return False

    @property
    def panicked(self) -> bool:
        return time.time() < self._panic_until

    # -------- 急停 --------

    def panic(self, seconds: int | None = None) -> None:
        """进入急停：seconds 内所有操作抛 InputBlocked。

        seconds=None 时读 ``cfg["input"]["panic_seconds"]``（默认 60）。
        重复调用取更长的截止时间；触发回调异常被吞掉。
        """
        if seconds is None:
            try:
                seconds = float(self._cfg.get("input", {})
                                .get("panic_seconds", 60))
            except Exception:
                seconds = 60.0
        seconds = max(0.0, float(seconds))
        self._panic_until = max(self._panic_until, time.time() + seconds)
        callback = self._on_panic
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def start_watch(self, on_panic: Any = None) -> None:
        """启动急停热键轮询（幂等）；on_panic 在触发时回调。"""
        with self._watch_lock:
            if on_panic is not None:
                self._on_panic = on_panic
            if self._watch_thread is not None and self._watch_thread.is_alive():
                return
            self._watch_stop.clear()
            self._watch_thread = threading.Thread(
                target=self._watch_loop, name="vbhelper-input-panic",
                daemon=True)
            self._watch_thread.start()

    def stop_watch(self) -> None:
        """停止热键轮询；可重复调用，不会 join 自己。"""
        with self._watch_lock:
            thread = self._watch_thread
            self._watch_thread = None
            self._watch_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _watch_loop(self) -> None:
        pressed_before = False
        while not self._watch_stop.wait(0.1):
            try:
                down = all(
                    _user32.GetAsyncKeyState(vk) & 0x8000
                    for vk in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_ESCAPE))
            except Exception:
                down = False
            if down and not pressed_before:
                self.panic()
            pressed_before = down

    def _guard(self) -> None:
        """所有真实输入前的策略检查。"""
        if not self.enabled:
            raise InputBlocked("输入控制未启用（请在 MCP 面板开启 input.enabled）")
        remaining = self._panic_until - time.time()
        if remaining > 0:
            raise InputBlocked("急停生效中，剩余 %.0f 秒" % remaining)

    # -------- 配置 --------

    def set_cfg(self, cfg: dict) -> None:
        self._cfg = cfg

    # -------- 鼠标 --------

    def mouse_move(self, x: int, y: int, duration: float = 0.0) -> None:
        """移动到虚拟屏幕坐标 (x, y)；duration>0 时按约 10ms 步长插值。"""
        self._guard()
        if duration is None or duration <= 0:
            self._backend.move(int(x), int(y))
            return
        start_x, start_y = self.position()
        steps = max(1, min(200, int(float(duration) / 0.01)))
        delay = float(duration) / steps
        for i in range(1, steps + 1):
            t = i / steps
            self._backend.move(int(round(start_x + (x - start_x) * t)),
                               int(round(start_y + (y - start_y) * t)))
            if i < steps:
                time.sleep(delay)

    def mouse_click(self, button: str = "left", count: int = 1,
                    interval: float = 0.05, x: int | None = None,
                    y: int | None = None) -> None:
        """点击；给了 x/y 先定位（两者必须同时给）。"""
        self._guard()
        if (x is None) != (y is None):
            raise InputError("x 与 y 必须同时提供")
        if count < 1:
            raise InputError("count 必须 >= 1")
        if str(button).lower() not in ("left", "right", "middle"):
            raise InputError("不支持的鼠标按键：%s" % button)
        if x is not None:
            self._backend.move(int(x), int(y))
        self._backend.click(str(button).lower(), int(count), float(interval))

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int,
                   button: str = "left", duration: float = 0.4) -> None:
        """从 (x1,y1) 按住拖到 (x2,y2)，中途插值；异常也保证抬起。"""
        self._guard()
        if str(button).lower() not in ("left", "right", "middle"):
            raise InputError("不支持的鼠标按键：%s" % button)
        steps = max(2, min(120, int(float(duration) / 0.02) or 2))
        delay = max(0.0, float(duration)) / steps
        self._backend.move(int(x1), int(y1))
        self._backend.button(str(button).lower(), True)
        try:
            for i in range(1, steps + 1):
                t = i / steps
                self._backend.move(int(round(x1 + (x2 - x1) * t)),
                                   int(round(y1 + (y2 - y1) * t)))
                if delay > 0:
                    time.sleep(delay)
        finally:
            self._backend.button(str(button).lower(), False)

    def scroll(self, amount: int = -3, *, horizontal: bool = False,
               x: int | None = None, y: int | None = None) -> None:
        """滚轮：amount 负数向下（见模块 docstring）；可选先定位。"""
        self._guard()
        if (x is None) != (y is None):
            raise InputError("x 与 y 必须同时提供")
        if x is not None:
            self._backend.move(int(x), int(y))
        self._backend.scroll(int(amount), bool(horizontal))

    # -------- 键盘 --------

    def type_text(self, text: str, *, method: str = "paste",
                  interval: float = 0.0) -> None:
        """输入文本；method="paste"（默认，剪贴板）或 "unicode"。

        paste：备份剪贴板 → 写文本 → Ctrl+V → 200ms → finally 恢复；
        没有剪贴板服务或写入失败时自动回退 unicode。
        """
        self._guard()
        if not isinstance(text, str):
            raise InputError("text 必须是字符串")
        if not text:
            return
        method = (method or "paste").strip().lower()
        if method not in ("paste", "unicode"):
            raise InputError("未知输入方式：%s（paste/unicode）" % method)
        if method == "paste" and self._clipboard is None:
            method = "unicode"
        if method == "paste":
            try:
                original = self._clipboard.snapshot()
            except Exception:
                # 备份失败时不能覆盖用户剪贴板：直接走 unicode
                self._type_unicode(text, interval)
                return
            try:
                if not self._clipboard.write_text(text):
                    raise InputError("剪贴板写入失败")
                self._press_parsed(parse_keys("ctrl+v"))
                time.sleep(0.2)
            except Exception:
                # 剪贴板不可用时回退到 unicode（仍保持已启用/未急停状态）
                self._type_unicode(text, interval)
            finally:
                try:
                    self._clipboard.restore(original)
                except Exception:
                    pass
            return
        self._type_unicode(text, interval)

    def press(self, keys: str) -> None:
        """按下并释放组合键，如 "ctrl+shift+t" / "enter" / "f12"。"""
        self._guard()
        self._press_parsed(parse_keys(keys))

    def _press_parsed(self, parsed: list[tuple[int, bool]]) -> None:
        for vk, extended in parsed:
            self._backend.key(vk, True, extended)
        for vk, extended in reversed(parsed):
            self._backend.key(vk, False, extended)

    def _type_unicode(self, text: str, interval: float = 0.0) -> None:
        """unicode 模式：\\n -> Enter，\\t -> Tab，\\r 忽略。"""
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                chunk = "".join(buffer)
                buffer.clear()
                if interval > 0:
                    for ch in chunk:
                        self._backend.unicode(ch)
                        time.sleep(interval)
                else:
                    self._backend.unicode(chunk)

        for ch in text:
            if ch == "\r":
                continue
            if ch == "\n":
                flush()
                self._backend.key(VK_RETURN, True)
                self._backend.key(VK_RETURN, False)
            elif ch == "\t":
                flush()
                self._backend.key(VK_TAB, True)
                self._backend.key(VK_TAB, False)
            else:
                buffer.append(ch)
        flush()

    # -------- 只读 --------

    def position(self) -> tuple[int, int]:
        """当前光标位置（虚拟屏幕坐标）；只读，不需要启用。"""
        point = wt.POINT()
        if not _user32.GetCursorPos(ctypes.byref(point)):
            raise InputError("读取鼠标位置失败")
        return int(point.x), int(point.y)

    def screen_size(self) -> tuple[int, int, int, int]:
        """虚拟屏幕 (x, y, w, h)；只读，不需要启用。"""
        return _virtual_screen()
