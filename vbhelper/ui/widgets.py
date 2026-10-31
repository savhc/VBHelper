# -*- coding: utf-8 -*-
"""可复用控件库（计划 01 §3.5）。

全部控件吃 Tokens：构造时取全局当前令牌（set_theme 设置），
也可单独 apply_theme(tokens) / refresh()。按钮/卡片/徽标一律
tk.Canvas + create_text + PIL 圆角背景图（render.photo 缓存），
不搞嵌套 Frame 模拟圆角；尺寸全部走 scale.s()。

用法示例::

    from vbhelper.ui import widgets
    widgets.set_theme(tokens)            # 主题切换时全局广播
    btn = widgets.PillButton(parent, "保存", on_save, kind="primary")
    card = widgets.Card(parent, "基础设置")
    tk.Label(card.body, text="内容", bg=card.body["bg"]).pack()
    widgets.Toast(root, "已保存", duration=2000)
"""
import tkinter as tk
from typing import Any, Callable

from . import icons, render, scale
from .theme import Tokens, build_preset, contrast_ratio

__all__ = [
    "Card", "PillButton", "IconButton", "ToggleSwitch", "SegmentedControl",
    "SearchEntry", "SidebarItem", "Badge", "EmptyState", "Toast",
    "LoadingOverlay", "ConfirmDialog", "confirm", "set_theme",
    "current_tokens",
]

# ---------------------------------------------------------------------------
# 全局令牌 + 存活控件广播表
# ---------------------------------------------------------------------------

_tokens: Tokens | None = None
_live: list = []          # 存活控件，set_theme 广播用
_TOASTS: list = []        # 当前活着的 Toast（同 root 纵向排布）


def current_tokens() -> Tokens:
    """全局当前令牌；未设置过时用默认主题 midnight。"""
    global _tokens
    if _tokens is None:
        _tokens = build_preset("midnight").tokens
    return _tokens


def set_theme(tokens: Tokens) -> None:
    """设置全局令牌并广播给所有存活控件（apply_theme）。"""
    global _tokens
    _tokens = tokens
    for w in list(_live):
        if not _is_alive(w):
            _discard(w)
            continue
        try:
            w.apply_theme(tokens)
        except Exception:
            pass


def _is_alive(w: Any) -> bool:
    try:
        return bool(w.winfo_exists())
    except Exception:
        return False


def _discard(w: Any) -> None:
    try:
        _live.remove(w)
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# 小工具：混色 / 可读文字色 / 父容器底色 / 圆角背景图
# ---------------------------------------------------------------------------

def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _blend(fg: str, bg: str, ratio: float) -> str:
    """fg 按 ratio 混入 bg（0=纯 bg，1=纯 fg）。"""
    r1, g1, b1 = _rgb(fg)
    r2, g2, b2 = _rgb(bg)
    return "#%02X%02X%02X" % (
        max(0, min(255, int(round(r2 + (r1 - r2) * ratio)))),
        max(0, min(255, int(round(g2 + (g1 - g2) * ratio)))),
        max(0, min(255, int(round(b2 + (b1 - b2) * ratio)))))


def _on_color(bg: str) -> str:
    """在给定底色上选对比度更好的文字色（深或浅）。"""
    dark, light = "#14161C", "#F2F4F8"
    return dark if contrast_ratio(dark, bg) >= contrast_ratio(light, bg) \
        else light


def _parent_bg(widget: tk.Misc, tokens: Tokens) -> str:
    """取父容器底色（圆角透明处透出的颜色），取不到回退 bg。"""
    try:
        bg = widget.master.cget("background")
        if isinstance(bg, str) and bg.startswith("#"):
            return bg
    except Exception:
        pass
    return tokens.bg


def _rounded_photo(w: int, h: int, radius: int, fill: str,
                   border: str | None, key: str):
    """圆角背景图 -> 缓存 PhotoImage。key 必须含尺寸/颜色。"""
    img = render.rounded((w, h), radius, fill, border=border, border_width=1)
    return render.photo(img, key=key)


# ---------------------------------------------------------------------------
# 主题混入：注册进广播表，apply_theme -> refresh
# ---------------------------------------------------------------------------

