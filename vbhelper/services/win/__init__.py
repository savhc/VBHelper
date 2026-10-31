# -*- coding: utf-8 -*-
"""Windows 能力服务包：截屏 / 鼠标键盘 / 剪贴板。

只依赖标准库 + PIL，不 import Tk；可在任意线程调用。
"""
from .clipboard import ClipboardError, ClipboardService
from .input import (InputBackend, InputBlocked, InputError, InputService,
                    SendInputBackend, parse_keys)
from .screen import (CaptureResult, Monitor, ScreenError, ScreenService,
                     WindowInfo)

__all__ = [
    "CaptureResult", "ClipboardError", "ClipboardService", "InputBackend",
    "InputBlocked", "InputError", "InputService", "Monitor", "ScreenError",
    "ScreenService", "SendInputBackend", "WindowInfo", "parse_keys",
]
