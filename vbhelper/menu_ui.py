"""可搜索的功能面板（替换纵向堆叠的旧菜单）。

- 分组 + 图标 + 一行说明；输入即筛，↑↓ 选择，Enter 打开，Esc 关闭；
- 复用 floating._popup 的窗口壳（深色/置顶/跟随面板），内容区自己画；
- 项数增长时只滚动/筛选，不再把窗口撑出屏幕。

"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Any, Callable

from .ui import icons, render, scale, widgets
from .ui.scroll import bind_canvas_wheel
from .ui.theme import build_preset
from .ui.window import (apply_dark_mode, enable_round_corners,
                        set_caption_colors)

@dataclass
class MenuItem:
    """一条功能入口。"""

    title: str
    desc: str
    command: Callable[[], Any]
    group: str = "其它"
    icon: str = "file"
    keywords: str = ""

    def haystack(self) -> str:
        return ("%s %s %s %s" % (self.title, self.desc, self.group,
                                 self.keywords)).casefold()

def build(app, win: tk.Toplevel, items: list[MenuItem]) -> tk.Toplevel:
    """把功能面板画进 ``win``（由 floating._popup 创建）。"""
    palette = _Palette(app, win, items)
    setattr(win, "_menu_palette", palette)     # 测试/诊断用，不影响业务
    return win

class _Palette:
    def __init__(self, app, win: tk.Toplevel, items: list[MenuItem]) -> None:
        self.app = app
        self.win = win
        self.items = list(items)
        try:
            self.t = app.theme.current()[1]
        except Exception:
            self.t = build_preset("midnight").tokens
        self._rows: list[dict] = []
        self._visible: list[MenuItem] = []
        self._selected = -1
        self._query = ""
        self._icon_cache: dict[tuple[str, str], Any] = {}
        self._build()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    def _build(self) -> None:
        t = self.t
        win = self.win
        win.configure(bg=t.bg)
        pad = scale.s(18)
        w, h = scale.window_size(880, 650)
        try:
            w, h = self.app._fit_work_area(w, h)
        except Exception:
            pass
        win.geometry("%dx%d" % (w, h))
        win.minsize(scale.s(620), scale.s(420))
        win.resizable(True, True)

        head = tk.Frame(win, bg=t.bg)
        head.pack(fill="x", padx=pad, pady=(pad, scale.s(2)))
        left = tk.Frame(head, bg=t.bg)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="功能与动作", bg=t.bg, fg=t.text,
                 font=scale.font(16, weight="bold"), anchor="w").pack(fill="x")
        tk.Label(left, text="输入筛选 · ↑↓ 选择 · Enter 打开 · Esc 关闭",
                 bg=t.bg, fg=t.muted, font=scale.font(10),
                 anchor="w").pack(fill="x", pady=(scale.s(2), 0))
        self._count = tk.Label(head, text="", bg=t.bg, fg=t.dim,
                               font=scale.font(10), anchor="e")
        self._count.pack(side="right", padx=(scale.s(8), 0))
        widgets.IconButton(head, "gear", self._open_settings,
                           tooltip="设置", size=30).pack(
            side="right", padx=(scale.s(4), 0))

        self.search = widgets.SearchEntry(
            win, "搜索功能、工具或设置…", self._on_query)
        self.search.pack(fill="x", padx=pad, pady=(scale.s(8), 0))

        body = tk.Frame(win, bg=t.bg)
        body.pack(fill="both", expand=True, padx=pad,
                  pady=(scale.s(8), pad))
        self._canvas = tk.Canvas(body, bg=t.bg, highlightthickness=0, bd=0)
        self._bar = ttk.Scrollbar(body, orient="vertical",
                                  command=self._canvas.yview)
        self._inner = tk.Frame(self._canvas, bg=t.bg)
        self._win_item = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda _e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(
            self._win_item, width=max(1, e.width)))
        self._attach_bar()
        self._canvas.pack(side="left", fill="both", expand=True)
        self._bar.pack(side="right", fill="y")
        bind_canvas_wheel(win, self._canvas, area=body, units=2)

        win.bind("<Down>", lambda _e: self._move(1))
        win.bind("<Up>", lambda _e: self._move(-1))
        win.bind("<Prior>", lambda _e: self._move(-6))
        win.bind("<Next>", lambda _e: self._move(6))
        win.bind("<Return>", lambda _e: self._invoke_selected())
        win.bind("<KP_Enter>", lambda _e: self._invoke_selected())
        win.protocol("WM_DELETE_WINDOW", self._close)

        try:
            hwnd = int(win.winfo_id())
            apply_dark_mode(hwnd, True)
            enable_round_corners(hwnd)
            set_caption_colors(hwnd, t.bg, t.text)
        except Exception:
            pass

        self._render()
        self.win.after(30, self._focus_search)

    def _focus_search(self) -> None:
        """菜单弹出后直接把键盘交给搜索框（否则输入会落到原窗口）。"""
        try:
            self.win.focus_force()
        except Exception:
            pass
        try:
            self.search.focus()
        except Exception:
            pass

    def _attach_bar(self) -> None:
        """ttk 禁用态会露白条：内容不满一屏时把滚动条藏掉。"""
        state = {"shown": True}

        def on_scroll(first, last):
            try:
                self._bar.set(first, last)
                full = float(first) <= 0.0 and float(last) >= 1.0
                if full and state["shown"]:
                    self._bar.pack_forget()
                    state["shown"] = False
                elif not full and not state["shown"]:
                    self._bar.pack(side="right", fill="y")
                    state["shown"] = True
            except Exception:
                pass

        self._canvas.configure(yscrollcommand=on_scroll)

    # ------------------------------------------------------------------
    # 筛选/渲染
    # ------------------------------------------------------------------

    def _on_query(self, text: str) -> None:
        self._query = str(text or "").strip().casefold()
        self._render()

    def _matched(self) -> list[MenuItem]:
        if not self._query:
            return list(self.items)
        tokens = [tok for tok in self._query.split() if tok]
        out = []
        for item in self.items:
            hay = item.haystack()
            if all(tok in hay for tok in tokens):
                out.append(item)
        return out

    def _render(self) -> None:
        t = self.t
        for child in list(self._inner.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        self._rows = []
        self._visible = self._matched()
        self._selected = -1

        groups: dict[str, list[MenuItem]] = {}
        order: list[str] = []
        for item in self._visible:
            if item.group not in groups:
                groups[item.group] = []
                order.append(item.group)
            groups[item.group].append(item)

        if not self._visible:
            box = tk.Frame(self._inner, bg=t.bg)
            box.pack(fill="x", pady=scale.s(28))
            tk.Label(box, text="没有匹配的功能", bg=t.bg, fg=t.text,
                     font=scale.font(13, weight="bold")).pack()
            tk.Label(box, text="换一个关键词，或清空搜索框看全部功能。",
                     bg=t.bg, fg=t.muted, font=scale.font(10)).pack(
                pady=(scale.s(4), 0))
        else:
            for group in order:
                self._group_header(group, len(groups[group]))
                for item in groups[group]:
                    self._row(item)

        total = len(self.items)
        shown = len(self._visible)
        self._count.configure(
            text=("%d / %d 项" % (shown, total)) if self._query
            else ("%d 项" % total))
        if self._visible:
            self._select(0, scroll=False)
            self._canvas.yview_moveto(0.0)

    def _group_header(self, name: str, count: int) -> None:
        t = self.t
        row = tk.Frame(self._inner, bg=t.bg)
        row.pack(fill="x", pady=(scale.s(10), scale.s(2)))
        tk.Label(row, text=name, bg=t.bg, fg=t.muted,
                 font=scale.font(10, weight="bold"),
                 anchor="w").pack(side="left")
        tk.Label(row, text=str(count), bg=t.bg, fg=t.dim,
                 font=scale.font(9), anchor="e").pack(side="right")
        tk.Frame(self._inner, bg=t.border, height=1).pack(fill="x")

    def _icon(self, name: str, selected: bool):
        color = self.t.accent if selected else self.t.muted
        key = (name, color)
        photo = self._icon_cache.get(key)
        if photo is None:
            try:
                img = icons.draw(name, scale.s(20), color)
                photo = render.photo(img, key="menu:icon:%s:%s"
                                     % (name, color))
            except Exception:
                photo = None
            self._icon_cache[key] = photo
        return photo

    def _row(self, item: MenuItem) -> None:
        t = self.t
        idx = len(self._rows)
        frame = tk.Frame(self._inner, bg=t.bg, cursor="hand2")
        frame.pack(fill="x", pady=scale.s(1))
        lab_icon = tk.Label(frame, bg=t.bg, cursor="hand2",
                            image=self._icon(item.icon, False))
        lab_icon.pack(side="left", padx=(scale.s(10), scale.s(10)),
                      pady=scale.s(7))
        text = tk.Frame(frame, bg=t.bg, cursor="hand2")
        text.pack(side="left", fill="x", expand=True)
        lab_title = tk.Label(text, text=item.title, bg=t.bg, fg=t.text,
                             font=scale.font(12, weight="bold"), anchor="w",
                             cursor="hand2")
        lab_title.pack(fill="x")
        desc = item.desc or ""
        if len(desc) > 110:
            desc = desc[:110] + "…"
        lab_desc = tk.Label(text, text=desc, bg=t.bg, fg=t.muted,
                            font=scale.font(9), anchor="w", justify="left",
                            cursor="hand2")
        lab_desc.pack(fill="x")
        lab_meta = tk.Label(frame, text="›", bg=t.bg, fg=t.dim,
                            font=scale.font(14), anchor="e", cursor="hand2")
        lab_meta.pack(side="right", padx=(scale.s(8), scale.s(14)))
        self._rows.append({
            "frame": frame, "icon": lab_icon, "title": lab_title,
            "desc": lab_desc, "meta": lab_meta, "item": item,
        })
        for widget in (frame, lab_icon, text, lab_title, lab_desc, lab_meta):
            widget.bind("<Enter>", lambda _e, i=idx: self._select(i))
            widget.bind("<Button-1>", lambda _e, i=idx: self._invoke(i))

    # ------------------------------------------------------------------
    # 选择/滚动
    # ------------------------------------------------------------------

    def _select(self, idx: int, *, scroll: bool = True) -> None:
        if not self._rows:
            self._selected = -1
            return
        idx = max(0, min(int(idx), len(self._rows) - 1))
        if idx == self._selected:
            return
        t = self.t
        for i, row in enumerate(self._rows):
            selected = i == idx
            bg = t.selected if selected else t.bg
            for key in ("frame", "icon", "title", "desc", "meta"):
                try:
                    row[key].configure(bg=bg)
                except Exception:
                    pass
            row["title"].configure(fg=t.accent if selected else t.text)
            row["desc"].configure(fg=t.text if selected else t.muted)
            row["meta"].configure(fg=t.accent if selected else t.dim)
            try:
                row["icon"].configure(
                    image=self._icon(row["item"].icon, selected))
            except Exception:
                pass
        self._selected = idx
        if scroll:
            self._scroll_to(idx)

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        base = self._selected if self._selected >= 0 else 0
        self._select(max(0, min(len(self._rows) - 1, base + delta)))

    def _scroll_to(self, idx: int) -> None:
        try:
            row = self._rows[idx]["frame"]
            self._inner.update_idletasks()
            total = max(1, self._inner.winfo_height())
            view = max(1, self._canvas.winfo_height())
            y = row.winfo_y()
            height = row.winfo_height()
            top = self._canvas.canvasy(0)
            if y < top:
                self._canvas.yview_moveto(max(0.0, (y - scale.s(6)) / total))
            elif y + height > top + view:
                self._canvas.yview_moveto(min(
                    1.0, (y + height - view + scale.s(6)) / total))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 打开
    # ------------------------------------------------------------------

    def _invoke_selected(self) -> None:
        if self._selected >= 0:
            self._invoke(self._selected)

    def _invoke(self, idx: int) -> None:
        if not (0 <= idx < len(self._rows)):
            return
        item = self._rows[idx]["item"]
        try:
            self.win.destroy()
        except Exception:
            pass
        try:
            self.app.root.after(10, lambda: self._run(item))
        except Exception:
            self._run(item)

    def _run(self, item: MenuItem) -> None:
        try:
            item.command()
        except Exception as exc:
            try:
                messagebox.showerror(
                    "功能打开失败", "%s\n\n%r" % (item.title, exc),
                    parent=self.app.root)
            except Exception:
                pass

    def _open_settings(self) -> None:
        """设置常驻在标题栏，避免功能多了以后要滚到底才找得到。"""
        try:
            self.win.destroy()
        except Exception:
            pass
        try:
            self.app.root.after(10, self.app._settings_window)
        except Exception:
            pass

    def _close(self) -> None:
        try:
            self.win.destroy()
        except Exception:
            pass
