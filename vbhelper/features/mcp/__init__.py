"""MCP 工具总线：把本机能力开放给 AI 客户端。

入口：主面板 ☰「功能」→「MCP 工具总线」。打开是控制面板：
服务启停、工具开关、屏幕/鼠标键盘总闸、确认策略、急停、客户端配置复制、
最近调用审计。

安全默认：服务首次需手动启动（之后按 auto_start 自动起）；鼠标键盘总闸默认关，
开启后按确认策略逐次/会话授权；所有调用落本机 SQLite 审计。
"""
from __future__ import annotations

from ..base import Feature

class McpFeature(Feature):
    id = "mcp_bus"
    title = "MCP 工具总线"
    icon = "network"
    group = "agent"
    order = 20
    default_enabled = True

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        from .runtime import McpRuntime
        self.runtime = McpRuntime(ctx)

    def mount(self) -> None:
        """注册后按配置自动启动，并监听 stdio 桥的启动请求。

        stdio 桥没法直接操作已运行 GUI 的内部对象；它写
        ``%LOCALAPPDATA%\\VBHelper\\mcp\\start.request``，这里轮询到就启动服务。
        这样「VBHelper 挂在后台、服务是停的」时，AI 客户端也能自己拉起来。
        """
        self._req_seen = self._request_mtime()
        self._req_job = None
        self._schedule_watch(1200)
        cfg = self.runtime.cfg
        if not cfg.get("enabled"):
            return
        if not (cfg.get("server") or {}).get("auto_start", True):
            return
        try:
            self.ctx.root.after(700, self._auto_start)
        except Exception:
            pass

    def open(self) -> None:
        ctx = self.ctx
        win = ctx.windows.get(self.id)
        if win is not None:
            win.show()
            return win
        from .window import McpWindow
        return ctx.windows.open(
            self.id, lambda: McpWindow(ctx, self.runtime))

    def shutdown(self) -> None:
        job, self._req_job = getattr(self, "_req_job", None), None
        if job is not None:
            try:
                self.ctx.root.after_cancel(job)
            except Exception:
                pass
        try:
            self.ctx.windows.close(self.id)
        finally:
            try:
                self.runtime.shutdown()
            except Exception:
                pass

    # ---------------- stdio 桥的启动请求 ----------------

    def _request_mtime(self) -> float:
        try:
            import os
            from . import config
            return os.path.getmtime(
                config.start_request_path(self.ctx.paths))
        except Exception:
            return 0.0

    def _schedule_watch(self, delay: int = 1200) -> None:
        try:
            self._req_job = self.ctx.root.after(
                delay, self._watch_start_request)
        except Exception:
            self._req_job = None

    def _watch_start_request(self) -> None:
        self._req_job = None
        try:
            mtime = self._request_mtime()
            if mtime > getattr(self, "_req_seen", 0.0):
                self._req_seen = mtime
                from . import config
                config.clear_start_request(self.ctx.paths)
                if not self.runtime.cfg.get("enabled"):
                    self._log("忽略 MCP 启动请求：mcp.enabled=false")
                elif not (self.runtime.cfg.get("server") or {}).get(
                        "auto_start", True):
                    self._log("忽略 MCP 启动请求：auto_start=false")
                elif self.runtime.status().get("running"):
                    self._log("忽略 MCP 启动请求：服务已在运行")
                else:
                    self._log("收到 MCP 启动请求，启动服务…")
                    self.runtime.start(persist=False)
        except Exception as exc:
            self._log("处理 MCP 启动请求失败：%r" % (exc,))
        finally:
            if not getattr(self.runtime, "_closed", False):
                self._schedule_watch(1200)

    @staticmethod
    def _log(msg: str) -> None:
        try:
            from ...core import dbg_log
            dbg_log(str(msg))
        except Exception:
            pass

    def _auto_start(self) -> None:
        try:
            info = self.runtime.start(persist=False)
            msg = "MCP 服务已自动启动：127.0.0.1:%d" % info.get("port", 0)
        except Exception as exc:
            msg = "MCP 自动启动失败：%r" % (exc,)
        try:
            from ...core import dbg_log
            dbg_log(msg)
        except Exception:
            pass
