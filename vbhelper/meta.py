"""目录树标记与注释：读写项目内的 .vbhelper.json。

标记按相对路径存储，项目目录不可写时回退到全局配置。
写盘采用临时文件加替换，避免中断产生半截文件。
"""
from . import core
from .core import *

class MetaMixin:
    def _init_marks(self):
        """标记/注释状态初始化：先读全局配置，项目内的 .vbhelper.json 在 _load 时覆盖"""
        self.marks = dict(self.cfg.get("marks") or {})
        self.notes = dict(self.cfg.get("notes") or {})
        self._meta_owner = None      # 当前标记/注释属于哪个项目根
        self.truncated = False

    def _meta_key(self, path):
        """用项目内相对路径做键：整个项目挪位置也不会丢标记和注释。

        前缀必须带分隔符再比：直接 startswith(root) 会把 D:\\proj 和 D:\\proj2
        当成同一个根，切出来的键是错的。
        """
        low = path.lower()
        pre = getattr(self, "_root_prefix", "")
        if not pre and self.root_dir:
            pre = (self.root_dir.rstrip("\\/") + os.sep).lower()
        if pre:
            if low.startswith(pre):
                return low[len(pre):].replace("/", "\\")
            if low == pre[:-1]:
                return ""
        try:
            return os.path.relpath(path, self.root_dir).replace("/", "\\").lower()
        except Exception:
            return low

    def _meta_abs(self, key):
        """相对键还原绝对路径，兼容早期写进去的绝对路径"""
        if not key:
            return ""
        if os.path.isabs(key):
            return key
        return os.path.join(self.root_dir, key) if self.root_dir else key

    def _load_meta(self, root):
        """读项目根下的 .vbhelper.json。

        - 换项目（root 变了）→ 以文件为准，没有文件就是全新的，不继承旧项目数据
        - 同一项目刷新（root 没变）→ 即使文件一时读不到，也保住内存里的标记注释
        """
        same = getattr(self, "_meta_owner", None) == root
        try:
            with open(os.path.join(root, META_NAME), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
        if isinstance(data, dict):
            raw_marks = data.get("marks") or {}
            raw_notes = data.get("notes") or {}
        elif same:
            raw_marks, raw_notes = self.marks, self.notes
        else:
            raw_marks, raw_notes = {}, {}
        self._meta_owner = root
        self.marks = {self._meta_key(self._meta_abs(k)): v for k, v in raw_marks.items()}
        self.notes = {self._meta_key(self._meta_abs(k)): v for k, v in raw_notes.items()}

    def _save_meta(self):
        """写回项目目录（先写临时文件再替换，避免半截文件）；目录不可写就退回全局配置"""
        data = {"version": 1,
                "marks": {k: v for k, v in self.marks.items() if k},
                "notes": {k: v for k, v in self.notes.items() if v}}
        if not self.root_dir:
            self.cfg["marks"], self.cfg["notes"] = self.marks, self.notes
            save_cfg(self.cfg)
            return False
        path = os.path.join(self.root_dir, META_NAME)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            if self.cfg.get("marks") or self.cfg.get("notes"):   # 清掉兜底残留
                self.cfg.pop("marks", None)
                self.cfg.pop("notes", None)
                save_cfg(self.cfg)
            return True
        except OSError:
            self.cfg["marks"], self.cfg["notes"] = self.marks, self.notes
            save_cfg(self.cfg)
            return False

    def _note_icon(self, path):
        return NOTE_ICON if self.notes.get(self._meta_key(path)) else ""

    def _init_mark_tags(self):
        """把 8 种颜色注册成 Treeview 的行 tag。

        踩坑记录：ttk 的样式名映射（mark0.Treeview）在 clam 主题下不会改变
        Treeview 行的背景色，必须直接调 Tcl 的 `tree tag configure` 才生效。
        """
        for name, tag in MARK_TAGS.items():
            color = MARK_COLORS[name]
            try:
                ttk.Style().configure(tag + ".Treeview", background=color)
            except tk.TclError:
                pass
            try:
                self.tree.tk.call(self.tree._w, "tag", "configure", tag,
                                  "-background", color, "-foreground", "#eceef2")
            except tk.TclError:
                pass
        # Git 状态 tag：只改前景色，不抢标记色的背景
        for tag, color in GIT_COLORS.items():
            try:
                self.tree.tk.call(self.tree._w, "tag", "configure", tag,
                                  "-foreground", color)
            except tk.TclError:
                pass

    def _mark_tag(self, path):
        """该路径的颜色标签，未标记返回 None"""
        return MARK_TAGS.get(self.marks.get(self._meta_key(path)))

    def _row_tags(self, kind, mark=None, git=None):
        tags = ["dir" if kind == "dir" else "file"]
        if mark:
            tags.append(mark)
        if git:
            tags.append(git)
        return tuple(tags)

    def _find_iid(self, path):
        stack = list(self.tree.get_children(""))
        while stack:
            iid = stack.pop()
            vals = self._vals(iid)
            if vals and vals[0] == path:
                return iid
            stack.extend(self.tree.get_children(iid))
        return None

    def _mark_menu(self, path):
        """标记颜色子菜单：勾选即当前颜色，"清除标记"恢复默认底色"""
        sub = self._mk_menu()
        var = tk.StringVar(value=self.marks.get(self._meta_key(path)) or "")
        for name, color in MARK_COLORS.items():
            sub.add_radiobutton(label=name, variable=var, value=name,
                                background=color, foreground="#e9e9ec",
                                activebackground=color, activeforeground="#ffffff",
                                selectcolor=color,
                                command=lambda n=name: self._apply_mark(path, n))
        sub.add_separator()
        sub.add_command(label="清除标记", command=lambda: self._apply_mark(path, None))
        return sub

    def _apply_mark(self, path, name):
        """给指定项目上色（name 为 None 表示清除），写进项目 JSON 并即时刷新该行"""
        key = self._meta_key(path)
        if name:
            self.marks[key] = name
        else:
            self.marks.pop(key, None)
        self._save_meta()
        if self.tree is None:
            return
        iid = self._find_iid(path)
        vals = self._vals(iid) if iid else None
        if vals:
            self.tree.item(iid, tags=self._row_tags(vals[1], self._mark_tag(path)))

    def _del_note(self, path):
        """右键菜单里的清除注释"""
        self._set_note(path, None)

    def _set_note(self, path, text):
        """写入/清除注释（空文本视为清除），保存并刷新该行的 i 图标"""
        key = self._meta_key(path)
        if text:
            self.notes[key] = text
        else:
            self.notes.pop(key, None)
        try:
            in_project = self._save_meta()
        except Exception as e:
            messagebox.showerror(APP_NAME, "注释保存失败：%s" % e, parent=self.root)
            return
        if self.tree is not None:
            iid = self._find_iid(path)
            if iid:
                try:
                    self._set_note_icon(iid, NOTE_ICON if self.notes.get(key) else "")
                except tk.TclError:
                    pass
        if not text:
            self._flash_status("已清除注释")
        elif in_project:
            self._flash_status("注释已保存到 .vbhelper.json")
        else:
            self._flash_status("项目目录不可写，注释已存到全局配置")

    def _flash_status(self, msg):
        """状态栏闪一条反馈，两秒后恢复常规信息"""
        if self.status is None:
            return
        try:
            self.status.configure(text=msg)
            if self._flash_job:
                self.root.after_cancel(self._flash_job)
            self._flash_job = self.root.after(2200, self._update_status)
        except tk.TclError:
            pass

    def _open_note(self, iid):
        """点右侧 i 图标：打开注释窗查看"""
        try:
            vals = self._vals(iid)
        except tk.TclError:
            return
        if vals and len(vals) >= 3 and vals[2]:
            self._note_dialog(vals[0])

    def _note_dialog(self, path):
        """注释窗：既是首次输入框，也是点 i 之后的查看窗（可直接改）"""
        key = self._meta_key(path)
        w = tk.Toplevel(self.root)
        w.title("注释 - " + display_name(path))
        w.configure(bg=C_BG)
        w.geometry("430x340")
        w.minsize(360, 260)
        try:
            w.attributes("-topmost", True)
        except Exception:
            pass

        original = self.notes.get(key, "")

        def try_close():
            """关闭前确认未保存的注释"""
            try:
                current = txt.get("1.0", "end").strip()
            except tk.TclError:
                w.destroy()
                return
            if current != original.strip():
                answer = messagebox.askyesnocancel(
                    APP_NAME, "注释有未保存的修改，保存吗？", parent=w)
                if answer is None:
                    return
                if answer:
                    self._set_note(path, current)
            w.destroy()

        head = tk.Frame(w, bg=C_BG)
        head.pack(fill="x", padx=14, pady=(12, 2))
        tk.Label(head, text="注释", bg=C_BG, fg=C_TEXT,
                 font=(core.FONT, 10, "bold")).pack(side="left")
        tk.Label(head, text=path, bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 8)).pack(side="right")
        tk.Frame(w, bg=C_BORDER, height=1).pack(fill="x", padx=14, pady=6)

        # 按钮栏必须先靠底排好，否则输入框会把按钮挤出窗口（Text 默认 24 行太高）
        bar = tk.Frame(w, bg=C_BG)
        bar.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        tk.Button(bar, text="保存", command=lambda: self._save_note(w, path, txt),
                  bg=C_ACCENT, fg="white", activebackground=C_ACCENT_H,
                  activeforeground="white", relief="flat", bd=0, padx=18, pady=5,
                  font=(core.FONT, 9, "bold"), cursor="hand2").pack(side="right")
        tk.Button(bar, text="关闭", command=try_close, bg=C_BG, fg=C_TEXT,
                  activebackground=C_HOVER, activeforeground=C_TEXT, relief="flat",
                  bd=0, padx=14, pady=5, font=(core.FONT, 9),
                  cursor="hand2").pack(side="right", padx=6)
        tk.Button(bar, text="清除注释",
                  command=lambda: (self._set_note(path, None), w.destroy()),
                  bg=C_BG, fg=C_MUTED, activebackground=C_HOVER, activeforeground=C_TEXT,
                  relief="flat", bd=0, padx=14, pady=5, font=(core.FONT, 9),
                  cursor="hand2").pack(side="left")

        txt = tk.Text(w, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT, bd=0,
                      relief="flat", highlightthickness=0, font=(core.FONT, 9),
                      wrap="word", padx=10, pady=8, height=6)
        txt.pack(fill="both", expand=True, padx=14)
        txt.insert("1.0", self.notes.get(key, ""))
        w.bind("<Escape>", lambda e: try_close())
        w.bind("<Control-s>", lambda e: self._save_note(w, path, txt))
        self._center_on_panel(w)
        w.after(60, txt.focus_set)

    def _save_note(self, win, path, txt):
        """保存注释窗内容并关窗"""
        try:
            text = txt.get("1.0", "end").strip()
        except tk.TclError:
            return
        self._set_note(path, text)
        win.destroy()
