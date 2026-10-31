"""悬浮球与主面板：拖动、吸附、展开/收起、右键菜单、设置页。

悬浮球是分层窗口（ui/ball.py 负责像素级透明），面板挂在隐藏主窗口下。
本模块只处理交互与布局，不承载业务功能；功能入口统一走 menu_ui。
"""
from . import core, elevate, finder, hud, launcher, music, nettools, promptlab
from .core import *
from .ui import icons, render, scale
from .ui.ball import BallWindow
from .ui.scroll import bind_canvas_wheel
from tkinter import colorchooser

# 主题换色要同步的颜色常量名
_CORE_COLORS = ("C_BG", "C_PANEL", "C_BORDER", "C_TEXT", "C_MUTED",
                "C_HOVER", "C_ACCENT", "C_ACCENT_H", "C_SELECT", "C_DIR")
_MUSIC_COLORS = ("M_BG", "M_SURF", "M_SURF2", "M_SURF3", "M_LINE", "M_TEXT",
                 "M_MUTED", "M_DIM", "M_ACC", "M_ACC_H", "M_ACC_BG",
                 "M_GOOD", "M_WARN", "M_BAD")

# 即时换色时尝试的控件颜色属性（不支持的会抛 TclError 跳过）
_COLOR_OPTS = ("background", "foreground", "activebackground", "activeforeground",
               "selectcolor", "troughcolor", "highlightbackground", "highlightcolor",
               "insertbackground", "selectbackground", "selectforeground",
               "disabledforeground", "readonlybackground")

# wm 的尺寸指的是客户区，窗口实际还要多占一条标题栏和边框。
# 夹位置/压尺寸时必须把它算进去，否则窗口最下面一截（往往正好是按钮排）会探出工作区。
WINDOW_FRAME_H = 36

