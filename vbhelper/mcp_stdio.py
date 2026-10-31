"""MCP stdio 桥：把 MCP 客户端的 stdin/stdout 转发到 VBHelper 的 HTTP 服务。

原型用法::

    python -m vbhelper.mcp_stdio [--endpoint PATH] [--no-autostart]

真实入口由 ``run.py --mcp-stdio`` 接入。约束：
- **stdout 只允许出现协议 JSON**，日志写 stderr 或
  ``%LOCALAPPDATA%\\VBHelper\\logs\\mcp-stdio.log``；
- 不 import tkinter / vbhelper.app；只读取 endpoint（含 token）与 config.json；
- endpoint 不健康时按需以无窗口方式拉起 GUI，最多等 15 秒；
- 不允许自启动或 ``mcp.enabled`` 不为真时，对每个请求返回 JSON-RPC
  -32000 中文错误，进程保持运行（等待用户去面板启用）。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, TextIO

__all__ = ["main"]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_HEADER = "Mcp-Session-Id"
HTTP_TIMEOUT = 120.0
HEALTH_TIMEOUT = 2.0
AUTOSTART_WAIT = 15.0
AUTOSTART_POLL = 0.25
ENABLE_HINT = "请在 VBHelper 面板启用 MCP 服务（功能 → MCP 工具总线 → 开启服务）。"

_CONFIG_MODULE: Any = None
_CONFIG_TRIED = False
_LOG_PATH_CACHE: str | None = None

# ---------------------------------------------------------------- 基础工具

def _log(message: str) -> None:
    """日志只写 stderr 和日志文件，绝不污染 stdout。"""
    text = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        stream = sys.stderr
        if stream is not None:
            stream.write(text + "\n")
            stream.flush()
    except Exception:
        pass
    try:
        path = _log_path()
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
    except Exception:
        pass

def _log_path() -> str | None:
    global _LOG_PATH_CACHE
    if _LOG_PATH_CACHE is not None:
        return _LOG_PATH_CACHE
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return None
    _LOG_PATH_CACHE = os.path.join(base, "VBHelper", "logs",
                                   "mcp-stdio.log")
    return _LOG_PATH_CACHE

def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() in ("1", "true", "yes", "on")

def _paths():
    """AppPaths 不依赖 Tk，可直接用于定位 endpoint/config。"""
    try:
        from vbhelper.context import AppPaths
        return AppPaths.default()
    except Exception as exc:
        _log("读取 AppPaths 失败：%r（回退到环境变量路径）" % (exc,))
        return None

def _mcp_config() -> Any:
    """features.mcp.config 属存储层；失败时回退为直接读写 JSON。"""
    global _CONFIG_MODULE, _CONFIG_TRIED
    if not _CONFIG_TRIED:
        _CONFIG_TRIED = True
        try:
            from vbhelper.features.mcp import config as module
            _CONFIG_MODULE = module
        except Exception as exc:
            _log("无法导入 features.mcp.config：%r（回退为直接读文件）"
                 % (exc,))
            _CONFIG_MODULE = None
    return _CONFIG_MODULE

def _default_endpoint_path() -> str:
    paths = _paths()
    module = _mcp_config()
    if paths is not None and module is not None \
            and hasattr(module, "endpoint_path"):
        try:
            return str(module.endpoint_path(paths))
        except Exception as exc:
            _log("config.endpoint_path 失败：%r" % (exc,))
    if paths is not None:
        mcp_dir = getattr(paths, "mcp_dir", None) \
            or os.path.join(paths.local_dir, "mcp")
        return os.path.join(mcp_dir, "endpoint.json")
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
        or os.path.expanduser("~")
    return os.path.join(base, "VBHelper", "mcp", "endpoint.json")

def _config_file_path() -> str:
    paths = _paths()
    if paths is not None:
        return str(getattr(paths, "config_file",
                           os.path.join(paths.config_dir, "config.json")))
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "VBHelper", "config.json")

def _load_endpoint(path: str) -> dict | None:
    module = _mcp_config()
    data: Any = None
    if module is not None and hasattr(module, "read_endpoint_file"):
        try:
            data = module.read_endpoint_file(path)
        except Exception as exc:
            _log("读取 endpoint 失败（config）：%r" % (exc,))
            data = None
    if not isinstance(data, dict):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except Exception as exc:
            _log("读取 endpoint 失败（json）：%r" % (exc,))
            return None
    return data if isinstance(data, dict) else None

def _endpoint_url(endpoint: dict) -> str:
    url = endpoint.get("url")
    if isinstance(url, str) and url.strip():
        text = url.strip()
        if text.startswith(("http://", "https://")):
            return text
    host = str(endpoint.get("host") or "127.0.0.1")
    try:
        port = int(endpoint.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if port <= 0:
        return ""
    return "http://%s:%d/mcp" % (host, port)

def _health_url(endpoint: dict) -> str:
    url = _endpoint_url(endpoint)
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    return "%s://%s/health" % (parts.scheme or "http", parts.netloc)

# ---------------------------------------------------------------- 流恢复

def _restore_std_handle(name: str) -> TextIO | None:
    """noconsole EXE 下 sys.stdout/stderr/stdin 为 None，用 CRT 句柄恢复。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import msvcrt
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        std_id = {"stdin": -10, "stdout": -11, "stderr": -12}[name]
        handle = kernel32.GetStdHandle(std_id)
        if not handle or int(handle) == int(ctypes.c_void_p(-1).value):
            _log("GetStdHandle(%s) 未返回有效句柄" % name)
            return None
        if name == "stdin":
            fd = msvcrt.open_osfhandle(int(handle),
                                      os.O_RDONLY | getattr(os, "O_BINARY", 0))
            raw = io.FileIO(fd, "r", closefd=True)
            return io.TextIOWrapper(raw, encoding="utf-8",
                                    errors="replace")
        fd = msvcrt.open_osfhandle(int(handle),
                                  os.O_WRONLY | getattr(os, "O_BINARY", 0))
        raw = io.FileIO(fd, "w", closefd=True)
        return io.TextIOWrapper(raw, encoding="utf-8", newline="\n",
                                write_through=True)
    except Exception as exc:
        _log("恢复 %s 句柄失败：%r" % (name, exc))
        return None

