# -*- coding: utf-8 -*-
"""剪贴板服务：CF_UNICODETEXT 读写 + 前台窗口信息（mcp-block.md §3.3）。

- 只用 ctypes user32/kernel32，不 import Tk；
- OpenClipboard 被占用时重试 3 次（间隔 50ms）；
- 写成功后 ``SetClipboardData`` 的内存所有权归系统，**不要** GlobalFree；
- 空/非文本剪贴板 ``read_text`` 返回 ""；写入失败抛 ``ClipboardError``；
- ``foreground_app`` 保留自身进程窗口的 hwnd，但用 ``is_self`` 标注，
  供上层做剪贴板隐私过滤。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
OPEN_ATTEMPTS = 3
OPEN_RETRY_DELAY = 0.05


class ClipboardError(RuntimeError):
    """剪贴板打开/写入失败（带中文原因）。"""


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.OpenClipboard.argtypes = [wt.HWND]
_user32.OpenClipboard.restype = wt.BOOL
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wt.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.EmptyClipboard.restype = wt.BOOL
_user32.GetClipboardData.argtypes = [wt.UINT]
_user32.GetClipboardData.restype = wt.HANDLE
_user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
_user32.SetClipboardData.restype = wt.HANDLE
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wt.HWND
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND,
                                             ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD

_kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wt.HGLOBAL
_kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
_kernel32.GlobalUnlock.restype = wt.BOOL
_kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
_kernel32.GlobalFree.restype = wt.HGLOBAL


def _open_clipboard() -> None:
    """带重试打开剪贴板；失败抛 ClipboardError。"""
    for attempt in range(OPEN_ATTEMPTS):
        if _user32.OpenClipboard(None):
            return
        if attempt < OPEN_ATTEMPTS - 1:
            time.sleep(OPEN_RETRY_DELAY)
    raise ClipboardError("剪贴板被其他程序占用，无法打开（已重试 %d 次）"
                         % OPEN_ATTEMPTS)


def _close_clipboard() -> None:
    try:
        _user32.CloseClipboard()
    except Exception:
        pass


class ClipboardService:
    """CF_UNICODETEXT 读写；线程调用方需自行串行（MCP server 全局锁保证）。"""

    # -------- 文本 --------

    def read_text(self) -> str:
        """读剪贴板文本；空/非文本返回 ""。打开失败抛 ClipboardError。"""
        _open_clipboard()
        try:
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = _kernel32.GlobalLock(handle)
            if not ptr:
                raise ClipboardError("GlobalLock 剪贴板句柄失败")
            try:
                return ctypes.wstring_at(ptr)
            finally:
                _kernel32.GlobalUnlock(handle)
        finally:
            _close_clipboard()

    def write_text(self, text: str) -> bool:
        """写文本剪贴板；失败抛 ClipboardError（不静默）。"""
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ClipboardError("剪贴板文本必须是字符串")
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        _open_clipboard()
        try:
            if not _user32.EmptyClipboard():
                raise ClipboardError("清空剪贴板失败")
            handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                raise ClipboardError("GlobalAlloc 失败（%d 字节）" % size)
            ptr = _kernel32.GlobalLock(handle)
            if not ptr:
                _kernel32.GlobalFree(handle)
                raise ClipboardError("GlobalLock 失败")
            try:
                ctypes.memmove(ptr, buf, size)
            finally:
                _kernel32.GlobalUnlock(handle)
            if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
                # 失败时内存所有权仍在本进程，必须 GlobalFree；成功后归系统
                _kernel32.GlobalFree(handle)
                raise ClipboardError("SetClipboardData 失败")
            return True
        finally:
            _close_clipboard()

    def snapshot(self) -> str:
        """备份当前文本（供 paste 后恢复）。"""
        return self.read_text()

    def restore(self, text: str) -> bool:
        """恢复文本；失败返回 False（恢复失败不该打断调用方）。"""
        try:
            return bool(self.write_text(text))
        except ClipboardError:
            return False
        except Exception:
            return False

    # -------- 前台窗口 --------

    def foreground_app(self) -> dict:
        """前台窗口信息：{title, class_name, pid, hwnd, is_self}。

        is_self=True 表示前台窗口属于本进程（hwnd 仍保留，供上层判断）。
        """
        hwnd = int(_user32.GetForegroundWindow() or 0)
        if not hwnd:
            return {"title": "", "class_name": "", "pid": 0, "hwnd": 0,
                    "is_self": False}
        length = int(_user32.GetWindowTextLengthW(hwnd))
        title_buf = ctypes.create_unicode_buffer(max(1, length + 1))
        _user32.GetWindowTextW(hwnd, title_buf, max(1, length + 1))
        class_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, class_buf, 256)
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {
            "title": title_buf.value,
            "class_name": class_buf.value,
            "pid": int(pid.value),
            "hwnd": hwnd,
            "is_self": int(pid.value) == os.getpid(),
        }
