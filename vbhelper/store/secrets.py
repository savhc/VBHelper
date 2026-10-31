# -*- coding: utf-8 -*-
"""密钥库：Windows DPAPI 加密 + JSON 原子落盘。

- 文件结构 ``{"version":1,"items":{"mcp.token":"dp1:base64"}}``，磁盘上永远不出现明文；
- DPAPI 绑定当前 Windows 用户，entropy 固定 ``VBHelper.Secrets.v1``；
  与 ``features/apiwork/store.py`` 是两套独立 entropy，互不影响（切勿混用）；
- 所有读写加进程内锁，写入走 ``tmp + os.replace``，不会读到半截文件；
- 解密失败（换机器/换用户）返回调用方给的默认值，不把异常抛给工具链路。

用法::

    from vbhelper.store.secrets import SecretStore

    store = SecretStore()                 # %LOCALAPPDATA%\\VBHelper\\secrets.json
    store.set("mcp.token", "abc123")
    store.get("mcp.token")                # "abc123"
    store.delete("mcp.token")
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import sys
import threading
from typing import Any

APP_DIR_NAME = "VBHelper"
DPAPI_ENTROPY = "VBHelper.Secrets.v1"
FILE_VERSION = 1
TOKEN_PREFIX = "dp1:"


def default_secrets_path() -> str:
    """默认密钥文件路径 %LOCALAPPDATA%\\VBHelper\\secrets.json。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME, "secrets.json")


# ---------------------------------------------------------------- DPAPI

class _BLOB(ctypes.Structure):
    """Windows DATA_BLOB。"""

    _fields_ = [("cbData", ctypes.c_uint),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes) -> tuple[_BLOB, Any]:
    """构造 DATA_BLOB，并把底层 buffer 一并返回（防止被 GC 后指针悬空）。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


_libs_lock = threading.Lock()
_libs: tuple[Any, Any] | None = None


def _dpapi_libs() -> tuple[Any, Any]:
    """取 crypt32/kernel32（惰性加载，非 Windows 抛 OSError）。"""
    global _libs
    if _libs is None:
        with _libs_lock:
            if _libs is None:
                if os.name != "nt":
                    raise OSError("DPAPI 仅支持 Windows，无法读写密钥库")
                crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                crypt32.CryptProtectData.restype = ctypes.c_int
                crypt32.CryptUnprotectData.restype = ctypes.c_int
                kernel32.LocalFree.restype = ctypes.c_void_p
                kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                _libs = (crypt32, kernel32)
    return _libs


def _entropy() -> tuple[_BLOB, Any]:
    return _make_blob(DPAPI_ENTROPY.encode("utf-8"))


def dpapi_encrypt(text: str) -> str:
    """明文 -> ``dp1:base64``；空值原样返回 ``""``。失败抛 OSError。"""
    if not text:
        return ""
    crypt32, kernel32 = _dpapi_libs()
    raw = text.encode("utf-8")
    inp, _inp_buf = _make_blob(raw)
    ent, _ent_buf = _entropy()
    out = _BLOB()
    if not crypt32.CryptProtectData(
            ctypes.byref(inp), "VBHelper.Secrets", ctypes.byref(ent),
            None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI 加密失败（错误码 %d）" % ctypes.get_last_error())
    try:
        return TOKEN_PREFIX + base64.b64encode(
            ctypes.string_at(out.pbData, out.cbData)).decode("ascii")
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def dpapi_decrypt(token: str) -> str:
    """``dp1:base64`` -> 明文；兼容不带前缀的旧明文；失败抛 OSError。"""
    if not token:
        return ""
    if not token.startswith(TOKEN_PREFIX):
        return token                      # 旧版本明文，原样返回
    try:
        raw = base64.b64decode(token[len(TOKEN_PREFIX):])
    except (binascii.Error, ValueError) as exc:
        raise OSError("DPAPI 密文不是合法 base64：%s" % exc) from exc
    crypt32, kernel32 = _dpapi_libs()
    inp, _inp_buf = _make_blob(raw)
    ent, _ent_buf = _entropy()
    out = _BLOB()
    if not crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, ctypes.byref(ent),
            None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI 解密失败（换 Windows 用户或机器后无法还原）")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


# ---------------------------------------------------------------- 打码

def mask_key(key: str) -> str:
    """密钥打码：前 5 + 12 个 • + 后 4；不超过 9 字符时全部打码。"""
    key = key or ""
    if len(key) <= 9:
        return "•" * len(key)
    return key[:5] + "•" * 12 + key[-4:]


# ---------------------------------------------------------------- 密钥库

def _warn(msg: str) -> None:
    """解密/落盘异常只提示到 stderr，不打断调用方。"""
    try:
        print("[secrets] " + msg, file=sys.stderr)
    except Exception:
        pass


class SecretStore:
    """加密密钥库：文件里只存 ``dp1:`` 密文。

    ``path=None`` 时用 :func:`default_secrets_path`（方便测试直接无参构造）。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_secrets_path()
        self._lock = threading.RLock()
        self._items: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------ 磁盘

    def _load(self) -> None:
        items: dict[str, str] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = None
        except (OSError, ValueError) as exc:
            _warn("密钥文件读取失败，按空处理：%r" % (exc,))
            data = None
        if isinstance(data, dict):
            raw = data.get("items")
            if isinstance(raw, dict):
                items = {str(k): str(v) for k, v in raw.items()
                         if isinstance(v, str) and v}
        self._items = items

    def _save_locked(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = "%s.%d.%d.tmp" % (self.path, os.getpid(),
                                threading.get_ident())
        payload = {"version": FILE_VERSION, "items": dict(self._items)}
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(self.path, 0o600)      # Windows 上只影响只读位，尽力而为
        except OSError:
            pass

    # ------------------------------------------------------------ 读写

    def get(self, name: str, default: str = "") -> str:
        """取明文；不存在/解密失败返回 ``default``。"""
        if not name:
            return default
        with self._lock:
            token = self._items.get(name) or ""
        if not token:
            return default
        try:
            return dpapi_decrypt(token)
        except OSError as exc:
            _warn("解密 %r 失败：%s" % (name, exc))
            return default

    def set(self, name: str, value: str) -> None:
        """写入（空 name 或空 value 等价删除）；值以 dp1 密文落盘。"""
        if not name or not value:
            self.delete(name)
            return
        token = dpapi_encrypt(value)
        with self._lock:
            self._items[str(name)] = token
            self._save_locked()

    def delete(self, name: str) -> None:
        """删除一条密钥；不存在时不动文件。"""
        if not name:
            return
        with self._lock:
            if name in self._items:
                del self._items[name]
                self._save_locked()

    def has(self, name: str) -> bool:
        """是否存在且能解出非空明文。"""
        return bool(self.get(name, ""))

    def reload(self) -> None:
        """丢弃内存缓存，从磁盘重新加载（多进程写后使用）。"""
        with self._lock:
            self._load()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return isinstance(name, str) and name in self._items

    def __repr__(self) -> str:              # pragma: no cover - 调试用
        return "SecretStore(%r, %d items)" % (self.path, len(self))
