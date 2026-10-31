# -*- coding: utf-8 -*-
"""十六进制查看器（只读，010 风格：偏移 / 16 字节 / ASCII）。

大文件不逐行建控件：Canvas 只画视口内的行，滚动条按总行数虚拟化。
"""
from __future__ import annotations

import string
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .. import core

_HEX_DIGITS = set(string.hexdigits)
_PER_ROW = 16


def format_hex_row(data: bytes, offset: int, per_row: int = _PER_ROW):
    """返回 (偏移文本, 十六进制列, ASCII 列)。"""
    chunk = data[offset:offset + per_row]
    parts = ["%02X" % b for b in chunk]
    parts += ["  "] * (per_row - len(parts))
    hex_text = " ".join(parts[:8]) + "  " + " ".join(parts[8:])
    ascii_text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    ascii_text += " " * (per_row - len(chunk))
    return "%08X" % offset, hex_text, ascii_text


def parse_hex_pattern(text: str) -> bytes:
    """搜索框内容 -> 字节：纯十六进制（可带空格/0x）按字节解析，否则当文本。"""
    s = (text or "").strip()
    if not s:
        return b""
    compact = s.replace("0x", "").replace("0X", "").replace(" ", "")
    if (len(compact) >= 2 and len(compact) % 2 == 0
            and all(c in _HEX_DIGITS for c in compact)):
        try:
            return bytes.fromhex(compact)
        except ValueError:
            pass
    return s.encode("utf-8", "replace")


