"""提示词工作台：分类浏览 125+ 套专业提示词模板，搜索、一键复制、自定义与导入导出。

界面约定：
- 左边是分类树，点分类会自动展开并选中第一条，省得每次都去点小三角
- 右边是预览区，**可以直接编辑**：高亮的 [请输入文本] 就是要替换的位置，
  改完点复制，复制的就是改过的内容——比弹一堆输入框填占位符顺手得多
- 从目录树右键「用提示词工作台」进来时会带着路径，自动填进第一个路径槽
"""
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import core, prompts
from .core import *
from .ui import icons, render

HINT = "提示：下面直接改——把高亮的 [请输入文本] 换成你的内容，再点「复制提示词」。"
PH_TAG = "ph"          # 普通占位符（黄）
PATH_TAG = "path_ph"   # 会被自动填路径的那个（绿）

def menu_title(app):
    """菜单上显示的名字：带上模板条数，一眼就知道里面有多少东西"""
    try:
        return "提示词工作台（%d 套模板）" % len(prompts.merged(app))
    except Exception:
        return "提示词工作台"

def open_window(app, path=None):
    """打开工作台；已经开着就拉到前面，并把新带进来的路径接上"""
    old = getattr(app, "_promptlab_win", None)
    if old is not None and old.alive():
        old.bring(path)
        return old
    win = PromptLabWindow(app, path)
    app._promptlab_win = win
    return win

