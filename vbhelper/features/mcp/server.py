# -*- coding: utf-8 -*-
"""MCP 2025-06-18 风格服务端（Streamable HTTP，JSON 响应）。

设计要点（计划文档 §4.3）：
- 只绑 IPv4 环回地址；必须配置 bearer token，常量时间比较；
- ``POST /mcp`` 处理单条或批量 JSON-RPC；``GET /health`` 探活（也要 token）；
- 不支持 SSE（``GET /mcp`` 返回 405），所有请求返回 application/json；
- 工具执行全局串行，单工具超时返回 ``isError``；确认策略 ask/session/never；
- 每次工具调用写审计（参数脱敏，input.type/clipboard.write 只记长度）；
- 日志只写 stderr（``VBHELPER_MCP_DEBUG=1`` 时），绝不写 stdout。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from concurrent.futures import Future, TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .registry import (
    RateLimiter,
    ToolCall,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    tool_enabled,
)

__all__ = ["McpServer", "PROTOCOL_VERSION", "SESSION_HEADER",
           "SUPPORTED_PROTOCOLS"]

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
SESSION_HEADER = "Mcp-Session-Id"

MAX_BODY = 8 * 1024 * 1024           # POST 体上限 8MB
SESSION_TTL = 3600.0                 # 会话 1 小时不活跃即回收
_CONFIRM_MODES = ("ask", "session", "never")

_DEBUG = os.environ.get("VBHELPER_MCP_DEBUG", "").strip().lower() in (
    "1", "true", "yes", "on")


def _app_version() -> str:
    try:
        from vbhelper import __version__
        return str(__version__) or "1.0"
    except Exception:
        return "1.0"


APP_VERSION = _app_version()


def _dbg(msg: str) -> None:
    """诊断日志：仅 stderr（不影响 stdio 协议的 stdout）。"""
    if not _DEBUG:
        return
    try:
        sys.stderr.write("[mcp %s] %s\n"
                         % (time.strftime("%H:%M:%S"), msg))
        sys.stderr.flush()
    except Exception:
        pass


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _is_loopback_host(host: Any) -> bool:
    """只允许 IPv4 环回：127.0.0.0/8 或 localhost。"""
    if not isinstance(host, str) or not host.strip():
        return False
    text = host.strip().lower()
    if text == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_loopback


def _rpc_error(request_id: Any, code: int, message: str,
               data: Any = None) -> dict:
    error: dict = {"code": int(code), "message": str(message)}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _rpc_result(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class _RpcError(Exception):
    """内部用：把 JSON-RPC 错误从处理方法里抛出来。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.data = data


class _ToolTimeout(Exception):
    """工具超时（或排队等待前一个工具超时）。"""

    def __init__(self, spec: ToolSpec, seconds: float,
                 waiting: bool = False) -> None:
        super().__init__("工具超时：%s" % spec.name)
        self.spec = spec
        self.seconds = float(seconds)
        self.waiting = bool(waiting)


