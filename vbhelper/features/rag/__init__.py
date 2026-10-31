# -*- coding: utf-8 -*-
"""项目 RAG（本地索引 + 检索）。

入口：
- 主面板 ☰「功能与动作」→「项目 RAG / 建立索引」→ 选文件夹；
- 目录树右键文件夹 →「建立 RAG 索引…」（仅文件夹显示）。

窗口只负责选目录/进度/统计；扫描与分块在后台多线程完成，数据库写入
由索引线程串行批量提交，UI 不阻塞。索引完成后可用 MCP ``rag.search``
让 AI 只读相关文件与行号。
"""
from __future__ import annotations

import os

from ..base import Feature


class RagFeature(Feature):
    id = "rag"
    title = "项目 RAG"
    icon = "grid"
    group = "dev"
    order = 30
    default_enabled = True

    def open(self, path: str | None = None) -> None:
        """打开 RAG 窗口；``path`` 为空时弹目录选择框。"""
        ctx = self.ctx
        win = ctx.windows.get(self.id)
        if not path:
            if win is not None:
                win.show()
                return win
            path = self._pick_dir()
            if not path:
                return None
        path = os.path.abspath(os.path.normpath(os.path.expanduser(path)))
        if win is not None:
            sw = getattr(win, "switch_project", None)
            if callable(sw):
                sw(path)
            else:
                win.show()
            return win
        from .window import RagWindow
        win = ctx.windows.open(self.id, lambda: RagWindow(ctx, path))
        return win

    def shutdown(self) -> None:
        try:
            self.ctx.windows.close(self.id)
        except Exception:
            pass

    # ---------------- 目录选择 ----------------

    def last_dir(self) -> str:
        cfg = self.ctx.cfg or {}
        rag = cfg.get("rag") if isinstance(cfg.get("rag"), dict) else {}
        return str(rag.get("last_dir") or cfg.get("last_dir")
                   or os.path.expanduser("~"))

    def _pick_dir(self) -> str | None:
        from tkinter import filedialog
        ball = getattr(getattr(self.ctx, "app", None), "ball", None)
        try:
            ball.withdraw()
        except Exception:
            pass
        try:
            path = filedialog.askdirectory(
                parent=self.ctx.root, title="项目 RAG - 选择项目文件夹",
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
            self.ctx.cfg.setdefault("rag", {})["last_dir"] = path
            self.ctx.save_cfg()
        except Exception:
            pass
        return path