def _configure_streams() -> None:
    """统一 UTF-8，避免中文 JSON 在 cp936 控制台/管道里炸掉。"""
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                                   newline="\n")
    except Exception:
        pass
    try:
        if sys.stderr is not None:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace",
                                   newline="\n")
    except Exception:
        pass
    try:
        if sys.stdin is not None:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _ensure_streams() -> bool:
    """确保 stdout/stderr/stdin 可用；stdout/stdin 恢复失败返回 False。"""
    if sys.stdout is None:
        sys.stdout = _restore_std_handle("stdout")
    if sys.stderr is None:
        sys.stderr = _restore_std_handle("stderr")
    if sys.stdin is None:
        sys.stdin = _restore_std_handle("stdin")
    _configure_streams()
    if sys.stdout is None:
        _log("无法恢复 stdout，stdio 桥无法工作")
        return False
    if sys.stdin is None:
        _log("无法恢复 stdin，stdio 桥无法工作")
        return False
    return True

# ---------------------------------------------------------------- 协议输出

def _emit(payload: Any) -> None:
    """输出单行 JSON 到 stdout 并 flush（唯一允许的 stdout 写入）。"""
    try:
        line = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":"))
        stream = sys.stdout
        if stream is None:
            _log("stdout 不可用，丢弃响应")
            return
        stream.write(line + "\n")
        stream.flush()
    except Exception as exc:
        _log("写 stdout 失败：%r" % (exc,))