class HexView(tk.Frame):
    """只读 Hex 视图。

    ``on_status(offset, total)``：点击/跳转时通知编辑器更新状态栏。
    """

    def __init__(self, parent, data: bytes, *, font=None,
                 on_status=None, per_row: int = _PER_ROW) -> None:
        super().__init__(parent, bg=core.C_BG)
        self.data = data or b""
        self.per_row = per_row
        self.on_status = on_status
        self.font = font or tkfont.Font(
            root=self.winfo_toplevel(), family="Consolas", size=10)
        self.row_h = max(16, self.font.metrics("linespace") + 4)
        self.char_w = max(6, self.font.measure("0"))
        self.total_rows = (len(self.data) + per_row - 1) // per_row
        self.off_digits = 8 if len(self.data) < 0x100000000 else 10
        self._sel_row = -1
        self._hits: list[int] = []
        self._hit_rows: set[int] = set()
        self._hit_i = -1
        self._last_pattern = b""

        self._colors = {
            "bg": core.C_BG, "panel": core.C_PANEL, "text": core.C_TEXT,
            "muted": core.C_MUTED, "border": core.C_BORDER,
            "accent": core.C_ACCENT, "hover": core.C_HOVER,
            "select": core.C_SELECT,
        }
        self._build_toolbar()
        self._build_canvas()
        self._update_scrollregion()
        self.after_idle(self._redraw)

    # ---------------- 布局 ----------------

    def _mk_button(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd,
                         bg=core.C_PANEL, fg=core.C_TEXT,
                         activebackground=core.C_HOVER,
                         activeforeground=core.C_TEXT, relief="flat", bd=0,
                         padx=8, pady=2, font=(core.FONT, 8), cursor="hand2")

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=core.C_BG)
        bar.pack(fill="x", pady=(0, 4))
        tk.Label(bar, text="偏移", bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 8)).pack(side="left")
        self.goto_var = tk.StringVar()
        self.goto_entry = tk.Entry(bar, textvariable=self.goto_var, width=12,
                                   bg=core.C_PANEL, fg=core.C_TEXT,
                                   insertbackground=core.C_TEXT, bd=0,
                                   relief="flat", font=(core.FONT, 9))
        self.goto_entry.pack(side="left", padx=(5, 4), ipady=2)
        self.goto_entry.bind("<Return>", lambda e: self._do_goto())
        self._mk_button(bar, "跳转", self._do_goto).pack(side="left")

        tk.Label(bar, text="搜索", bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 8)).pack(side="left", padx=(16, 0))
        self.find_var = tk.StringVar()
        self.find_entry = tk.Entry(bar, textvariable=self.find_var, width=20,
                                   bg=core.C_PANEL, fg=core.C_TEXT,
                                   insertbackground=core.C_TEXT, bd=0,
                                   relief="flat", font=(core.FONT, 9))
        self.find_entry.pack(side="left", padx=(5, 4), ipady=2)
        self.find_entry.bind("<Return>", lambda e: self.find_next())
        self._mk_button(bar, "查找", self.find_next).pack(side="left")
        self._mk_button(bar, "下一个", self.find_next).pack(side="left", padx=(4, 0))
        self.hit_lab = tk.Label(bar, text="", bg=core.C_BG, fg=core.C_MUTED,
                                font=(core.FONT, 8))
        self.hit_lab.pack(side="left", padx=(8, 0))
        self.size_lab = tk.Label(bar, text=core.human_size(len(self.data)),
                                 bg=core.C_BG, fg=core.C_MUTED,
                                 font=(core.FONT, 8))
        self.size_lab.pack(side="right")

    def _build_canvas(self):
        wrap = tk.Frame(self, bg=core.C_BG)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=core.C_BG, highlightthickness=0, bd=0,
                                yscrollincrement=self.row_h)
        self.vsb = ttk.Scrollbar(wrap, orient="vertical",
                                 command=self.canvas.yview,
                                 style="VB.Vertical.TScrollbar")
        self.hsb = ttk.Scrollbar(wrap, orient="horizontal",
                                 command=self.canvas.xview,
                                 style="VB.Horizontal.TScrollbar")
        self.canvas.configure(yscrollcommand=self._on_scroll,
                              xscrollcommand=self.hsb.set)
        self.vsb.pack(side="right", fill="y")
        self.hsb.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_wheel)

    def _update_scrollregion(self):
        hex_x = 8 + self.char_w * (self.off_digits + 2)
        ascii_x = hex_x + self.char_w * (self.per_row * 3 + 2)
        width = ascii_x + self.char_w * (self.per_row + 4)
        height = max(self.row_h, self.total_rows * self.row_h)
        self._x_off = 8
        self._x_hex = hex_x
        self._x_ascii = ascii_x
        self.canvas.configure(scrollregion=(0, 0, width, height))

    # ---------------- 绘制 ----------------

    def _redraw(self):
        try:
            self.canvas.delete("row")
        except tk.TclError:
            return
        if not self.data:
            self.canvas.create_text(12, 12, anchor="nw", text="空文件",
                                    fill=core.C_MUTED, font=(core.FONT, 9),
                                    tags="row")
            return
        try:
            top = max(0, int(self.canvas.canvasy(0) // self.row_h))
            height = max(1, self.canvas.winfo_height())
        except tk.TclError:
            return
        first = max(0, top - 1)
        last = min(self.total_rows, top + height // self.row_h + 3)
        for row in range(first, last):
            y = row * self.row_h + 2
            if row % 2:
                self.canvas.create_rectangle(
                    0, y - 1, 4000, y + self.row_h - 3, fill=core.C_PANEL,
                    outline="", tags="row")
            if row in self._hit_rows:
                fill = core.C_SELECT if row == self._hits[self._hit_i] // self.per_row \
                    else core.C_HOVER
                self.canvas.create_rectangle(
                    0, y - 1, 4000, y + self.row_h - 3, fill=fill,
                    outline="", tags="row")
            if row == self._sel_row:
                self.canvas.create_rectangle(
                    0, y - 1, 4000, y + self.row_h - 3,
                    fill=core.C_SELECT, outline=core.C_ACCENT, tags="row")
            off, hex_text, ascii_text = format_hex_row(
                self.data, row * self.per_row, self.per_row)
            self.canvas.create_text(self._x_off, y, anchor="nw", text=off,
                                    fill=core.C_MUTED, font=self.font,
                                    tags="row")
            self.canvas.create_text(self._x_hex, y, anchor="nw", text=hex_text,
                                    fill=core.C_TEXT, font=self.font,
                                    tags="row")
            self.canvas.create_text(self._x_ascii, y, anchor="nw",
                                    text=ascii_text, fill=core.C_TEXT,
                                    font=self.font, tags="row")

    def _on_scroll(self, first, last):
        self.vsb.set(first, last)
        self._redraw()

    def _on_wheel(self, event):
        step = int(-event.delta / 120) * 3
        if step == 0:
            step = -3 if event.delta > 0 else 3
        self.canvas.yview_scroll(step, "units")
        return "break"

    def _on_shift_wheel(self, event):
        step = -1 if event.delta > 0 else 1
        self.canvas.xview_scroll(step, "units")
        return "break"

    def _on_click(self, event):
        if not self.data:
            return
        row = int(self.canvas.canvasy(event.y) // self.row_h)
        row = max(0, min(self.total_rows - 1, row))
        self._sel_row = row
        self._notify(row * self.per_row)
        self._redraw()

    def _notify(self, offset: int):
        if callable(self.on_status):
            try:
                self.on_status(offset, len(self.data))
            except Exception:
                pass

    # ---------------- 跳转 / 搜索 ----------------

    def _do_goto(self):
        text = self.goto_var.get().strip()
        if not text:
            return "break"
        try:
            raw = text.lower()
            if raw.startswith("0x"):
                offset = int(raw[2:], 16)
            elif all(c in _HEX_DIGITS for c in raw):
                offset = int(raw, 16)       # 与偏移列一致：默认按十六进制
            else:
                offset = int(raw, 10)
        except ValueError:
            self.hit_lab.configure(text="偏移格式不对")
            return "break"
        self.goto(offset)
        return "break"

    def goto(self, offset: int):
        if not self.data:
            return
        offset = max(0, min(len(self.data) - 1, int(offset)))
        row = offset // self.per_row
        self._sel_row = row
        total = max(1, self.total_rows)
        self.canvas.yview_moveto(row / float(total))
        self._notify(offset)
        self._redraw()

    def find_next(self):
        """按搜索框内容查找；返回命中数（0 表示没找到）。"""
        pattern = parse_hex_pattern(self.find_var.get())
        if not pattern:
            self._hits = []
            self._hit_rows = set()
            self._hit_i = -1
            self._last_pattern = b""
            self.hit_lab.configure(text="")
            self._redraw()
            return 0
        if pattern != self._last_pattern:
            self._hit_i = -1
            self._last_pattern = pattern
        hits = []
        start = 0
        cap = 5000
        while len(hits) < cap:
            idx = self.data.find(pattern, start)
            if idx < 0:
                break
            hits.append(idx)
            start = idx + 1
        self._hits = hits
        self._hit_rows = {h // self.per_row for h in hits}
        if not hits:
            self._hit_i = -1
            self.hit_lab.configure(text="未找到")
            self._redraw()
            return 0
        self._hit_i = (self._hit_i + 1) % len(hits) if self._hit_i >= 0 else 0
        self.hit_lab.configure(text="%d/%d 处" % (self._hit_i + 1, len(hits)))
        self.goto(hits[self._hit_i])
        return len(hits)