class PromptLabWindow:
    def __init__(self, app, path=None):
        self.app = app
        self.carried = self._norm(path)
        self.cur = None            # 当前模板
        self.raw = ""              # 当前模板的原始正文，用于「还原」
        self._search_job = None
        self.node_of = {}          # 模板 id -> 树节点 iid
        self.id_of = {}            # 树节点 iid -> 模板 id
        self.cat_open = set()      # 重建树时要保持展开的分类

        win = tk.Toplevel(app.root)
        self.win = win
        win.title("提示词工作台 - " + APP_NAME)
        win.configure(bg=C_BG)
        win.geometry("980x680")
        win.minsize(780, 480)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.bind("<Escape>", lambda e: win.destroy())

        self._init_style()
        self._build_head()
        self._build_body()
        self._build_status()
        self._update_carry()
        self._refresh_tree(select_first=True)
        self._center()
        win.after(80, self.entry_search.focus_set)

    def alive(self):
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def bring(self, path=None):
        """从目录树再点一次：把新路径接上，并把窗口拉到最前"""
        new = self._norm(path)
        if new and new != self.carried:
            self.carried = new
            self._update_carry()
            if self.cur is not None:
                self._show(self.cur)
            self.say("已带入路径：%s" % new)
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:
            pass

    # ---------------- 外壳 ----------------
    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Lab.Treeview", background=C_PANEL, foreground=C_TEXT,
                     fieldbackground=C_PANEL, borderwidth=0, rowheight=23,
                     font=(core.FONT, 9))
        st.map("Lab.Treeview", background=[("selected", C_SELECT)],
               foreground=[("selected", "#ffffff")])
        st.configure("Lab.Vertical.TScrollbar", background="#3a3d46",
                     troughcolor=C_PANEL, bordercolor=C_PANEL, arrowcolor=C_MUTED,
                     borderwidth=0, width=12)

    def _center(self):
        """摆位置交给主类统一处理：它会连标题栏一起算，免得窗口探出屏幕把底部按钮顶出去"""
        self.app._center_on_panel(self.win)

    def _icon_photo(self, name, size, color):
        ph = render.photo(icons.draw(name, size, color),
                          key="promptlab:ico:%s:%d:%s" % (name, size, color))
        self._icon_cache = getattr(self, "_icon_cache", {})
        self._icon_cache[(name, size, color)] = ph
        return ph

    def _btn(self, parent, text, cmd, accent=False, icon=None):
        kw = {}
        if icon:
            color = "white" if accent else C_TEXT
            kw = {"image": self._icon_photo(icon, 14, color), "compound": "left"}
        b = tk.Button(parent, text=text, command=cmd,
                      bg=C_ACCENT if accent else C_PANEL,
                      fg="white" if accent else C_TEXT,
                      activebackground=C_ACCENT_H if accent else C_HOVER,
                      activeforeground="white", relief="flat", bd=0,
                      padx=12, pady=4, font=(core.FONT, 9), cursor="hand2", **kw)
        return b

    def _build_head(self):
        head = tk.Frame(self.win, bg=C_BG)
        head.pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(head, text="提示词工作台", bg=C_BG, fg=C_TEXT,
                 font=(core.FONT, 12, "bold")).pack(side="left")
        self.lbl_count = tk.Label(head, text="", bg=C_BG, fg=C_MUTED,
                                  font=(core.FONT, 8))
        self.lbl_count.pack(side="left", padx=10)
        self._btn(head, "关闭", self.win.destroy).pack(side="right")

        row = tk.Frame(self.win, bg=C_BG)
        row.pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(row, image=self._icon_photo("search", 14, C_MUTED),
                 bg=C_BG).pack(side="left")
        self.var_search = tk.StringVar()
        self.entry_search = tk.Entry(row, textvariable=self.var_search, bg=C_PANEL,
                                     fg=C_TEXT, insertbackground=C_TEXT, relief="flat",
                                     bd=0, highlightthickness=0, font=(core.FONT, 9))
        self.entry_search.pack(side="left", fill="x", expand=True, padx=(6, 10), ipady=4)
        self.entry_search.bind("<KeyRelease>", self._on_search)
        self._btn(row, "清空", self._clear_search).pack(side="left")
        self._btn(row, "导出…", self._export).pack(side="right", padx=(6, 0))
        self._btn(row, "导入…", self._import).pack(side="right")
        self._btn(row, "新建模板…", self._new_custom,
                  accent=True).pack(side="right", padx=6)

        # 带入路径的横幅：常驻一行，只换文字，避免动态 pack 把布局挤乱
        self.carry = tk.Frame(self.win, bg=C_BG)
        self.carry.pack(fill="x", padx=16, pady=(6, 0))
        self.lbl_carry = tk.Label(self.carry, text="", bg=C_BG, fg=C_MUTED,
                                  font=(core.FONT, 8), anchor="w", justify="left")
        self.lbl_carry.pack(side="left")
        self.btn_clear_carry = tk.Label(
            self.carry, text="清除", bg=C_BG, fg=C_MUTED, font=(core.FONT, 8),
            image=self._icon_photo("x", 12, C_MUTED), compound="left",
            cursor="hand2")
        for w in (self.lbl_carry, self.btn_clear_carry):
            w.bind("<Button-1>", lambda e: self._clear_carry())

        tk.Frame(self.win, bg=C_BORDER, height=1).pack(fill="x", padx=16, pady=(8, 0))

    def _build_body(self):
        body = tk.Frame(self.win, bg=C_BG)
        body.pack(fill="both", expand=True, padx=16, pady=8)
        body.columnconfigure(0, minsize=250)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=C_PANEL)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.tree = ttk.Treeview(left, show="tree", style="Lab.Treeview",
                                 selectmode="browse")
        vsb = ttk.Scrollbar(left, style="Lab.Vertical.TScrollbar", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("cat", foreground=C_DIR)
        self.tree.tag_configure("tpl", foreground=C_TEXT)
        self.tree.tag_configure("mine", foreground="#ffcf6b")
        self.tree.tag_configure("empty", foreground=C_MUTED)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<<TreeviewOpen>>", lambda e: self._remember_open())
        self.tree.bind("<<TreeviewClose>>", lambda e: self._remember_open())

        right = tk.Frame(body, bg=C_BG)
        right.grid(row=0, column=1, sticky="nsew")

        self.lbl_title = tk.Label(right, text="", bg=C_BG, fg=C_TEXT,
                                  font=(core.FONT, 11, "bold"), anchor="w",
                                  justify="left", wraplength=620)
        self.lbl_title.pack(fill="x")
        self.lbl_use = tk.Label(right, text="", bg=C_BG, fg=C_MUTED,
                                font=(core.FONT, 8), anchor="w", justify="left",
                                wraplength=620)
        self.lbl_use.pack(fill="x", pady=(2, 0))
        self.lbl_tags = tk.Label(right, text="", bg=C_BG, fg=C_MUTED,
                                 font=(core.FONT, 8), anchor="w")
        self.lbl_tags.pack(fill="x", pady=(2, 4))

        wrap = tk.Frame(right, bg=C_BORDER)
        wrap.pack(fill="both", expand=True)
        self.txt = tk.Text(wrap, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                           bd=0, relief="flat", highlightthickness=0,
                           font=(core.FONT, 9), wrap="word", padx=12, pady=10,
                           undo=True)
        tsb = ttk.Scrollbar(wrap, style="Lab.Vertical.TScrollbar", command=self.txt.yview)
        self.txt.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        # 占位符高亮：一眼看出"要我填哪儿"
        self.txt.tag_configure(PH_TAG, background="#4a3c14", foreground="#ffd479")
        self.txt.tag_configure(PATH_TAG, background="#123a2e", foreground="#7ee787")

        self.lbl_note = tk.Label(right, text=HINT, bg=C_BG, fg=C_MUTED,
                                 font=(core.FONT, 8), anchor="w", justify="left",
                                 wraplength=620)
        self.lbl_note.pack(fill="x", pady=(6, 4))

        bar = tk.Frame(right, bg=C_BG)
        bar.pack(fill="x")
        self._btn(bar, "复制提示词", self._copy, accent=True).pack(side="left")
        self.btn_fav = self._btn(bar, "收藏", self._toggle_fav, icon="star")
        self.btn_fav.pack(side="left", padx=6)
        self._btn(bar, "还原", self._restore).pack(side="left")
        self._btn(bar, "下一个空位", self._next_slot,
                  icon="arrow-right").pack(side="left", padx=6)
        self.btn_path = self._btn(bar, "插入路径", self._insert_path)
        self.btn_del = self._btn(bar, "删除模板", self._del_custom)
        self.btn_edit = self._btn(bar, "编辑模板", self._edit_custom)

    def _build_status(self):
        tk.Frame(self.win, bg=C_BORDER, height=1).pack(side="bottom", fill="x")
        self.status = tk.Label(self.win, text="就绪", bg=C_BG, fg=C_MUTED,
                               font=(core.FONT, 8), anchor="w")
        self.status.pack(side="bottom", fill="x", padx=16, pady=4)

    def say(self, msg):
        try:
            self.status.configure(text=msg)
        except tk.TclError:
            pass

    # ---------------- 分类树 ----------------
    def _keyword(self):
        return (self.var_search.get() or "").strip().lower()

    def _match(self, tpl, kw):
        if not kw:
            return True
        hay = " ".join((tpl.get("title", ""), tpl.get("use", ""), tpl.get("tags", ""),
                        tpl.get("body", ""),
                        prompts.CAT_LABEL.get(tpl.get("cat"), ""))).lower()
        return all(part in hay for part in kw.split())

    def _groups(self, hits, kw):
        """分组：收藏 / 最近 / 我的 固定排最前，然后按分类；搜索时只留命中的"""
        by_id = {t["id"]: t for t in prompts.merged(self.app)}
        hit_ids = {t["id"] for t in hits}
        pick = lambda ids: [by_id[i] for i in ids if i in by_id and i in hit_ids]
        out = [("fav", "收藏", pick(prompts.favourites(self.app))),
               ("recent", "最近使用", pick(prompts.recent(self.app))),
               ("mine", "我的模板", [t for t in hits if t.get("custom")])]
        for key in prompts.CAT_ORDER:
            rows = [t for t in hits if t["cat"] == key]
            if rows:
                out.append(("cat:" + key, prompts.cat_title(key), rows))
        # 没搜索时把三个固定分组都留着（让用户知道有这些功能）；搜索时只留命中的
        if not kw:
            return out
        return [g for g in out if g[2]]

    def _group_icon(self, gid):
        if gid == "fav":
            return "star"
        if gid == "recent":
            return "clock"
        if gid == "mine":
            return "edit-2"
        if gid.startswith("cat:"):
            return prompts.CAT_ICON.get(gid[4:], "file-text")
        return "file-text"

    def _refresh_tree(self, keep=None, select_first=False):
        kw = self._keyword()
        items = prompts.merged(self.app)
        hits = [t for t in items if self._match(t, kw)]
        self.id_of.clear()
        self.node_of.clear()
        self.tree.delete(*self.tree.get_children(""))

        groups = self._groups(hits, kw)
        for gid, label, rows in groups:
            self.tree.insert("", "end", iid=gid, text="%s (%d)" % (label, len(rows)),
                             image=self._icon_photo(self._group_icon(gid), 14,
                                                    C_DIR),
                             tags=("cat",))
            for t in rows:
                node = "%s|%s" % (gid, t["id"])
                self.node_of[t["id"]] = node
                self.id_of[node] = t["id"]
                self.tree.insert(gid, "end", iid=node, text=t["title"],
                                 tags=("mine" if t.get("custom") else "tpl",))
            if rows and (kw or gid.startswith("cat:")):
                self.tree.item(gid, open=True)
            elif gid in self.cat_open:
                self.tree.item(gid, open=True)

        self.lbl_count.configure(
            text=("命中 %d / %d 条" % (len(hits), len(items))) if kw
            else ("共 %d 条模板" % len(items)))

        target = keep if (keep and self.tree.exists(keep)) else self._first_leaf()
        if target:
            self.tree.selection_set(target)
            self.tree.see(target)
        elif not hits:
            self._show_empty("没找到匹配的模板，换个词试试。")

    def _first_leaf(self):
        for gid in self.tree.get_children(""):
            kids = self.tree.get_children(gid)
            if kids:
                return kids[0]
        return None

    def _remember_open(self):
        for gid in self.tree.get_children(""):
            if self.tree.item(gid, "open"):
                self.cat_open.add(gid)
            else:
                self.cat_open.discard(gid)

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        tid = self.id_of.get(iid)
        if tid:
            # 目标条目已选中时跳过重绘，否则会覆盖用户已填写的内容
            if self.cur is not None and self.cur.get("id") == tid:
                return
            tpl = prompts.find(self.app, tid)
            if tpl is not None:
                self._show(tpl)
            return
        # 点的是分类节点：自动展开并选第一条，省得每次都要点三角
        self.cat_open.add(iid)
        kids = self.tree.get_children(iid)
        if kids:
            self.tree.item(iid, open=True)
            self.tree.selection_set(kids[0])
        else:
            self._show_empty(
                "这个分类下还没有模板。\n\n"
                "「我的模板」可以点右上角「新建模板…」加，或者从别处「导入…」。")

    # ---------------- 预览 ----------------
    def _show_empty(self, msg):
        self.cur = None
        self.raw = ""
        self.lbl_title.configure(text="")
        self.lbl_use.configure(text=msg)
        self.lbl_tags.configure(text="")
        self.txt.delete("1.0", "end")
        self._paint_slots()
        self._sync_buttons()

    def _compose(self, tpl):
        """完整正文；带了路径就自动填进第一个路径槽"""
        body = prompts.full_text(tpl)
        note = ""
        if self.carried:
            label = prompts.path_slot_label(body)
            if label:
                body, ok = prompts.fill_path(body, self.carried)
                if ok:
                    note = "已把路径填进「%s」；想换到别的地方就自己改。" % label
        return body, note

    def _show(self, tpl):
        if tpl is None:
            return
        self.cur = tpl
        body, note = self._compose(tpl)
        self.raw = body
        self.lbl_title.configure(text=tpl["title"])
        use = tpl.get("use") or ""
        self.lbl_use.configure(text=("适用：" + use) if use else "")
        tags = tpl.get("tags") or ""
        kind = "我的模板" if tpl.get("custom") else prompts.cat_title(tpl["cat"])
        self.lbl_tags.configure(text="%s%s%s" % (kind, "　·　" if tags else "", tags))
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", body)
        self._paint_slots()
        self.lbl_note.configure(text=note or HINT)
        self._sync_buttons()
        self._focus_slot(0)

    def _paint_slots(self):
        """高亮所有 [请输入文本]；会被自动填路径的那一处用另一种颜色"""
        self.txt.tag_remove(PH_TAG, "1.0", "end")
        self.txt.tag_remove(PATH_TAG, "1.0", "end")
        text = self.txt.get("1.0", "end-1c")
        green = prompts.path_slot_label(text) if self.carried else ""
        for i, end, label in prompts.slots(text):
            tag = PATH_TAG if (green and label == green) else PH_TAG
            self.txt.tag_add(tag, "1.0+%dc" % i, "1.0+%dc" % end)

    def _focus_slot(self, i, end=None):
        """把某个占位符选中并滚到可见处，用户可以直接打字替换"""
        self.txt.tag_remove("sel", "1.0", "end")
        if end is None:
            found = prompts.slots(self._current_text())
            if not found:
                return
            i, end, _l = found[0]
        self.txt.tag_add("sel", "1.0+%dc" % i, "1.0+%dc" % end)
        self.txt.mark_set("insert", "1.0+%dc" % i)
        self.txt.see("1.0+%dc" % i)
        self.txt.focus_set()

    def _sync_buttons(self):
        """自定义模板才给编辑/删除；没带路径就别显示「插入路径」"""
        for w in (self.btn_edit, self.btn_del):
            w.pack_forget()
        if self.cur is not None and self.cur.get("custom"):
            self.btn_del.pack(side="right")
            self.btn_edit.pack(side="right", padx=(0, 6))
        if self.carried:
            self.btn_path.pack(side="left")
        else:
            self.btn_path.pack_forget()
        fav = self.cur is not None and self.cur["id"] in prompts.favourites(self.app)
        self.btn_fav.configure(text="已收藏" if fav else "收藏")

    # ---------------- 顶部路径 ----------------
    def _norm(self, path):
        if not path:
            return ""
        try:
            return os.path.normpath(str(path))
        except Exception:
            return str(path)

    def _update_carry(self):
        """横幅常驻，只换文字：带路径时显示路径并给出清除入口，没带时顺便教怎么带"""
        if self.carried:
            self.lbl_carry.configure(text="已带入路径　" + self.carried, fg=C_DIR)
            self.btn_clear_carry.pack(side="left", padx=8)
        else:
            self.lbl_carry.configure(
                text="未带入路径　（右击目录树里的文件或文件夹 → 「用提示词工作台」，"
                     "路径会自动填进模板的路径槽）", fg=C_MUTED)
            self.btn_clear_carry.pack_forget()

    def _clear_carry(self):
        if not self.carried:
            return
        self.carried = ""
        self._update_carry()
        if self.cur is not None:
            self._show(self.cur)
        self.say("已清除带入的路径。")

    # ---------------- 动作 ----------------
    def _on_search(self, _e=None):
        if self._search_job:
            try:
                self.win.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.win.after(220, lambda: self._refresh_tree())

    def _clear_search(self):
        self.var_search.set("")
        self.cat_open.clear()
        self._refresh_tree(select_first=True)

    def _current_text(self):
        try:
            return self.txt.get("1.0", "end-1c")
        except tk.TclError:
            return ""

    def _copy(self):
        if self.cur is None:
            self.say("先选一条模板。")
            return
        text = self._current_text()
        if not text.strip():
            self.say("内容是空的，没什么可复制。")
            return
        if not set_clipboard(text):
            try:
                self.win.clipboard_clear()
                self.win.clipboard_append(text)
            except Exception:
                messagebox.showerror(
                    APP_NAME, "复制失败：剪贴板被别的程序占着，过一会儿再试。",
                    parent=self.win)
                return
        prompts.push_recent(self.app, self.cur["id"])
        left = text.count(prompts.PLACEHOLDER)
        tail = "，还有 %d 处 [请输入文本] 没填" % left if left else "，占位符都填完了"
        self.say("已复制「%s」%s" % (self.cur["title"], tail))
        self._refresh_tree(keep=self.node_of.get(self.cur["id"]))

    def _restore(self):
        if self.cur is None:
            return
        self._show(self.cur)
        self.say("已还原成模板原文。")

    def _toggle_fav(self):
        if self.cur is None:
            return
        on = prompts.toggle_fav(self.app, self.cur["id"])
        self.say("已收藏，排在「收藏」最上面。" if on else "已取消收藏。")
        self._refresh_tree(keep=self.node_of.get(self.cur["id"]))

    def _next_slot(self):
        """从光标往后找下一个占位符并选中；到底了就回到第一个"""
        text = self._current_text()
        found = prompts.slots(text)
        if not found:
            self.say("这个模板里没有 [请输入文本] 了，可以直接复制。")
            return
        try:
            cur_off = len(self.txt.get("1.0", self.txt.index("insert")))
        except tk.TclError:
            cur_off = 0
        for i, end, _l in found:
            if i >= cur_off:
                self._focus_slot(i, end)
                return
        self._focus_slot(found[0][0], found[0][1])
        self.say("已经到底了，回到第一个占位符。")

    def _insert_path(self):
        if not self.carried:
            return
        try:
            self.txt.insert(self.txt.index("insert"), self.carried)
        except tk.TclError:
            return
        self._paint_slots()
        self.say("已把路径插到光标处。")

    # ---------------- 自定义模板 ----------------
    def _new_custom(self):
        self._template_dialog(None)

    def _edit_custom(self):
        if self.cur is not None and self.cur.get("custom"):
            self._template_dialog(self.cur)

    def _del_custom(self):
        if self.cur is None or not self.cur.get("custom"):
            return
        title = self.cur["title"]
        if not messagebox.askyesno(
                APP_NAME, "删除模板「%s」？\n\n删了就找不回来了（内置的 125 条不受影响）。"
                          % title, parent=self.win):
            return
        items = [t for t in prompts.custom(self.app) if t["id"] != self.cur["id"]]
        prompts.save_custom(self.app, items)
        self.cur = None
        self.cat_open.add("mine")
        self._refresh_tree(select_first=True)
        self.say("已删除「%s」。" % title)

    def _template_dialog(self, tpl):
        d = tk.Toplevel(self.win)
        d.title(("编辑模板" if tpl else "新建模板") + " - " + APP_NAME)
        d.configure(bg=C_BG)
        d.geometry("720x580")
        d.minsize(560, 420)
        try:
            d.attributes("-topmost", True)
        except Exception:
            pass
        src = tpl or {}
        orig = (src.get("title") or "", src.get("use") or "", src.get("tags") or "",
                src.get("body") or "")
        var_title = tk.StringVar(value=src.get("title") or "")
        var_use = tk.StringVar(value=src.get("use") or "")
        var_tags = tk.StringVar(value=src.get("tags") or "")
        labels = [prompts.cat_title(k) for k in prompts.CAT_ORDER]
        var_cat = tk.StringVar(value=prompts.cat_title(src.get("cat") or "general"))

        def field(label, var, hint=""):
            tk.Label(d, text=(label + ("　" + hint if hint else "")), bg=C_BG,
                     fg=C_MUTED, font=(core.FONT, 8)).pack(anchor="w", padx=16,
                                                           pady=(10, 0))
            e = tk.Entry(d, textvariable=var, bg=C_PANEL, fg=C_TEXT,
                         insertbackground=C_TEXT, relief="flat", bd=0,
                         highlightthickness=0, font=(core.FONT, 9))
            e.pack(fill="x", padx=16, ipady=4)
            return e

        tk.Label(d, text="标题", bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 8)).pack(anchor="w", padx=16, pady=(14, 0))
        e_title = tk.Entry(d, textvariable=var_title, bg=C_PANEL, fg=C_TEXT,
                           insertbackground=C_TEXT, relief="flat", bd=0,
                           highlightthickness=0, font=(core.FONT, 10))
        e_title.pack(fill="x", padx=16, ipady=5)

        crow = tk.Frame(d, bg=C_BG)
        crow.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(crow, text="分类", bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 8)).pack(anchor="w")
        om = tk.OptionMenu(crow, var_cat, *labels)
        om.configure(bg=C_PANEL, fg=C_TEXT, activebackground=C_HOVER, relief="flat",
                     bd=0, highlightthickness=0, font=(core.FONT, 9), padx=8)
        om["menu"].configure(bg=C_PANEL, fg=C_TEXT, activebackground=C_ACCENT)
        om.pack(anchor="w", pady=(2, 0))

        field("适用场景", var_use, "什么时候该用它，一句话")
        field("标签", var_tags, "空格分隔，方便搜索")

        tk.Label(d, text="正文　用 %s 表示要让用户自己填的地方，界面里会显示成黄色"
                 % prompts.PLACEHOLDER, bg=C_BG, fg=C_MUTED, font=(core.FONT, 8),
                 anchor="w", justify="left").pack(anchor="w", padx=16, pady=(10, 0))
        wrap = tk.Frame(d, bg=C_BORDER)
        wrap.pack(fill="both", expand=True, padx=16, pady=(4, 0))
        txt = tk.Text(wrap, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT, bd=0,
                      relief="flat", highlightthickness=0, font=(core.FONT, 9),
                      wrap="word", padx=10, pady=8, undo=True)
        tsb = ttk.Scrollbar(wrap, style="Lab.Vertical.TScrollbar", command=txt.yview)
        txt.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", src.get("body") or "")
        txt.tag_configure(PH_TAG, background="#4a3c14", foreground="#ffd479")
        txt.bind("<KeyRelease>", lambda e: _paint(txt))

        def _paint(w):
            w.tag_remove(PH_TAG, "1.0", "end")
            body = w.get("1.0", "end-1c")
            for i, end, _l in prompts.slots(body):
                w.tag_add(PH_TAG, "1.0+%dc" % i, "1.0+%dc" % end)

        _paint(txt)

        def try_close():
            """关闭前确认未保存的模板内容"""
            cur = (var_title.get(), var_use.get(), var_tags.get(),
                   txt.get("1.0", "end-1c"))
            if cur != orig:
                answer = messagebox.askyesnocancel(
                    APP_NAME, "模板有未保存的修改，保存吗？", parent=d)
                if answer is None:
                    return
                if answer:
                    ok()
                    return
            d.destroy()

        d.bind("<Escape>", lambda e: try_close())

        bar = tk.Frame(d, bg=C_BG)
        bar.pack(fill="x", padx=16, pady=(10, 14))

        def ok():
            body = txt.get("1.0", "end-1c").strip()
            title = var_title.get().strip()
            if not title:
                messagebox.showinfo(APP_NAME, "给模板起个标题吧，不然在列表里认不出来。",
                                    parent=d)
                return
            if not body:
                messagebox.showinfo(APP_NAME, "正文不能为空。", parent=d)
                return
            key = next((k for k in prompts.CAT_ORDER
                        if prompts.cat_title(k) == var_cat.get()), "general")
            items = prompts.custom(self.app)
            data = {"id": src.get("id") or prompts.new_custom_id(items),
                    "cat": key, "title": title,
                    "use": var_use.get().strip(), "tags": var_tags.get().strip(),
                    "body": body, "custom": True}
            if tpl:
                items = [data if t["id"] == tpl["id"] else t for t in items]
            else:
                items.insert(0, data)
            prompts.save_custom(self.app, items)
            d.destroy()
            self.cat_open.add("mine")
            self._refresh_tree(keep=self.node_of.get(data["id"]))
            self.say("已保存「%s」，在「我的模板」里。" % title)

        self._btn(bar, "确定", ok, accent=True).pack(side="right")
        self._btn(bar, "取消", try_close).pack(side="right", padx=6)
        d.after(80, e_title.focus_set)
        return d

    # ---------------- 导入导出 ----------------
    def _export(self):
        only_custom = False
        if prompts.custom(self.app):
            ans = messagebox.askyesnocancel(
                APP_NAME,
                "导出哪些？\n\n「是」只导出我自己加的模板\n"
                "「否」导出全部（含内置模板，适合备份或分享给别人）\n"
                "「取消」不导出", parent=self.win)
            if ans is None:
                return
            only_custom = bool(ans)
        dft = prompts.default_export_path(self.app)
        path = filedialog.asksaveasfilename(
            parent=self.win, title="导出提示词模板",
            initialdir=os.path.dirname(dft), initialfile=os.path.basename(dft),
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            prompts.export_file(self.app, path, only_custom)
        except Exception as e:
            messagebox.showerror(APP_NAME, "导出失败：%s" % e, parent=self.win)
            return
        n = len(prompts.custom(self.app)) if only_custom else len(prompts.merged(self.app))
        self.say("已导出 %d 条到 %s" % (n, path))

    def _import(self):
        path = filedialog.askopenfilename(
            parent=self.win, title="导入提示词模板",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception as e:
            messagebox.showerror(APP_NAME, "读不了这个文件：%s" % e, parent=self.win)
            return
        parsed, err = prompts.parse_import(raw)
        if err:
            messagebox.showerror(APP_NAME, err, parent=self.win)
            return
        n = prompts.import_into(self.app, parsed)
        self.cat_open.add("mine")
        self._refresh_tree(select_first=True)
        self.say("已导入 %d 条模板，都在「我的模板」下。" % n)