def _rpc_error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": int(code), "message": str(message)}}

def _emit_error(request_id: Any, code: int, message: str) -> None:
    _emit(_rpc_error(request_id, code, message))

def _is_notification(payload: Any) -> bool:
    if isinstance(payload, dict):
        method = payload.get("method")
        if isinstance(method, str) and method.startswith("notifications/"):
            return True
        return "id" not in payload
    if isinstance(payload, list):
        return bool(payload) and all(_is_notification(item)
                                     for item in payload)
    return False

def _request_id(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("id")
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and "id" in item:
                return item.get("id")
    return None

def _iter_lines(stream: TextIO) -> Iterator[str]:
    while True:
        try:
            line = stream.readline()
        except KeyboardInterrupt:
            return
        except Exception as exc:
            _log("读取 stdin 失败：%r" % (exc,))
            return
        if not line:
            return
        yield line

# ---------------------------------------------------------------- HTTP

def _headers_dict(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for key, value in headers.items():
            out[str(key).lower()] = str(value)
    except Exception:
        pass
    return out

def _health(endpoint: dict, timeout: float = HEALTH_TIMEOUT) -> bool:
    token = endpoint.get("token")
    url = _health_url(endpoint)
    if not token or not url:
        return False
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer %s" % token,
                 "Accept": "application/json"},
        method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status) != 200:
                return False
            data = json.loads(response.read().decode("utf-8", "replace"))
            return isinstance(data, dict) and bool(data.get("ok"))
    except Exception:
        return False

