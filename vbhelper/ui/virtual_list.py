# -*- coding: utf-8 -*-
"""虚拟列表（计划 01 §3.6）：只渲染视口内行的高性能列表。

单一真相 = 绝对像素偏移 + 行高前缀和 + 二分定位，100k 行也只创建
视口内几十个 Canvas item。滚动条自绘：细、内容不满一屏自动隐藏、
可点击跳转/拖动，不用 ttk 默认粗滚动条。

render_row 回调签名（绘制完全交给回调，组件只提供状态信息）::

    render_row(canvas, row, index, x, y, width, height, state)

    canvas : tk.Canvas   在它上面自由 create_*
    row    : 当前行数据
    index  : 行号
    x, y   : 行左上角（x 恒为 0，y 为视口内坐标）
    width  : 可用宽度（已扣除滚动条区域）
    height : 行高
    state  : {"selected": bool, "hover": bool, "theme": ThemeManager}

hover / selected 只是状态信息，怎么画由回调自己决定；主题色通过
state["theme"].color(...) / .font(...) 取。

用法（需先 scale.init(root)，theme 为 ThemeManager）::

    from vbhelper.ui import scale, virtual_list

    vl = virtual_list.VirtualList(
        parent, theme_manager,
        row_height=40,                        # 或 lambda row, i: 40
        render_row=draw_row,
        on_activate=lambda row, i: play(i),    # 双击/回车
        empty_text="暂无数据")
    vl.pack(fill="both", expand=True)
    vl.set_rows(rows, selected=0)
    vl.set_empty("加载失败", action_text="重试", action=retry)

其他约定：

- row_height 为 callable(row, index) 时行高可变；其返回值随外部状态
  变化后，宿主须调用 refresh_heights() 重算前缀和并重绘。
- 空态/加载态/错误态共用 set_empty 插拔：加载传 "正在加载…"（不带
  action），错误可带 action_text/action 做重试按钮。
- visible_range() 返回视口行区间 (first, last+1)，宿主可借它懒加载。
- 键盘：上下移动选中、Home/End 跳首尾、PgUp/PgDn 翻页、回车激活；
  鼠标：单击选中、双击激活、右键 on_context(event, index, row)。
- 滚轮一格滚约 3 行（不是 Windows 默认的 1/3 行）。
"""
import bisect
import tkinter as tk
from collections.abc import Callable, Iterable

from . import scale