class FloatingMixin:
    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("VB.Treeview", background=C_PANEL, foreground=C_TEXT,
                     fieldbackground=C_PANEL, borderwidth=0, rowheight=24,
                     font=(core.FONT, 9))
        st.map("VB.Treeview", background=[("selected", C_SELECT)],
               foreground=[("selected", "#ffffff")])
        st.configure("VB.Vertical.TScrollbar", background=C_BORDER, troughcolor=C_PANEL,
                     bordercolor=C_PANEL, arrowcolor=C_MUTED, borderwidth=0, width=12)
        st.map("VB.Vertical.TScrollbar", background=[("active", C_HOVER)])
        # 通用 ttk 控件（Combobox/Treeview/Scrollbar 等）也按主题令牌重刷
        try:
            self.theme.apply_ttk(st)
        except Exception:
            pass
        self._init_marks()

    def _build_ball(self):
        bw = BallWindow(self.root, self.theme, size=BALL_SIZE)
        bw.bind_events(
            press=self._press, motion=self._motion, release=self._release,
            context=self._ball_menu,
            enter=lambda e: bw.set_state("hover"),
            leave=lambda e: bw.set_state("idle"))
        # 主题切换时球跟着重绘（subscribe 回调带 preset/tokens 两个参数）
        self._ball_theme_sub = self.theme.subscribe(
            lambda preset, tokens: bw.set_theme())
        # 主题切换时全局换色：同步 C_* 常量并重建面板/设置窗
        self._theme_sub = self.theme.subscribe(self._on_theme_changed)
        self.ball_win = bw
        self.ball_px = bw.px           # 物理像素尺寸，DPI 缩放后与 BALL_SIZE 不同
        if self.root_dir:
            self._set_ball_text(display_name(self.root_dir))
        return bw.win

    def _color_snapshot(self):
        """收集各模块当前的颜色常量值，用于换主题时建立旧色->新色映射"""
        snap = {}
        for mod, names in ((core, _CORE_COLORS), (music, _MUSIC_COLORS)):
            for k in names:
                try:
                    snap[(mod.__name__, k)] = getattr(mod, k)
                except AttributeError:
                    pass
        return snap

    def _sync_colors(self, preset, tokens):
        """把主题令牌写回 core 与各功能模块的 C_*/M_* 颜色常量"""
        import sys

        def mix(a, b, t):
            a1, a2, a3 = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
            b1, b2, b3 = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
            return "#%02x%02x%02x" % (int(a1 + (b1 - a1) * t),
                                      int(a2 + (b2 - a2) * t),
                                      int(a3 + (b3 - a3) * t))

        core_vals = {
            "C_BG": tokens.bg, "C_PANEL": tokens.surface,
            "C_BORDER": tokens.border, "C_TEXT": tokens.text,
            "C_MUTED": tokens.muted, "C_HOVER": tokens.hover,
            "C_ACCENT": tokens.accent, "C_ACCENT_H": tokens.accent_hover,
            "C_SELECT": tokens.selected,
            # 目录色：主色向正文色偏一点的浅色，安静但和文件有区分
            "C_DIR": mix(tokens.accent, tokens.text, 0.45),
        }
        for mod in list(sys.modules.values()):
            name = getattr(mod, "__name__", "")
            if name == "vbhelper" or name.startswith("vbhelper."):
                if hasattr(mod, "C_BG"):
                    for k, v in core_vals.items():
                        if hasattr(mod, k):
                            setattr(mod, k, v)

        music_vals = {
            "M_BG": tokens.bg, "M_SURF": tokens.surface,
            "M_SURF2": tokens.hover, "M_SURF3": tokens.elevated,
            "M_LINE": tokens.border, "M_TEXT": tokens.text,
            "M_MUTED": tokens.muted, "M_DIM": tokens.dim,
            "M_ACC": tokens.accent, "M_ACC_H": tokens.accent_hover,
            "M_ACC_BG": tokens.accent_soft,
            "M_GOOD": tokens.success, "M_WARN": tokens.warning,
            "M_BAD": tokens.danger,
        }
        for k, v in music_vals.items():
            if hasattr(music, k):
                setattr(music, k, v)

    def _recolor_widget(self, w, cmap):
        """递归把已建好的控件/画布项从旧色换成新色"""
        for opt in _COLOR_OPTS:
            try:
                cur = str(w.cget(opt)).lower()
            except Exception:
                continue
            new = cmap.get(cur)
            if new:
                try:
                    w.configure(**{opt: new})
                except Exception:
                    pass
        if w.winfo_class() == "Canvas":
            try:
                items = w.find_all()
            except Exception:
                items = ()
            for it in items:
                for opt in ("fill", "outline"):
                    try:
                        cur = str(w.itemcget(it, opt)).lower()
                    except Exception:
                        continue
                    new = cmap.get(cur)
                    if new:
                        try:
                            w.itemconfigure(it, **{opt: new})
                        except Exception:
                            pass
        try:
            for child in w.winfo_children():
                self._recolor_widget(child, cmap)
        except Exception:
            pass

    def _recolor_open_windows(self, cmap):
        """遍历所有已打开窗口即时换色（悬浮球和将被重建的面板除外）"""
        if not cmap:
            return
        skip = set()
        for win in (self.panel, getattr(self, "_settings_win", None)):
            if win is not None:
                try:
                    skip.add(str(win))
                except Exception:
                    pass
        try:
            tops = self.root.winfo_children()
        except Exception:
            return
        for win in tops:
            if win is self.ball:
                continue           # 球由 layered 渲染自己重绘
            try:
                if str(win) in skip:
                    continue
                if not win.winfo_exists():
                    continue
                self._recolor_widget(win, cmap)
            except Exception:
                pass

    def _on_theme_changed(self, preset, tokens):
        """主题切换：全局换色 + 已开窗口即时重绘 + 面板/设置窗重建"""
        old = self._color_snapshot()
        self._sync_colors(preset, tokens)
        new = self._color_snapshot()
        cmap = {}
        for key, old_val in old.items():
            new_val = new.get(key)
            if old_val and new_val and old_val.lower() != new_val.lower():
                cmap.setdefault(old_val.lower(), new_val)
        try:
            self._init_style()
        except Exception:
            pass
        self._recolor_open_windows(cmap)
        # 面板重建（含标题栏/按钮/树/状态栏）
        if self.panel is not None:
            try:
                was_open = self.panel.winfo_ismapped()
            except Exception:
                was_open = False
            try:
                self.panel.destroy()
            except Exception:
                pass
            self.panel = None
            self.tree = None
            self.rz_grip = None
            if was_open and self.root_dir:
                self._expand()
        # 设置窗重建换色（浏览主题时窗口会闪一下重开，属预期）
        old_win = getattr(self, "_settings_win", None)
        if old_win is not None:
            try:
                if old_win.winfo_exists():
                    old_win.destroy()
            except Exception:
                pass
            self._settings_win = None
            self.root.after(80, self._settings_window)

    def _set_ball_text(self, name):
        """球面不显示长文字，统一放文件夹图标；完整项目名看面板标题"""
        self.ball_win.set_label("folder")

    def _place_ball(self):
        L, T, R, B = self.work
        pos = self.cfg.get("ball_pos")
        if pos and len(pos) == 2:
            x, y = pos
        else:
            x, y = R - self.ball_px - 30, B - self.ball_px - 40
        x = max(L - 4, min(x, R - self.ball_px))
        y = max(T - 4, min(y, B - self.ball_px))
        self.ball.geometry(f"+{x}+{y}")

    def _press(self, e):
        win = e.widget.winfo_toplevel()
        if self.parked:
            self._unpark()
        self._stop_slide()          # 滑出动画还在跑时开拖会互相抢位置
        self._dragging = True
        if win is self.ball:
            self.ball_win.set_state("press")
        self._drag = {"win": win, "x": e.x_root - win.winfo_x(),
                      "y": e.y_root - win.winfo_y(), "moved": False,
                      "locked": self.locked}

    def _motion(self, e):
        d = self._drag
        if not d or d.get("locked"):   # 锁定状态下既不许移动，也不许改大小
            return
        if e.widget.winfo_toplevel() is not d["win"]:
            return                     # 非本窗口事件直接忽略
        if d.get("resize"):
            self._resize_to(e)
            d["moved"] = True
            return
        nx, ny = e.x_root - d["x"], e.y_root - d["y"]
        if abs(nx - d["win"].winfo_x()) > 3 or abs(ny - d["win"].winfo_y()) > 3:
            d["moved"] = True
        if d["moved"]:
            d["win"].geometry(f"+{nx}+{ny}")

    def _release(self, e):
        d, self._drag = self._drag, None
        self._dragging = False
        if not d:
            return
        win = d["win"]
        if win is self.ball:
            self.ball_win.set_state("idle")
        if d.get("resize"):
            if d["moved"]:
                self.cfg["panel_size"] = [win.winfo_width(), win.winfo_height()]
                save_cfg(self.cfg)
            return
        if not d["moved"]:
            if win is self.ball:
                self._toggle_panel()
            return
        if win is self.ball:
            self._save_ball_pos()
        self._snap(win)

    def _active_win(self):
        try:
            if self.panel is not None and self.panel.winfo_ismapped():
                return self.panel
        except Exception:
            pass
        return self.ball

    def _snap(self, win):
        if not self.set.get("snap"):
            self.snap_side = None
            return
        w, h = win.winfo_width(), win.winfo_height()
        x, y = win.winfo_x(), win.winfo_y()
        L, T, R, B = self.work
        side = None
        if x <= L + SNAP_MARGIN:
            side = "left"
        elif x + w >= R - SNAP_MARGIN:
            side = "right"
        elif y <= T + SNAP_MARGIN:
            side = "top"
        elif y + h >= B - SNAP_MARGIN:
            side = "bottom"
        self.snap_side = side
        if side is None:
            win.geometry(f"+{max(L, min(x, R - w))}+{max(T, min(y, B - h))}")
            self.parked = False
        elif self.set.get("edge_ball") and win is self.panel:
            # 靠边收成悬浮球模式：不贴条停靠，面板直接收回，只留悬浮球
            self._collapse()
        else:
            self._park(win)

    def _park(self, win):
        side, w, h = self.snap_side, win.winfo_width(), win.winfo_height()
        L, T, R, B = self.work
        x, y = win.winfo_x(), win.winfo_y()
        if side == "left":
            x = L - w + HANDLE_W
        elif side == "right":
            x = R - HANDLE_W
        elif side == "top":
            y = T - h + HANDLE_W
        else:
            y = B - HANDLE_W
        self._stop_slide()
        win.geometry(f"+{x}+{y}")
        self.parked = True

    def _unpark(self):
        if not self.parked or not self.snap_side:
            self.parked = False
            return
        win = self._active_win()
        w, h = win.winfo_width(), win.winfo_height()
        L, T, R, B = self.work
        side = self.snap_side
        tx = 0 if side == "left" else (R - w if side == "right" else win.winfo_x())
        ty = 0 if side == "top" else (B - h if side == "bottom" else win.winfo_y())
        tx = max(L, min(tx, R - w))
        ty = max(T, min(ty, B - h))
        self.parked = False
        self.armed_at = time.time()
        self._slide(win, tx, ty)

    def _stop_slide(self):
        if self._slide_job:
            try:
                self.root.after_cancel(self._slide_job)
            except Exception:
                pass
            self._slide_job = None

    def _slide(self, win, tx, ty):
        self._stop_slide()

        def step():
            x, y = win.winfo_x(), win.winfo_y()
            dx, dy = tx - x, ty - y
            if abs(dx) <= SLIDE_STEP and abs(dy) <= SLIDE_STEP:
                win.geometry(f"+{tx}+{ty}")
                self._slide_job = None
                return
            nx = x + (SLIDE_STEP if dx > 0 else -SLIDE_STEP)
            ny = y + (SLIDE_STEP if dy > 0 else -SLIDE_STEP)
            win.geometry(f"+{nx}+{ny}")
            self._slide_job = self.root.after(10, step)

        step()

    def _in_handle(self, mx, my):
        win = self._active_win()
        x, y = win.winfo_x(), win.winfo_y()
        w, h = win.winfo_width(), win.winfo_height()
        L, T, R, B = self.work
        s = self.snap_side
        if s == "left":
            return L <= mx <= L + HANDLE_W + 3 and y <= my <= y + h
        if s == "right":
            return R - HANDLE_W - 3 <= mx <= R and y <= my <= y + h
        if s == "top":
            return T <= my <= T + HANDLE_W + 3 and x <= mx <= x + w
        return B - HANDLE_W - 3 <= my <= B and x <= mx <= x + w

    def _mouse_over(self, win):
        mx, my = cursor_pos()
        x, y = win.winfo_x(), win.winfo_y()
        return x <= mx <= x + win.winfo_width() and y <= my <= y + win.winfo_height()

    def _hover_poll(self):
        try:
            # 拖动/缩放期间不做任何自动移动，否则窗口会被抢走，拖起来一抖一抖的
            if self.snap_side and not self._dragging:
                if self.parked:
                    if self._in_handle(*cursor_pos()):
                        self._unpark()
                elif (not self.locked and time.time() - self.armed_at > 0.8
                        and not self._mouse_over(self._active_win())):
                    self._park(self._active_win())
        except Exception:
            pass
        self.root.after(POLL_MS, self._hover_poll)

    def _ensure_panel(self):
        if self.panel is not None:
            return
        p = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.configure(bg=C_BORDER)
        size = self.cfg.get("panel_size") or [PANEL_W, PANEL_H]
        try:
            pw, ph = int(size[0]), int(size[1])
        except Exception:
            pw, ph = PANEL_W, PANEL_H
        L, T, R, B = self.work
        p.geometry("%dx%d" % (max(300, min(pw, R - L)), max(220, min(ph, B - T))))
        try:
            p.wm_attributes("-topmost", self.topmost)
        except Exception:
            pass

        outer = tk.Frame(p, bg=C_BG)
        outer.pack(fill="both", expand=True)
        self._init_resizer(outer)

        # 标题栏
        bar = tk.Frame(outer, bg=C_BG, height=34, cursor="fleur")
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.title_bar = bar
        self.title_lbl = tk.Label(bar, text="未选择目录", bg=C_BG, fg=C_TEXT,
                                  font=(core.FONT, 9, "bold"), anchor="w", cursor="fleur")
        self.title_lbl.pack(side="left", padx=10)
        self._btn(bar, "x", self._quit).pack(side="right", padx=(2, 5))
        self._btn(bar, "minus", self._collapse).pack(side="right", padx=2)
        self.pin_btn = self._btn(bar, self._pin_icon(), self._toggle_topmost)
        self.pin_btn.pack(side="right", padx=2)
        self.lock_btn = self._btn(bar, self._lock_icon(), self._toggle_lock)
        self.lock_btn.pack(side="right", padx=2)
        self._btn(bar, "folder", lambda: self._pick_dir()).pack(side="right", padx=(0, 2))
        self._btn(bar, "settings", self._settings_window).pack(side="right", padx=2)
        self._btn(bar, "menu", self._menu_window).pack(side="right", padx=2)
        if self.locked:
            self.lock_btn.configure(bg=C_HOVER, fg=C_ACCENT_H)
            self.pin_btn.configure(fg=C_MUTED)
            for w in (bar, self.title_lbl):
                w.configure(cursor="")
        for w in (bar, self.title_lbl):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._motion)
            w.bind("<ButtonRelease-1>", self._release)
        tk.Frame(outer, bg=C_BORDER, height=1).pack(fill="x")

        # 目录树
        wrap = tk.Frame(outer, bg=C_BG)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, show="tree", columns=("note",),
                                 style="VB.Treeview", selectmode="extended")
        self.tree.column("#0", width=220, minwidth=80, stretch=True)
        self.tree.column("note", width=26, minwidth=26, stretch=False, anchor="center")
        vsb = ttk.Scrollbar(wrap, style="VB.Vertical.TScrollbar", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("dir", foreground=C_DIR)
        self.tree.tag_configure("file", foreground=C_TEXT)
        self._init_mark_tags()
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Right>", lambda e: self._key_expand())
        self.tree.bind("<Button-3>", self._tree_menu)
        self.tree.bind("<Double-Button-1>", self._tree_dclick)
        self.tree.bind("<Control-c>", lambda e: self._copy_selected())
        self.tree.bind("<F5>", lambda e: self._refresh())
        self.tree.bind("<Control-p>", lambda e: self._open_finder())
        self.tree.bind("<Control-f>", lambda e: self._open_finder())

        # 状态栏
        tk.Frame(outer, bg=C_BORDER, height=1).pack(fill="x")
        self.status = tk.Label(outer, text="", bg=C_BG, fg=C_MUTED,
                               font=(core.FONT, 8), anchor="w")
        self.status.pack(fill="x", padx=10, pady=4)

        p.bind("<Escape>", lambda e: self._collapse())
        p.bind("<F5>", lambda e: self._refresh())
        p.update_idletasks()
        self.panel = p
        p.withdraw()

    def _btn(self, parent, icon, cmd):
        lbl = tk.Label(parent, bg=C_BG, padx=6, pady=2, cursor="hand2")
        self._set_btn_icon(lbl, icon, C_MUTED)
        lbl.bind("<Button-1>", lambda e: cmd())
        lbl.bind("<Enter>", lambda e: (
            lbl.configure(bg=C_HOVER),
            self._set_btn_icon(lbl, lbl._icon_name, C_TEXT)))
        lbl.bind("<Leave>", lambda e: (
            lbl.configure(bg=C_BG),
            self._set_btn_icon(lbl, lbl._icon_name, C_MUTED)))
        return lbl

    def _set_btn_icon(self, lbl, icon, color=None):
        """换按钮图标：图像按当前前景色重新栅格化，缓存由 render.photo 管。"""
        color = color or getattr(lbl, "_icon_fg", C_MUTED)
        lbl._icon_name = icon
        lbl._icon_fg = color
        ph = render.photo(icons.draw(icon, 16, color),
                          key="float:ico:%s:16:%s" % (icon, color))
        lbl.configure(image=ph)
        lbl._icon_photo = ph

    def _popup(self, title, w=0, h=0):
        """两个弹窗共用的小外壳：深色、置顶、跟着主面板走"""
        win = tk.Toplevel(self.root)     # 挂在隐藏主窗口下：面板是无边框覆盖窗口，不能作为父窗口
        win.title(title + " - " + APP_NAME)
        win.configure(bg=C_BG)
        if w and h:
            win.geometry("%dx%d" % (w, h))
            win.resizable(False, False)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.bind("<Escape>", lambda e: win.destroy())
        self.root.after(60, lambda: self._center_on_panel(win))
        return win

    def _popup_size(self, win):
        """弹窗要占多大地方。

        必须按**真实高度**算，不能只信 winfo_reqheight()：固定尺寸的窗口需求高度
        往往比实际小得多（设置窗实际 640，内容只要 485），拿需求高度去夹位置会把
        窗口夹到屏幕底边之外，下半截（那一排按钮）就永远看不见了。
        """
        reqw, reqh = win.winfo_reqwidth(), win.winfo_reqheight()
        try:
            size = win.geometry().split("+")[0].split("x")
            if len(size) == 2:
                return max(reqw, int(size[0])), max(reqh, int(size[1]))
        except Exception:
            pass
        return reqw, reqh

    def _fit_work_area(self, w, h):
        """把窗口压到工作区装得下（含标题栏）。

        wm 的尺寸说的是**客户区**，窗口实际占的屏幕还要多一条标题栏和边框。
        不留出这块，客户区高度一旦贴着工作区高度，最下面那排按钮就会被顶到
        屏幕外——设置窗的「添加/编辑/删除」就是这么消失的。本机实测边框 31。
        """
        L, T, R, B = self.work
        return (max(260, min(w, (R - L) - 24)),
                max(200, min(h, (B - T) - WINDOW_FRAME_H - 8)))

    def _center_on_panel(self, win):
        try:
            win.update_idletasks()
            w, h = self._fit_work_area(*self._popup_size(win))
            win.geometry("%dx%d" % (w, h))   # 超出工作区时压缩高度，避免遮挡任务栏
            L, T, R, B = self.work
            if self.panel is not None and self.panel.winfo_ismapped():
                px, py = self.panel.winfo_x(), self.panel.winfo_y()
                x = px + (self.panel.winfo_width() - w) // 2
                y = py + 40
            else:
                x = (L + R - w) // 2
                y = (T + B - h) // 2
            # 夹位置时把标题栏也算进去，保证"客户区底边"不越过工作区底边
            x = max(L, min(x, R - w - 12))
            y = max(T, min(y, B - h - WINDOW_FRAME_H))
            win.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _open_nettools(self):
        """网络工具入口：会改系统网络设置，点之前先自检管理员权限，不够就提权重启"""
        if not elevate.is_admin():
            yes = messagebox.askyesno(
                APP_NAME,
                "网络工具会修改系统网络设置（切换 DNS、保存 hosts），"
                "需要管理员权限。\n\n现在以管理员身份重启 VBHelper 吗？"
                "重启后会自动打开网络工具。",
                parent=self.panel or self.root)
            if not yes:
                return
            self.cfg["open_nettools_after_elevate"] = True
            save_cfg(self.cfg)
            if elevate.relaunch_as_admin():
                self._do_quit()          # 旧实例退出，避免出现两个悬浮球
            else:
                self.cfg.pop("open_nettools_after_elevate", None)
                save_cfg(self.cfg)
                messagebox.showerror(APP_NAME, "提权被取消或失败，网络工具没有打开。",
                                     parent=self.root)
            return
        nettools.open_window(self)

    def _toggle_hud(self):
        """菜单里的常驻仪表盘开关：挂着就收掉，没挂就挂上"""
        try:
            hud.toggle(self)
        except Exception as e:
            messagebox.showerror(APP_NAME, "仪表盘打开失败：%s" % e, parent=self.root)

    def _open_hud(self):
        """启动时自动恢复仪表盘；这里出错只能咽下去，别让主程序起不来"""
        try:
            hud.open_hud(self)
        except Exception:
            pass

    def _menu_window(self):
        """工具栏菜单按钮：可搜索的功能面板（分组 + 图标 + 键盘导航）。

        以前是一条纵向长列表，功能越多越撑出屏幕；现在用搜索框筛选，
        RAG/Windows 助手/语音后续加进来也不会把菜单堆爆。
        """
        from . import menu_ui
        win = self._popup("菜单")
        if hud.is_open(self):
            hud_title = "关闭常驻仪表盘 (HUD)"
            hud_desc = "仪表盘正挂在桌面上，点这里收掉（下次启动就不会自己冒出来）"
        else:
            hud_title = "启用常驻仪表盘 (HUD)"
            hud_desc = ("CPU · 内存 · 磁盘 · 网络 · GPU 利用率与硬件温度，"
                        "常驻桌面、可拖动、可锁定、可透明")
        items = []
        if self.features.get("dependency_doctor") is not None:
            items.append(menu_ui.MenuItem(
                "依赖医生",
                "扫描项目依赖清单：声明 / 锁定 / 最新版本 · 大版本落后 · "
                "已知漏洞 · 升级命令与体检报告（先选项目文件夹）",
                self._open_deps, group="开发工具", icon="package",
                keywords="依赖 版本 漏洞 cve 升级 pip npm maven go"))
        if self.features.get("api_workbench") is not None:
            items.append(menu_ui.MenuItem(
                "API 工作台",
                "API 站点管理：密钥加密存储 · 一键测试连通 · 余额查询 · "
                "一键应用到 Claude Code",
                self._open_apiwork, group="开发工具", icon="zap",
                keywords="api 站点 密钥 余额 claude 中转"))
        if self.features.get("mcp_bus") is not None:
            items.append(menu_ui.MenuItem(
                "MCP 工具总线",
                "给 AI 客户端开放本机能力：自动截屏返回图片 · 鼠标键盘操作 · "
                "联网搜索；工具开关/权限/急停/审计都在面板里",
                self._open_mcp, group="开发工具", icon="share",
                keywords="ai mcp 截屏 截图 鼠标 键盘 搜索 自动化 工具"))
        if self.features.get("rag") is not None:
            items.append(menu_ui.MenuItem(
                "项目 RAG / 建立索引",
                "选一个项目文件夹，后台静默多线程建立本地索引；"
                "之后 AI 用 rag.search 只读相关文件，不再整盘扫描",
                self._open_rag, group="开发工具", icon="grid",
                keywords="rag 索引 向量 数据库 搜索 上下文 代码库 ai"))
        items += [
            menu_ui.MenuItem(
                "打开文件 / 快捷修改",
                "选任意文件：文本编辑 · Hex 查看 · PE/DLL 结构 · Markdown 预览"
                "（不限于当前项目）",
                self._open_editor_file, group="文件与搜索", icon="file",
                keywords="打开 编辑 hex pe jar class markdown 查看"),
            menu_ui.MenuItem(
                "快速搜索",
                "项目内搜文件名 · 搜文件内容 · Enter 直接打开（Ctrl+P）",
                self._open_finder, group="文件与搜索", icon="search",
                keywords="搜索 查找 文件名 内容 ctrl+p"),
            menu_ui.MenuItem(
                "网络工具",
                "DNS 优选 · 加密 DNS（DoH/DoT）· ECH 检测 · 网络诊断 · hosts 编辑",
                self._open_nettools, group="网络", icon="globe",
                keywords="dns doh dot hosts 网络 诊断 加密"),
            menu_ui.MenuItem(
                promptlab.menu_title(self),
                "分类浏览 · 搜索 · 一键复制 · 可编辑填占位符 · 自定义模板与导入导出",
                self._open_promptlab, group="创作与娱乐", icon="note",
                keywords="提示词 模板 prompt 复制"),
            menu_ui.MenuItem(
                "音乐播放器",
                "本地曲库 · 搜索 · 歌词滚动 · 播放模式 · 迷你条（FLAC/M4A 需 ffmpeg）",
                self._open_music, group="创作与娱乐", icon="music",
                keywords="音乐 播放 歌词 曲库 歌"),
            menu_ui.MenuItem(
                hud_title, hud_desc, self._toggle_hud,
                group="桌面与系统", icon="chart",
                keywords="cpu 内存 磁盘 网络 gpu 温度 仪表盘 hud"),
            menu_ui.MenuItem(
                "设置",
                "自动下钻 · 靠边吸附 · 面板行为 · 仪表盘刷新周期（改完立即生效）",
                self._settings_window, group="桌面与系统", icon="gear",
                keywords="设置 偏好 自动下钻 吸附 dpi 主题"),
        ]
        menu_ui.build(self, win, items)
        return win

    def _open_promptlab(self, path=None):
        """菜单里的提示词工作台：模板库，跟项目无关，直接开"""
        return self._promptlab(path)

    def _open_music(self):
        """菜单里的音乐播放器：全局只留一个窗口，重复点就拉到前面"""
        try:
            return music.open_window(self)
        except Exception as e:
            messagebox.showerror(APP_NAME, "音乐播放器打开失败：%s" % e,
                                 parent=self.root)
            return None

    def _open_apiwork(self):
        """菜单里的 API 工作台：单例窗口，重复点就拉到前面。"""
        try:
            if self.features.open("api_workbench"):
                return None
        except Exception as e:
            messagebox.showerror(APP_NAME, "API 工作台打开失败：%s" % e,
                                 parent=self.root)
            return None
        messagebox.showinfo(
            APP_NAME,
            "API 工作台未启用：请把 config.json 里的 "
            "features.api_workbench 设为 true。", parent=self.root)
        return None

    def _open_rag(self, path=None):
        """菜单/目录树右键：打开项目 RAG。path 为空时先选文件夹。"""
        feat = self.features.get("rag")
        if feat is None:
            messagebox.showinfo(
                APP_NAME,
                "项目 RAG 未启用：请把 config.json 里的 "
                "features.rag 设为 true。", parent=self.root)
            return None
        try:
            return feat.open(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, "项目 RAG 打开失败：%s" % e,
                                 parent=self.root)
            return None

    def _open_mcp(self):
        """菜单里的 MCP 工具总线：单例面板，服务开关都在面板里。"""
        try:
            if self.features.open("mcp_bus"):
                return None
        except Exception as e:
            messagebox.showerror(APP_NAME, "MCP 工具总线打开失败：%s" % e,
                                 parent=self.root)
            return None
        messagebox.showinfo(
            APP_NAME,
            "MCP 工具总线未启用：请把 config.json 里的 "
            "features.mcp_bus 设为 true。", parent=self.root)
        return None

    def _open_deps(self):
        """菜单里的依赖医生：点开先让用户选项目文件夹。

        功能未启用时（config.features.dependency_doctor = false）注册表里
        没有这个实例，这里给一句明确提示，不静默失败。
        """
        try:
            if self.features.open("dependency_doctor"):
                return None
        except Exception as e:
            messagebox.showerror(APP_NAME, "依赖医生打开失败：%s" % e,
                                 parent=self.root)
            return None
        messagebox.showinfo(
            APP_NAME,
            "依赖医生未启用：请把 config.json 里的 "
            "features.dependency_doctor 设为 true。",
            parent=self.root)
        return None

    def _open_finder(self):
        finder.open_finder(self)

    def _open_editor_file(self):
        """万能查看：直接选任意文件交给快捷修改（文本/Hex/PE/MD 模式）"""
        from tkinter import filedialog
        from . import editor
        # 和「切换目录」一致：文件对话框期间收起置顶悬浮球，免得挡住选择窗
        try:
            self.ball.withdraw()
        except Exception:
            pass
        try:
            path = filedialog.askopenfilename(
                parent=self.root, title="快捷修改 - 选择文件")
        finally:
            try:
                self.ball.deiconify()
                self.ball.lift()
            except Exception:
                pass
        if not path:
            return None
        try:
            return editor.open_editor(self, path)
        except Exception as e:
            messagebox.showerror(APP_NAME, "打开失败：%s" % e,
                                 parent=self.root)
            return None

    def _promptlab(self, path=None):
        """打开提示词工作台；带 path 时会把路径自动填进模板的路径槽"""
        try:
            return promptlab.open_window(self, path)
        except Exception as e:
            messagebox.showerror(APP_NAME, "提示词工作台打开失败：%s" % e,
                                 parent=self.root)
            return None

    def _settings_window(self):
        """工具栏设置按钮：设置窗口。全局只留一个，重复点就把它拉到前面"""
        old = getattr(self, "_settings_win", None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.deiconify()
                    old.lift()
                    old.focus_force()
                    return old
            except tk.TclError:
                pass
        win = self._popup("设置", 480, 640)
        self._settings_win = win
        # 按钮栏先靠底排，避免内容多时被挤出可视区域
        bar = tk.Frame(win, bg=C_BG)
        bar.pack(side="bottom", fill="x", padx=18, pady=(10, 14))
        tk.Button(bar, text="关闭", command=win.destroy, bg=C_ACCENT, fg="white",
                  activebackground=C_ACCENT_H, activeforeground="white", relief="flat",
                  bd=0, padx=18, pady=5, font=(core.FONT, 9), cursor="hand2").pack(side="right")
        tk.Label(win, text="设置", bg=C_BG, fg=C_TEXT,
                 font=(core.FONT, 11, "bold")).pack(anchor="w", padx=18, pady=(16, 2))
        tk.Label(win, text="改完立即生效，并随配置文件保存。", bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 8)).pack(anchor="w", padx=18, pady=(0, 8))
        tk.Frame(win, bg=C_BORDER, height=1).pack(fill="x", padx=18)
        # 设置内容放可滚动区域：窗口固定高度不够时，开关/按钮不会被挤出屏幕
        canvas = tk.Canvas(win, bg=C_BG, bd=0, highlightthickness=0)
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True, padx=(18, 0), pady=(4, 0))
        inner = tk.Frame(canvas, bg=C_BG)
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # 绑在窗口上：指针落在任意开关/按钮上也能滚（只绑 canvas 时子控件会截走事件）
        bind_canvas_wheel(win, canvas, area=win)
        self._switch(inner, "drill", "展开文件夹时自动下钻",
                     "某层只有子文件夹（没有文件）时，自动继续往里展开，直到遇到文件")
        self._switch(inner, "root_expand", "选完目录自动展开第一层",
                     "关掉后只显示根节点，层级都靠手动点开")
        self._switch(inner, "snap", "拖到屏幕边缘自动吸附",
                     "停靠后只留一条把手，鼠标移上去滑出")
        self._switch(inner, "edge_ball", "面板靠边时收成悬浮球",
                     "打开后拖面板贴近屏幕边缘松手：不贴条停靠，直接收回成悬浮球，"
                     "点球再展开")
        self._switch(inner, "auto_start", "开机自启动",
                     "登录 Windows 后自动运行 VBHelper（写注册表 Run 键，可随时关闭）")
        self._switch(inner, "debug_log", "记录诊断日志",
                     "排查目录树卡顿时打开：扫描/渲染耗时、超时、缓存与变更侦测"
                     "都会追加到日志文件")
        row = tk.Frame(inner, bg=C_BG)
        row.pack(fill="x", padx=0, pady=(4, 0))
        tk.Button(row, text="打开日志文件", command=self._open_debug_log,
                  bg=C_PANEL, fg=C_TEXT, activebackground=C_HOVER,
                  activeforeground=C_TEXT, relief="flat", bd=0, padx=10, pady=3,
                  font=(core.FONT, 8), cursor="hand2").pack(anchor="w", padx=26)
        self._theme_section(inner)
        hud.period_row(self, inner)
        launcher.section(self, inner)
        return win

    def _theme_section(self, parent):
        """外观设置：主题预设 + 自定义主色，改完悬浮球立即重绘"""
        tk.Label(parent, text="外观", bg=C_BG, fg=C_MUTED, font=(core.FONT, 8),
                 anchor="w").pack(fill="x", padx=18, pady=(12, 2))
        row = tk.Frame(parent, bg=C_BG)
        row.pack(fill="x", padx=18, pady=(4, 0))
        tk.Label(row, text="主题", bg=C_BG, fg=C_TEXT, font=(core.FONT, 9)
                 ).pack(side="left")
        presets = self.theme.presets()
        names = [p.name for p in presets]
        current = self.theme.current()[0]
        box = ttk.Combobox(row, values=names, state="readonly", width=10,
                           font=(core.FONT, 9))
        box.set(current.name)
        box.pack(side="left", padx=(10, 0))
        pid_by_name = {p.name: p.id for p in presets}
        box.bind("<<ComboboxSelected>>",
                 lambda e: self._set_theme_preset(pid_by_name[box.get()]))
        tk.Button(row, text="自定义主色…", command=self._pick_accent,
                  bg=C_PANEL, fg=C_TEXT, activebackground=C_HOVER,
                  activeforeground=C_TEXT, relief="flat", bd=0, padx=10, pady=3,
                  font=(core.FONT, 8), cursor="hand2").pack(side="left", padx=10)

    def _set_theme_preset(self, pid):
        self.cfg.setdefault("theme", {})["preset"] = pid
        save_cfg(self.cfg)
        self.theme.set_preset(pid)

    def _pick_accent(self):
        rgb, hexcolor = colorchooser.askcolor(
            parent=self._settings_win, title="选择主色")
        if not hexcolor:
            return
        self.cfg.setdefault("theme", {})["accent"] = hexcolor
        save_cfg(self.cfg)
        self.theme.set_accent(hexcolor)

    def _open_debug_log(self):
        p = debug_log_path()
        if os.path.exists(p):
            os.startfile(p)
        else:
            messagebox.showinfo(APP_NAME, "还没有日志。先在上方打开「记录诊断日志」"
                                          "再操作一轮目录树。", parent=self.root)

    def _switch(self, parent, key, label, hint):
        """一排设置开关：勾上即写配置；auto_start 以注册表真实状态为准"""
        if key == "auto_start":
            var = tk.BooleanVar(value=core.is_auto_start())
        else:
            var = tk.BooleanVar(value=bool(self.set.get(key)))
        row = tk.Frame(parent, bg=C_BG)
        row.pack(fill="x", padx=18, pady=(8, 0))
        tk.Checkbutton(row, text=label, variable=var,
                       command=lambda: self._set_option(key, var.get()),
                       bg=C_BG, fg=C_TEXT, selectcolor=C_PANEL, activebackground=C_BG,
                       activeforeground=C_TEXT, font=(core.FONT, 9), anchor="w", bd=0,
                       highlightthickness=0, cursor="hand2").pack(anchor="w")
        tk.Label(row, text=hint, bg=C_BG, fg=C_MUTED, font=(core.FONT, 8),
                 justify="left", wraplength=320).pack(anchor="w", padx=26)

    def _set_option(self, key, val):
        self.set[key] = val
        self.cfg["settings"] = self.set
        save_cfg(self.cfg)
        if key == "snap" and not val:
            self.snap_side = None
            self.parked = False
        elif key == "edge_ball":
            if val:
                # 切到收球模式：清掉已存在的贴条状态，避免残留把手
                self.snap_side = None
                self.parked = False
        elif key == "debug_log":
            core.DEBUG_LOG_ON = bool(val)   # 开关即时生效，不用重启
        elif key == "auto_start":
            ok, err = core.set_auto_start(bool(val))
            if not ok:
                messagebox.showerror(APP_NAME, "设置自启动失败：%s" % err)
        self._update_status()

    def _init_resizer(self, parent):
        """面板是无边框窗口，没有系统边框，得自己做缩放热区。

        锁定模式下这个把手不响应——不能移动，也不能改大小。
        """
        grip = tk.Frame(parent, bg=C_BG, width=16, height=16,
                        cursor="" if self.locked else "sizing")
        grip.place(relx=1.0, rely=1.0, anchor="se")
        for dx, dy in ((9, 9), (5, 9), (9, 5)):
            tk.Frame(grip, bg=C_MUTED, width=2, height=2).place(x=dx, y=dy)
        grip.bind("<Button-1>", self._rz_press)
        grip.bind("<B1-Motion>", self._motion)
        grip.bind("<ButtonRelease-1>", self._release)
        self.rz_grip = grip

    def _rz_press(self, e):
        p = self.panel
        self._drag = {"win": p, "x": e.x_root, "y": e.y_root,
                      "w": p.winfo_width(), "h": p.winfo_height(),
                      "moved": False, "resize": True, "locked": self.locked}

    def _resize_to(self, e):
        d = self._drag
        L, T, R, B = self.work
        w = max(300, min(d["w"] + (e.x_root - d["x"]), R - self.panel.winfo_x()))
        h = max(220, min(d["h"] + (e.y_root - d["y"]), B - self.panel.winfo_y()))
        self.panel.geometry("%dx%d" % (w, h))

    def _toggle_panel(self):
        if self.panel is not None and self.panel.winfo_ismapped():
            self._collapse()
        else:
            self._expand()

    def _expand(self):
        if self.root_dir and not os.path.isdir(self.root_dir):
            self.root_dir = None          # 上次的目录被删/盘没挂上，重新挑
        if not self.root_dir:
            self._pick_dir()
            return
        self._ensure_panel()
        # 启动后第一次展开：上次的目录只存在配置里，树还是空的，这里补一次加载
        if not self.tree.get_children(""):
            self._load(self.root_dir)
        if self.snap_side and self.parked:
            self._park(self.panel)
        p = self.panel
        p.deiconify()
        p.lift()
        self.ball.withdraw()
        if self._preheat_job:
            self.root.after_cancel(self._preheat_job)
            self._preheat_job = None
        self._preheat_tick()
        self._update_status()

    def _collapse(self):
        if self.panel is None:
            return
        self.panel.withdraw()
        self.snap_side = None
        self.parked = False
        self._stop_slide()
        if self._preheat_job:
            self.root.after_cancel(self._preheat_job)
            self._preheat_job = None
        self.ball.deiconify()
        self.ball.lift()

    def _toggle_topmost(self):
        if self.locked:      # 锁定期间置顶由锁按钮统一接管
            return
        self.topmost = not self.topmost
        try:
            self.panel.wm_attributes("-topmost", self.topmost)
        except Exception:
            pass
        self._set_btn_icon(self.pin_btn, self._pin_icon())
        self.cfg["topmost"] = self.topmost
        save_cfg(self.cfg)

    def _pin_icon(self):
        return "map-pin" if self.topmost else "navigation"

    def _lock_icon(self):
        return "unlock" if self.locked else "lock"

    def _toggle_lock(self):
        self._set_lock(not self.locked)

    def _set_lock(self, on):
        """锁定：强制置顶 + 钉死位置，直到再次点击解除"""
        self.locked = on
        if on:
            self._pre_lock_topmost = self.topmost
            self.topmost = True
            if self.parked and self.snap_side:
                self._unpark()      # 先滑回可见位置，再解除吸附
            self.parked = False
            self.snap_side = None
        else:
            self.topmost = bool(self._pre_lock_topmost)
        self.cfg["locked"] = on
        save_cfg(self.cfg)
        for w in (self.panel, self.ball):
            try:
                w.wm_attributes("-topmost", self.topmost)
            except Exception:
                pass
        if self.panel is not None:
            self._set_btn_icon(self.pin_btn, self._pin_icon(),
                               C_MUTED if on else C_TEXT)
            self._set_btn_icon(self.lock_btn, self._lock_icon(),
                               C_ACCENT_H if on else C_MUTED)
            self.lock_btn.configure(bg=C_HOVER if on else C_BG)
            cur = "" if on else "fleur"
            for w in (self.title_bar, self.title_lbl):
                w.configure(cursor=cur)
            if self.rz_grip is not None:
                self.rz_grip.configure(cursor="" if on else "sizing")
        self._update_status()

    def _pick_dir(self):
        init = self.cfg.get("last_dir") or os.path.expanduser("~")
        try:
            self.ball.withdraw()
        except Exception:
            pass
        try:
            d = filedialog.askdirectory(parent=self.root, initialdir=init,
                                        title="选择要浏览的文件夹")
        finally:
            try:
                self.ball.deiconify()
                self.ball.lift()
            except Exception:
                pass
        if not d:
            return
        self._ensure_panel()
        self._load(d)
        self._expand()

    def _update_status(self, path=None):
        if self.tree is None:
            return
        p = path or self.root_dir or ""
        extra = []
        if self.locked:
            extra.append("已锁定")
        if self.truncated:
            extra.append("超大目录已截断")
        tail = ("   ·   " + "   ·   ".join(extra)) if extra else ""
        self.status.configure(text="%s   ·   已加载 %d 项%s   ·   右键：属性/标记/注释/复制路径   ·   点右侧 i 看注释"
                              % (p, self._loaded, tail))

    def _save_ball_pos(self):
        self.cfg["ball_pos"] = [self.ball.winfo_x(), self.ball.winfo_y()]
        save_cfg(self.cfg)

    def _quit(self):
        """工具栏关闭按钮 / 右键菜单退出：先弹确认框。

        早先用的是无边框窗口 + grab_set，一旦窗口没正常映射，grab 会把全部点击
        吞掉，表现就是「点了没反应、整个程序僵住」；改成普通窗口，稳定优先。
        """
        try:
            if self._quit_dialog is not None:
                try:
                    if self._quit_dialog.winfo_exists():
                        self._quit_dialog.lift()
                        self._quit_dialog.focus_force()
                        return
                except tk.TclError:
                    pass
            d = tk.Toplevel(self.root)
            self._quit_dialog = d
            d.title("退出 " + APP_NAME)
            d.configure(bg=C_BG)
            d.resizable(False, False)
            try:
                d.attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(d, text="退出 VBHelper？", bg=C_BG, fg=C_TEXT,
                     font=(core.FONT, 11, "bold")).pack(anchor="w", padx=20, pady=(18, 4))
            tk.Label(d, text="悬浮球与目录面板会一并关闭，配置会保留。", bg=C_BG,
                     fg=C_MUTED, font=(core.FONT, 9)).pack(anchor="w", padx=20)
            btns = tk.Frame(d, bg=C_BG)
            btns.pack(fill="x", padx=20, pady=(16, 18))
            tk.Button(btns, text="退出", command=self._do_quit, bg="#b3261e", fg="white",
                      activebackground="#d33a30", activeforeground="white", relief="flat",
                      bd=0, padx=18, pady=6, font=(core.FONT, 9), cursor="hand2").pack(
                side="right", padx=(8, 0))
            tk.Button(btns, text="取消", command=d.destroy, bg=C_PANEL, fg=C_TEXT,
                      activebackground=C_HOVER, activeforeground=C_TEXT,
                      relief="flat", bd=0, padx=18, pady=6, font=(core.FONT, 9),
                      cursor="hand2").pack(side="right")
            d.protocol("WM_DELETE_WINDOW", d.destroy)
            d.bind("<Escape>", lambda e: d.destroy())
            self._center_on_panel(d)
            d.after(80, lambda: d.focus_force())
        except Exception:
            self._do_quit()

    def _do_quit(self):
        try:
            self._save_ball_pos()
        except Exception:
            pass
        try:
            self._tray_cleanup()    # 移除托盘图标，避免通知区残留
        except Exception:
            pass
        try:
            hud.shutdown(self)      # 先停止仪表盘采集进程，避免退出后残留
        except Exception:
            pass
        try:
            music.shutdown(self)    # 停掉音乐并保存播放位置
        except Exception:
            pass
        try:
            # 功能窗口 -> 后台任务 -> HTTP/缓存；必须在 destroy 之前
            if getattr(self, "ctx", None) is not None:
                self.ctx.shutdown()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _ball_menu(self, e):
        m = self._mk_menu()
        if self.root_dir:
            m.add_command(label="快速搜索…", command=self._open_finder)
            m.add_command(label="切换目录…", command=self._pick_dir)
            m.add_command(label="刷新", command=self._refresh)
            m.add_command(label="收起面板", command=self._collapse)
        else:
            m.add_command(label="打开文件夹…", command=self._pick_dir)
        m.add_separator()
        m.add_command(label="快捷修改…", command=self._open_editor_file)
        m.add_separator()
        m.add_command(label="退出 " + APP_NAME, command=self._quit)
        m.post(e.x_root, e.y_root)
