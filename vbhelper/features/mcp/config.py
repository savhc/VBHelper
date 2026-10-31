"""MCP 工具总线配置：深合并默认值、endpoint 文件、bearer token 存取。

- ``get(ctx)``：把 ``ctx.cfg["mcp"]`` 与默认配置深合并，用户已有值优先，
  缺失的嵌套键补默认；结果写回 ``ctx.cfg["mcp"]``，但**不落盘**（``save()`` 才落盘）。
- ``DEFAULT_CONFIG`` 是只读常量，合并时从不修改它。
- endpoint 文件（``%LOCALAPPDATA%\\VBHelper\\mcp\\endpoint.json``）供 stdio 桥发现
  本机服务：原子写、内容带 ``version:1``、读取时校验 port/url，坏文件返回 None。
- token 存 DPAPI 密钥库，名字 ``mcp.token``；``ensure_token`` 复用、``rotate_token`` 强换。

    cfg = mcp_config.get(ctx)
    info = mcp_config.read_endpoint(ctx.paths)     # {"host":..., "port":..., ...} 或 None
    token = mcp_config.ensure_token(secret_store)
"""

from __future__ import annotations

import copy
import json
import os
import secrets as _secrets
import threading
import time
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "server": {"bind": "127.0.0.1", "port": 0, "auto_start": True,
               "max_sessions": 8, "token_ref": "mcp.token"},
    "confirm": {"mode": "ask", "session_minutes": 5, "timeout": 30},
    "tools": {},                       # {"input.type": {"enabled": false}}
    "input": {"enabled": False, "panic_seconds": 60},
    "screen": {"allow": True, "max_width": 1600},
    "web": {"max_fetch_bytes": 5242880, "allow_private": False},
    "search": {"provider": "auto", "api_key_ref": "web.search_key",
               "searxng_url": "", "timeout": 20},
}

TOKEN_NAME = "mcp.token"
ENDPOINT_VERSION = 1
ENDPOINT_FILENAME = "endpoint.json"

_MISSING = object()

_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()

# ---------------------------------------------------------------- 配置合并

def _merge(defaults: dict, user: dict) -> dict:
    """深合并：defaults 提供骨架，user 已知值优先，未知键原样保留。"""
    result: dict[str, Any] = {}
    for key, dval in defaults.items():
        uval = user.get(key, _MISSING)
        if isinstance(dval, dict):
            if isinstance(uval, dict):
                result[key] = _merge(dval, uval)
            elif uval is _MISSING or uval is None:
                result[key] = _merge(dval, {})
            else:
                result[key] = copy.deepcopy(uval)    # 用户用非 dict 覆盖整块
        else:
            result[key] = (copy.deepcopy(dval) if uval is _MISSING
                           else copy.deepcopy(uval))
    for key, uval in user.items():
        if key not in defaults:
            result[key] = copy.deepcopy(uval)        # 未知键不丢
    return result

def get(ctx: Any) -> dict:
    """取 MCP 配置（深合并后），并写回 ``ctx.cfg["mcp"]``；不自动 save。"""
    cfg = ctx.cfg
    user = cfg.get("mcp") if isinstance(cfg, dict) else None
    if not isinstance(user, dict):
        user = {}
    merged = _merge(DEFAULT_CONFIG, user)
    if isinstance(cfg, dict):
        cfg["mcp"] = merged
    return merged

def save(ctx: Any) -> None:
    """持久化配置；写盘失败向上抛，由调用方提示用户。"""
    ctx.save_cfg()

# ---------------------------------------------------------------- endpoint

def endpoint_path(paths: Any) -> str:
    """endpoint 文件路径：``paths.mcp_dir/endpoint.json``。

    兼容主代理尚未加 ``mcp_dir`` 的旧 ``AppPaths``：回退到 ``local_dir/mcp``。
    """
    base = getattr(paths, "mcp_dir", None)
    if not base:
        base = os.path.join(getattr(paths, "local_dir", "") or ".", "mcp")
    return os.path.join(base, ENDPOINT_FILENAME)

def _lock_for(path: str) -> threading.Lock:
    with _write_locks_guard:
        lock = _write_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _write_locks[path] = lock
        return lock

def write_endpoint(paths: Any, info: dict) -> str:
    """原子写 endpoint（tmp + replace），内容强制 ``version:1``；返回路径。"""
    path = endpoint_path(paths)
    data = dict(info or {})
    data["version"] = ENDPOINT_VERSION
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with _lock_for(path):
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)          # Windows 上尽力而为
        except OSError:
            pass
    return path

def read_endpoint_file(path: str) -> dict | None:
    """读并校验 endpoint 文件；不是合法 dict / port / url 都返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    port = data.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    if not 1 <= port <= 65535:
        return None
    url = data.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    return data

def read_endpoint(paths: Any) -> dict | None:
    """读 ``paths`` 对应的 endpoint；不存在/损坏返回 None。"""
    return read_endpoint_file(endpoint_path(paths))

def clear_endpoint(paths: Any) -> None:
    """删除 endpoint 文件；不存在或无权限都静默忽略。"""
    try:
        os.remove(endpoint_path(paths))
    except OSError:
        pass

# ---------------------------------------------------------------- 启动请求
# stdio 桥无法直接操作已运行 GUI 的内部对象；用一个小文件请它启动服务。
# 只有 mcp.enabled=true 时 GUI 才会响应（面板「停止」会置 false）。

START_REQUEST_FILENAME = "start.request"

def start_request_path(paths: Any) -> str:
    """``paths.mcp_dir/start.request``（与 endpoint 同目录）。"""
    return os.path.join(os.path.dirname(endpoint_path(paths)),
                        START_REQUEST_FILENAME)

def write_start_request(paths: Any, info: dict | None = None) -> str:
    """原子写启动请求；返回路径。内容含 ts/pid/reason。"""
    path = start_request_path(paths)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    data = dict(info or {})
    data.setdefault("ts", time.time())
    data.setdefault("pid", os.getpid())
    with _lock_for(path):
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    return path

def read_start_request_file(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

def read_start_request(paths: Any) -> dict | None:
    return read_start_request_file(start_request_path(paths))

def clear_start_request(paths: Any) -> None:
    try:
        os.remove(start_request_path(paths))
    except OSError:
        pass

# ---------------------------------------------------------------- token

def _check_secrets(secrets: Any) -> None:
    if secrets is None:
        raise ValueError("需要 SecretStore（或带 get/set 的对象）才能存取 MCP token")
    if not hasattr(secrets, "get") or not hasattr(secrets, "set"):
        raise ValueError("secrets 参数缺少 get/set 方法")

def ensure_token(secrets: Any) -> str:
    """返回 ``mcp.token``；不存在就生成 token_urlsafe(32) 并写入。"""
    _check_secrets(secrets)
    token = secrets.get(TOKEN_NAME, "") or ""
    if token:
        return str(token)
    token = _secrets.token_urlsafe(32)
    secrets.set(TOKEN_NAME, token)
    return token

def rotate_token(secrets: Any) -> str:
    """强制生成新 token 并覆盖旧值。"""
    _check_secrets(secrets)
    token = _secrets.token_urlsafe(32)
    secrets.set(TOKEN_NAME, token)
    return token