class McpServer:
    """MCP 服务端：HTTP 生命周期 + JSON-RPC 分发 + 工具执行策略。"""

    def __init__(self, ctx: Any, registry: ToolRegistry, services: Any, *,
                 host: str = "127.0.0.1", port: int = 0, token: str = "",
                 max_sessions: int = 8,
                 confirm: Callable[[str, str], bool] | None = None,
                 audit: Any = None, cfg: dict | None = None,
                 notify: Callable[[str, str], None] | None = None) -> None:
        if not _is_loopback_host(host):
            raise ValueError("MCP 服务只允许绑定 IPv4 环回地址（127.0.0.1），"
                             "当前 host=%r" % (host,))
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry 必须是 ToolRegistry")
        self._ctx = ctx
        self._registry = registry
        self._services = services
        self._host = host.strip()
        self._port_req = int(port or 0)
        self._token = str(token or "")
        self._max_sessions = max(1, int(max_sessions or 8))
        self._confirm = confirm
        self._audit = audit
        self._notify = notify
        self._cfg: dict = cfg if isinstance(cfg, dict) else {}

        self._state_lock = threading.RLock()
        self._exec_lock = threading.Lock()
        self._limiter = RateLimiter()

        self._sessions: dict[str, dict] = {}
        self._grants: dict[tuple[str, str], float] = {}

        self._httpd: _McpHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port_actual = 0
        self._started_at = 0.0

        self._calls = 0
        self._errors = 0

    # ------------------------------------------------------------ 生命周期

    def start(self) -> int:
        """启动 HTTP 服务并立即返回实际端口；重复调用幂等。"""
        with self._state_lock:
            if self._httpd is not None and self._thread is not None \
                    and self._thread.is_alive():
                return self._port_actual
            if not self._token:
                raise ValueError("MCP 服务必须配置 bearer token，"
                                 "拒绝无认证启动")
            if not _is_loopback_host(self._host):
                raise ValueError("MCP 服务只允许绑定 127.0.0.1")
            httpd = _McpHTTPServer((self._host, self._port_req),
                                   _McpRequestHandler, self)
            thread = threading.Thread(target=self._serve,
                                      args=(httpd,),
                                      name="mcp-server", daemon=True)
            self._httpd = httpd
            self._port_actual = int(httpd.server_address[1])
            self._thread = thread
            self._started_at = time.time()
            self._prune_locked(time.time())
            _dbg("服务已启动：%s:%d" % (self._host, self._port_actual))
            thread.start()
            return self._port_actual

    def _serve(self, httpd: "_McpHTTPServer") -> None:
        try:
            httpd.serve_forever(poll_interval=0.2)
        except Exception as exc:       # 关闭阶段偶发异常不影响进程
            _dbg("serve_forever 退出：%r" % (exc,))

    def stop(self) -> None:
        """关闭端口并等待服务线程退出；幂等。"""
        with self._state_lock:
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception as exc:
                _dbg("shutdown 异常：%r" % (exc,))
            try:
                httpd.server_close()
            except Exception as exc:
                _dbg("server_close 异常：%r" % (exc,))
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._state_lock:
            self._sessions.clear()
            self._grants.clear()
            self._port_actual = 0
        self._limiter.reset()
        _dbg("服务已停止")

    @property
    def running(self) -> bool:
        with self._state_lock:
            return (self._httpd is not None and self._thread is not None
                    and self._thread.is_alive())

    @property
    def port(self) -> int:
        with self._state_lock:
            return int(self._port_actual)

    @property
    def token(self) -> str:
        return self._token

    @property
    def url(self) -> str:
        with self._state_lock:
            if self._httpd is None or not self._port_actual:
                return ""
            return "http://%s:%d/mcp" % (self._host, self._port_actual)

    def status(self) -> dict:
        """运行状态（绝不包含 token）。"""
        now = time.time()
        self._prune(now)
        with self._state_lock:
            running = (self._httpd is not None
                       and self._thread is not None
                       and self._thread.is_alive())
            url = ("http://%s:%d/mcp" % (self._host, self._port_actual)
                   if self._httpd is not None and self._port_actual else "")
            return {
                "running": running,
                "port": int(self._port_actual) if self._httpd is not None
                        else 0,
                "url": url,
                "sessions": len(self._sessions),
                "calls": int(self._calls),
                "errors": int(self._errors),
                "started_at": float(self._started_at),
            }

    def health_payload(self) -> dict:
        """GET /health 的响应体（不含 token）。"""
        self._prune(time.time())
        with self._state_lock:
            count = len(self._sessions)
        return {
            "ok": True,
            "name": "vbhelper",
            "version": APP_VERSION,
            "port": self.port,
            "sessions": count,
        }

    # ------------------------------------------------------------ 会话

    def _prune(self, now: float) -> None:
        with self._state_lock:
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - SESSION_TTL
        for sid in [sid for sid, info in self._sessions.items()
                    if float(info.get("last_seen") or 0.0) < cutoff]:
            self._sessions.pop(sid, None)
        for key in [key for key, expiry in self._grants.items()
                    if expiry <= now]:
            self._grants.pop(key, None)

    def _touch_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        now = time.time()
        with self._state_lock:
            info = self._sessions.get(session_id)
            if info is not None:
                info["last_seen"] = now

    def _new_session(self, client_name: str) -> str:
        now = time.time()
        with self._state_lock:
            self._prune_locked(now)
            if len(self._sessions) >= self._max_sessions:
                raise _RpcError(
                    -32600,
                    "会话数已达上限（%d），请关闭其他客户端或稍后重试"
                    % self._max_sessions)
            sid = secrets.token_hex(8)
            self._sessions[sid] = {
                "client": client_name or "unknown",
                "created": now,
                "last_seen": now,
            }
            return sid

    # ------------------------------------------------------------ JSON-RPC

    def handle(self, payload: Any, session_id: str | None = None
               ) -> tuple[Any, str | None]:
        """处理一条 JSON-RPC 消息或 batch；返回 (响应, 会话 id)。

        通知返回 ``(None, session_id)``；batch 全为通知时也返回 None。
        """
        self._prune(time.time())
        self._touch_session(session_id)
        if isinstance(payload, list):
            responses: list[Any] = []
            sid = session_id
            for item in payload:
                response, sid = self._handle_one(item, sid)
                if response is not None:
                    responses.append(response)
            return (responses if responses else None), sid
        return self._handle_one(payload, session_id)

    def _handle_one(self, message: Any, session_id: str | None
                    ) -> tuple[Any, str | None]:
        if not isinstance(message, dict):
            return _rpc_error(None, -32600,
                              "无效的 JSON-RPC 请求：消息必须是对象"), session_id
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return _rpc_error(request_id, -32600,
                              "无效的 JSON-RPC 请求：缺少 method"), session_id
        if method.startswith("notifications/"):
            return None, session_id
        if "id" not in message:
            # 其它无 id 消息按通知处理：不执行，避免产生没有确认路径的副作用
            return None, session_id

        params = message.get("params")
        if params is None:
            params = {}
        try:
            return self._dispatch(method, params, request_id, session_id)
        except _RpcError as exc:
            return _rpc_error(request_id, exc.code, exc.message,
                              exc.data), session_id
        except Exception as exc:
            _dbg("处理 %s 异常：%r" % (method, exc))
            return _rpc_error(request_id, -32603,
                              "服务内部错误：%s" % type(exc).__name__), session_id

    def _dispatch(self, method: str, params: Any, request_id: Any,
                  session_id: str | None) -> tuple[Any, str | None]:
        if method == "initialize":
            return self._initialize(params, request_id, session_id)
        if method == "ping":
            return _rpc_result(request_id, {}), session_id
        if method == "tools/list":
            return _rpc_result(request_id, self._tools_list()), session_id
        if method == "tools/call":
            return _rpc_result(request_id, self._tools_call(params,
                                                            session_id)
                               ), session_id
        if method == "resources/list":
            return _rpc_result(request_id, {"resources": []}), session_id
        if method == "prompts/list":
            return _rpc_result(request_id, {"prompts": []}), session_id
        if method in ("resources/read", "resources/templates/list",
                      "prompts/get", "logging/setLevel",
                      "completion/complete"):
            raise _RpcError(-32601, "本服务不支持方法：%s" % method)
        raise _RpcError(-32601, "未知方法：%s" % method)

    def _initialize(self, params: Any, request_id: Any,
                    session_id: str | None) -> tuple[Any, str | None]:
        params = _as_dict(params)
        requested = params.get("protocolVersion")
        negotiated = requested if requested in SUPPORTED_PROTOCOLS \
            else PROTOCOL_VERSION
        client_info = _as_dict(params.get("clientInfo"))
        client_name = str(client_info.get("name") or "unknown")[:80]

        sid = session_id if session_id and self._session_exists(session_id) \
            else None
        if sid is None:
            sid = self._new_session(client_name)
        else:
            with self._state_lock:
                info = self._sessions.get(sid)
                if info is not None:
                    info["client"] = client_name
                    info["last_seen"] = time.time()
        result = {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {},
            },
            "serverInfo": {"name": "vbhelper", "version": APP_VERSION},
            "instructions": (
                "VBHelper 是 Windows 悬浮球/托盘工具，窗口可能隐藏或缩到托盘，"
                "这不代表进程已退出。若某次工具调用失败，先调用 system.info 确认 "
                "app_running/app_pid，再用 screen.capture 查看屏幕；不要直接断言"
                "「程序已死」。截屏坐标换算见工具返回文本。鼠标键盘工具需要用户"
                "在面板开启；危险操作会请求确认，用户拒绝时不要重复强推。"
                "如果项目已在 VBHelper 里建立 RAG 索引，先用 rag.search 检索，"
                "只读取返回的 path:line 片段；不要用 shell 大批量扫盘。"
                "不要点击或关闭 VBHelper 自己的悬浮球/面板，也不要操作 Windows "
                "安全界面、锁屏或其它 topmost 安全窗口；需要这类操作时请用户自己来。"
            ),
        }
        return _rpc_result(request_id, result), sid

    def _session_exists(self, session_id: str) -> bool:
        with self._state_lock:
            return session_id in self._sessions

    # ------------------------------------------------------------ 工具列表

    def _tools_list(self) -> dict:
        tools = []
        for spec in self._registry.enabled_specs(self._cfg):
            tools.append({
                "name": spec.name,
                "title": spec.title,
                "description": spec.description,
                "inputSchema": spec.schema,
                "annotations": {
                    "readOnlyHint": spec.permission == "safe",
                },
            })
        return {"tools": tools}

    # ------------------------------------------------------------ 工具调用

    def _tools_call(self, params: Any, session_id: str | None) -> dict:
        if not isinstance(params, dict):
            raise _RpcError(-32602, "tools/call 的 params 必须是对象")
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _RpcError(-32602, "tools/call 缺少工具名 name")
        name = name.strip()
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(-32602, "tools/call 的 arguments 必须是对象")

        spec = self._registry.get(name)
        if spec is None or not tool_enabled(spec, self._cfg):
            safe = (self._audit_args(spec, arguments) if spec is not None
                    else _short_args(arguments))
            self._audit_call(name, safe, False, 0, "未知或未启用的工具")
            raise _RpcError(-32602,
                            "未知或未启用的工具：%s（请在 VBHelper 面板启用）"
                            % name)

        safe_args = self._audit_args(spec, arguments)
        started = time.perf_counter()
        result: ToolResult | None = None
        error_note = ""
        try:
            if spec.rate_limit and not self._limiter.allow(spec.name,
                                                           spec.rate_limit):
                result = _error_result(
                    "调用过于频繁：%s 超过限流 %s，请稍后再试。"
                    % (spec.name, spec.rate_limit))
                error_note = "限流"
            elif spec.permission == "confirm" \
                    and not self._confirm_allowed(spec, arguments, session_id):
                result = _error_result(
                    "用户已拒绝执行工具 %s（%s）。"
                    % (spec.name, spec.title))
                error_note = "用户拒绝"
            else:
                call = ToolCall(
                    ctx=self._ctx,
                    services=self._services,
                    args=dict(arguments),
                    session=session_id or "",
                    confirm=self._confirm,
                    audit=self._audit,
                    cfg=self._cfg,
                    notify=self._notify,
                )
                try:
                    outcome = self._execute(spec, call)
                except _ToolTimeout as exc:
                    if exc.waiting:
                        message = ("工具执行排队超时：前一个工具仍在运行，"
                                   "已放弃本次调用 %s。" % spec.name)
                    else:
                        message = ("工具执行超时（%s 秒）：%s。"
                                   % (_fmt_seconds(exc.seconds), spec.name))
                    result = _error_result(message)
                    error_note = "超时"
                except ToolError as exc:
                    result = _error_result(
                        "工具执行失败（%s）：%s"
                        % (exc.code or "tool_error", exc.message))
                    error_note = "ToolError:%s" % (exc.code or "tool_error")
                except Exception as exc:
                    result = _error_result(
                        "工具执行异常（%s）：%s"
                        % (type(exc).__name__, exc or "无详细信息"))
                    error_note = type(exc).__name__
                else:
                    result = _normalize_result(outcome)
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            ok = result is not None and not result.is_error
            if result is not None and result.is_error and not error_note:
                error_note = _first_text(result)[:120]
            self._audit_call(name, safe_args, ok, elapsed_ms, error_note)
            with self._state_lock:
                self._calls += 1
                if not ok:
                    self._errors += 1
            if result is None:          # 理论上不会发生，防御异常路径
                result = _error_result("工具执行未返回结果（内部错误）。")
        return _result_payload(result)

    def _confirm_allowed(self, spec: ToolSpec, arguments: dict,
                         session_id: str | None) -> bool:
        confirm_cfg = _as_dict(self._cfg.get("confirm"))
        mode = confirm_cfg.get("mode", "ask")
        if mode not in _CONFIRM_MODES:
            mode = "ask"
        if mode == "never":
            return True
        grant_key = "input" if spec.group == "input" else spec.name
        sid = session_id or ""
        if mode == "session" and sid \
                and self._grant_valid(sid, grant_key, confirm_cfg):
            return True
        if self._confirm is None:
            return False           # ask/session 且没有确认回调 → 视为拒绝
        summary = self._confirm_summary(spec, arguments)
        try:
            answer = bool(self._confirm(spec.title, summary))
        except Exception as exc:
            _dbg("confirm 回调异常：%r" % (exc,))
            return False
        if answer and mode == "session" and sid:
            self._store_grant(sid, grant_key, confirm_cfg)
        return answer

    def _grant_valid(self, sid: str, key: str, confirm_cfg: dict) -> bool:
        minutes = _session_minutes(confirm_cfg)
        if minutes <= 0:
            return False
        now = time.time()
        with self._state_lock:
            expiry = self._grants.get((sid, key))
            if expiry is None:
                return False
            if expiry <= now:
                self._grants.pop((sid, key), None)
                return False
            return True

    def _store_grant(self, sid: str, key: str, confirm_cfg: dict) -> None:
        minutes = _session_minutes(confirm_cfg)
        if minutes <= 0:
            return
        with self._state_lock:
            self._grants[(sid, key)] = time.time() + minutes * 60.0

    def _confirm_summary(self, spec: ToolSpec, arguments: dict) -> str:
        safe = self._audit_args(spec, arguments)
        try:
            arg_text = json.dumps(safe, ensure_ascii=False)
        except Exception:
            arg_text = str(safe)
        if len(arg_text) > 160:
            arg_text = arg_text[:160] + "…"
        return "工具：%s（%s）\n参数：%s" % (spec.title, spec.name, arg_text)

    def _audit_args(self, spec: ToolSpec, arguments: dict) -> dict:
        """审计/确认用参数：文本类只记长度，其余字符串截 200 字符。"""
        safe: dict = {}
        for key, value in arguments.items():
            label = str(key)
            if label == "text" and spec.name in ("input.type",
                                                 "clipboard.write"):
                if isinstance(value, str):
                    safe[label] = "<%d 字符>" % len(value)
                else:
                    safe[label] = "<非文本>"
            elif isinstance(value, str):
                safe[label] = value[:200] + ("…" if len(value) > 200 else "")
            elif isinstance(value, (int, float, bool)) or value is None:
                safe[label] = value
            else:
                try:
                    safe[label] = json.dumps(value, ensure_ascii=False)[:200]
                except Exception:
                    safe[label] = str(value)[:200]
        return safe

    def _audit_call(self, action: str, safe_args: dict, ok: bool,
                    elapsed_ms: int, error: str = "") -> None:
        if self._audit is None:
            return
        detail: dict = {"arguments": safe_args}
        if error:
            detail["error"] = str(error)[:300]
        try:
            self._audit.log("mcp", action, detail, ok=bool(ok),
                            elapsed_ms=int(elapsed_ms))
        except Exception as exc:
            _dbg("审计失败 %s：%r" % (action, exc))

    def _execute(self, spec: ToolSpec, call: ToolCall) -> Any:
        """全局串行执行，单工具超时；超时后执行线程自行释放锁。"""
        timeout = float(spec.timeout or 30.0)
        if timeout <= 0:
            timeout = 30.0
        acquired = self._exec_lock.acquire(timeout=timeout + 5.0)
        if not acquired:
            raise _ToolTimeout(spec, timeout, waiting=True)

        future: Future = Future()

        def runner() -> None:
            try:
                future.set_result(spec.handler(call))
            except BaseException as exc:        # 包含 ToolError
                try:
                    future.set_exception(exc)
                except Exception:
                    pass
            finally:
                try:
                    self._exec_lock.release()
                except RuntimeError:
                    pass

        thread = threading.Thread(target=runner,
                                  name="mcp-tool-%s" % spec.name,
                                  daemon=True)
        try:
            thread.start()
        except Exception:
            self._exec_lock.release()
            raise
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            raise _ToolTimeout(spec, timeout) from exc