class VirtualList(tk.Frame):
    """虚拟化列表组件：视口渲染 + 可变行高前缀和 + 自绘滚动条。"""

    def __init__(self, parent, theme, *,
                 row_height: int | Callable[[object, int], int] = 40,
                 render_row: Callable[..., None] = None,
                 on_activate: Callable[[object, int], None] | None = None,
                 on_context: Callable[..., object, int, object] | None = None,
                 empty_text: str = "暂无数据") -> None:
        tk.Frame.__init__(self, parent, bg=theme.color("bg"))
        self.theme = theme
        self._render_row = render_row
        self._on_activate = on_activate
        self._on_context = on_context
        self._rh_spec = row_height
        self._variable = callable(row_height)
        self._fixed_h = int(row_height) if not self._variable else 40
        self._rows: list = []
        self._heights: list[int] = []       # 可变行高时的每行高度
        self._prefix: list[int] = [0]       # 前缀和：prefix[i] = 第 i 行顶部 y
        self._total = 0                     # 内容总高
        self._scroll = 0.0                  # 绝对像素偏移（单一真相）
        self._sel = -1
        self._hover = -1
        self._row_items: dict[int, tuple[int, int]] = {}   # 视口内 index -> (首id, 末id)
        self._empty = {"text": empty_text, "action_text": None,
                       "action": None}
        self._bar_drag = False
        self._bar_grab = 0.0
        self._bar_w = scale.s(4)            # 细滚动条
        self._bar_pad = scale.s(8)

        c = self._canvas = tk.Canvas(
            self, bg=theme.color("bg"), highlightthickness=0, bd=0,
            takefocus=1)
        c.pack(fill="both", expand=True)

        c.bind("<Configure>", lambda e: self._redraw())
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self._scroll_by(-self._step_h() * 3))
        c.bind("<Button-5>", lambda e: self._scroll_by(self._step_h() * 3))
        c.bind("<Button-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", self._activate)
        c.bind("<Motion>", self._hover_move)
        c.bind("<Leave>", self._hover_leave)
        c.bind("<Button-3>", self._context)
        c.bind("<Key-Up>", self._key_move_up)
        c.bind("<Key-Down>", self._key_move_down)
        c.bind("<Home>", self._key_home)
        c.bind("<End>", self._key_end)
        c.bind("<Prior>", self._key_page_up)
        c.bind("<Next>", self._key_page_down)
        c.bind("<Return>", self._key_return)

        self._unsub = theme.subscribe(lambda p, t: self._apply_theme())
        self.bind("<Destroy>", self._on_destroy)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def set_rows(self, rows: Iterable, *, keep_scroll: bool = False,
                 selected: int = -1) -> None:
        """整体替换数据。keep_scroll=True 保留当前滚动偏移；selected 为
        新选中行（-1 表示清空选中）。"""
        self._rows = list(rows) if rows else []
        self._rebuild_heights()
        if not keep_scroll:
            self._scroll = 0.0
        if selected >= 0:
            self._sel = min(selected, len(self._rows) - 1) if self._rows else -1
        else:
            self._sel = -1
        self._hover = -1
        self._clamp()
        self._redraw()

    def refresh(self) -> None:
        """整帧重绘（视口内行全部重画）。"""
        self._redraw()

    def refresh_row(self, index: int) -> None:
        """只重绘目标行；视口外的行忽略（整帧重绘时自然会画）。"""
        if not (0 <= index < len(self._rows)):
            return
        if index not in self._row_items:
            return
        first, last = self._row_items.pop(index)
        if first <= last:
            self._canvas.delete(*range(first, last + 1))
        y = int(round(self._y_at(index) - self._scroll))
        self._draw_row(index, y, self._content_w(), self._h_at(index))

    def refresh_heights(self) -> None:
        """row_height 为 callable 且返回值随外部状态变化后，宿主调用
        本方法重算行高前缀和（内部会钳制偏移并重绘）。"""
        if not self._variable:
            return
        self._rebuild_heights()
        self._clamp()
        self._redraw()

    def scroll_to(self, index: int) -> None:
        """滚动到目标行（尽量居中，首尾自动钳制到边界）。"""
        if not (0 <= index < len(self._rows)):
            return
        y, h = self._y_at(index), self._h_at(index)
        self._scroll_to(y - (self._view_h() - h) / 2.0)

    def selection(self) -> int:
        """当前选中行 index；无选中返回 -1。"""
        return self._sel

    def set_empty(self, text: str, action_text: str | None = None,
                  action: Callable[[], None] | None = None) -> None:
        """配置空/加载/错误状态文案与可选动作按钮（rows 为空时显示）。"""
        self._empty = {"text": text, "action_text": action_text,
                       "action": action}
        if not self._rows:
            self._redraw()

    def visible_range(self) -> tuple[int, int]:
        """当前视口覆盖的行区间 (first, last+1)；无数据返回 (0, 0)。"""
        if not self._rows:
            return (0, 0)
        first = self._index_at_y(self._scroll)
        if first < 0:
            return (0, 0)
        view = self._view_h()
        last, y = first, self._y_at(first)
        while last < len(self._rows) and y < self._scroll + view:
            y += self._h_at(last)
            last += 1
        return (first, last)

    # ------------------------------------------------------------------
    # 几何：前缀和 + 二分定位
    # ------------------------------------------------------------------

    def _view_w(self) -> int:
        return max(1, self._canvas.winfo_width())

    def _view_h(self) -> int:
        return max(1, self._canvas.winfo_height())

    def _content_w(self) -> int:
        # 可用宽度恒扣滚动条区域，避免滚动条出现/消失时行内容回流
        return max(10, self._view_w() - self._bar_w - self._bar_pad)

    def _h_at(self, i: int) -> int:
        return self._heights[i] if self._variable else self._fixed_h

    def _y_at(self, i: int) -> int:
        return self._prefix[i] if self._variable else i * self._fixed_h

    def _index_at_y(self, y: float) -> int:
        """绝对像素 y 所在行号（二分；固定行高走整除快路径）。"""
        n = len(self._rows)
        if n == 0:
            return -1
        if y < 0:
            return 0
        if self._variable:
            return min(n - 1, bisect.bisect_right(self._prefix, y) - 1)
        return min(n - 1, int(y // max(1, self._fixed_h)))

    def _step_h(self) -> int:
        """滚轮/翻页的单步高度：取视口首行行高（固定行高即常数）。"""
        if not self._variable:
            return max(1, self._fixed_h)
        i = self._index_at_y(self._scroll)
        return max(1, self._heights[i]) if 0 <= i < len(self._heights) else 40

    def _max_scroll(self) -> float:
        return max(0.0, self._total - self._view_h())

    def _clamp(self) -> None:
        self._scroll = max(0.0, min(self._scroll, self._max_scroll()))

    def _rebuild_heights(self) -> None:
        """重算每行高度与前缀和（set_rows / refresh_heights 共用）。"""
        if not self._variable:
            self._total = len(self._rows) * self._fixed_h
            return
        rh = self._rh_spec
        hs = [max(1, int(rh(r, i))) for i, r in enumerate(self._rows)]
        pre = [0] * (len(hs) + 1)
        acc = 0
        for i, h in enumerate(hs):
            acc += h
            pre[i + 1] = acc
        self._heights = hs
        self._prefix = pre
        self._total = acc

    def _index_at_pointer(self, py: float) -> int:
        if not self._rows:
            return -1
        return self._index_at_y(self._scroll + max(0, py))

    # ------------------------------------------------------------------
    # 滚动
    # ------------------------------------------------------------------

    def _scroll_to(self, value: float) -> None:
        self._scroll = value
        self._clamp()
        self._redraw()

    def _scroll_by(self, delta: float) -> None:
        self._scroll_to(self._scroll + delta)

    def _ensure_visible(self, index: int) -> None:
        if not (0 <= index < len(self._rows)):
            return
        y, h = self._y_at(index), self._h_at(index)
        view = self._view_h()
        if y < self._scroll:
            self._scroll_to(float(y))
        elif y + h > self._scroll + view:
            self._scroll_to(float(y + h - view))

    def _wheel(self, e) -> None:
        # 一格滚约 3 行，覆盖 Windows 默认 1/3 行的手感
        self._scroll_by(-(e.delta / 120.0) * self._step_h() * 3)
        return "break"

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def _redraw(self) -> None:
        if not self.winfo_exists():
            return
        c = self._canvas
        c.delete("all")
        self._row_items.clear()
        if not self._rows:
            self._draw_empty()
            return
        w = self._content_w()
        view = self._view_h()
        i = self._index_at_y(self._scroll)
        if i < 0:
            return
        y = self._y_at(i) - self._scroll
        n = len(self._rows)
        while i < n and y < view:
            h = self._h_at(i)
            if y + h > 0:
                self._draw_row(i, int(round(y)), w, h)
            y += h
            i += 1
        self._draw_bar()

    def _draw_row(self, index: int, y: int, w: int, h: int) -> None:
        """画一行并记录其 item id 区间（供 refresh_row 定点重绘）。"""
        c = self._canvas
        state = {"selected": index == self._sel,
                 "hover": index == self._hover,
                 "theme": self.theme}
        items = c.find_all()
        first = (max(items) + 1) if items else 1
        if callable(self._render_row):
            try:
                self._render_row(c, self._rows[index], index, 0, y, w, h,
                                 state)
            except Exception:
                pass   # 单行绘制出错不拖垮整帧
        items = c.find_all()
        last = max(items) if items else 0
        self._row_items[index] = (first, last)
        # 定点重绘的新 item 压到滚动条之下，避免遮挡滚动条
        bar = c.find_withtag("bar")
        if bar and first <= last:
            anchor = min(bar)
            for iid in range(last, first - 1, -1):
                try:
                    c.tag_lower(iid, anchor)
                except Exception:
                    break

    def _draw_bar(self) -> None:
        if not self._bar_visible():
            return
        pos, knob, _room = self._bar_geom()
        x = self._view_w() - self._bar_w
        self._canvas.create_rectangle(
            x, pos + 2, x + self._bar_w - 1, pos + knob + 2,
            fill=self.theme.color("muted"), outline="", tags="bar")

    def _bar_visible(self) -> bool:
        return bool(self._rows) and self._total > self._view_h()

    def _bar_geom(self) -> tuple[float, float, float]:
        """滚动条 knob 的 (顶部位置, 高度, 可移动行程)。"""
        view = self._view_h()
        knob = max(scale.s(24), view * view / max(1.0, self._total))
        room = max(1.0, view - knob - 4)
        m = self._max_scroll()
        pos = (self._scroll / m) * room if m > 0 else 0.0
        return pos, knob, room

    def _draw_empty(self) -> None:
        c = self._canvas
        w, h = self._view_w(), self._view_h()
        cfg = self._empty
        c.create_text(w / 2, h / 2 - scale.s(20), text=cfg["text"] or "",
                      fill=self.theme.color("muted"),
                      font=self.theme.font(13), tags="empty")
        if cfg["action_text"] and callable(cfg["action"]):
            bw, bh = scale.s(96), scale.s(30)
            x0, y0 = (w - bw) / 2, h / 2 + scale.s(4)
            c.create_rectangle(x0, y0, x0 + bw, y0 + bh,
                               fill=self.theme.color("accent"),
                               outline=self.theme.color("accent_hover"),
                               tags=("empty", "empty_action"))
            c.create_text(x0 + bw / 2, y0 + bh / 2, text=cfg["action_text"],
                          fill=self.theme.color("bg"),
                          font=self.theme.font(12, weight="bold"),
                          tags=("empty", "empty_action"))
            c.tag_bind("empty_action", "<Enter>",
                       lambda e: c.configure(cursor="hand2"))
            c.tag_bind("empty_action", "<Leave>",
                       lambda e: c.configure(cursor=""))
            c.tag_bind("empty_action", "<Button-1>", self._empty_clicked)

    def _empty_clicked(self, _e=None) -> None:
        action = self._empty.get("action")
        if callable(action):
            action()

    # ------------------------------------------------------------------
    # 鼠标交互
    # ------------------------------------------------------------------

    def _bar_hit(self, x: float, y: float) -> bool:
        return (self._bar_visible() and
                x >= self._view_w() - self._bar_w - scale.s(4))

    def _press(self, e) -> None:
        try:
            self._canvas.focus_set()
        except Exception:
            pass
        if self._bar_hit(e.x, e.y):
            self._bar_drag = True
            pos, knob, _room = self._bar_geom()
            if pos + 2 <= e.y <= pos + knob + 2:
                self._bar_grab = e.y - (pos + 2)
            else:
                self._bar_grab = knob / 2   # 点轨道：knob 居中跳到指针
                self._bar_jump(e.y - self._bar_grab)
            return "break"
        idx = self._index_at_pointer(e.y)
        if idx >= 0:
            self._select(idx)

    def _select(self, idx: int) -> None:
        old = self._sel
        self._sel = idx
        if old != idx:
            if old in self._row_items:
                self.refresh_row(old)
            if idx in self._row_items:
                self.refresh_row(idx)

    def _motion(self, e) -> None:
        if self._bar_drag:
            self._bar_jump(e.y - self._bar_grab)

    def _release(self, _e) -> None:
        self._bar_drag = False

    def _bar_jump(self, y: float) -> None:
        _pos, knob, room = self._bar_geom()
        ratio = max(0.0, min(1.0, (y - 2) / room))
        self._scroll_to(ratio * self._max_scroll())

    def _hover_move(self, e) -> None:
        if self._bar_drag:
            return
        idx = self._index_at_pointer(e.y)
        if idx != self._hover:
            old = self._hover
            self._hover = idx
            if old in self._row_items:
                self.refresh_row(old)
            if idx in self._row_items:
                self.refresh_row(idx)

    def _hover_leave(self, _e) -> None:
        if self._hover != -1:
            old = self._hover
            self._hover = -1
            if old in self._row_items:
                self.refresh_row(old)

    def _activate(self, e) -> None:
        idx = self._index_at_pointer(e.y)
        if idx >= 0 and callable(self._on_activate):
            self._on_activate(self._rows[idx], idx)

    def _context(self, e) -> None:
        idx = self._index_at_pointer(e.y)
        if idx >= 0 and callable(self._on_context):
            self._select(idx)
            self._on_context(e, idx, self._rows[idx])

    # ------------------------------------------------------------------
    # 键盘交互
    # ------------------------------------------------------------------

    def _key_move(self, sign: int):
        n = len(self._rows)
        if not n:
            return "break"
        old = self._sel
        self._sel = 0 if self._sel < 0 else max(0, min(self._sel + sign,
                                                       n - 1))
        self._ensure_visible(self._sel)
        if old != self._sel:
            if old in self._row_items:
                self.refresh_row(old)
            if self._sel in self._row_items:
                self.refresh_row(self._sel)
        return "break"

    def _key_move_up(self, _e):
        return self._key_move(-1)

    def _key_move_down(self, _e):
        return self._key_move(1)

    def _key_home(self, _e):
        if not self._rows:
            return "break"
        self._sel = 0
        self._scroll_to(0.0)
        return "break"

    def _key_end(self, _e):
        if not self._rows:
            return "break"
        self._sel = len(self._rows) - 1
        self._scroll_to(self._max_scroll())
        return "break"

    def _key_page(self, sign: int):
        n = len(self._rows)
        if not n:
            return "break"
        view = self._view_h()
        step = self._step_h()
        page = max(1, int(view // max(1, step)))
        old = self._sel
        self._sel = 0 if self._sel < 0 else max(
            0, min(self._sel + sign * page, n - 1))
        self._scroll_by(sign * max(step, view - step))   # 翻页留一行重叠
        self._ensure_visible(self._sel)
        if old in self._row_items and old != self._sel:
            self.refresh_row(old)
        return "break"

    def _key_page_up(self, _e):
        return self._key_page(-1)

    def _key_page_down(self, _e):
        return self._key_page(1)

    def _key_return(self, _e):
        if (0 <= self._sel < len(self._rows)
                and callable(self._on_activate)):
            self._on_activate(self._rows[self._sel], self._sel)
        return "break"

    # ------------------------------------------------------------------
    # 主题与生命周期
    # ------------------------------------------------------------------

    def _apply_theme(self) -> None:
        if not self.winfo_exists():
            return
        try:
            bg = self.theme.color("bg")
            self.configure(bg=bg)
            self._canvas.configure(bg=bg)
        except Exception:
            pass
        self._redraw()

    def _on_destroy(self, e) -> None:
        if e.widget is self and self._unsub:
            try:
                self._unsub()
            except Exception:
                pass
            self._unsub = None
