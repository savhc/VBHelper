"""依赖医生窗口。

三个页面共用一条扫描流水线：

- **扫描索引**：边扫边长出来的目录树（目录/清单/依赖数/状态），可展开、
  可折叠、可复制，用来看清「这次到底扫了哪些文件」；
- **检测结果**：统计 + 生态/状态筛选 + 搜索 + 虚拟表格 + 右侧详情
  （漏洞、升级命令、复制 AI 提示词）；
- **报告**：完整 Markdown 报告（结论/待处理项/全量表格/清单索引），
  可导出 md / json。

扫描跑在 TaskRunner 后台线程，进度通过队列 + ``after`` 回主线程；
关窗或点「取消」置停止位，后台在下一个检查点收工，不阻塞退出。

窗口视觉约定（与主面板一致，别用 ttk 默认外观）：
页签是自绘文字页签（主色下划线），进度是 3px 细线，复选框开关用
``widgets.ToggleSwitch``——ttk 的 Checkbutton/Notebook 在 clam 下会露出
白底方块和 Win7 灰框。

"""
from __future__ import annotations

import os
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ... import core
from ...ui import scale, widgets
from ...ui.scroll import bind_canvas_wheel
from ...ui.virtual_list import VirtualList
from ...ui.window import FeatureWindow
from . import parsers, report, versions

# 结果表的列宽比例：名称 / 当前 / 最新 / 状态 / 来源
_COLS = (0.34, 0.13, 0.13, 0.12, 0.28)

_STATUS_TONE = {
    "ok": "success",
    "outdated": "warning",
    "major": "warning",
    "vulnerable": "danger",
    "unknown": "neutral",
}
_STATUS_COLOR = {"vulnerable": "danger", "major": "warning",
                 "outdated": "warning", "ok": "success",
                 "unknown": "dim"}
_FILE_STATE = {"parsed": "已解析", "empty": "无依赖",
               "warn": "有备注", "error": "失败"}
_STATUS_FILTER = {"漏洞": "vulnerable", "大版本": "major",
                  "过期": "outdated", "未知": "unknown"}

# ---------------------------------------------------------------------------
# 自绘小件（ttk 在 clam 下太"系统灰"，与主题不搭）
# ---------------------------------------------------------------------------

class _TabBar(tk.Canvas):
    """扁平文字页签：选中项主色 2px 下划线，未选中 muted。"""

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
        index = max(0, min(index, len(self._items) - 1))
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
        index = self._index_at(event.x)
        if index >= 0:
            self.select(index)

    def _motion(self, event) -> None:
        self._set_hover(self._index_at(event.x))

    def _set_hover(self, index: int) -> None:
        if index != self._hover:
            self._hover = index
            self._draw()

class _SlimBar(tk.Canvas):
    """3px 进度线；ratio 为 None 时整条透明（只留占位）。"""

    def __init__(self, parent, tokens, *, height: int = 3) -> None:
        self._tokens = tokens
        self._ratio: float | None = None
        self._h = scale.s(height)
        tk.Canvas.__init__(self, parent, height=self._h, bd=0,
                           highlightthickness=0)
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def set_tokens(self, tokens) -> None:
        self._tokens = tokens
        self._draw()

    def set(self, ratio: float | None) -> None:
        if ratio is not None:
            ratio = max(0.0, min(1.0, float(ratio)))
        self._ratio = ratio
        self._draw()

    def redraw(self) -> None:
        self._draw()

    def _draw(self) -> None:
        t = self._tokens
        self.configure(bg=t.bg)
        self.delete("all")
        width = max(1, self.winfo_width())
        if self._ratio is None:
            return
        self.create_rectangle(0, 0, width, self._h,
                              fill=t.surface, outline="")
        self.create_rectangle(0, 0, width * self._ratio, self._h,
                              fill=t.accent, outline="")

# ---------------------------------------------------------------------------
# 结果表
# ---------------------------------------------------------------------------

class DepTable(VirtualList):
    """VirtualList + 选中变化回调（单击/键盘都要刷新右侧详情）。"""

    def __init__(self, parent, theme, *, on_select=None, **kwargs) -> None:
        super().__init__(parent, theme, **kwargs)
        self._on_select = on_select
        self._last_sel = -1
        canvas = self._canvas
        canvas.bind("<Button-1>", self._after_click, add="+")
        canvas.bind("<KeyRelease>", self._after_key, add="+")

    def _after_click(self, _event) -> None:
        self.after_idle(self._emit)

    def _after_key(self, event) -> None:
        if event.keysym in ("Up", "Down", "Home", "End", "Prior", "Next"):
            self._emit()

    def _emit(self) -> None:
        if not self.winfo_exists():
            return
        index = self.selection()
        if index == self._last_sel:
            return
        self._last_sel = index
        if callable(self._on_select):
            try:
                self._on_select(index)
            except Exception:
                pass

    def set_rows(self, rows, *, keep_scroll: bool = False,
                 selected: int = -1) -> None:
        super().set_rows(rows, keep_scroll=keep_scroll, selected=selected)
        self._last_sel = self.selection()

# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class DepsWindow(FeatureWindow):
    """依赖医生主窗口。"""

    def __init__(self, ctx, root_dir: str) -> None:
        self._root_dir = os.path.abspath(root_dir)
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._poll_job = None
        self._scanning = False
        self._parse_total = 0
        self._parse_done = 0
        self._resolve_total = 0
        self._resolve_done = 0
        self._result = None
        self._deps: list = []
        self._rows: list = []
        self._filter_eco = "全部"
        self._filter_status = "全部"
        self._filter_text = ""
        self._dir_nodes: dict[str, str] = {}
        self._dir_counts: dict[str, int] = {}
        self._file_nodes = 0
        self._file_paths: set[str] = set()
        self._tree_root = ""
        self._include_transitive = False
        self._f_row = scale.font(11)
        self._f_small = scale.font(10)
        self._f_head = scale.font(10)
        self._f_num = scale.font(12, weight="bold")
        super().__init__(ctx, "dependency_doctor", "依赖医生",
                         (920, 620), min_size=(780, 500),
                         subtitle="只读扫描 · 版本 / 漏洞 / 升级建议")
        self._build_body()
        self.apply_theme()
        self._update_path()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    def _build_body(self) -> None:
        body = self.content()
        self._body = body
        t = self._tokens
        pad = scale.s(10)

        # 头部右侧：动作按钮（选择/重扫/取消），紧凑排一行
        acts = self.actions()
        self._btn_cancel = widgets.PillButton(acts, "取消", self._cancel_scan,
                                              kind="ghost")
        self._btn_cancel.pack(side="right", padx=(scale.s(6), 0))
        self._btn_rescan = widgets.PillButton(acts, "重新扫描", self.start_scan,
                                              kind="secondary")
        self._btn_rescan.pack(side="right", padx=(scale.s(6), 0))
        self._btn_pick = widgets.PillButton(acts, "选择文件夹",
                                            self._pick_dir, kind="secondary")
        self._btn_pick.pack(side="right")
        self._btn_cancel.set_enabled(False)

        # 路径行：文件夹图标 + 路径 + 右侧「核对间接依赖」开关
        path_row = tk.Frame(body)
        path_row.pack(fill="x")
        self._btn_folder = widgets.IconButton(
            path_row, "folder", self._pick_dir, tooltip="选择项目文件夹",
            size=26)
        self._btn_folder.pack(side="left")
        self._path_lbl = tk.Label(path_row, text="", anchor="w",
                                  font=scale.font(11))
        self._path_lbl.pack(side="left", fill="x", expand=True,
                            padx=(scale.s(6), scale.s(10)))
        self._toggle = widgets.ToggleSwitch(
            path_row, False, self._on_toggle_transitive)
        self._toggle.pack(side="right")
        self._toggle_lbl = tk.Label(path_row, text="核对间接依赖", anchor="e",
                                    font=scale.font(10))
        self._toggle_lbl.pack(side="right", padx=(0, scale.s(8)))

        # 进度：一行状态文字 + 3px 细线（不扫描时线是空的，只占位）
        prog_row = tk.Frame(body)
        prog_row.pack(fill="x", pady=(pad, 0))
        self._prog_lbl = tk.Label(prog_row, text="选择项目文件夹后开始扫描",
                                  anchor="w", font=scale.font(10))
        self._prog_lbl.pack(side="left", fill="x", expand=True)
        self._pct_lbl = tk.Label(prog_row, text="", anchor="e",
                                 font=scale.font(10))
        self._pct_lbl.pack(side="right")
        self._bar = _SlimBar(body, t)
        self._bar.pack(fill="x", pady=(scale.s(3), 0))

        # 自绘页签 + 页面容器
        self._tabs = _TabBar(body, ("扫描索引", "检测结果", "报告"), t,
                             command=self._show_page)
        self._tabs.pack(fill="x", pady=(scale.s(8), 0))
        self._pages = tk.Frame(body)
        self._pages.pack(fill="both", expand=True)
        self._tab_index = tk.Frame(self._pages)
        self._tab_result = tk.Frame(self._pages)
        self._tab_report = tk.Frame(self._pages)
        self._page_frames = (self._tab_index, self._tab_result,
                             self._tab_report)

        self._build_index_tab()
        self._build_result_tab()
        self._build_report_tab()
        self._show_page(0, fire=False)

    def _show_page(self, index: int, fire: bool = True) -> None:
        if fire:
            self._tabs.select(index)
        for i, frame in enumerate(self._page_frames):
            if i == index:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

    def _build_index_tab(self) -> None:
        bar = tk.Frame(self._tab_index)
        bar.pack(fill="x", pady=(scale.s(6), scale.s(4)))
        widgets.PillButton(bar, "展开全部", self._expand_all,
                           kind="ghost").pack(side="left", padx=(0, scale.s(2)))
        widgets.PillButton(bar, "折叠全部", self._collapse_all,
                           kind="ghost").pack(side="left",
                                              padx=(0, scale.s(2)))
        widgets.PillButton(bar, "复制索引", self._copy_index,
                           kind="ghost").pack(side="left")
        self._index_hint = tk.Label(bar, text="", anchor="e",
                                    font=scale.font(10))
        self._index_hint.pack(side="right")

        wrap = tk.Frame(self._tab_index)
        wrap.pack(fill="both", expand=True)
        self._tree = ttk.Treeview(
            wrap, columns=("kind", "count", "state"), show="tree headings",
            style="Deps.Treeview", selectmode="browse")
        self._tree.heading("#0", text="目录 / 清单", anchor="w")
        self._tree.heading("kind", text="类型", anchor="w")
        self._tree.heading("count", text="依赖", anchor="e")
        self._tree.heading("state", text="状态", anchor="w")
        # 列宽固定、整体靠左：目录树层级浅，拉伸只会在中间留出大空洞
        self._tree.column("#0", width=scale.s(430), minwidth=scale.s(180),
                          stretch=False)
        self._tree.column("kind", width=scale.s(180), minwidth=scale.s(96),
                          stretch=False)
        self._tree.column("count", width=scale.s(56), minwidth=scale.s(46),
                          stretch=False, anchor="e")
        self._tree.column("state", width=scale.s(70), minwidth=scale.s(56),
                          stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                            command=self._tree.yview,
                            style="VB.Vertical.TScrollbar")
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)

    def _build_result_tab(self) -> None:
        filt = tk.Frame(self._tab_result)
        filt.pack(fill="x", pady=(scale.s(6), scale.s(6)))
        self._seg_eco = widgets.SegmentedControl(
            filt, ["全部", "Python", "Node", "Java", "Go"], "全部",
            self._on_filter_eco)
        self._seg_eco.pack(side="left")
        self._seg_status = widgets.SegmentedControl(
            filt, ["全部", "漏洞", "大版本", "过期", "未知"], "全部",
            self._on_filter_status)
        self._seg_status.pack(side="left", padx=(scale.s(6), 0))
        self._search = widgets.SearchEntry(filt, "搜索包名 / 文件…",
                                           self._on_search)
        self._search.configure(width=scale.s(230), height=scale.s(32))
        self._search.pack_propagate(False)
        self._search.pack(side="right")

        self._stats = tk.Frame(self._tab_result)
        self._stats.pack(fill="x", pady=(0, scale.s(2)))

        self._sep = tk.Frame(self._tab_result, height=1)
        self._sep.pack(fill="x", pady=(scale.s(6), 0))

        main = tk.Frame(self._tab_result)
        main.pack(fill="both", expand=True)
        self._vsep = tk.Frame(main, width=1)
        self._detail = tk.Frame(main, width=scale.s(300))
        self._detail.pack(side="right", fill="y", padx=(scale.s(12), 0))
        self._vsep.pack(side="right", fill="y")
        self._detail.pack_propagate(False)
        # 信息区可滚动，操作按钮固定在底部
        self._detail_actions = tk.Frame(self._detail)
        self._detail_actions.pack(side="bottom", fill="x",
                                  pady=(scale.s(8), 0))
        canvas = self._detail_canvas = tk.Canvas(
            self._detail, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(self._detail, orient="vertical",
                            command=canvas.yview,
                            style="VB.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._detail_inner = tk.Frame(canvas)
        self._detail_item = canvas.create_window(
            (0, 0), window=self._detail_inner, anchor="nw")
        self._detail_inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(self._detail_item, width=e.width))
        bind_canvas_wheel(self, canvas, area=canvas, units=2)

        table_wrap = tk.Frame(main)
        table_wrap.pack(side="left", fill="both", expand=True)
        self._thead = tk.Canvas(table_wrap, height=scale.s(24),
                                highlightthickness=0, bd=0)
        self._thead.pack(fill="x")
        self._thead.bind("<Configure>", lambda _e: self._draw_thead())
        self._table = DepTable(
            table_wrap, self.ctx.theme, row_height=scale.s(26),
            render_row=self._draw_row, on_select=self._on_row_select,
            empty_text="还没有扫描结果")
        self._table.pack(fill="both", expand=True)

    def _build_report_tab(self) -> None:
        bar = tk.Frame(self._tab_report)
        bar.pack(fill="x", pady=(scale.s(6), scale.s(4)))
        widgets.PillButton(bar, "导出 Markdown", lambda: self._export("md"),
                           kind="primary").pack(side="right")
        widgets.PillButton(bar, "导出 JSON", lambda: self._export("json"),
                           kind="secondary").pack(side="right",
                                                 padx=(0, scale.s(6)))
        widgets.PillButton(bar, "复制报告", self._copy_report,
                           kind="ghost").pack(side="right",
                                              padx=(0, scale.s(6)))
        self._report_holder = tk.Frame(self._tab_report)
        self._report_holder.pack(fill="both", expand=True)
        self._render_report("")

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------

    def apply_theme(self) -> None:
        super().apply_theme()
        if not hasattr(self, "_body"):     # 外壳首次装配时业务控件还没建
            return
        t = self._tokens
        style = ttk.Style(self)
        try:
            self.ctx.theme.apply_ttk(style)
        except Exception:
            pass
        try:
            style.configure("Deps.Treeview", background=t.bg,
                            fieldbackground=t.bg, foreground=t.text,
                            borderwidth=0, relief="flat", rowheight=scale.s(22),
                            bordercolor=t.bg, lightcolor=t.bg,
                            darkcolor=t.bg, font=scale.font(10))
            style.configure("Deps.Treeview.Heading", background=t.bg,
                            foreground=t.dim, relief="flat",
                            font=scale.font(10), padding=(scale.s(4), scale.s(2)))
            style.map("Deps.Treeview",
                      background=[("selected", t.selected)],
                      foreground=[("selected", t.text)])
        except Exception:
            pass

        # 容器与普通标签先统一底色，_Themed 控件再按新父级底色重绘
        self._recolor(self._body, t)
        self._refresh_themed(self._body, t)
        self._tabs.set_tokens(t)
        self._bar.set_tokens(t)
        self._bar.redraw()

        for label in (self._path_lbl, self._prog_lbl, self._pct_lbl,
                      self._index_hint, self._toggle_lbl):
            try:
                label.configure(bg=t.bg, fg=t.dim)
            except Exception:
                pass
        try:
            self._path_lbl.configure(fg=t.text)
            self._sep.configure(bg=t.border)
            self._vsep.configure(bg=t.border)
            self._thead.configure(bg=t.bg)
        except Exception:
            pass
        try:
            self._tree.tag_configure("dir", foreground=t.muted)
            self._tree.tag_configure("parsed", foreground=t.text)
            self._tree.tag_configure("empty", foreground=t.dim)
            self._tree.tag_configure("warn", foreground=t.warning)
            self._tree.tag_configure("error", foreground=t.danger)
        except Exception:
            pass
        self._draw_thead()
        self._update_stats(self._result)
        if self._rows:
            self._show_detail(self._rows[max(0, self._table.selection())])
        self._render_report(
            report.build_markdown(self._result) if self._result else "")

    def _recolor(self, widget, t) -> None:
        """把 tk 容器/标签刷成主题底色（ttk 控件由样式负责，跳过）。"""
        kind = widget.winfo_class()
        try:
            if kind in ("Frame", "Label", "Canvas"):
                widget.configure(bg=t.bg)
            elif kind == "Text":
                widget.configure(bg=t.bg, fg=t.text,
                                 insertbackground=t.text)
            elif kind == "Entry":
                widget.configure(bg=t.surface, fg=t.text,
                                 insertbackground=t.text)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._recolor(child, t)

    def _refresh_themed(self, widget, t) -> None:
        """让 _Themed 控件（按钮/徽标/搜索框/开关）按新父级底色重绘。"""
        for child in widget.winfo_children():
            fn = getattr(child, "apply_theme", None)
            if callable(fn) and not isinstance(child, tk.Toplevel):
                try:
                    fn(t)
                except Exception:
                    pass
            self._refresh_themed(child, t)

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    @property
    def scanning(self) -> bool:
        return self._scanning

    def set_root(self, path: str) -> None:
        self._root_dir = os.path.abspath(path)
        self._update_path()
        self._remember_dir()

    def _update_path(self) -> None:
        self._path_lbl.configure(text=self._root_dir)

    def _remember_dir(self) -> None:
        try:
            self.ctx.cfg.setdefault("deps", {})["last_dir"] = self._root_dir
            self.ctx.save_cfg()
        except Exception:
            pass

    def _on_toggle_transitive(self, value: bool) -> None:
        self._include_transitive = bool(value)

    def start_scan(self) -> None:
        """开始（或重开）一次扫描。"""
        if not os.path.isdir(self._root_dir):
            messagebox.showwarning("依赖医生", "目录不存在：%s" % self._root_dir,
                                   parent=self)
            return
        if self._scanning:
            self._cancel_scan()
        self._stop = threading.Event()
        self._queue = queue.Queue()
        self._scanning = True
        self._parse_total = self._parse_done = 0
        self._resolve_total = self._resolve_done = 0
        self._result = None
        self._deps = []
        self._rows = []
        self._include_transitive = bool(self._toggle.get())
        self._reset_index()
        self._table.set_rows([])
        self._table.set_empty("正在扫描…")
        self._clear_detail()
        self._update_stats(None)
        self._render_report("")
        self._bar.set(0.0)
        self._pct_lbl.configure(text="0%")
        self._prog_lbl.configure(text="正在浏览目录…", fg=self._tokens.muted)
        self._btn_cancel.set_enabled(True)
        self.set_status("正在扫描 %s" % self._root_dir)
        self._remember_dir()
        self.ctx.tasks.submit("deps:scan", self._work,
                              group=self.feature_id,
                              on_done=self._on_scan_done,
                              on_error=self._on_scan_error)
        self._schedule_poll()

    def _work(self):
        """后台线程：跑 Scanner，进度塞队列。"""
        from .registry import RemoteRegistry
        from .scanner import Scanner
        registry = RemoteRegistry(self.ctx.http, cache=self.ctx.cache)
        scanner = Scanner(
            self._root_dir, registry=registry,
            include_transitive=bool(self._include_transitive),
            stop=self._stop.is_set)
        return scanner.run(progress=self._queue.put)

    def _cancel_scan(self) -> None:
        if not self._scanning:
            return
        self._stop.set()
        self._scanning = False
        try:
            self.ctx.tasks.cancel("deps:scan")
        except Exception:
            pass
        self._bar.set(None)
        self._pct_lbl.configure(text="")
        self._prog_lbl.configure(text="已取消", fg=self._tokens.dim)
        self._btn_cancel.set_enabled(False)
        self._table.set_empty("扫描已取消", action_text="重新扫描",
                              action=self.start_scan)
        self.set_status("已取消")

    def _schedule_poll(self) -> None:
        if self._poll_job is None:
            try:
                self._poll_job = self.after(80, self._poll)
            except Exception:
                self._poll_job = None

    def _poll(self) -> None:
        self._poll_job = None
        drained = self._drain()
        if self._scanning or drained:
            self._schedule_poll()

    def _drain(self) -> bool:
        """把队列里积压的事件全部处理掉，返回是否处理过。"""
        drained = False
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            drained = True
            if kind == "done":
                continue
            try:
                self._handle_event(kind, payload)
            except Exception:
                pass
        return drained

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "walk":
            self._prog_lbl.configure(
                text="正在浏览目录 · 已浏览 %d 个" % payload.get("dirs", 0))
        elif kind == "manifests":
            self._parse_total = max(1, int(payload.get("count") or 0))
            self._parse_done = 0
            self._bar.set(0.0)
            self._prog_lbl.configure(
                text="发现 %d 个清单，开始解析" % payload.get("count", 0))
        elif kind == "file":
            self._parse_done += 1
            self._tree_add(payload)
            ratio = min(0.55, self._parse_done / self._parse_total * 0.55)
            self._bar.set(ratio)
            self._pct_lbl.configure(text="%d%%" % int(ratio * 100))
            self._prog_lbl.configure(
                text="解析 %d/%d · %s · %d 个依赖"
                     % (self._parse_done, self._parse_total,
                        payload.path, payload.count))
        elif kind == "resolve_start":
            total = int(payload.get("total") or 0)
            if not total:
                self._bar.set(0.55)
                self._prog_lbl.configure(text="清单解析完成，无需联网核对")
                return
            self._resolve_total = total
            self._resolve_done = 0
            self._bar.set(0.55)
            self._pct_lbl.configure(text="55%")
            self._prog_lbl.configure(text="开始核对 %d 个包的最新版本" % total)
        elif kind == "resolve":
            self._resolve_done = int(payload.get("done") or 0)
            ratio = 0.55 + self._resolve_done / max(1, self._resolve_total) * 0.40
            self._bar.set(min(0.95, ratio))
            self._pct_lbl.configure(text="%d%%" % int(ratio * 100))
            self._prog_lbl.configure(
                text="核对 %d/%d · %s"
                     % (self._resolve_done, self._resolve_total,
                        payload.get("name", "")))
        elif kind == "phase":
            if payload.get("name") == "vulns":
                self._bar.set(0.95)
                self._pct_lbl.configure(text="95%")
                self._prog_lbl.configure(text="查询漏洞库（OSV）")

    def _on_scan_done(self, result) -> None:
        self._scanning = False
        self._drain()                 # 先把队列里剩下的事件处理掉
        self._result = result
        self._deps = list(result.deps)
        self._sync_index(result)
        self._btn_cancel.set_enabled(False)
        if getattr(result, "cancelled", False):
            self._bar.set(None)
            self._pct_lbl.configure(text="")
            self._prog_lbl.configure(text="已取消", fg=self._tokens.dim)
            self.set_status("扫描已取消")
            return
        self._bar.set(1.0)
        self._pct_lbl.configure(text="")
        self._prog_lbl.configure(
            text="完成 · %d 个清单 / %d 个依赖 · 核对 %d 个 · %.1fs"
                 % (len(result.files), len(result.deps), result.checked,
                    result.elapsed),
            fg=self._tokens.muted)
        self._index_hint.configure(
            text="浏览 %d 个目录 · %d 个清单" % (result.dirs_scanned,
                                             len(result.files)))
        self._apply_filter()
        self._update_stats(result)
        self._render_report(report.build_markdown(result))
        self.set_status("扫描完成")
        if result.deps:
            widgets.Toast(self.ctx.root,
                          "依赖扫描完成：%d 个依赖，%d 个待处理"
                          % (len(result.deps), self._todo_count(result)))

    def _on_scan_error(self, error) -> None:
        self._scanning = False
        self._bar.set(None)
        self._pct_lbl.configure(text="")
        self._prog_lbl.configure(text="扫描失败", fg=self._tokens.danger)
        self._btn_cancel.set_enabled(False)
        self._table.set_rows([])
        self._table.set_empty("扫描失败：%s" % error, action_text="重试",
                              action=self.start_scan)
        self.set_status("扫描失败：%s" % error)

    @staticmethod
    def _todo_count(result) -> int:
        return sum(1 for dep in result.deps
                   if dep.status in ("vulnerable", "major", "outdated"))

    # ------------------------------------------------------------------
    # 扫描索引
    # ------------------------------------------------------------------

    def _reset_index(self) -> None:
        self._tree.delete(*self._tree.get_children())
        self._dir_nodes.clear()
        self._dir_counts.clear()
        self._file_nodes = 0
        self._file_paths.clear()
        name = os.path.basename(os.path.normpath(self._root_dir)) \
            or self._root_dir
        self._tree_root = self._tree.insert(
            "", "end", text=name, values=("项目", "", ""), open=True,
            tags=("dir",))
        self._index_hint.configure(text="等待扫描")

    def _sync_index(self, result) -> None:
        """扫描收尾时按最终结果重建索引（事件里的计数是合并前的）。"""
        self._reset_index()
        for entry in result.files:
            self._tree_add(entry)

    def _tree_add(self, entry) -> None:
        if entry.path in self._file_paths:      # 重建后又收到迟到事件
            return
        self._file_paths.add(entry.path)
        parts = entry.path.split("/")
        parent = self._tree_root
        chain: list[str] = []
        for depth, part in enumerate(parts[:-1]):
            key = "/".join(parts[:depth + 1])
            node = self._dir_nodes.get(key)
            if node is None:
                node = self._tree.insert(parent, "end", text=part,
                                         values=("目录", "", ""),
                                         open=(depth == 0), tags=("dir",))
                self._dir_nodes[key] = node
            parent = node
            chain.append(key)
        kind = "%s · %s" % (parsers.ECO_LABEL.get(entry.eco, entry.eco),
                            entry.kind)
        state = _FILE_STATE.get(entry.state, entry.state)
        self._tree.insert(parent, "end", text=parts[-1],
                          values=(kind, entry.count or "", state),
                          tags=(entry.state,))
        self._file_nodes += 1
        for key in chain:
            total = self._dir_counts.get(key, 0) + entry.count
            self._dir_counts[key] = total
            try:
                self._tree.item(self._dir_nodes[key],
                                values=("目录", total or "", ""))
            except Exception:
                pass

    def _expand_all(self) -> None:
        for iid in self._tree.get_children(""):
            self._tree.item(iid, open=True)
            self._expand_children(iid)

    def _expand_children(self, iid: str) -> None:
        for child in self._tree.get_children(iid):
            if self._tree.get_children(child):
                self._tree.item(child, open=True)
                self._expand_children(child)

    def _collapse_all(self) -> None:
        for iid in self._tree.get_children(""):
            self._tree.item(iid, open=True)      # 根保持展开
            self._collapse_children(iid)

    def _collapse_children(self, iid: str) -> None:
        for child in self._tree.get_children(iid):
            if self._tree.get_children(child):
                self._tree.item(child, open=False)
                self._collapse_children(child)

    def _copy_index(self) -> None:
        lines = []
        for entry in (self._result.files if self._result else []):
            lines.append("%s\t%s\t%s\t%d\t%s"
                         % (entry.path,
                            parsers.ECO_LABEL.get(entry.eco, entry.eco),
                            entry.kind, entry.count,
                            _FILE_STATE.get(entry.state, entry.state)))
        self._copy("\n".join(lines) or "（还没有扫描结果）", "索引已复制")

    # ------------------------------------------------------------------
    # 结果表
    # ------------------------------------------------------------------

    def _on_filter_eco(self, value: str) -> None:
        self._filter_eco = value
        self._apply_filter()

    def _on_filter_status(self, value: str) -> None:
        self._filter_status = value
        self._apply_filter()

    def _on_search(self, text: str) -> None:
        self._filter_text = text or ""
        self._apply_filter()

    def _apply_filter(self) -> None:
        eco = self._filter_eco
        status = self._filter_status
        needle = self._filter_text.strip().lower()
        wanted = _STATUS_FILTER.get(status)
        rows = []
        for dep in self._deps:
            if eco != "全部" and parsers.ECO_LABEL.get(dep.ecosystem) != eco:
                continue
            if wanted and dep.status != wanted:
                continue
            if needle and (needle not in dep.name.lower()
                           and needle not in dep.source_file.lower()):
                continue
            rows.append(dep)
        self._rows = report.sort_deps(rows)
        self._table.set_rows(self._rows,
                             selected=0 if self._rows else -1)
        if self._rows:
            self._show_detail(self._rows[0])
        else:
            self._clear_detail()
        if self._result and not self._rows:
            self._table.set_empty("没有匹配的依赖")

    def _col_x(self, width: float) -> list[float]:
        xs, acc = [0.0], 0.0
        for frac in _COLS:
            acc += width * frac
            xs.append(acc)
        return xs

    def _clip(self, text: str, width: float) -> str:
        text = str(text or "")
        if width <= 0 or self._f_row.measure(text) <= width:
            return text
        while text and self._f_row.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def _draw_thead(self) -> None:
        c = self._thead
        t = self._tokens
        c.delete("all")
        try:
            c.configure(bg=t.bg)
        except Exception:
            pass
        width = max(10, c.winfo_width())
        xs = self._col_x(width)
        pad = scale.s(10)
        mid = c.winfo_height() / 2
        for i, title in enumerate(("名称", "当前", "最新", "状态", "来源")):
            c.create_text(xs[i] + pad, mid, text=title, anchor="w",
                          font=self._f_head, fill=t.dim)
        c.create_line(0, c.winfo_height() - 1, width,
                      c.winfo_height() - 1, fill=t.border)

    def _draw_row(self, canvas, dep, index, x, y, width, height, state) -> None:
        theme = state["theme"]
        if state["selected"]:
            bg = theme.color("selected")
        elif state["hover"]:
            bg = theme.color("hover")
        else:
            bg = theme.color("bg")
        canvas.create_rectangle(0, y, width, y + height, fill=bg, outline="")
        canvas.create_line(0, y + height - 1, width, y + height - 1,
                           fill=theme.color("border"))
        xs = self._col_x(width)
        pad = scale.s(10)
        mid = y + height / 2
        canvas.create_text(xs[0] + pad, mid,
                           text=self._clip(dep.name, xs[1] - xs[0] - pad),
                           anchor="w", font=self._f_row,
                           fill=theme.color("text"))
        canvas.create_text(xs[1] + pad, mid,
                           text=self._clip(dep.current or "-",
                                           xs[2] - xs[1] - pad),
                           anchor="w", font=self._f_row,
                           fill=theme.color("muted"))
        canvas.create_text(xs[2] + pad, mid,
                           text=self._clip(dep.latest or "-",
                                           xs[3] - xs[2] - pad),
                           anchor="w", font=self._f_row,
                           fill=theme.color("text" if dep.latest else "dim"))
        canvas.create_text(xs[3] + pad, mid,
                           text=versions.STATUS_LABEL.get(dep.status,
                                                          dep.status),
                           anchor="w", font=self._f_row,
                           fill=theme.color(
                               _STATUS_COLOR.get(dep.status, "dim")))
        canvas.create_text(xs[4] + pad, mid,
                           text=self._clip(self._source(dep),
                                           width - xs[4] - pad),
                           anchor="w", font=self._f_row,
                           fill=theme.color("dim"))

    def _on_row_select(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            self._show_detail(self._rows[index])

    # ------------------------------------------------------------------
    # 详情面板
    # ------------------------------------------------------------------

    def _clear_detail(self) -> None:
        for child in self._detail_inner.winfo_children():
            child.destroy()
        for child in self._detail_actions.winfo_children():
            child.destroy()
        try:
            self._detail_canvas.yview_moveto(0)
        except Exception:
            pass

    @staticmethod
    def _fmt_spec(text) -> str:
        """规格串更好读：``==1.26.0`` -> ``== 1.26.0``，``^18.2`` 原样。"""
        if not text:
            return "-"
        return re.sub(r"^(===|==|>=|<=|~=|!=|>|<|=)\s*",
                      lambda m: m.group(1) + " ", str(text))

    @staticmethod
    def _source(dep) -> str:
        """来源展示：有行号才带冒号。"""
        return ("%s:%s" % (dep.source_file, dep.line) if dep.line
                else dep.source_file)

    def _show_detail(self, dep) -> None:
        self._clear_detail()
        t = self._tokens
        panel = self._detail_inner
        if dep is None:
            return
        head = tk.Frame(panel, bg=t.bg)
        head.pack(fill="x")
        tk.Label(head, text=dep.name, bg=t.bg, fg=t.text, anchor="w",
                 justify="left", wraplength=scale.s(270),
                 font=scale.font(12, weight="bold")).pack(fill="x")
        badges = tk.Frame(head, bg=t.bg)
        badges.pack(fill="x", pady=(scale.s(5), scale.s(7)))
        widgets.Badge(badges, versions.STATUS_LABEL.get(dep.status,
                                                        dep.status),
                      _STATUS_TONE.get(dep.status, "neutral")).pack(side="left")
        widgets.Badge(badges, parsers.ECO_LABEL.get(dep.ecosystem,
                                                    dep.ecosystem)).pack(
            side="left", padx=(scale.s(5), 0))
        if dep.transitive:
            widgets.Badge(badges, "间接", "neutral").pack(
                side="left", padx=(scale.s(5), 0))

        def row(key: str, value: str, tone: str = "text") -> None:
            line = tk.Frame(panel, bg=t.bg)
            line.pack(fill="x", pady=scale.s(1))
            tk.Label(line, text=key, bg=t.bg, fg=t.dim, width=4, anchor="w",
                     font=self._f_small).pack(side="left")
            tk.Label(line, text=value or "-", bg=t.bg,
                     fg=getattr(t, tone), anchor="w", justify="left",
                     wraplength=scale.s(240),
                     font=self._f_small).pack(side="left", fill="x",
                                              expand=True)

        # 只渲染有内容的行：常见依赖通常 2-3 行，避免面板出现大片空白
        if dep.declared:
            row("声明", self._fmt_spec(dep.declared))
        if dep.resolved:
            row("锁定", dep.resolved)
        row("最新", dep.latest or "未核对",
            "text" if dep.latest else "dim")
        if dep.scope != "runtime" or dep.transitive:
            row("范围", dep.scope + (" · 间接" if dep.transitive else ""))
        row("来源", self._source(dep))
        if dep.note:
            row("备注", dep.note, "muted")

        command = report.upgrade_command(dep)
        tk.Frame(panel, bg=t.border, height=1).pack(fill="x",
                                                    pady=(scale.s(10), scale.s(2)))
        tk.Label(panel, text="升级命令", bg=t.bg, fg=t.dim, anchor="w",
                 font=self._f_small).pack(fill="x")
        cmd_row = tk.Frame(panel, bg=t.bg)
        cmd_row.pack(fill="x", pady=(scale.s(4), 0))
        field = tk.Entry(cmd_row, bg=t.surface, fg=t.text, relief="flat",
                         insertbackground=t.text, highlightthickness=1,
                         highlightbackground=t.border, highlightcolor=t.accent,
                         font=scale.font(10))
        field.insert(0, command or "—")
        field.configure(state="readonly", readonlybackground=t.surface)
        field.pack(side="left", fill="x", expand=True, ipady=scale.s(3))
        widgets.IconButton(cmd_row, "copy",
                           lambda: self._copy(command or "", "升级命令已复制"),
                           tooltip="复制升级命令", size=26).pack(
            side="left", padx=(scale.s(4), 0))
        if dep.cves:
            tk.Frame(panel, bg=t.border, height=1).pack(fill="x",
                                                        pady=(scale.s(6), 0))
            tk.Label(panel, text="漏洞 %d 个" % len(dep.cves), bg=t.bg,
                     fg=t.danger, anchor="w",
                     font=scale.font(10, weight="bold")).pack(
                fill="x", pady=(scale.s(6), scale.s(2)))
            for cve in dep.cves[:6]:
                block = tk.Frame(panel, bg=t.bg)
                block.pack(fill="x", pady=(0, scale.s(3)))
                fixed = ", ".join(cve.get("fixed") or [])
                head_text = " ".join(part for part in (
                    cve.get("id", ""), cve.get("severity", "")) if part)
                if fixed:
                    head_text += " · 修复 " + fixed
                tk.Label(block, text=head_text, bg=t.bg, fg=t.danger,
                         anchor="w", font=self._f_small).pack(fill="x")
                summary = cve.get("summary") or ""
                if summary:
                    tk.Label(block, text=summary, bg=t.bg, fg=t.muted,
                             anchor="w", justify="left",
                             wraplength=scale.s(270),
                             font=scale.font(10)).pack(fill="x")

        actions = self._detail_actions
        widgets.PillButton(actions, "复制 AI 提示词", self._copy_prompt,
                           kind="secondary").pack(side="left")
        widgets.IconButton(actions, "folder",
                           lambda: self._reveal(dep.source_file),
                           tooltip="在资源管理器中显示",
                           size=30).pack(side="left", padx=(scale.s(6), 0))

    def _copy_prompt(self) -> None:
        if self._result is None:
            return
        self._copy(report.build_prompt(self._result), "AI 提示词已复制")

    def _reveal(self, rel_path: str) -> None:
        """在资源管理器里选中来源文件。"""
        target = os.path.normpath(os.path.join(self._root_dir, rel_path))
        if not os.path.exists(target):
            widgets.Toast(self.ctx.root, "文件不存在：%s" % rel_path)
            return
        try:
            import subprocess
            subprocess.Popen(["explorer", "/select,", target])
        except Exception as exc:
            messagebox.showerror("依赖医生", "打开失败：%s" % exc, parent=self)

    # ------------------------------------------------------------------
    # 统计与报告
    # ------------------------------------------------------------------

    def _update_stats(self, result) -> None:
        for child in self._stats.winfo_children():
            child.destroy()
        if result is None:
            return
        t = self._tokens
        stats = report.counts(result.deps)
        chips = (
            ("依赖", len(result.deps), t.text),
            ("漏洞", stats.get("vulnerable", 0), t.danger),
            ("大版本", stats.get("major", 0), t.warning),
            ("过期", stats.get("outdated", 0), t.warning),
            ("正常", stats.get("ok", 0), t.success),
            ("未知", stats.get("unknown", 0), t.dim),
        )
        for label, value, color in chips:
            box = tk.Frame(self._stats, bg=t.bg)
            box.pack(side="left", padx=(0, scale.s(14)))
            tk.Label(box, text=str(value), bg=t.bg, fg=color,
                     font=self._f_num).pack(side="left")
            tk.Label(box, text=" " + label, bg=t.bg, fg=t.dim,
                     font=scale.font(10)).pack(side="left",
                                               pady=(scale.s(2), 0))

    def _render_report(self, text: str) -> None:
        for child in self._report_holder.winfo_children():
            child.destroy()
        if not text:
            tk.Label(self._report_holder,
                     text="扫描完成后，这里显示完整报告（可导出 md / json）",
                     bg=self._tokens.bg, fg=self._tokens.dim,
                     font=scale.font(11)).pack(pady=scale.s(30))
            return
        from ...ui.markdown import MarkdownView
        try:
            view = MarkdownView(self._report_holder, text)
            view.pack(fill="both", expand=True)
        except Exception as exc:
            tk.Label(self._report_holder, text="报告渲染失败：%s" % exc,
                     bg=self._tokens.bg, fg=self._tokens.danger,
                     font=self._f_small).pack(pady=scale.s(20))

    def _copy_report(self) -> None:
        if self._result is None:
            widgets.Toast(self.ctx.root, "还没有扫描结果")
            return
        self._copy(report.build_markdown(self._result), "报告已复制")

    def _export(self, kind: str) -> None:
        if self._result is None:
            widgets.Toast(self.ctx.root, "还没有扫描结果")
            return
        suffix = "md" if kind == "md" else "json"
        path = filedialog.asksaveasfilename(
            parent=self, title="导出依赖报告",
            initialdir=self._root_dir,
            initialfile=report.default_filename(self._root_dir, suffix),
            defaultextension="." + suffix,
            filetypes=[("Markdown", "*.md")] if suffix == "md"
            else [("JSON", "*.json")])
        if not path:
            return
        text = (report.build_markdown(self._result) if suffix == "md"
                else report.build_json(self._result))
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            messagebox.showerror("依赖医生", "写入失败：%s" % exc, parent=self)
            return
        widgets.Toast(self.ctx.root, "已导出：%s" % os.path.basename(path))
        self.set_status("已导出 %s" % path)

    def _copy(self, text: str, tip: str = "已复制") -> None:
        if not text:
            return
        try:
            core.set_clipboard(text)
        except Exception:
            try:
                self.ctx.root.clipboard_clear()
                self.ctx.root.clipboard_append(text)
            except Exception:
                return
        widgets.Toast(self.ctx.root, tip)

    def _pick_dir(self) -> None:
        ball = getattr(getattr(self.ctx, "app", None), "ball", None)
        try:
            ball.withdraw()
        except Exception:
            pass
        try:
            path = filedialog.askdirectory(
                parent=self, title="依赖医生 - 选择项目文件夹",
                initialdir=self._root_dir or os.path.expanduser("~"))
        finally:
            try:
                ball.deiconify()
                ball.lift()
            except Exception:
                pass
        if not path:
            return
        self.set_root(path)
        self.start_scan()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._stop.set()
        self._scanning = False
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        super().close()