# ---------------------------------------------------------------- 结果封装


def _error_result(message: str) -> ToolResult:
    result = ToolResult.text(message)
    result.is_error = True
    return result


def _normalize_result(outcome: Any) -> ToolResult:
    if isinstance(outcome, ToolResult):
        return outcome
    if outcome is None:
        return ToolResult.text("（工具执行完成，没有返回内容。）")
    return ToolResult.text(str(outcome))


def _result_payload(result: ToolResult) -> dict:
    content = result.content if isinstance(result.content, list) \
        else [{"type": "text", "text": str(result.content)}]
    payload: dict = {"content": content, "isError": bool(result.is_error)}
    if result.structured:
        payload["structuredContent"] = result.structured
    return payload


def _first_text(result: ToolResult) -> str:
    for item in result.content or []:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text") or "")
    return ""


def _short_args(arguments: dict) -> dict:
    """未知工具时没有 spec，按通用规则裁剪。"""
    safe: dict = {}
    for key, value in arguments.items():
        label = str(key)
        if isinstance(value, str):
            safe[label] = value[:200] + ("…" if len(value) > 200 else "")
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[label] = value
        else:
            try:
                safe[label] = json.dumps(value, ensure_ascii=False)[:200]
            except Exception:
                safe[label] = str(value)[:200]
    return safe


