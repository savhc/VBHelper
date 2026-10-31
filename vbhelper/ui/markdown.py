# -*- coding: utf-8 -*-
"""轻量 Markdown 渲染（Tk Text tag，不引入 markdown 库）。

覆盖常见语法：标题、段落、粗体/斜体、行内代码、代码块、列表、引用、
分隔线、链接/图片、表格（等宽字体 + 显示宽度补空格对齐）。
HTML 原样显示，保证不崩、不吞内容。
"""
from __future__ import annotations

import re
import tkinter as tk
import tkinter.font as tkfont
import unicodedata
from tkinter import ttk

from .. import core
from . import scale

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)\s*$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_MAX_CELL = 36

_INLINE_RE = re.compile(
    r"(\*\*(?P<bold>[^*]+?)\*\*"
    r"|(?<!\w)__(?P<bold2>[^_]+?)__(?!\w)"
    r"|`(?P<code>[^`]+)`"
    r"|!\[(?P<alt>[^\]]*)\]\((?P<imgsrc>[^)\s]+)(?:\s+[^)]*)?\)"
    r"|\[(?P<link>[^\]]+)\]\((?P<url>[^)\s]+)(?:\s+[^)]*)?\)"
    r"|\*(?P<ital>[^*]+?)\*"
    r"|(?<!\w)_(?P<ital2>[^_]+?)_(?!\w))"
)


def _split_row(line: str) -> list[str]:
    """把表格行切成单元格；``\\|`` 是单元格里的竖线。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT.split(text)]


def _is_table_sep(line: str) -> bool:
    text = line.strip().replace("|", "").strip()
    return bool(text) and text.count("-") >= 3 and set(text) <= set("-: ")


def _parse_table(lines: list[str], i: int):
    """从第 i 行起解析一个表格；不是合法表格返回 None。"""
    if i + 1 >= len(lines) or not _is_table_sep(lines[i + 1]):
        return None
    if not lines[i].lstrip().startswith("|"):
        return None
    rows = [_split_row(lines[i])]
    j = i + 2
    while j < len(lines) and lines[j].strip().startswith("|"):
        rows.append(_split_row(lines[j]))
        j += 1
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    return rows, j


def _disp_width(text: str) -> int:
    """等宽字体下的显示宽度：CJK/全角算 2 格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _cell_fmt(text: str, width: int) -> str:
    """截断到 width 个显示格并用空格补齐（等宽字体下可对齐）。"""
    text = (text or "").replace("\n", " ")
    if _disp_width(text) > width:
        out = ""
        used = 0
        for ch in text:
            step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if used + step > width - 1:
                break
            out += ch
            used += step
        return out + "…" + " " * max(0, width - used - 1)
    return text + " " * max(0, width - _disp_width(text))


def parse_blocks(text: str) -> list[dict]:
    """把 Markdown 切块；纯函数，方便测试。"""
    blocks: list[dict] = []
    lines = (text or "").splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _FENCE_RE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2) or ""
            i += 1
            body = []
            while i < n and not lines[i].strip().startswith(fence[:3]):
                body.append(lines[i])
                i += 1
            if i < n:
                i += 1
            blocks.append({"type": "code", "lang": lang,
                           "text": "\n".join(body)})
            continue
        m = _HEADING_RE.match(line)
        if m:
            blocks.append({"type": "heading", "level": len(m.group(1)),
                           "text": m.group(2).strip()})
            i += 1
            continue
        if _HR_RE.match(line):
            blocks.append({"type": "hr"})
            i += 1
            continue
        m = _QUOTE_RE.match(line)
        if m:
            parts = [m.group(1)]
            i += 1
            while i < n:
                mm = _QUOTE_RE.match(lines[i])
                if not mm:
                    break
                parts.append(mm.group(1))
                i += 1
            blocks.append({"type": "quote", "text": "\n".join(parts)})
            continue
        m = _LIST_RE.match(line)
        if m:
            blocks.append({"type": "list", "indent": len(m.group(1)),
                           "ordered": m.group(2)[0].isdigit(),
                           "marker": m.group(2), "text": m.group(3)})
            i += 1
            continue
        if line.lstrip().startswith("|"):
            table = _parse_table(lines, i)
            if table is not None:
                rows, i = table
                blocks.append({"type": "table", "rows": rows})
                continue
        parts = [line.strip()]
        i += 1
        while i < n:
            nxt = lines[i]
            if not nxt.strip():
                break
            if (_HEADING_RE.match(nxt) or _FENCE_RE.match(nxt)
                    or _HR_RE.match(nxt) or _QUOTE_RE.match(nxt)
                    or _LIST_RE.match(nxt)
                    or nxt.lstrip().startswith("|")):
                break
            parts.append(nxt.strip())
            i += 1
        blocks.append({"type": "paragraph", "text": " ".join(parts)})
    return blocks


