"""依赖医生：项目清单体检 + 报告。

入口：主面板 ☰「功能」→「依赖医生」。点开先让用户选项目文件夹
（记住上次目录），再按扫描索引 -> 检测结果 -> 报告 三步呈现。

首次使用无需配置；网络不可用时自动降级为本地解析（版本标未知）。
"""
from __future__ import annotations

import os

from ..base import Feature

class DepsFeature(Feature):
    """依赖体检：只读扫描，不修改项目文件，也不执行 pip/npm。"""

    id = "dependency_doctor"
    title = "依赖医生"
    icon = "package"
    group = "dev"
    order = 10
    default_enabled = True

    # ---------------- 生命周期 ----------------

    def open(self) -> None:
        """打开窗口：先选文件夹（已开着就直接前置）。"""
        ctx = self.ctx
        win = ctx.windows.get(self.id)
        if win is not None:
            win.show()
            return win
        path = self._pick_dir()
        if not path:
            return None
        from .window import DepsWindow      # 懒加载：启动时不建 Tk 控件
        win = ctx.windows.open(self.id, lambda: DepsWindow(ctx, path))
        starter = getattr(win, "start_scan", None)
        if callable(starter):
            starter()
        return win

    def shutdown(self) -> None:
        try:
            self.ctx.windows.close(self.id)
        except Exception:
            pass

    # ---------------- 目录选择 ----------------

    def last_dir(self) -> str:
        """上次扫描的项目目录，缺省回落到配置里的 last_dir / 用户主目录。"""
        cfg = self.ctx.cfg or {}
        deps = cfg.get("deps")
        if isinstance(deps, dict) and deps.get("last_dir"):
            return str(deps["last_dir"])
        return str(cfg.get("last_dir") or os.path.expanduser("~"))

    def _pick_dir(self) -> str | None:
        """弹目录选择框；文件对话框期间收起置顶悬浮球，免得挡脸。"""
        from tkinter import filedialog
        ball = getattr(getattr(self.ctx, "app", None), "ball", None)
        try:
            ball.withdraw()
        except Exception:
            pass
        try:
            path = filedialog.askdirectory(
                parent=self.ctx.root, title="依赖医生 - 选择项目文件夹",
                initialdir=self.last_dir())
        finally:
            try:
                ball.deiconify()
                ball.lift()
            except Exception:
                pass
        if not path:
            return None
        try:
            self.ctx.cfg.setdefault("deps", {})["last_dir"] = path
            self.ctx.save_cfg()
        except Exception:
            pass
        return path