def _session_minutes(confirm_cfg: dict) -> float:
    try:
        minutes = float(confirm_cfg.get("session_minutes", 5))
    except (TypeError, ValueError):
        minutes = 5.0
    return max(0.0, min(minutes, 24 * 60.0))


def _fmt_seconds(seconds: float) -> str:
    text = "%.2f" % float(seconds)
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------- HTTP 层


class _McpHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int],
                 handler_cls: type[BaseHTTPRequestHandler],
                 mcp: McpServer) -> None:
        super().__init__(server_address, handler_cls)
        self.mcp = mcp


class _McpRequestHandler(BaseHTTPRequestHandler):
    """极简 Streamable HTTP 处理器：只支持 JSON 响应，不实现 SSE。"""

    protocol_version = "HTTP/1.1"
    server_version = "VBHelperMCP"
    sys_version = ""
    timeout = 30

    # ---------------- 基础工具 ----------------

    @property
    def mcp(self) -> McpServer:
        return self.server.mcp        # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        _dbg("http: " + (fmt % args))

    def log_error(self, fmt: str, *args: Any) -> None:
        _dbg("http error: " + (fmt % args))

    def address_string(self) -> str:
        try:
            return self.client_address[0]
        except Exception:
            return "-"

    def _route(self) -> str:
        try:
            path = urllib.parse.urlsplit(self.path).path
        except Exception:
            return "/"
        path = path.rstrip("/")
        return path or "/"

    def _header_auth(self) -> bool:
        header = self.headers.get("Authorization") or ""
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        token = parts[1].strip()
        expected = self.mcp.token
        if not expected or not token:
            return False
        return hmac.compare_digest(token, expected)

    def _send(self, status: int, body: bytes, *,
              content_type: str = "application/json; charset=utf-8",
              extra: dict[str, str] | None = None) -> None:
        try:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                if key.lower() == "connection" \
                        and str(value).strip().lower() == "close":
                    self.close_connection = True
                self.send_header(key, str(value))
            self.end_headers()
            if body:
                self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            _dbg("发送响应失败：%r" % (exc,))

    def _send_json(self, status: int, payload: Any,
                   extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        self._send(status, body, extra=extra)

    def _send_empty(self, status: int,
                    extra: dict[str, str] | None = None) -> None:
        self._send(status, b"", content_type="", extra=extra)

    def _unauthorized(self) -> None:
        self._send_json(401,
                        {"error": "unauthorized",
                         "message": "需要 Bearer token"},
                        extra={"WWW-Authenticate":
                               'Bearer realm="VBHelper-MCP"',
                               "Connection": "close"})

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:
        path = self._route()
        if path == "/mcp":
            self._send_json(
                405,
                {"error": "method_not_allowed",
                 "message": "MCP 端点只支持 POST JSON 响应模式，不支持 SSE。"},
                extra={"Allow": "POST"})
            return
        if path != "/health":
            self._send_json(404, {"error": "not_found",
                                  "message": "未知路径：%s" % path})
            return
        if not self._header_auth():
            self._unauthorized()
            return
        self._send_json(200, self.mcp.health_payload())

    def do_HEAD(self) -> None:
        path = self._route()
        if path in ("/mcp", "/health"):
            status = 405 if path == "/mcp" else 200
            try:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass
            return
        self._send_empty(404)

    def do_POST(self) -> None:
        path = self._route()
        if path != "/mcp":
            self._send_json(404, {"error": "not_found",
                                  "message": "未知路径：%s" % path},
                            extra={"Connection": "close"})
            return
        if not self._header_auth():
            self._unauthorized()
            return

        if self.headers.get("Transfer-Encoding"):
            self._send_json(400, {"error": "bad_request",
                                  "message": "不支持分块传输，请提供 "
                                             "Content-Length"},
                            extra={"Connection": "close"})
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._send_json(411, {"error": "length_required",
                                  "message": "缺少有效的 Content-Length"},
                            extra={"Connection": "close"})
            return
        if length < 0:
            self._send_json(400, {"error": "bad_request",
                                  "message": "Content-Length 无效"},
                            extra={"Connection": "close"})
            return
        if length > MAX_BODY:
            self._send_json(413, {"error": "payload_too_large",
                                  "message": "请求体超过 8MB 上限"},
                            extra={"Connection": "close"})
            return
        try:
            raw = self.rfile.read(length) if length else b""
        except Exception as exc:
            _dbg("读取请求体失败：%r" % (exc,))
            self._send_json(400, {"error": "bad_request",
                                  "message": "读取请求体失败"})
            return

        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            self._send_json(200,
                            _rpc_error(None, -32700,
                                       "服务器无法解析 JSON 请求体"))
            return

        session_id = self.headers.get(SESSION_HEADER)
        if session_id:
            text = session_id.strip()
            # 只接受本服务生成的 16 位十六进制会话 id，避免把任意客户端
            # 字符串回显到响应头（防头注入/伪造授权 key）
            if len(text) == 16 and all(ch in "0123456789abcdef"
                                       for ch in text):
                session_id = text
            else:
                session_id = None
        try:
            response, sid = self.mcp.handle(payload, session_id)
        except Exception as exc:
            _dbg("handle 异常：%r" % (exc,))
            response, sid = _rpc_error(None, -32603,
                                       "服务内部错误：%s"
                                       % type(exc).__name__), session_id

        extra: dict[str, str] = {}
        if sid:
            extra[SESSION_HEADER] = sid
        if response is None:
            self._send_empty(202, extra=extra)
        else:
            self._send_json(200, response, extra=extra)


# ---------------------------------------------------------------- 内部函数
