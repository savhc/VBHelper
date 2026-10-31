# -*- coding: utf-8 -*-
"""扁平文字页签（避免 ttk.Notebook 在 clam 下的 Win7 灰框页签）。

选中项用主色 2px 下划线，未选中 muted；主题切换时调 ``set_tokens``。
"""
from __future__ import annotations

import tkinter as tk

from . import scale


class TabBar(tk.Canvas):
    """文字页签。``command(index)`` 在点击时触发。"""

    def __init__(self, parent, items, tokens, *, command=None) -> None:
        self._items = [str(i) for i in items]
        self._command = command
        self._tokens = tokens
        self._active = 0
        self._hover = -1
        self._font = scale.font(12)
        self._pad = scale.s(13)
        self._gap = scale.s(6)
        self._h = scale.s(32)
        self._spans: list[tuple[int, int]] = []
        tk.Canvas.__init__(self, parent, height=self._h, bd=0,
                           highlightthickness=0)
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _e: self._set_hover(-1))
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    # -- 对外 ---------------------------------------------------------

    def set_tokens(self, tokens) -> None:
        self._tokens = tokens
        self._draw()

    def select(self, index: int, fire: bool = True) -> None:
        index = max(0, min(int(index), len(self._items) - 1))
        if index == self._active:
            return
        self._active = index
        self._draw()
        if fire and callable(self._command):
            try:
                self._command(index)
            except Exception:
                pass

    def active(self) -> int:
        return self._active

    # -- 绘制 ---------------------------------------------------------

    def _draw(self) -> None:
        t = self._tokens
        self.configure(bg=t.bg)
        self.delete("all")
        self._spans = []
        x = scale.s(2)
        mid = self._h // 2
        for i, text in enumerate(self._items):
            width = self._font.measure(text) + 2 * self._pad
            active = i == self._active
            color = t.text if active else (t.text if i == self._hover
                                           else t.muted)
            self.create_text(x + self._pad, mid, text=text, anchor="w",
                             font=self._font, fill=color)
            if active:
                self.create_rectangle(x + scale.s(6), self._h - scale.s(3),
                                      x + width - scale.s(6), self._h - 1,
                                      fill=t.accent, outline="")
            self._spans.append((x, x + width))
            x += width + self._gap
        self.create_line(0, self._h - 1, max(1, self.winfo_width()),
                         self._h - 1, fill=t.border)

    def _index_at(self, x: int) -> int:
        for i, (left, right) in enumerate(self._spans):
            if left <= x <= right:
                return i
        return -1

    def _click(self, event) -> None:
        idx = self._index_at(int(event.x))
        if idx >= 0:
            self.select(idx, fire=True)

    def _motion(self, event) -> None:
        self._set_hover(self._index_at(int(event.x)))

    def _set_hover(self, idx: int) -> None:
        if idx != self._hover:
            self._hover = idx
            self._draw()