def _configure_tags(txt: tk.Text):
    c = core
    for level, size in ((1, 20), (2, 17), (3, 15), (4, 13), (5, 12), (6, 11)):
        txt.tag_configure("h%d" % level, font=scale.font(size, "bold"),
                          foreground=c.C_TEXT, spacing1=scale.s(10),
                          spacing3=scale.s(4))
    txt.tag_configure("bold", font=scale.font(11, "bold"),
                      foreground=c.C_TEXT)
    txt.tag_configure("italic",
                      font=tkfont.Font(root=txt, family=c.FONT,
                                       size=-scale.s(11), slant="italic"),
                      foreground=c.C_TEXT)
    mono = tkfont.Font(root=txt, family="Consolas", size=-scale.s(9))
    txt.tag_configure("icode", font=mono, background=c.C_PANEL,
                      foreground="#d7ba7d")
    txt.tag_configure("codeblock", font=mono, background=c.C_PANEL,
                      foreground=c.C_TEXT, lmargin1=scale.s(12),
                      lmargin2=scale.s(12), spacing1=scale.s(3),
                      spacing3=scale.s(3))
    txt.tag_configure("quote", foreground=c.C_MUTED,
                      lmargin1=scale.s(18), lmargin2=scale.s(18))
    txt.tag_configure("hr", foreground=c.C_BORDER)
    txt.tag_configure("link", foreground=c.C_ACCENT, underline=True)
    table_font = tkfont.Font(root=txt, family="Consolas", size=-scale.s(10))
    txt.tag_configure("table", font=table_font, foreground=c.C_TEXT,
                      spacing1=scale.s(1))
    txt.tag_configure("table_head", font=tkfont.Font(
        root=txt, family="Consolas", size=-scale.s(10), weight="bold"),
        foreground=c.C_ACCENT)


def _insert_inline(txt: tk.Text, text: str, base: tuple[str, ...],
                   links: dict[str, str]):
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            txt.insert("end", text[pos:m.start()], base)
        if m.group("bold") or m.group("bold2"):
            txt.insert("end", m.group("bold") or m.group("bold2"),
                       base + ("bold",))
        elif m.group("code") is not None:
            txt.insert("end", m.group("code"), base + ("icode",))
        elif m.group("alt") is not None:
            alt = m.group("alt") or "图片"
            tag = "mdlink%d" % len(links)
            links[tag] = m.group("imgsrc") or ""
            txt.insert("end", "[图片] " + alt, base + ("link", tag))
        elif m.group("link") is not None:
            tag = "mdlink%d" % len(links)
            links[tag] = m.group("url") or ""
            txt.insert("end", m.group("link"), base + ("link", tag))
        elif m.group("ital") or m.group("ital2"):
            txt.insert("end", m.group("ital") or m.group("ital2"),
                       base + ("italic",))
        pos = m.end()
    if pos < len(text):
        txt.insert("end", text[pos:], base)


def _render_table(txt: tk.Text, rows: list[list[str]]) -> None:
    """等宽字体 + 显示宽度补齐，把表格画成对齐的文本行。"""
    cols = len(rows[0])
    widths = [0] * cols
    for row in rows:
        for c, cell in enumerate(row[:cols]):
            widths[c] = min(_MAX_CELL, max(widths[c], _disp_width(cell)))
    widths = [max(4, w) for w in widths]

    def line_of(row: list[str]) -> str:
        cells = [_cell_fmt(cell, widths[c]) for c, cell in enumerate(row[:cols])]
        return " │ ".join(cells).rstrip()

    lines = [line_of(row) for row in rows]
    # 分隔线按最长可见行算宽度：补位空格不算进去，避免线比表长
    widest = max(_disp_width(line) for line in lines) if lines else 0
    txt.insert("end", lines[0] + "\n", ("table", "table_head"))
    txt.insert("end", "─" * widest + "\n", ("table",))
    for line in lines[1:]:
        txt.insert("end", line + "\n", ("table",))