def _post_json(url: str, token: str, session: str | None, raw: str,
               timeout: float = HTTP_TIMEOUT) -> tuple[int, dict, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
    }
    if session:
        headers[SESSION_HEADER] = session
    request = urllib.request.Request(url, data=raw.encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return int(response.status), _headers_dict(response.headers), body
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return int(exc.code), _headers_dict(exc.headers), body

def _short_exc(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:200]

def _handle_http_response(status: int, body: str, *,
                          is_notification: bool, request_id: Any) -> None:
    if is_notification:
        if status >= 400:
            _log("通知转发返回 HTTP %d" % status)
        return
    if 200 <= status < 300:
        text = body.strip()
        if not text:
            _emit_error(request_id, -32000,
                        "MCP 服务返回了空响应（HTTP %d）" % status)
            return
        try:
            payload = json.loads(text)
        except Exception:
            _log("服务返回非 JSON（HTTP %d）：%r" % (status, text[:300]))
            _emit_error(request_id, -32000,
                        "MCP 服务返回了非 JSON 响应（HTTP %d）" % status)
            return
        _emit(payload)
        return

    # 非 2xx：优先识别服务端返回的 JSON-RPC error
    detail = ""
    try:
        data = json.loads(body)
    except Exception:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        _emit(data)
        return
    if isinstance(data, dict):
        detail = str(data.get("message") or data.get("error") or "")
    if status == 401:
        message = ("认证失败（HTTP 401）：endpoint 中的 token 已失效，"
                   "请重启 VBHelper 或更新 MCP 连接配置。")
    else:
        message = "MCP 服务返回 HTTP %d" % status
        if detail:
            message += "：%s" % detail[:200]
    _emit_error(request_id, -32000, message)

# ---------------------------------------------------------------- 自启动

def _config_state() -> tuple[bool, bool]:
    """返回 (mcp.enabled, mcp.server.auto_start)；缺省 auto_start=True。"""
    path = _config_file_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return False, True
    except Exception as exc:
        _log("读取 config.json 失败：%r" % (exc,))
        return False, True
    mcp_cfg = data.get("mcp") if isinstance(data, dict) else None
    if not isinstance(mcp_cfg, dict):
        return False, True
    server = mcp_cfg.get("server")
    auto = bool(server.get("auto_start", True)) \
        if isinstance(server, dict) else True
    return bool(mcp_cfg.get("enabled")), auto

def _config_enabled() -> bool:
    return _config_state()[0]

def _launch_gui() -> subprocess.Popen | None:
    """冻结环境直接跑 EXE；源码环境用 pythonw.exe + run.py。"""
    if getattr(sys, "frozen", False):
        args = [sys.executable]
        cwd = os.path.dirname(sys.executable) or None
    else:
        exe = sys.executable
        pythonw = os.path.join(os.path.dirname(exe) or ".", "pythonw.exe")
        if os.path.isfile(pythonw):
            exe = pythonw
        run_py = os.path.join(REPO_ROOT, "run.py")
        if not os.path.isfile(run_py):
            _log("找不到 run.py：%s" % run_py)
            return None
        args = [exe, run_py]
        cwd = REPO_ROOT
    flags = 0
    if os.name == "nt":
        flags = (int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                 | int(getattr(subprocess, "DETACHED_PROCESS", 0)))
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as exc:
        _log("启动 VBHelper 失败：%r" % (exc,))
        return None
    _log("已请求启动 VBHelper：%r" % (args,))
    return proc

def _app_running() -> bool:
    """检查 VBHelper 的单实例互斥体是否存在（不弹窗、不启动进程）。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenMutexW(0x00100000, False,
                                     "VBHelper_SingleInstance")
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False

def _request_service_start(endpoint_path: str, reason: str = "stdio") -> bool:
    """请已运行的 GUI 启动 MCP 服务（它在轮询 start.request）。"""
    directory = os.path.dirname(endpoint_path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "start.request")
        data = {"ts": time.time(), "pid": os.getpid(), "reason": reason}
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except Exception as exc:
        _log("写启动请求失败：%r" % (exc,))
        return False

def _wait_healthy(path: str,
                  proc: subprocess.Popen | None = None,
                  timeout: float = AUTOSTART_WAIT) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        endpoint = _load_endpoint(path)
        if endpoint and _health(endpoint):
            return endpoint
        if proc is not None and proc.poll() is not None:
            _log("VBHelper 进程提前退出（code=%r）" % (proc.returncode,))
            break
        time.sleep(AUTOSTART_POLL)
    return None

# ---------------------------------------------------------------- 主循环

_LAUNCH_TRIED = False

def _recover_endpoint(endpoint_path: str | None,
                      wait: float = 4.5) -> dict | None:
    """服务掉线/重启后恢复：先读 endpoint，必要时请后台实例启动。"""
    global _LAUNCH_TRIED
    if not endpoint_path:
        return None
    endpoint = _load_endpoint(endpoint_path)
    if endpoint and _health(endpoint):
        return endpoint
    enabled, auto_start = _config_state()
    if not enabled or not auto_start:
        return None
    if _app_running():
        _log("MCP 服务未响应，请求后台实例启动/恢复 ...")
        _request_service_start(endpoint_path, reason="reconnect")
        return _wait_healthy(endpoint_path, None, wait)
    # 主进程不在：后台拉起 GUI，但不要在这里等 15 秒；让本次调用报错，
    # 客户端稍后重试时 endpoint 通常已经就绪。并发请求只尝试一次，避免
    # 同时拉起多个 GUI 撞单实例弹窗。
    if not _LAUNCH_TRIED:
        _LAUNCH_TRIED = True
        _log("VBHelper 未运行，后台请求启动 GUI ...")
        _launch_gui()
    return None

def _serve_error_mode(stream: TextIO, reason: str,
                      endpoint_path: str | None = None,
                      recover_first: bool = True) -> int:
    """服务不可用时：每个请求都回 -32000，通知静默，不退出。

    每收到一行先探一次健康；用户在面板点「启动」/后台实例恢复后，
    **同一个桥进程**会自动切回转发模式，不需要客户端重连。
    ``recover_first=False`` 时第一条请求直接报错（刚等过启动，别让
    客户端再多等 3 秒），之后的行仍会尝试恢复。
    """
    _log("进入错误应答模式：%s" % reason)
    allow_recover = bool(recover_first)
    for line in _iter_lines(stream):
        raw = line.strip()
        if not raw:
            continue
        if endpoint_path and allow_recover:
            endpoint = _recover_endpoint(endpoint_path, wait=3.0)
            if endpoint is not None:
                _log("MCP 服务已恢复，切换转发模式")
                return _serve_bridge(stream, endpoint,
                                     endpoint_path=endpoint_path,
                                     first_line=line)
        allow_recover = True
        try:
            payload = json.loads(raw)
        except Exception:
            _emit_error(None, -32700, "JSON 解析失败：请求不是合法 JSON")
            continue
        if isinstance(payload, dict):
            if "id" in payload:
                _emit_error(payload.get("id"), -32000, reason)
        elif isinstance(payload, list):
            errors = [_rpc_error(item.get("id"), -32000, reason)
                      for item in payload
                      if isinstance(item, dict) and "id" in item]
            if errors:
                _emit(errors)
        else:
            _emit_error(None, -32000, reason)
    return 0

def _serve_bridge(stream: TextIO, endpoint: dict, *,
                  endpoint_path: str | None = None,
                  first_line: str | None = None) -> int:
    url = _endpoint_url(endpoint)
    token = str(endpoint.get("token") or "")
    if not url or not token:
        return _serve_error_mode(stream, "MCP endpoint 文件缺少 url 或 token；"
                                         "请重启 VBHelper 后重试。",
                                 endpoint_path)
    _log("已连接 MCP 服务：%s" % url)
    session: str | None = None

    def _lines():
        if first_line is not None:
            yield first_line
        yield from _iter_lines(stream)

    for line in _lines():
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            _emit_error(None, -32700, "JSON 解析失败：请求不是合法 JSON")
            continue
        is_notification = _is_notification(payload)
        request_id = _request_id(payload)
        try:
            status, headers, body = _post_json(url, token, session, raw)
        except Exception as first_exc:
            _log("请求 MCP 服务失败：%r" % (first_exc,))
            fresh = _recover_endpoint(endpoint_path)
            if fresh is not None:
                new_url = _endpoint_url(fresh)
                new_token = str(fresh.get("token") or "")
                if new_url and new_token:
                    _log("已切换到新的 MCP endpoint：%s" % new_url)
                    url, token = new_url, new_token
                    session = None          # 旧会话随进程重启已失效
                    try:
                        status, headers, body = _post_json(
                            url, token, session, raw)
                    except Exception as second_exc:
                        _log("恢复后重试仍失败：%r" % (second_exc,))
                        if not is_notification:
                            _emit_error(
                                request_id, -32000,
                                "VBHelper MCP 服务未响应（%s）。"
                                "请在面板确认服务已启动。"
                                % _short_exc(second_exc))
                        continue
                else:
                    if not is_notification:
                        _emit_error(request_id, -32000,
                                    "MCP endpoint 缺少 url/token；请重启 VBHelper。")
                    continue
            else:
                if not is_notification:
                    _emit_error(
                        request_id, -32000,
                        "无法连接 VBHelper MCP 服务（%s）。"
                        "若 VBHelper 在后台运行，请打开面板 → MCP 工具总线 → "
                        "启动；面板「停止」会同时关闭自动启动。"
                        % _short_exc(first_exc))
                continue
        new_session = headers.get(SESSION_HEADER.lower())
        if new_session:
            session = new_session
        _handle_http_response(status, body,
                              is_notification=is_notification,
                              request_id=request_id)
    return 0

def _usage() -> str:
    return ("用法：python -m vbhelper.mcp_stdio [--endpoint PATH] "
            "[--no-autostart]\n"
            "环境变量：VBHELPER_MCP_ENDPOINT / "
            "VBHELPER_MCP_NO_AUTOSTART=1")

def _parse_args(argv: list[str]) -> tuple[str | None, bool, bool] | None:
    """返回 (endpoint, no_autostart, help)；参数错误返回 None。"""
    endpoint: str | None = None
    no_autostart = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-h", "--help"):
            return None, False, True
        if arg == "--endpoint":
            if index + 1 >= len(argv):
                _log("--endpoint 需要一个路径参数")
                return None
            endpoint = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--endpoint="):
            endpoint = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--no-autostart":
            no_autostart = True
            index += 1
            continue
        _log("未知参数：%s" % arg)
        return None
    return endpoint, no_autostart, False

def _version() -> str:
    try:
        from vbhelper import __version__
        return str(__version__) or "1.0"
    except Exception:
        return "1.0"

def main(argv: list[str] | None = None) -> int:
    """stdio 桥入口：返回进程退出码。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not _ensure_streams():
        return 2
    _log("VBHelper MCP stdio 桥启动 v%s（pid=%d）" % (_version(), os.getpid()))
    parsed = _parse_args(argv)
    if parsed is None:
        try:
            sys.stderr.write(_usage() + "\n")
        except Exception:
            pass
        return 2
    endpoint_arg, no_autostart, help_requested = parsed
    if help_requested:
        try:
            sys.stderr.write(_usage() + "\n")
        except Exception:
            pass
        return 0

    endpoint_path = (endpoint_arg
                     or os.environ.get("VBHELPER_MCP_ENDPOINT")
                     or _default_endpoint_path())
    if not no_autostart:
        no_autostart = _env_flag("VBHELPER_MCP_NO_AUTOSTART")

    endpoint = _load_endpoint(endpoint_path)
    if endpoint is not None and _health(endpoint):
        return _serve_bridge(sys.stdin, endpoint,
                             endpoint_path=endpoint_path)

    if no_autostart:
        return _serve_error_mode(sys.stdin, ENABLE_HINT, endpoint_path)

    enabled, auto_start = _config_state()
    if not enabled:
        _log("config.json 中 mcp.enabled 不为真，不自动启动 GUI")
        return _serve_error_mode(sys.stdin, ENABLE_HINT, endpoint_path)
    if not auto_start:
        _log("config.json 中 server.auto_start=false，只允许手动启动")
        return _serve_error_mode(
            sys.stdin,
            "MCP 自动启动已关闭。请打开悬浮球 → ☰ 功能 → MCP 工具总线 → "
            "启动，或在「连接」页打开自动启动。", endpoint_path)

    # 先请「已经在后台运行的 GUI」启动服务（它轮询 start.request）；
    # 这样不会撞上单实例互斥、也不会弹「已经在运行」。
    if _app_running():
        _log("VBHelper 正在后台运行，请求它启动 MCP 服务 ...")
        _request_service_start(endpoint_path, reason="startup")
        endpoint = _wait_healthy(endpoint_path, None, timeout=6.0)
        if endpoint is not None:
            return _serve_bridge(sys.stdin, endpoint,
                                 endpoint_path=endpoint_path)
        return _serve_error_mode(
            sys.stdin,
            "VBHelper 正在后台运行，但 MCP 服务没有响应。请打开悬浮球 → "
            "☰ 功能 → MCP 工具总线 → 启动；若面板里是旧版本，请退出"
            "VBHelper 后重开。",
            endpoint_path, recover_first=False)

    _log("VBHelper 未运行，启动 GUI ...")
    proc = _launch_gui()
    endpoint = _wait_healthy(endpoint_path, proc)
    if endpoint is None:
        return _serve_error_mode(
            sys.stdin, "VBHelper 已请求启动，但 MCP 服务在 15 秒内未就绪。"
                       + ENABLE_HINT, endpoint_path)
    return _serve_bridge(sys.stdin, endpoint, endpoint_path=endpoint_path)

if __name__ == "__main__":
    sys.exit(main())
