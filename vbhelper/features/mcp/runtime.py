"""MCP 运行时：把存储、服务、工具注册表、HTTP 服务端组装成一个开关。

- 懒加载：功能未启用/面板未打开时不 import PIL/HTMLParser/ctypes 服务；
- 所有工具在 GUI 进程里执行，确认弹窗通过 ui/mainthread 回主线程；
- endpoint.json 只在服务运行期间存在，stdio 桥据此连接。
"""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

from ... import __version__
from . import config

class RuntimeError_(RuntimeError):
    """MCP 运行时错误（配置/依赖不可用）。"""

class McpRuntime:
    """MCP 总线生命周期。面板与 Feature 只通过这个类操作服务。"""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.cfg = config.get(ctx)
        self._lock = threading.RLock()
        self._services = None
        self._registry = None
        self._server = None
        self._started_at = 0.0
        self._last_error = ""
        self._closed = False

    # ---------------- 组装 ----------------

    def _ensure_services(self):
        """懒加载服务层与工具注册表；失败抛 RuntimeError_。"""
        if self._services is not None:
            return self._services
        if self.ctx.db is None or self.ctx.secrets is None:
            raise RuntimeError_("存储底座不可用（数据库或密钥库初始化失败）")
        try:
            from ...services.web.search import SearchService
            from ...services.win.clipboard import ClipboardService
            from ...services.win.input import InputService
            from ...services.win.screen import ScreenService
            from .tools import build_registry
        except Exception as exc:      # 缺模块/依赖时给出可读信息
            raise RuntimeError_("MCP 服务模块加载失败：%r" % (exc,)) from exc
        cfg = self.cfg
        search_cfg = cfg.get("search") or {}
        key_ref = str(search_cfg.get("api_key_ref") or "")
        api_key = ""
        if key_ref:
            try:
                api_key = self.ctx.secrets.get(key_ref)
            except Exception:
                api_key = ""
        screen = ScreenService(
            self.ctx.paths.captures_dir,
            max_width=int((cfg.get("screen") or {}).get("max_width") or 1600))
        clipboard = ClipboardService()
        input_svc = InputService(cfg, clipboard=clipboard)
        search = SearchService(
            self.ctx.http,
            provider=str(search_cfg.get("provider") or "auto"),
            api_key=api_key,
            searxng_url=str(search_cfg.get("searxng_url") or ""),
            allow_private=bool((cfg.get("web") or {}).get("allow_private")),
            timeout=float(search_cfg.get("timeout") or 20))
        services = SimpleNamespace(
            screen=screen, input=input_svc, search=search,
            clipboard=clipboard, http=self.ctx.http)
        registry = build_registry(services)   # 失败时不要留下半初始化状态
        self._services = services
        self._registry = registry
        return services

    def _make_server(self, token: str):
        from .server import McpServer
        server_cfg = self.cfg.get("server") or {}
        return McpServer(
            self.ctx, self._registry, self._services,
            host=str(server_cfg.get("bind") or "127.0.0.1"),
            port=int(server_cfg.get("port") or 0),
            token=token,
            max_sessions=int(server_cfg.get("max_sessions") or 8),
            confirm=self.confirm,
            audit=self.ctx.audit,
            cfg=self.cfg,
            notify=self.notify)

    # ---------------- 生命周期 ----------------

    def start(self, *, persist: bool = True) -> dict:
        """启动 HTTP 服务；返回端点信息。已运行则直接返回。"""
        with self._lock:
            if self._closed:
                raise RuntimeError_("MCP 运行时已关闭")
            if self._server is not None and self._server.running:
                return self.endpoint_info()
            services = self._ensure_services()
            try:
                services.input.start_watch(self._on_panic)
            except Exception:
                pass
            token = config.ensure_token(self.ctx.secrets)
            server = self._make_server(token)
            port = server.start()
            self._server = server
            self._started_at = time.time()
            self._last_error = ""
            self.cfg["enabled"] = True
            if persist:
                try:
                    config.save(self.ctx)
                except Exception:
                    pass
            info = self.endpoint_info()
            self._write_endpoint(info)
            try:
                self.ctx.audit.log("mcp", "server.start",
                                   {"port": port}, ok=True)
            except Exception:
                pass
            return info

    def stop(self, *, persist: bool = True) -> None:
        """停止服务并删 endpoint；幂等。"""
        with self._lock:
            server, self._server = self._server, None
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    pass
            svc = self._services
            if svc is not None:
                try:
                    svc.input.stop_watch()   # 服务停了就不再轮询急停热键
                except Exception:
                    pass
            self._clear_endpoint()
            try:
                self.ctx.audit.log("mcp", "server.stop", {}, ok=True)
            except Exception:
                pass
            if persist:
                # 「停止」只停本轮监听；是否允许客户端/开机自动启动由
                # server.auto_start 单独控制（面板的自动启动开关）。
                try:
                    config.save(self.ctx)
                except Exception:
                    pass

    def restart(self) -> dict:
        self.stop()
        return self.start()

    def shutdown(self) -> None:
        """应用退出：停服务、停急停轮询。幂等。"""
        if self._closed:
            return
        self.stop(persist=False)
        svc = self._services
        if svc is not None:
            try:
                svc.input.stop_watch()
            except Exception:
                pass
        self._closed = True

    # ---------------- 状态 ----------------

    def status(self) -> dict:
        server = self._server
        st = {}
        if server is not None:
            try:
                st = server.status()
            except Exception as exc:
                st = {"running": False, "error": repr(exc)}
        cfg = self.cfg
        return {
            "running": bool(st.get("running")),
            "port": int(st.get("port") or 0),
            "url": st.get("url") or "",
            "sessions": int(st.get("sessions") or 0),
            "calls": int(st.get("calls") or 0),
            "started_at": self._started_at,
            "errors": int(st.get("errors") or 0),
            "enabled": bool(cfg.get("enabled")),
            "token_masked": self._masked_token(),
            "input_enabled": bool((cfg.get("input") or {}).get("enabled")),
            "screen_allow": bool((cfg.get("screen") or {}).get("allow")),
            "confirm_mode": str((cfg.get("confirm") or {}).get("mode") or "ask"),
            "panicked": self._panicked(),
            "last_error": self._last_error,
            "version": __version__,
        }

    def registry(self):
        """工具注册表（面板列工具用）；懒加载服务。"""
        self._ensure_services()
        return self._registry

    def endpoint_info(self) -> dict:
        server = self._server
        port = int(getattr(server, "port", 0) or 0)
        return {
            "version": 1,
            "host": "127.0.0.1",
            "port": port,
            "url": "http://127.0.0.1:%d/mcp" % port if port else "",
            "token": getattr(server, "token", "") if server else "",
            "pid": os.getpid(),
            "started": self._started_at,
            "app_version": __version__,
        }

    def recent_audit(self, limit: int = 100) -> list:
        try:
            rows = self.ctx.audit.recent(limit, scope="mcp")
        except Exception:
            return []
        return [dict(r) if not isinstance(r, dict) else r for r in rows]

    # ---------------- 配置操作（面板调用） ----------------

    def set_enabled(self, on: bool) -> None:
        self.cfg["enabled"] = bool(on)
        self._save()

    def set_tool_enabled(self, name: str, on: bool) -> None:
        tools = self.cfg.setdefault("tools", {})
        tools.setdefault(name, {})["enabled"] = bool(on)
        self._save()

    def set_auto_start(self, on: bool) -> None:
        """允许 AI 客户端/开机后自动启动服务（不影响当前监听）。"""
        self.cfg.setdefault("server", {})["auto_start"] = bool(on)
        self._save()

    def set_screen_allow(self, on: bool) -> None:
        self.cfg.setdefault("screen", {})["allow"] = bool(on)
        self._save()

    def set_input_enabled(self, on: bool) -> None:
        self.cfg.setdefault("input", {})["enabled"] = bool(on)
        svc = self._services
        if svc is not None:
            try:
                if on:
                    svc.input.start_watch(self._on_panic)
                else:
                    svc.input.stop_watch()
            except Exception:
                pass
        self._save()

    def set_confirm_mode(self, mode: str) -> None:
        if mode not in ("ask", "session", "never"):
            mode = "ask"
        self.cfg.setdefault("confirm", {})["mode"] = mode
        self._save()

    def rotate_token(self) -> str:
        """换新令牌；服务运行中则重启让它生效。"""
        token = config.rotate_token(self.ctx.secrets)
        if self._server is not None and self._server.running:
            self.restart()
        return token

    def panic(self) -> None:
        """面板/热键急停：禁用输入 60 秒。"""
        svc = self._services
        if svc is None:
            return
        try:
            svc.input.panic()
        except Exception:
            pass
        self._on_panic()

    def _on_panic(self) -> None:
        try:
            self.ctx.audit.log("mcp", "input.panic", {}, ok=True)
        except Exception:
            pass
        try:
            self.ctx.toast("已急停：AI 鼠标键盘操作暂停 60 秒")
        except Exception:
            pass

    # ---------------- 确认 / 通知（服务线程调用） ----------------

    def confirm(self, title: str, summary: str) -> bool:
        """服务线程 -> 主线程确认弹窗。超时/异常一律拒绝。"""
        from .confirm import ask_confirm
        timeout = float((self.cfg.get("confirm") or {}).get("timeout") or 30)
        mode = str((self.cfg.get("confirm") or {}).get("mode") or "ask")
        try:
            return bool(self.ctx.post(
                lambda: ask_confirm(self.ctx, title, summary,
                                    session=(mode == "session")),
                timeout + 5))
        except Exception as exc:
            self._last_error = "确认弹窗失败：%r" % (exc,)
            try:
                self.ctx.audit.log("mcp", "confirm.error",
                                   self._last_error, ok=False)
            except Exception:
                pass
            return False

    def notify(self, title: str, message: str) -> bool:
        """桌面通知：投递到 Tk 主线程，返回是否入队（不阻塞工具调用）。"""
        text = "%s：%s" % (title, message) if title else str(message)
        try:
            ok = bool(self.ctx.toast(text[:180], duration=5000))
        except Exception as exc:
            ok = False
            self._last_error = "通知失败：%r" % (exc,)
        if not ok:
            self._last_error = self._last_error or "通知未送达（主界面已关闭？）"
            try:
                self.ctx.audit.log("mcp", "notify.failed",
                                   self._last_error, ok=False)
            except Exception:
                pass
        return ok

    # ---------------- 内部 ----------------

    def _masked_token(self) -> str:
        try:
            from ...store.secrets import mask_key
            token = config.ensure_token(self.ctx.secrets)
            return mask_key(token)
        except Exception:
            return "未配置"

    def _panicked(self) -> bool:
        svc = self._services
        try:
            return bool(getattr(svc.input, "panicked", False)) if svc else False
        except Exception:
            return False

    def _save(self) -> None:
        try:
            config.save(self.ctx)
        except Exception as exc:
            self._last_error = "保存配置失败：%r" % (exc,)

    def _write_endpoint(self, info: dict) -> None:
        try:
            config.write_endpoint(self.ctx.paths, info)
        except Exception as exc:
            self._last_error = "写入 endpoint 失败：%r" % (exc,)
            try:
                self.ctx.audit.log("mcp", "endpoint.write",
                                   self._last_error, ok=False)
            except Exception:
                pass

    def _clear_endpoint(self) -> None:
        try:
            config.clear_endpoint(self.ctx.paths)
        except Exception:
            pass
