# -*- coding: utf-8 -*-
"""大文本分页只读视图：一次只把一页读进 Tk Text。

超过编辑上限的文本不再整块塞进 Text（Tk 的文本+undo 会成倍吃内存），
而是按页读取、上一页/下一页/跳转。只读，无高亮，内存占用跟文件大小无关。
"""
from __future__ import annotations

import os
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .. import core
from ..ui import scale

PAGE_BYTES = 256 * 1024


class PaginatedTextView(tk.Frame):
    def __init__(self, parent, path: str, codec: str = "utf-8", *,
                 encoding_name: str = "UTF-8", page_bytes: int = PAGE_BYTES,
                 on_status=None) -> None:
        super().__init__(parent, bg=core.C_BG)
        self.path = path
        self.codec = codec or "utf-8"
        self.encoding_name = encoding_name or self.codec
        self.page_bytes = max(64 * 1024, int(page_bytes))
        self.on_status = on_status
        self._size = os.path.getsize(path)
        self._page = 1
        self._offsets: list[int] = [0]
        self._offset = 0
        self._next_off = 0
        self._eof = False
        self._build()
        self._load(0)

    def _build(self):
        bar = tk.Frame(self, bg=core.C_BG)
        bar.pack(fill="x", pady=(0, 4))
        self.first_btn = self._btn(bar, "首页", self.first)
        self.first_btn.pack(side="left")
        self.prev_btn = self._btn(bar, "上一页", self.prev)
        self.prev_btn.pack(side="left", padx=(4, 0))
        self.next_btn = self._btn(bar, "下一页", self.next)
        self.next_btn.pack(side="left", padx=(4, 0))
        tk.Label(bar, text="跳到偏移", bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 8)).pack(side="left", padx=(14, 0))
        self.goto_var = tk.StringVar()
        ent = tk.Entry(bar, textvariable=self.goto_var, width=12,
                       bg=core.C_PANEL, fg=core.C_TEXT,
                       insertbackground=core.C_TEXT, bd=0, relief="flat",
                       font=(core.FONT, 9))
        ent.pack(side="left", padx=(5, 4), ipady=2)
        ent.bind("<Return>", lambda e: self.goto())
        self._btn(bar, "跳转", self.goto).pack(side="left")
        self.page_lab = tk.Label(bar, text="", bg=core.C_BG, fg=core.C_MUTED,
                                 font=(core.FONT, 8), anchor="w")
        self.page_lab.pack(side="left", padx=(12, 0))
        tk.Label(bar, text="只读 · 分页加载", bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 8)).pack(side="right")

        wrap = tk.Frame(self, bg=core.C_BG)
        wrap.pack(fill="both", expand=True)
        self.txt = tk.Text(
            wrap, bg=core.C_BG, fg=core.C_TEXT, bd=0, relief="flat",
            highlightthickness=0, wrap="none", undo=False,
            padx=scale.s(10), pady=scale.s(8),
            font=tkfont.Font(root=wrap, family="Consolas",
                             size=-scale.s(9)),
            insertbackground=core.C_TEXT)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt.yview,
                            style="VB.Vertical.TScrollbar")
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.txt.xview,
                            style="VB.Horizontal.TScrollbar")
        self.txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.configure(state="disabled")

    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=core.C_PANEL,
                         fg=core.C_TEXT, activebackground=core.C_HOVER,
                         activeforeground=core.C_TEXT, relief="flat", bd=0,
                         padx=10, pady=2, font=(core.FONT, 8),
                         cursor="hand2")

    # ---------------- 分页 ----------------

    def _load(self, offset: int):
        offset = max(0, min(self._size, int(offset)))
        try:
            with open(self.path, "rb") as f:
                f.seek(offset)
                data = f.read(self.page_bytes)
        except OSError as exc:
            text = "读取失败：%s" % exc
            data = b""
            self._eof = True
        else:
            eof = len(data) < self.page_bytes
            if not eof and not self.codec.lower().startswith(("utf-16", "utf-32")):
                cut = data.rfind(b"\n")
                if cut > len(data) // 2:
                    data = data[:cut + 1]
            self._eof = eof
            text = data.decode(self.codec, errors="replace")
        self._offset = offset
        self._next_off = offset + len(data)
        self._offsets[self._page - 1] = offset
        try:
            self.txt.configure(state="normal")
            self.txt.delete("1.0", "end")
            self.txt.insert("1.0", text)
            self.txt.configure(state="disabled")
            self.txt.yview_moveto(0)
        except tk.TclError:
            return
        self._update_buttons()
        if callable(self.on_status):
            try:
                self.on_status(self._page, offset, self._size)
            except Exception:
                pass

    def _update_buttons(self):
        try:
            self.prev_btn.configure(state="normal" if self._page > 1
                                    else "disabled")
            self.first_btn.configure(state="normal" if self._page > 1
                                     else "disabled")
            self.next_btn.configure(state="normal" if not self._eof
                                    else "disabled")
            self.page_lab.configure(
                text="第 %d 页 · 偏移 0x%X / 0x%X · %s"
                     % (self._page, self._offset, self._size,
                        self.encoding_name))
        except tk.TclError:
            pass

    def next(self):
        if self._eof:
            return
        self._page += 1
        self._offsets.append(self._next_off)
        self._load(self._next_off)

    def prev(self):
        if self._page <= 1:
            return
        self._page -= 1
        self._load(self._offsets[self._page - 1])

    def first(self):
        self._page = 1
        self._offsets = [0]
        self._load(0)

    def goto(self):
        text = self.goto_var.get().strip()
        if not text:
            return
        try:
            raw = text.lower()
            if raw.startswith("0x"):
                offset = int(raw[2:], 16)
            elif all(c in "0123456789abcdef" for c in raw):
                offset = int(raw, 16)
            else:
                offset = int(raw, 10)
        except ValueError:
            return
        self._page = 1
        self._offsets = [0]
        self._load(max(0, min(self._size, offset)))

    def current_page(self):
        return self._page, self._offset, self._size
