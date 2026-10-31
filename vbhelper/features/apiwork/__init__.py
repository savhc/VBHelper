"""API 工作台（cc-switch 形态）：API 站点管理 + 一键连通/余额。

入口：主面板 ☰「功能」→「API 工作台」。密钥 DPAPI 加密落盘、界面打码显示；
连通测试打 {base}/v1/models，余额走 one-api/new-api 通用协议；
支持把站点一键应用到 Claude Code（~/.claude/settings.json，自动备份）。
"""
from __future__ import annotations

from ..base import Feature

class ApiWorkbenchFeature(Feature):
    """本地 HTTP 调试台：不做脚本沙箱/团队同步，数据不出本机。"""

    id = "api_workbench"
    title = "API 工作台"
    icon = "zap"
    group = "dev"
    order = 11
    default_enabled = True

    def open(self) -> None:
        ctx = self.ctx
        win = ctx.windows.get(self.id)
        if win is not None:
            win.show()
            return win
        from .window import ApiWindow        # 懒加载：启动时不建 Tk 控件
        return ctx.windows.open(self.id, lambda: ApiWindow(ctx))

    def shutdown(self) -> None:
        try:
            self.ctx.windows.close(self.id)
        except Exception:
            pass