def render_markdown(txt: tk.Text, source: str, *, on_link=None) -> dict[str, str]:
    """把 Markdown 渲染进 Text，返回 {tag: url} 供点击打开链接。"""
    _configure_tags(txt)
    links: dict[str, str] = {}
    txt.configure(state="normal")
    txt.delete("1.0", "end")
    for block in parse_blocks(source):
        kind = block["type"]
        if kind == "heading":
            txt.insert("end", block["text"] + "\n",
                       ("h%d" % block["level"],))
        elif kind == "paragraph":
            _insert_inline(txt, block["text"], (), links)
            txt.insert("end", "\n\n")
        elif kind == "code":
            if block["text"]:
                txt.insert("end", block["text"] + "\n", ("codeblock",))
            txt.insert("end", "\n")
        elif kind == "list":
            indent = scale.s(14 + min(block["indent"] // 2, 6) * 12)
            marker = ("•" if not block["ordered"] else block["marker"])
            tag = "li%d" % min(block["indent"] // 2, 6)
            if tag not in txt.tag_names():
                txt.tag_configure(tag, lmargin1=indent, lmargin2=indent)
            txt.insert("end", marker + " ", (tag,))
            _insert_inline(txt, block["text"], (tag,), links)
            txt.insert("end", "\n")
        elif kind == "quote":
            for line in block["text"].splitlines():
                txt.insert("end", "│ " + line + "\n", ("quote",))
            txt.insert("end", "\n")
        elif kind == "table":
            rows = block.get("rows") or []
            if rows:
                _render_table(txt, rows)
                txt.insert("end", "\n")
        elif kind == "hr":
            txt.insert("end", "─" * 60 + "\n\n", ("hr",))
    handler = on_link or _safe_open
    for tag, url in links.items():
        txt.tag_configure(tag, foreground=core.C_ACCENT, underline=True)
        txt.tag_bind(tag, "<Button-1>",
                     lambda e, u=url, h=handler: h(u))
    txt.configure(state="disabled")
    return links


def _safe_open(url: str) -> str:
    """只放行 http/https/mailto；本地路径不让 Markdown 直接拉起程序。"""
    low = (url or "").strip().lower()
    if low.startswith(("http://", "https://", "mailto:")):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    return "break"


class MarkdownView(tk.Frame):
    """只读 Markdown 预览（滚轮/选择由 Text 自带）。"""

    def __init__(self, parent, source: str, *, on_link=None) -> None:
        super().__init__(parent, bg=core.C_BG)
        self._on_link = on_link or _safe_open
        wrap = tk.Frame(self, bg=core.C_BG)
        wrap.pack(fill="both", expand=True)
        # height/width=1：让 pack 决定实际大小，否则 Text 的 24 行请求高度
        # 会把编辑器窗口撑到 900px 高（150% DPI 下尤其明显）
        self.txt = tk.Text(
            wrap, bg=core.C_BG, fg=core.C_TEXT, bd=0, relief="flat",
            highlightthickness=0, wrap="word", padx=scale.s(16),
            pady=scale.s(12), spacing1=scale.s(2), spacing3=scale.s(2),
            font=scale.font(11), cursor="arrow", takefocus=0,
            height=1, width=1,
        )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt.yview,
                            style="VB.Vertical.TScrollbar")
        self.txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.links = render_markdown(self.txt, source, on_link=self._on_link)
        self.txt.bind("<Button-1>", self._click)
        self.txt.bind("<MouseWheel>", self._wheel)

    def _click(self, event):
        idx = self.txt.index("@%d,%d" % (event.x, event.y))
        for tag in self.txt.tag_names(idx):
            url = self.links.get(tag)
            if url:
                try:
                    self._on_link(url)
                except Exception:
                    pass
                return "break"
        return None

    def _wheel(self, event):
        step = int(-event.delta / 120) * 3
        if step == 0:
            step = -3 if event.delta > 0 else 3
        self.txt.yview_scroll(step, "units")
        return "break"