class _Themed:
    """主题混入基类：apply_theme(tokens) -> refresh()。"""

    def _init_theme(self) -> Tokens:
        self._tokens = current_tokens()
        _live.append(self)
        try:
            self.bind("<Destroy>", lambda _e: _discard(self), add="+")
        except Exception:
            pass
        return self._tokens

    def apply_theme(self, tokens: Tokens) -> None:
        """换令牌并重绘。"""
        self._tokens = tokens
        self.refresh()

    def refresh(self) -> None:
        """按当前令牌重绘（子类实现）。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

class Card(_Themed, tk.Frame):
    """圆角卡片：PIL 背景图 + create_text 标题；内容装进 .body。

    示例::

        card = Card(parent, "网络工具", "批量 ping")
        tk.Label(card.body, text="内容").pack()
    """

    def __init__(self, parent: tk.Misc, title: str,
                 subtitle: str | None = None) -> None:
        tk.Frame.__init__(self, parent)
        self._init_theme()
        t = self._tokens
        self.configure(bg=_parent_bg(self, t))
        self._canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                 bg=_parent_bg(self, t))
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        # 先创建的在堆叠序最底，body 之后 pack 会盖在画布之上
        self._title = title
        self._subtitle = subtitle
        self._pad = scale.s(16)
        self._font_t = scale.font(14, weight="bold")
        self._font_s = scale.font(11)
        self._body = tk.Frame(self, bg=t.surface)
        self._body.pack(fill="both", expand=True,
                        padx=self._pad, pady=(self._header_h(), self._pad))
        self._last_size: tuple[int, int] = (0, 0)
        self.bind("<Configure>", lambda _e: self._on_configure())
        self.refresh()

    # -- 布局 --------------------------------------------------------

    def _header_h(self) -> int:
        """头部高度：内边距 + 标题行高（+ 副标题行高）。"""
        h = self._pad + self._font_t.metrics("linespace") + scale.s(6)
        if self._subtitle:
            h += self._font_s.metrics("linespace") + scale.s(2)
        return h

    def _on_configure(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if (w, h) != self._last_size and w > 1 and h > 1:
            self._last_size = (w, h)
            self._draw()

    def refresh(self) -> None:
        t = self._tokens
        pbg = _parent_bg(self, t)
        self.configure(bg=pbg)
        self._canvas.configure(bg=pbg)
        self._body.configure(bg=t.surface)
        self._body.pack_configure(pady=(self._header_h(), self._pad))
        self._draw()

    def _draw(self) -> None:
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        if w <= 2 or h <= 2:      # 未映射，等 Configure 再画
            return
        t = self._tokens
        c = self._canvas
        c.delete("all")
        ph = _rounded_photo(w, h, t.radius_lg, t.surface, t.border,
                            key="card:%dx%d:%s:%s" % (w, h, t.surface, t.border))
        self._bg_item = c.create_image(0, 0, anchor="nw", image=ph)
        self._photo = ph
        y = self._pad
        c.create_text(self._pad, y, text=self._title, anchor="nw",
                      font=self._font_t, fill=t.text)
        y += self._font_t.metrics("linespace") + scale.s(6)
        if self._subtitle:
            c.create_text(self._pad, y, text=self._subtitle, anchor="nw",
                          font=self._font_s, fill=t.muted)

    @property
    def body(self) -> tk.Frame:
        """内容容器，业务控件 pack/grid 进这里。"""
        return self._body


# ---------------------------------------------------------------------------
# PillButton
# ---------------------------------------------------------------------------

class PillButton(_Themed, tk.Canvas):
    """胶囊按钮。

    kind: ``primary``(主色实心) / ``secondary``(浮起描边) /
    ``ghost``(透明) / ``danger``(危险红)。

    示例::

        btn = PillButton(parent, "确定", on_ok, kind="primary")
        btn.invoke()          # 程序化点击
    """

    _KINDS = ("primary", "secondary", "ghost", "danger")

    def __init__(self, parent: tk.Misc, text: str,
                 command: Callable[[], Any] | None = None,
                 kind: str = "primary") -> None:
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0)
        self._init_theme()
        self._kind = kind if kind in self._KINDS else "secondary"
        self._text = text
        self._command = command
        self._enabled = True
        self._state = "normal"
        self._font = scale.font(13)
        self._ch = scale.s(32)
        self._cw = self._font.measure(text) + scale.s(36)
        self.configure(width=self._cw, height=self._ch,
                       bg=_parent_bg(self, self._tokens))
        self._bg_item = self.create_image(0, 0, anchor="nw")
        self._text_item = self.create_text(self._cw // 2, self._ch // 2,
                                           text=text, font=self._font)
        self.bind("<Enter>", lambda _e: self._set_state("hover"))
        self.bind("<Leave>", lambda _e: self._set_state("normal"))
        self.bind("<ButtonPress-1>", lambda _e: self._set_state("pressed"))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.refresh()

    # -- 状态色 ------------------------------------------------------

    def refresh(self) -> None:
        """按 kind 从 tokens 重算三态色并重绘。"""
        t = self._tokens
        if self._kind == "primary":
            base = (t.accent, t.accent_hover, t.accent_pressed)
            border = None
            fg = _on_color(t.accent)
        elif self._kind == "ghost":
            base = (_parent_bg(self, t), t.hover, t.selected)
            border = None
            fg = t.text
        elif self._kind == "danger":
            base = (t.danger, _blend("#FFFFFF", t.danger, 0.14),
                    _blend("#000000", t.danger, 0.18))
            border = None
            fg = _on_color(t.danger)
        else:   # secondary
            base = (t.elevated, t.hover, t.selected)
            border = t.border
            fg = t.text
        self._colors = {"normal": base[0], "hover": base[1],
                        "pressed": base[2]}
        self._border = border
        self._fg = fg
        self._draw()

    def _draw(self) -> None:
        t = self._tokens
        if self._enabled:
            fill = self._colors.get(self._state, self._colors["normal"])
            fg = self._fg
            border = self._border
        else:
            fill = _blend(self._colors["normal"], _parent_bg(self, t), 0.45)
            fg = t.dim
            border = None
        ph = _rounded_photo(self._cw, self._ch, self._ch // 2, fill, border,
                            key="pill:%s:%dx%d:%s:%s"
                                % (self._kind, self._cw, self._ch, fill, border))
        self.itemconfigure(self._bg_item, image=ph)
        self._photo = ph
        self.itemconfigure(self._text_item, fill=fg)

    def _set_state(self, state: str) -> None:
        if not self._enabled:
            state = "normal"
        if state != self._state:
            self._state = state
            self._draw()

    def _on_release(self, _e: tk.Event) -> None:
        inside = self._pointer_inside()
        self._set_state("hover")
        if inside:
            self.invoke()

    def _pointer_inside(self) -> bool:
        try:
            x = self.winfo_pointerx() - self.winfo_rootx()
            y = self.winfo_pointery() - self.winfo_rooty()
            return 0 <= x < self.winfo_width() and 0 <= y < self.winfo_height()
        except Exception:
            return False

    # -- 公共 --------------------------------------------------------

    def invoke(self) -> None:
        """程序化触发 command。"""
        if self._enabled and self._command:
            try:
                self._command()
            except Exception:
                pass

    def set_text(self, text: str) -> None:
        """换文案（重算宽度）。"""
        self._text = text
        self._cw = self._font.measure(text) + scale.s(36)
        self.configure(width=self._cw)
        self.coords(self._text_item, self._cw // 2, self._ch // 2)
        self.itemconfigure(self._text_item, text=text)
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        """启用/禁用。"""
        self._enabled = bool(enabled)
        self._state = "normal"
        self._draw()


# ---------------------------------------------------------------------------
# Tooltip（IconButton 内部用）
# ---------------------------------------------------------------------------

class _Tooltip:
    """悬停 400ms 后出现的小气泡，离开即消失。"""

    def __init__(self, widget: tk.Misc, text: str) -> None:
        self._w = widget
        self._text = text
        self._job: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress-1>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e: tk.Event | None = None) -> None:
        self._cancel_job()
        try:
            self._job = self._w.after(400, self._show)
        except Exception:
            pass

    def _show(self) -> None:
        if self._tip or not _is_alive(self._w):
            return
        t = current_tokens()
        tip = tk.Toplevel(self._w)
        tip.overrideredirect(True)
        tip.withdraw()
        tk.Label(tip, text=self._text, bg=t.elevated, fg=t.text,
                 font=scale.font(11), padx=scale.s(8),
                 pady=scale.s(4)).pack()
        tip.update_idletasks()
        x = self._w.winfo_rootx() + \
            (self._w.winfo_width() - tip.winfo_reqwidth()) // 2
        y = self._w.winfo_rooty() + self._w.winfo_height() + scale.s(6)
        tip.geometry("+%d+%d" % (x, y))
        tip.deiconify()
        self._tip = tip

    def _hide(self, _e: tk.Event | None = None) -> None:
        self._cancel_job()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    def _cancel_job(self) -> None:
        if self._job:
            try:
                self._w.after_cancel(self._job)
            except Exception:
                pass
            self._job = None


# ---------------------------------------------------------------------------
# IconButton
# ---------------------------------------------------------------------------

class IconButton(_Themed, tk.Canvas):
    """方形图标按钮；icon 传 Feather 图标名（search/close/...）。

    示例::

        IconButton(parent, "refresh", on_refresh, tooltip="刷新")
    """

    def __init__(self, parent: tk.Misc, icon: str,
                 command: Callable[[], Any] | None = None,
                 tooltip: str | None = None, *, size: int = 32) -> None:
        self._size = scale.s(size)
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0)
        self._init_theme()
        self._icon = icon
        self._command = command
        self._enabled = True
        self._state = "normal"
        self.configure(width=self._size, height=self._size,
                       bg=_parent_bg(self, self._tokens))
        self._bg_item = self.create_image(0, 0, anchor="nw")
        self._icon_img_item = self.create_image(self._size // 2,
                                                self._size // 2,
                                                anchor="center")
        self.bind("<Enter>", lambda _e: self._set_state("hover"))
        self.bind("<Leave>", lambda _e: self._set_state("normal"))
        self.bind("<ButtonPress-1>", lambda _e: self._set_state("pressed"))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.refresh()
        if tooltip:
            _Tooltip(self, tooltip)

    def refresh(self) -> None:
        t = self._tokens
        pbg = _parent_bg(self, t)
        self._colors = {"normal": pbg, "hover": t.hover, "pressed": t.selected}
        self.configure(bg=pbg)
        self._draw()

    def _draw(self) -> None:
        t = self._tokens
        if self._enabled:
            fill = self._colors.get(self._state, self._colors["normal"])
            col = t.text if self._state != "normal" else t.muted
        else:
            fill = _blend(self._colors["normal"], t.bg, 0.45)
            col = t.dim
        ph = _rounded_photo(self._size, self._size, scale.s(8), fill, None,
                            key="iconbtn:%dx%d:%s"
                                % (self._size, self._size, fill))
        self.itemconfigure(self._bg_item, image=ph)
        self._photo = ph
        isz = scale.s(18)
        iph = render.photo(
            icons.draw(self._icon, isz, col),
            key="iconbtn:ico:%s:%d:%s" % (self._icon, isz, col))
        self.itemconfigure(self._icon_img_item, image=iph)
        self._iphoto = iph

    def _set_state(self, state: str) -> None:
        if not self._enabled:
            state = "normal"
        if state != self._state:
            self._state = state
            self._draw()

    def _on_release(self, _e: tk.Event) -> None:
        inside = self._pointer_inside()
        self._set_state("hover")
        if inside:
            self.invoke()

    def _pointer_inside(self) -> bool:
        try:
            x = self.winfo_pointerx() - self.winfo_rootx()
            y = self.winfo_pointery() - self.winfo_rooty()
            return 0 <= x < self.winfo_width() and 0 <= y < self.winfo_height()
        except Exception:
            return False

    def invoke(self) -> None:
        """程序化触发 command。"""
        if self._enabled and self._command:
            try:
                self._command()
            except Exception:
                pass

    def set_icon(self, icon: str) -> None:
        """换图标（glyph 名）。"""
        self._icon = icon
        self._draw()


# ---------------------------------------------------------------------------
# ToggleSwitch
# ---------------------------------------------------------------------------

class ToggleSwitch(_Themed, tk.Canvas):
    """开关。command 回调签名 command(value: bool)。

    示例::

        ToggleSwitch(parent, value=False, command=lambda v: print(v))
    """

    def __init__(self, parent: tk.Misc, value: bool = False,
                 command: Callable[[bool], Any] | None = None) -> None:
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0)
        self._init_theme()
        self._cw, self._ch = scale.s(44), scale.s(24)
        self._d = self._ch - scale.s(4)
        self._value = bool(value)
        self._command = command
        self.configure(width=self._cw, height=self._ch,
                       bg=_parent_bg(self, self._tokens))
        self._bg_item = self.create_image(0, 0, anchor="nw")
        self._knob_item = self.create_image(0, 0, anchor="nw")
        self.bind("<ButtonRelease-1>", lambda _e: self.toggle())
        self.refresh()

    def refresh(self) -> None:
        t = self._tokens
        self.configure(bg=_parent_bg(self, t))
        fill = t.accent if self._value else t.surface
        border = None if self._value else t.border
        ph = _rounded_photo(self._cw, self._ch, self._ch // 2, fill, border,
                            key="toggle:%dx%d:%s:%s"
                                % (self._cw, self._ch, fill, border))
        self.itemconfigure(self._bg_item, image=ph)
        self._photo = ph
        knob = _on_color(fill)
        kph = render.photo(
            render.circle(self._d, knob, shadow=False, highlight=False),
            key="toggle:knob:%d:%s" % (self._d, knob))
        self.itemconfigure(self._knob_item, image=kph)
        self._kphoto = kph
        x = self._cw - self._d - scale.s(2) if self._value else scale.s(2)
        self.coords(self._knob_item, x, (self._ch - self._d) // 2)

    def get(self) -> bool:
        """当前状态。"""
        return self._value

    def set(self, value: bool, fire: bool = False) -> None:
        """设置状态；fire=True 时触发回调。"""
        v = bool(value)
        if v == self._value:
            return
        self._value = v
        self.refresh()
        if fire and self._command:
            try:
                self._command(v)
            except Exception:
                pass

    def toggle(self) -> None:
        """翻转并触发回调。"""
        self.set(not self._value, fire=True)


# ---------------------------------------------------------------------------
# SegmentedControl
# ---------------------------------------------------------------------------

class SegmentedControl(_Themed, tk.Canvas):
    """分段选择器。items 为字符串列表；command(value: str)。

    示例::

        seg = SegmentedControl(parent, ["全部", "进行中", "已完成"],
                               "全部", lambda v: print(v))
    """

    def __init__(self, parent: tk.Misc, items: list[str],
                 value: str | None = None,
                 command: Callable[[str], Any] | None = None) -> None:
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0)
        self._init_theme()
        self._items = [str(i) for i in items] or [""]
        if value in self._items:
            self._value = str(value)
        else:
            self._value = self._items[0]
        self._command = command
        self._hover = -1
        self._font = scale.font(12)
        self._ch = scale.s(32)
        self._pad = scale.s(14)
        self._xs: list[int] = []
        self._widths = [self._font.measure(i) + 2 * self._pad
                        for i in self._items]
        self._cw = sum(self._widths) + scale.s(4)
        self.configure(width=self._cw, height=self._ch,
                       bg=_parent_bg(self, self._tokens))
        self._bg_item = self.create_image(0, 0, anchor="nw")
        self._seg_items: list[int] = []
        self._txt_items: list[int] = []
        x = scale.s(2)
        for wd in self._widths:
            self._seg_items.append(
                self.create_image(x, scale.s(2), anchor="nw"))
            self._txt_items.append(
                self.create_text(x + wd // 2, self._ch // 2, anchor="center"))
            self._xs.append(x)
            x += wd
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda _e: self._set_hover(-1))
        self.refresh()

    def _on_click(self, e: tk.Event) -> None:
        idx = self._hit(e.x)
        if idx >= 0:
            self.set(self._items[idx], fire=True)

    def _on_motion(self, e: tk.Event) -> None:
        self._set_hover(self._hit(e.x))

    def _hit(self, x: int) -> int:
        for i, x0 in enumerate(self._xs):
            if x0 <= x < x0 + self._widths[i]:
                return i
        return -1

    def _set_hover(self, idx: int) -> None:
        if idx != self._hover:
            self._hover = idx
            self._draw_segments()

    def refresh(self) -> None:
        t = self._tokens
        self.configure(bg=_parent_bg(self, t))
        ph = _rounded_photo(self._cw, self._ch, scale.s(8), t.elevated, t.border,
                            key="seg:bg:%dx%d:%s:%s"
                                % (self._cw, self._ch, t.elevated, t.border))
        self.itemconfigure(self._bg_item, image=ph)
        self._photo = ph
        self._draw_segments()

    def _draw_segments(self) -> None:
        t = self._tokens
        seg_h = self._ch - scale.s(4)
        for i, item in enumerate(self._items):
            wd = self._widths[i]
            selected = item == self._value
            hovered = i == self._hover and not selected
            if selected:
                fill, border = t.surface, t.border
            elif hovered:
                fill, border = t.hover, None
            else:
                fill, border = None, None
            if fill:
                ph = _rounded_photo(wd, seg_h, scale.s(6), fill, border,
                                    key="seg:%dx%d:%s:%s"
                                        % (wd, seg_h, fill, border))
                self.itemconfigure(self._seg_items[i], image=ph)
                self._seg_photos = getattr(self, "_seg_photos", {})
                self._seg_photos[i] = ph
            else:
                self.itemconfigure(self._seg_items[i], image="")
            self.itemconfigure(self._txt_items[i], text=item,
                               font=self._font,
                               fill=t.text if selected else t.muted)

    def get(self) -> str:
        """当前选中值。"""
        return self._value

    def set(self, value: str, fire: bool = False) -> None:
        """设置选中项；fire=True 时触发回调。"""
        if value in self._items and value != self._value:
            self._value = value
            self._draw_segments()
            if fire and self._command:
                try:
                    self._command(value)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# SearchEntry
# ---------------------------------------------------------------------------

class SearchEntry(_Themed, tk.Frame):
    """搜索输入框：搜索图标 + 占位符 + 清空按钮。

    示例::

        entry = SearchEntry(parent, "搜索…", lambda text: refresh(text))
        entry.get(); entry.set("关键词"); entry.clear()
    """

    def __init__(self, parent: tk.Misc, placeholder: str = "",
                 on_change: Callable[[str], Any] | None = None) -> None:
        tk.Frame.__init__(self, parent)
        self._init_theme()
        t = self._tokens
        self._placeholder = placeholder
        self._on_change = on_change
        self._ph_on = False
        self._updating = False
        self.configure(bg=t.surface)
        isz = scale.s(14)
        self._icon_photo = render.photo(
            icons.draw("search", isz, t.muted),
            key="search:ico:%d:%s" % (isz, t.muted))
        self._icon = tk.Label(self, image=self._icon_photo, bg=t.surface)
        self._icon.pack(side="left", padx=(scale.s(10), 0), pady=scale.s(2))
        self._var = tk.StringVar(self)
        self._entry = tk.Entry(self, textvariable=self._var,
                               bg=t.surface, fg=t.text, relief="flat",
                               insertbackground=t.text,
                               selectbackground=t.selected,
                               highlightthickness=1,
                               highlightbackground=t.border,
                               highlightcolor=t.accent,
                               font=scale.font(12))
        self._entry.pack(side="left", fill="x", expand=True,
                         padx=(scale.s(6), scale.s(4)), pady=scale.s(4))
        self._clear = IconButton(self, "clear", self.clear, size=22)
        self._var.trace_add("write", self._on_var)
        self._entry.bind("<FocusIn>", lambda _e: self._hide_ph())
        self._entry.bind("<FocusOut>", lambda _e: self._check_ph())
        self._check_ph()
        self.refresh()

    def _on_var(self, *_a) -> None:
        if self._updating:
            return
        txt = self._var.get()
        if self._ph_on and txt == self._placeholder:
            return
        self._toggle_clear(bool(txt))
        if self._on_change:
            try:
                self._on_change(txt)
            except Exception:
                pass

    def _toggle_clear(self, shown: bool) -> None:
        try:
            if shown:
                self._clear.pack(side="right", padx=(0, scale.s(4)),
                                 pady=scale.s(5))
            else:
                self._clear.pack_forget()
        except Exception:
            pass

    def _show_ph(self) -> None:
        self._updating = True
        try:
            self._ph_on = True
            self._entry.delete(0, "end")
            self._entry.insert(0, self._placeholder)
            self._entry.configure(fg=self._tokens.dim)
        finally:
            self._updating = False

    def _hide_ph(self) -> None:
        self._updating = True
        try:
            if self._ph_on:
                self._ph_on = False
                self._entry.delete(0, "end")
            self._entry.configure(fg=self._tokens.text)
        finally:
            self._updating = False

    def _check_ph(self) -> None:
        if self._placeholder and not self._var.get():
            self._show_ph()

    def refresh(self) -> None:
        t = self._tokens
        self.configure(bg=t.surface)
        isz = scale.s(14)
        self._icon_photo = render.photo(
            icons.draw("search", isz, t.muted),
            key="search:ico:%d:%s" % (isz, t.muted))
        self._icon.configure(bg=t.surface, image=self._icon_photo)
        self._entry.configure(bg=t.surface,
                              fg=t.dim if self._ph_on else t.text,
                              insertbackground=t.text,
                              selectbackground=t.selected,
                              highlightbackground=t.border,
                              highlightcolor=t.accent)
        self._toggle_clear(not self._ph_on and bool(self._var.get()))

    def get(self) -> str:
        """当前文本（占位符状态返回空串）。"""
        return "" if self._ph_on else self._var.get()

    def set(self, text: str) -> None:
        """设置文本（不触发 on_change 回调）。"""
        self._updating = True
        try:
            self._ph_on = False
            self._var.set(text)
            self._entry.configure(fg=self._tokens.text)
        finally:
            self._updating = False
        self._toggle_clear(bool(text))

    def clear(self) -> None:
        """清空文本（不触发 on_change 回调）。"""
        self.set("")
        self._check_ph()

    def focus(self) -> None:
        """聚焦输入框。"""
        self._hide_ph()
        self._entry.focus_set()


# ---------------------------------------------------------------------------
# SidebarItem
# ---------------------------------------------------------------------------

class SidebarItem(_Themed, tk.Canvas):
    """侧栏条目：图标 + 文本 + 右侧计数；active 高亮。

    示例::

        item = SidebarItem(parent, "folder", "文件", count=3, active=True,
                           command=lambda t: print(t))
    """

    def __init__(self, parent: tk.Misc, icon: str, text: str,
                 count: int | str | None = None, active: bool = False,
                 command: Callable[[str], Any] | None = None) -> None:
        self._ch = scale.s(36)
        self._font = scale.font(12)
        self._font_c = scale.font(11)
        self._min_w = self._font.measure(text) + scale.s(72)
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0,
                           height=self._ch, width=self._min_w)
        self._init_theme()
        self._icon = icon
        self._text = text
        self._count = count
        self._active = bool(active)
        self._command = command
        self._hover = False
        self._last_w = 0
        self.bind("<Configure>", lambda _e: self._on_configure())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._on_click)
        self.refresh()

    def _on_configure(self) -> None:
        w = self.winfo_width()
        if w > 1 and w != self._last_w:
            self._last_w = w
            self._draw()

    def _set_hover(self, v: bool) -> None:
        if v != self._hover:
            self._hover = v
            self._draw()

    def _on_click(self, _e: tk.Event) -> None:
        if self._command:
            try:
                self._command(self._text)
            except Exception:
                pass

    def refresh(self) -> None:
        t = self._tokens
        self.configure(bg=_parent_bg(self, t))
        self._draw()

    def _draw(self) -> None:
        t = self._tokens
        w = max(self._min_w, self.winfo_width())
        h = self._ch
        self.delete("all")
        if self._active:
            fill, border = t.selected, None
        elif self._hover:
            fill, border = t.hover, None
        else:
            fill, border = _parent_bg(self, t), None
        ph = _rounded_photo(w, h, scale.s(8), fill, border,
                            key="side:%dx%d:%s" % (w, h, fill))
        self._bg_item = self.create_image(0, 0, anchor="nw", image=ph)
        self._photo = ph
        pad = scale.s(12)
        col = t.text if (self._active or self._hover) else t.muted
        isz = scale.s(16)
        iph = render.photo(icons.draw(self._icon, isz, col),
                           key="side:ico:%s:%d:%s" % (self._icon, isz, col))
        self.create_image(pad + scale.s(1), (h - isz) // 2, anchor="nw",
                          image=iph)
        self._iphoto = iph
        self.create_text(pad + scale.s(26), h // 2, text=self._text,
                         anchor="w", font=self._font,
                         fill=t.text if self._active else t.muted)
        if self._count is not None and str(self._count) != "":
            self.create_text(w - pad, h // 2, text=str(self._count),
                             anchor="e", font=self._font_c, fill=t.dim)

    def set_active(self, active: bool) -> None:
        if bool(active) != self._active:
            self._active = bool(active)
            self._draw()

    def set_count(self, count: int | str | None) -> None:
        self._count = count
        self._draw()

    @property
    def text(self) -> str:
        return self._text


# ---------------------------------------------------------------------------
# Badge
# ---------------------------------------------------------------------------

class Badge(_Themed, tk.Canvas):
    """小徽标。tone: neutral/accent/success/warning/danger。

    示例::

        Badge(parent, "NEW", tone="accent")
    """

    _TONES = ("neutral", "accent", "success", "warning", "danger")

    def __init__(self, parent: tk.Misc, text: str,
                 tone: str = "neutral") -> None:
        self._font = scale.font(11)
        self._ch = scale.s(22)
        self._cw = self._font.measure(text) + scale.s(20)
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0,
                           width=self._cw, height=self._ch)
        self._init_theme()
        self._text = text
        self._tone = tone if tone in self._TONES else "neutral"
        self._bg_item = self.create_image(0, 0, anchor="nw")
        self._txt_item = self.create_text(self._cw // 2, self._ch // 2,
                                          anchor="center", font=self._font)
        self.refresh()

    def refresh(self) -> None:
        t = self._tokens
        soft = 0.18
        colors = {
            "neutral": (t.elevated, t.muted),
            "accent": (t.accent_soft,
                       t.accent if contrast_ratio(t.accent, t.accent_soft) >= 3
                       else t.text),
            "success": (_blend(t.success, t.surface, soft), t.success),
            "warning": (_blend(t.warning, t.surface, soft), t.warning),
            "danger": (_blend(t.danger, t.surface, soft), t.danger),
        }
        fill, fg = colors[self._tone]
        self._fill, self._fg = fill, fg
        self.configure(bg=_parent_bg(self, t))
        ph = _rounded_photo(self._cw, self._ch, self._ch // 2, fill, None,
                            key="badge:%dx%d:%s" % (self._cw, self._ch, fill))
        self.itemconfigure(self._bg_item, image=ph)
        self._photo = ph
        self.itemconfigure(self._txt_item, text=self._text, fill=fg)

    def set_text(self, text: str) -> None:
        """换文案（重算宽度）。"""
        self._text = text
        self._cw = self._font.measure(text) + scale.s(20)
        self.configure(width=self._cw)
        self.coords(self._txt_item, self._cw // 2, self._ch // 2)
        self.refresh()


# ---------------------------------------------------------------------------
# EmptyState
# ---------------------------------------------------------------------------

class EmptyState(_Themed, tk.Frame):
    """空状态：大图标 + 标题 + 提示 + 可选动作按钮。

    示例::

        EmptyState(parent, "folder", "还没有收藏",
                   "点击右上角添加", action_text="去添加", action=add)
    """

    def __init__(self, parent: tk.Misc, icon: str, title: str, hint: str,
                 action_text: str | None = None,
                 action: Callable[[], Any] | None = None) -> None:
        tk.Frame.__init__(self, parent)
        self._init_theme()
        t = self._tokens
        self._icon_name = icon
        self.configure(bg=_parent_bg(self, t))
        self._canvas = tk.Canvas(self, width=scale.s(56), height=scale.s(56),
                                 highlightthickness=0, bd=0,
                                 bg=_parent_bg(self, t))
        self._canvas.pack(pady=(scale.s(8), scale.s(4)))
        self._icon_img_item = self._canvas.create_image(
            scale.s(28), scale.s(28), anchor="center")
        self._title = tk.Label(self, text=title, bg=_parent_bg(self, t),
                               fg=t.text, font=scale.font(14, weight="bold"))
        self._title.pack()
        self._hint = tk.Label(self, text=hint, bg=_parent_bg(self, t),
                              fg=t.muted, font=scale.font(12),
                              wraplength=scale.s(320), justify="center")
        self._hint.pack(pady=(scale.s(2), 0))
        self._btn: PillButton | None = None
        if action_text:
            self._btn = PillButton(self, action_text, action,
                                   kind="secondary")
            self._btn.pack(pady=(scale.s(12), scale.s(8)))
        self.refresh()

    def refresh(self) -> None:
        t = self._tokens
        pbg = _parent_bg(self, t)
        self.configure(bg=pbg)
        self._canvas.configure(bg=pbg)
        self._title.configure(bg=pbg, fg=t.text)
        self._hint.configure(bg=pbg, fg=t.muted)
        isz = scale.s(40)
        iph = render.photo(
            icons.draw(self._icon_name, isz, t.dim),
            key="empty:%s:%d:%s" % (self._icon_name, isz, t.dim))
        self._canvas.itemconfigure(self._icon_img_item, image=iph)
        self._iphoto = iph


# ---------------------------------------------------------------------------
# Toast
# ---------------------------------------------------------------------------

def _wrap_toast_text(font: Any, text: str,
                     max_w: int, max_lines: int = 4) -> list[str]:
    """把提示文字按像素宽度折行（中英文都按字符断，最多 max_lines 行）。"""
    text = " ".join(str(text or "").split())
    if not text:
        return [""]
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if not cur or font.measure(trial) <= max_w:
            cur = trial
            continue
        lines.append(cur)
        cur = ch
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines and cur:
        lines.append(cur)
    used = "".join(lines)
    if len(used) < len(text) and lines:
        last = lines[-1]
        while last and font.measure(last + "…") > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines or [""]


def _capture_toast_bitmap(hwnd: int, w: int, h: int):
    """PrintWindow 抓 Toast 客户区；全黑时返回 None（让调用方回退）。"""
    try:
        import ctypes
        from PIL import Image
        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32
        hdc = u32.GetDC(ctypes.c_void_p(hwnd))
        memdc = g32.CreateCompatibleDC(ctypes.c_void_p(hdc))
        bmp = g32.CreateCompatibleBitmap(ctypes.c_void_p(hdc), w, h)
        old = g32.SelectObject(ctypes.c_void_p(memdc), ctypes.c_void_p(bmp))
        u32.PrintWindow(ctypes.c_void_p(hwnd), ctypes.c_void_p(memdc), 2)

        class _BIH(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
                        ("biHeight", ctypes.c_int),
                        ("biPlanes", ctypes.c_ushort),
                        ("biBitCount", ctypes.c_ushort),
                        ("biCompression", ctypes.c_uint),
                        ("biSizeImage", ctypes.c_uint),
                        ("biXPelsPerMeter", ctypes.c_int),
                        ("biYPelsPerMeter", ctypes.c_int),
                        ("biClrUsed", ctypes.c_uint),
                        ("biClrImportant", ctypes.c_uint)]

        hdr = _BIH()
        hdr.biSize = ctypes.sizeof(_BIH)
        hdr.biWidth, hdr.biHeight, hdr.biPlanes = w, -h, 1
        hdr.biBitCount, hdr.biCompression = 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        g32.GetDIBits(ctypes.c_void_p(hdc), ctypes.c_void_p(bmp), 0, h,
                      buf, ctypes.byref(hdr), 0)
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
        g32.SelectObject(ctypes.c_void_p(memdc), ctypes.c_void_p(old))
        g32.DeleteObject(ctypes.c_void_p(bmp))
        g32.DeleteDC(ctypes.c_void_p(memdc))
        u32.ReleaseDC(ctypes.c_void_p(hwnd), ctypes.c_void_p(hdc))
        if sum(img.convert("L").getdata()) / max(1, w * h) < 6:
            return None
        return img
    except Exception:
        return None


def _layer_toast(win, w: int, h: int, radius: int) -> bool:
    """把 Tk 画好的 Toast 抓成位图，加圆角 alpha 后贴到 layered 窗口。"""
    try:
        import ctypes
        from PIL import Image, ImageDraw, ImageGrab
        from .ball import _set_layered_style, _update_layered
        hwnd = int(ctypes.windll.user32.GetAncestor(
            ctypes.c_void_p(int(win.winfo_id())), 2) or win.winfo_id())
        img = _capture_toast_bitmap(hwnd, w, h)
        if img is None:
            x, y = win.winfo_rootx(), win.winfo_rooty()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h),
                                 all_screens=True)
        img = img.convert("RGBA")
        factor = 4
        mask = Image.new("L", (w * factor, h * factor), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, w * factor - 1, h * factor - 1),
            radius=max(1, radius) * factor, fill=255)
        mask = mask.resize((w, h), Image.LANCZOS)
        img.putalpha(mask)
        return bool(_set_layered_style(hwnd) and
                    _update_layered(hwnd, img))
    except Exception:
        return False


class Toast(_Themed, tk.Toplevel):
    """右下角轻提示，duration 毫秒后自动消失；多条自动纵向排布。

    默认置顶（Tk 根窗口是隐藏壳时也能看见）；长文本自动折行。
    不传 ``topmost=False`` 则始终显示在其它窗口上方。

    示例::

        Toast(root, "已保存", duration=2000)
    """

    def __init__(self, root: tk.Misc, text: str, duration: int = 2500,
                 *, topmost: bool = True) -> None:
        tk.Toplevel.__init__(self, root)
        self._init_theme()
        t = self._tokens
        self._text = text
        self._font = scale.font(12)
        max_text_w = scale.s(348)
        lines = _wrap_toast_text(self._font, text, max_text_w)
        line_h = self._font.metrics("linespace")
        text_w = max(self._font.measure(line) for line in lines)
        self._cw = min(scale.s(380), max(scale.s(120), text_w + scale.s(32)))
        self._ch = max(scale.s(40), line_h * len(lines) + scale.s(18))
        self.overrideredirect(True)
        try:
            if topmost:
                self.attributes("-topmost", True)
        except Exception:
            pass
        # 圆角外不能露方形窗口底色。先用填充色画，映射后抓成位图，
        # 加圆角 alpha 遮罩并按悬浮球同样的 layered 方式贴上去：
        # 四角真正逐像素透明，不会出现黑块或颜色键描边。
        self._layered = False
        fill_bg = t.elevated
        self.configure(bg=fill_bg)
        self._canvas = tk.Canvas(self, width=self._cw, height=self._ch,
                                 highlightthickness=0, bd=0, bg=fill_bg)
        self._canvas.pack()
        radius = self._ch // 2 if len(lines) == 1 else scale.s(14)
        ph = _rounded_photo(self._cw, self._ch, radius, t.elevated,
                            t.border,
                            key="toast:%dx%d:%d:%s:%s"
                                % (self._cw, self._ch, radius, t.elevated,
                                   t.border))
        self._canvas.create_image(0, 0, anchor="nw", image=ph)
        self._photo = ph
        self._canvas.create_text(
            scale.s(16), (self._ch - line_h * len(lines)) // 2,
            text="\n".join(lines), anchor="nw", justify="left",
            font=self._font, fill=t.text)
        self.bind("<Button-1>", lambda _e: self.destroy())
        _TOASTS.append(self)
        _relayout_toasts()
        self.deiconify()
        try:
            self.update_idletasks()
            self.update()
            self._layered = _layer_toast(self, self._cw, self._ch, radius)
        except Exception:
            self._layered = False
        try:
            if topmost:
                self.lift()
        except Exception:
            pass
        self._job = self.after(max(1, int(duration)), self.destroy)

    def refresh(self) -> None:
        """Toast 是短命窗口，主题切换时跟随全局重建，不单独重绘。"""

    def destroy(self) -> None:
        try:
            if getattr(self, "_job", None):
                self.after_cancel(self._job)
                self._job = None
        except Exception:
            pass
        if self in _TOASTS:
            _TOASTS.remove(self)
        tk.Toplevel.destroy(self)
        _relayout_toasts()


def _relayout_toasts() -> None:
    """同 root 的 Toast 从右下角向上排布，不互相叠死。

    VBHelper 的 Tk 根窗口是隐藏壳（1x1/不可见），不能用它的几何；
    这种情况回落到 Windows 工作区（排除任务栏）右下角。
    """
    margin, gap = scale.s(16), scale.s(8)
    row = 0
    for toast in list(_TOASTS):
        if not _is_alive(toast):
            continue
        root = toast.master
        rx = ry = rw = rh = 0
        try:
            if root is not None and root.winfo_viewable():
                rx, ry = root.winfo_rootx(), root.winfo_rooty()
                rw, rh = root.winfo_width(), root.winfo_height()
        except Exception:
            pass
        if rw < scale.s(80) or rh < scale.s(80):
            try:
                from .. import core
                left, top, right, bottom = core.work_area()
                rx, ry = left, top
                rw, rh = right - left, bottom - top
            except Exception:
                rx, ry = 0, 0
                rw = toast.winfo_screenwidth()
                rh = toast.winfo_screenheight()
        x = max(0, rx + rw - toast._cw - margin)
        y = max(0, ry + rh - (row + 1) * toast._ch - row * gap - margin)
        try:
            toast.geometry("%dx%d+%d+%d" % (toast._cw, toast._ch, x, y))
        except Exception:
            pass
        row += 1


# ---------------------------------------------------------------------------
# LoadingOverlay
# ---------------------------------------------------------------------------

class LoadingOverlay(_Themed, tk.Frame):
    """加载遮罩：盖住 parent，半暗底 + 文本 + 呼吸点动画。

    示例::

        ov = LoadingOverlay(parent, "正在加载…")
        ...
        ov.hide()
    """

    def __init__(self, parent: tk.Misc, text: str = "正在加载…") -> None:
        tk.Frame.__init__(self, parent)
        self._init_theme()
        t = self._tokens
        self._base_text = text
        self._dots = 0
        self._job: str | None = None
        self._dim = _blend("#000000", t.bg, 0.45)
        self.configure(bg=self._dim)
        self._label = tk.Label(self, text=text, bg=self._dim, fg=t.text,
                               font=scale.font(13))
        self._label.pack(expand=True)
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self.lift()
        self.bind("<Destroy>", self._stop_anim, add="+")
        self._start_anim()

    def _start_anim(self) -> None:
        self._stop_anim()
        self._tick()

    def _tick(self) -> None:
        self._dots = (self._dots + 1) % 4
        try:
            self._label.configure(
                text=self._base_text + "·" * (self._dots % 4))
        except Exception:
            return
        self._job = self.after(400, self._tick)

    def _stop_anim(self, *_a) -> None:
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def refresh(self) -> None:
        t = self._tokens
        self._dim = _blend("#000000", t.bg, 0.45)
        self.configure(bg=self._dim)
        try:
            self._label.configure(bg=self._dim, fg=t.text)
        except Exception:
            pass

    def set_text(self, text: str) -> None:
        """换提示文案。"""
        self._base_text = text
        try:
            self._label.configure(text=text)
        except Exception:
            pass

    def show(self) -> None:
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self.lift()
        self._start_anim()

    def hide(self) -> None:
        """隐藏遮罩（销毁用 destroy）。"""
        self._stop_anim()
        self.place_forget()


# ---------------------------------------------------------------------------
# ConfirmDialog
# ---------------------------------------------------------------------------

class ConfirmDialog(_Themed, tk.Toplevel):
    """模态确认框；结果存 ``.result``（True/False）。

    直接构造会阻塞到关闭（wait_window）；要拿 bool 用模块函数
    confirm()。

    示例::

        if widgets.confirm(root, "删除", "确定删除该文件？", danger=True):
            do_delete()
    """

    def __init__(self, root: tk.Misc, title: str, message: str,
                 danger: bool = False) -> None:
        tk.Toplevel.__init__(self, root)
        self._init_theme()
        self.result: bool | None = None
        self._danger = bool(danger)
        self.title(title)
        try:
            self.transient(root)
        except Exception:
            pass
        self._build(title, message)
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(False))
        self.bind("<Escape>", lambda _e: self._finish(False))
        self.bind("<Return>", lambda _e: self._finish(True))
        try:   # 出现在 root 附近
            x = root.winfo_rootx() + (root.winfo_width() - self.winfo_reqwidth()) // 2
            y = root.winfo_rooty() + (root.winfo_height() - self.winfo_reqheight()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass
        self.deiconify()
        self.update_idletasks()
        try:
            self.grab_set()
        except Exception:
            pass
        self.focus_set()
        self.wait_window(self)

    def _build(self, title: str, message: str) -> None:
        t = self._tokens
        pad = scale.s(20)
        self.configure(bg=t.bg)
        box = tk.Frame(self, bg=t.bg)
        box.pack(fill="both", expand=True, padx=pad, pady=pad)
        tk.Label(box, text=title, bg=t.bg, fg=t.text,
                 font=scale.font(14, weight="bold"), anchor="w"
                 ).pack(fill="x")
        tk.Label(box, text=message, bg=t.bg, fg=t.muted,
                 font=scale.font(12), wraplength=scale.s(340),
                 justify="left", anchor="w").pack(fill="x", pady=(scale.s(8), scale.s(16)))
        btns = tk.Frame(box, bg=t.bg)
        btns.pack(fill="x")
        self.btn_cancel = PillButton(btns, "取消",
                                     lambda: self._finish(False),
                                     kind="secondary")
        self.btn_cancel.pack(side="right", padx=(scale.s(8), 0))
        self.btn_ok = PillButton(btns, "确定", lambda: self._finish(True),
                                 kind="danger" if self._danger else "primary")
        self.btn_ok.pack(side="right")

    def _finish(self, value: bool) -> None:
        self.result = bool(value)
        self.destroy()

    def refresh(self) -> None:
        """对话框短生命周期，主题切换交给重建策略。"""


def confirm(root: tk.Misc, title: str, message: str,
            danger: bool = False) -> bool:
    """模态确认，返回 True/False。"""
    dlg = ConfirmDialog(root, title, message, danger=danger)
    return bool(dlg.result)
