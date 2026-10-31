# -*- coding: utf-8 -*-
"""KBox / ClassGuard 加密 class 解密（Windows CNG，无第三方依赖）。

样本特征（Minecraft KBox）：
- 条目以 4 字节 ``62 39 E2 86`` 开头；
- JAR 内带 ``META-INF/kbox/seed.bin`` 与 ``class-seed.bin``；
- key = SHA-256("KBox-ClassGuard-v1:" + class-seed + SHA-256(seed))；
- 数据 = magic(4) + iv(12) + AES-GCM(ciphertext + tag16)。

解密使用 Windows bcrypt.dll；失败返回 None，由查看器回退原始 Hex/自定义解码器。
"""
from __future__ import annotations

import ctypes
import hashlib
try:
    from ctypes import wintypes as wt
except ImportError:          # 非 Windows 平台没有 wintypes
    wt = None

MAGIC = b"\x62\x39\xe2\x86"
SEED_PATH = "META-INF/kbox/seed.bin"
CLASS_SEED_PATH = "META-INF/kbox/class-seed.bin"
LIC_PATH = "META-INF/kbox/lic.pub"
DOMAIN = b"KBox-ClassGuard-v1:"

STATUS_SUCCESS = 0
STATUS_AUTH_TAG_MISMATCH = 0xC000A002
BCRYPT_CHAIN_MODE_GCM = "ChainingModeGCM"


def looks_kbox(data) -> bool:
    try:
        return bytes(data[:4]) == MAGIC
    except Exception:
        return False


def derive_key(seed: bytes, class_seed: bytes) -> bytes:
    return hashlib.sha256(
        DOMAIN + class_seed + hashlib.sha256(seed).digest()).digest()


class _AuthInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.ULONG),
        ("dwInfoVersion", wt.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wt.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wt.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wt.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wt.ULONG),
        ("cbAAD", wt.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wt.ULONG),
    ]


def _bytes_ptr(buf: bytes):
    arr = (ctypes.c_ubyte * len(buf)).from_buffer_copy(buf)
    return arr, ctypes.cast(arr, ctypes.POINTER(ctypes.c_ubyte))


def bcrypt_aes_gcm_decrypt(key: bytes, iv: bytes, ct: bytes,
                           tag: bytes) -> bytes | None:
    """Windows CNG AES-GCM 解密；失败返回 None。"""
    try:
        bcrypt = ctypes.WinDLL("bcrypt")
    except OSError:
        return None
    HANDLE = wt.HANDLE
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(HANDLE), wt.LPCWSTR, wt.LPCWSTR, wt.ULONG]
    bcrypt.BCryptOpenAlgorithmProvider.restype = ctypes.c_int
    bcrypt.BCryptSetProperty.argtypes = [
        HANDLE, wt.LPCWSTR, ctypes.c_void_p, wt.ULONG, wt.ULONG]
    bcrypt.BCryptSetProperty.restype = ctypes.c_int
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        HANDLE, ctypes.POINTER(HANDLE), ctypes.c_void_p, wt.ULONG,
        ctypes.c_void_p, wt.ULONG, wt.ULONG]
    bcrypt.BCryptGenerateSymmetricKey.restype = ctypes.c_int
    bcrypt.BCryptDecrypt.argtypes = [
        HANDLE, ctypes.c_void_p, wt.ULONG, ctypes.c_void_p,
        ctypes.c_void_p, wt.ULONG, ctypes.c_void_p, wt.ULONG,
        ctypes.POINTER(wt.ULONG), wt.ULONG]
    bcrypt.BCryptDecrypt.restype = ctypes.c_int
    bcrypt.BCryptDestroyKey.argtypes = [HANDLE]
    bcrypt.BCryptDestroyKey.restype = ctypes.c_int
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [HANDLE, wt.ULONG]
    bcrypt.BCryptCloseAlgorithmProvider.restype = ctypes.c_int

    alg = HANDLE()
    hkey = HANDLE()
    try:
        if bcrypt.BCryptOpenAlgorithmProvider(
                ctypes.byref(alg), "AES", None, 0) != STATUS_SUCCESS:
            return None
        mode = ctypes.create_unicode_buffer(BCRYPT_CHAIN_MODE_GCM)
        if bcrypt.BCryptSetProperty(
                alg, "ChainingMode", ctypes.cast(mode, ctypes.c_void_p),
                ctypes.sizeof(mode), 0) != STATUS_SUCCESS:
            return None
        key_buf = ctypes.create_string_buffer(key, len(key))
        if bcrypt.BCryptGenerateSymmetricKey(
                alg, ctypes.byref(hkey), None, 0, key_buf, len(key),
                0) != STATUS_SUCCESS:
            return None

        iv_arr, iv_ptr = _bytes_ptr(iv)
        tag_arr, tag_ptr = _bytes_ptr(tag)
        info = _AuthInfo()
        info.cbSize = ctypes.sizeof(_AuthInfo)
        info.dwInfoVersion = 1
        info.pbNonce = iv_ptr
        info.cbNonce = len(iv)
        info.pbAuthData = None
        info.cbAuthData = 0
        info.pbTag = tag_ptr
        info.cbTag = len(tag)
        info.pbMacContext = None
        info.cbMacContext = 0
        info.cbAAD = 0
        info.cbData = len(ct)
        info.dwFlags = 0

        ct_buf = ctypes.create_string_buffer(ct, len(ct))
        out = ctypes.create_string_buffer(len(ct) + 16)
        out_len = wt.ULONG(0)
        status = bcrypt.BCryptDecrypt(
            hkey, ct_buf, len(ct), ctypes.byref(info), None, 0,
            out, ctypes.sizeof(out), ctypes.byref(out_len), 0)
        if status != STATUS_SUCCESS:
            return None
        return out.raw[:out_len.value]
    except Exception:
        return None
    finally:
        try:
            if hkey:
                bcrypt.BCryptDestroyKey(hkey)
        except Exception:
            pass
        try:
            if alg:
                bcrypt.BCryptCloseAlgorithmProvider(alg, 0)
        except Exception:
            pass


def decrypt_blob(blob: bytes, seed: bytes, class_seed: bytes) -> bytes | None:
    """解密单个 KBox class；返回 class 字节或 None。"""
    if len(blob) < 4 + 12 + 16 or not looks_kbox(blob):
        return None
    iv = blob[4:16]
    body = blob[16:]
    if len(body) < 16:
        return None
    ct, tag = body[:-16], body[-16:]
    key = derive_key(seed, class_seed)
    plain = bcrypt_aes_gcm_decrypt(key, iv, ct, tag)
    if plain is None or len(plain) < 4 or plain[:4] != b"\xca\xfe\xba\xbe":
        return None
    return plain


def decrypt_entry(zf, data: bytes) -> bytes | None:
    """从当前 ZipFile 读 KBox 种子并解密一个条目。"""
    try:
        seed = zf.read(SEED_PATH)
        class_seed = zf.read(CLASS_SEED_PATH)
    except (KeyError, OSError, RuntimeError):
        return None
    if not seed or not class_seed:
        return None
    # 有 lic.pub 时 KBox 会在 key 里再混入许可证材料；当前样本没有 lic.pub。
    try:
        if zf.getinfo(LIC_PATH) is not None:
            return None
    except KeyError:
        pass
    return decrypt_blob(bytes(data), seed, class_seed)
