"""项目内快速搜索：文件名模糊匹配 + 文件内容全文搜索。

只在当前打开的文件夹范围内搜，不全盘扫。文件名搜索走预建索引（毫秒级），
文件内容搜索走后台线程（不卡 UI），结果分批回填。
"""
import os
import threading
import time
import tkinter as tk
from tkinter import ttk

from . import core
from .core import (APP_NAME, C_ACCENT, C_ACCENT_H, C_BG, C_BORDER,
                   C_MUTED, C_PANEL, C_SELECT, C_TEXT, display_name)
from .ui import icons, render

TITLE = "快速搜索"
MAX_FILE_RESULTS = 80       # 文件名匹配最多显示条数
MAX_CONTENT_RESULTS = 40    # 内容匹配最多显示条数
CONTENT_TIMEOUT = 6.0       # 内容搜索超时（秒）
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".svn", ".hg",
             "venv", ".venv", "env", ".idea", ".vscode", "dist", "build",
             ".gradle", "target", ".next", ".nuxt"}
SKIP_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".o", ".a",
            ".lib", ".pyc", ".pyo", ".class", ".jar", ".war", ".zip",
            ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".iso",
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
            ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flac",
            ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
MAX_FILE_SIZE = 2 * 1024 * 1024   # 内容搜索跳过超过 2MB 的文件

def open_finder(app):
    """打开（或聚焦）快速搜索窗口"""
    old = getattr(app, "_finder_win", None)
    if old is not None:
        try:
            if old.win.winfo_exists():
                old.win.deiconify()
                old.win.lift()
                old.win.focus_force()
                old.entry.focus_set()
                old.entry.select_range(0, "end")
                return old
        except tk.TclError:
            pass
    f = Finder(app)
    app._finder_win = f
    return f

class Finder:
    def __init__(self, app):
        self.app = app
        self.root_dir = app.root_dir
        self._file_index = []          # [(显示名, 相对路径, 完整路径)] 预建索引
        self._index_ready = False
        self._index_lock = threading.Lock()
        self._content_job = None       # 内容搜索线程
        self._content_gen = 0          # 代号：换关键字时旧线程自动作废
        self._search_job = None        # 防抖 after
        self._closed = False

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(TITLE + " - " + APP_NAME)
        win.configure(bg=C_BG)
        win.geometry("680x480")
        win.minsize(520, 360)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.protocol("WM_DELETE_WINDOW", self._close)
        win.bind("<Escape>", lambda e: self._close())

        top = tk.Frame(win, bg=C_BG)
        top.pack(fill="x", padx=14, pady=(12, 0))
        self._search_icon = render.photo(
            icons.draw("search", 15, C_MUTED), key="finder:search:15:%s" % C_MUTED)
        tk.Label(top, image=self._search_icon, bg=C_BG).pack(side="left")
        self.var_q = tk.StringVar()
        self.entry = tk.Entry(top, textvariable=self.var_q, bg=C_PANEL,
                              fg=C_TEXT, insertbackground=C_TEXT, bd=0,
                              relief="flat", font=(core.FONT, 11),
                              highlightthickness=1,
                              highlightbackground=C_BORDER,
                              highlightcolor=C_ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, padx=(8, 8), ipady=5)
        self.entry.bind("<KeyRelease>", self._on_type)
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Down>", self._focus_list)
        self.lbl_hint = tk.Label(top, text="", bg=C_BG, fg=C_MUTED,
                                 font=(core.FONT, 8))
        self.lbl_hint.pack(side="right")

        mode_row = tk.Frame(win, bg=C_BG)
        mode_row.pack(fill="x", padx=14, pady=(6, 0))
        self.var_mode = tk.StringVar(value="name")
        for val, label in (("name", "文件名"), ("content", "文件内容")):
            tk.Radiobutton(mode_row, text=label, variable=self.var_mode,
                           value=val, command=self._on_type,
                           bg=C_BG, fg=C_TEXT, selectcolor=C_PANEL,
                           activebackground=C_BG, activeforeground=C_TEXT,
                           font=(core.FONT, 9), bd=0,
                           highlightthickness=0,
                           cursor="hand2").pack(side="left", padx=(0, 12))

        box = tk.Frame(win, bg=C_BG)
        box.pack(fill="both", expand=True, padx=14, pady=(8, 12))
        self.tree = ttk.Treeview(box, columns=("info",), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="文件")
        self.tree.heading("info", text="位置 / 匹配行")
        self.tree.column("#0", width=260, stretch=True)
        self.tree.column("info", width=360, stretch=True)
        vsb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-Button-1>", self._on_enter)
        self.tree.bind("<Return>", self._on_enter)

        self._init_style()
        self._build_index_async()
        self._update_hint()
        win.after(80, self.entry.focus_set)

    # ---------- 外壳 ----------

    def _tree_icon(self, name):
        """结果行图标：Treeview 的 #0 列放 Feather 图片。"""
        ph = render.photo(icons.draw(name, 14, C_MUTED),
                          key="finder:ico:%s:14:%s" % (name, C_MUTED))
        self._icon_cache = getattr(self, "_icon_cache", {})
        self._icon_cache[name] = ph
        return ph

    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Find.Treeview", background=C_PANEL, foreground=C_TEXT,
                     fieldbackground=C_PANEL, borderwidth=0, rowheight=24,
                     font=(core.FONT, 9))
        st.map("Find.Treeview", background=[("selected", C_SELECT)],
               foreground=[("selected", "#ffffff")])
        st.configure("Find.Vertical.TScrollbar", background="#3a3d46",
                     troughcolor=C_PANEL, bordercolor=C_PANEL,
                     arrowcolor=C_MUTED, borderwidth=0, width=12)

    def _close(self):
        self._closed = True
        self._content_gen += 1    # 作废后台搜索
        if self._search_job is not None:
            try:
                self.win.after_cancel(self._search_job)
            except Exception:
                pass
        self.win.destroy()

    def _update_hint(self):
        if self.root_dir:
            short = display_name(self.root_dir)
        else:
            short = "（未选择目录）"
        self.lbl_hint.configure(text="范围：%s" % short)

    # ---------- 文件名索引 ----------

    def _build_index_async(self):
        """后台建文件名索引：只扫当前项目目录，跳过 .git/node_modules 等"""
        root = self.root_dir
        if not root or not os.path.isdir(root):
            return
        self.lbl_hint.configure(text="索引中…")

        def work():
            idx = []
            for dirpath, dirs, files in os.walk(root, onerror=lambda e: None):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                rel_dir = os.path.relpath(dirpath, root)
                for fn in files:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in SKIP_EXT:
                        continue
                    rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
                    full = os.path.join(dirpath, fn)
                    idx.append((rel.replace("\\", "/"), full))
            with self._index_lock:
                self._file_index = idx
                self._index_ready = True
            if not self._closed:
                try:
                    self.win.after(0, self._on_index_done)
                except tk.TclError:
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _on_index_done(self):
        self.lbl_hint.configure(text="索引完成 · %d 个文件" % len(self._file_index))
        if self.var_q.get().strip():
            self._search_now()

    # ---------- 输入 / 搜索 ----------

    def _on_type(self, _e=None):
        if self._search_job is not None:
            try:
                self.win.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.win.after(180, self._search_now)

    def _search_now(self):
        self._search_job = None
        q = self.var_q.get().strip()
        self.tree.delete(*self.tree.get_children(""))
        if not q:
            self._update_hint()
            return
        if self.var_mode.get() == "name":
            self._search_name(q)
        else:
            self._search_content(q)

    def _search_name(self, q):
        """文件名模糊匹配：子序列匹配 + 包含匹配优先"""
        if not self._index_ready:
            self.lbl_hint.configure(text="索引中…稍候再搜")
            return
        ql = q.lower()
        scored = []
        with self._index_lock:
            for rel, full in self._file_index:
                name = rel.rsplit("/", 1)[-1].lower()
                if ql in name:
                    score = (0, name.index(ql), len(name))
                elif ql in rel.lower():
                    score = (1, rel.lower().index(ql), len(name))
                elif _subsequence(ql, name):
                    score = (2, 0, len(name))
                else:
                    continue
                scored.append((score, rel, full))
        scored.sort(key=lambda x: x[0])
        for _score, rel, full in scored[:MAX_FILE_RESULTS]:
            name = rel.rsplit("/", 1)[-1]
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            self.tree.insert("", "end", text=name,
                             image=self._tree_icon("file"),
                             values=(parent or "·",),
                             tags=("file",))
        n = len(scored)
        self.lbl_hint.configure(
            text="%d 个匹配" % n if n <= MAX_FILE_RESULTS
            else "前 %d / %d 个匹配" % (MAX_FILE_RESULTS, n))

    def _search_content(self, q):
        """文件内容搜索：后台线程逐文件 grep，结果分批回填"""
        if not self.root_dir:
            return
        self._content_gen += 1
        gen = self._content_gen
        self.lbl_hint.configure(text="搜索中…")

        def work():
            hits = []
            t0 = time.perf_counter()
            for dirpath, dirs, files in os.walk(self.root_dir,
                                                onerror=lambda e: None):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for fn in files:
                    if self._closed or gen != self._content_gen:
                        return
                    if time.perf_counter() - t0 > CONTENT_TIMEOUT:
                        self._batch(hits, gen, truncated=True)
                        return
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in SKIP_EXT:
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        if os.path.getsize(fp) > MAX_FILE_SIZE:
                            continue
                    except OSError:
                        continue
                    found = _grep_file(fp, q)
                    if found:
                        hits.append((fp, found))
                        if len(hits) >= MAX_CONTENT_RESULTS:
                            self._batch(hits, gen, truncated=True)
                            return
            self._batch(hits, gen, truncated=False)

        self._content_job = threading.Thread(target=work, daemon=True)
        self._content_job.start()

    def _batch(self, hits, gen, truncated):
        """内容搜索结果回填（必须回主线程）"""
        if self._closed or gen != self._content_gen:
            return
        try:
            self.win.after(0, self._show_content, hits, truncated)
        except tk.TclError:
            pass

    def _show_content(self, hits, truncated):
        if self._closed:
            return
        self.tree.delete(*self.tree.get_children(""))
        root = self.root_dir or ""
        for fp, lines in hits:
            rel = os.path.relpath(fp, root) if root else fp
            rel = rel.replace("\\", "/")
            name = rel.rsplit("/", 1)[-1]
            preview = lines[0][1][:80] if lines else ""
            self.tree.insert("", "end", text=name,
                             image=self._tree_icon("file"),
                             values=("%s:%d  %s" % (rel, lines[0][0], preview),),
                             tags=("file",),
                             open=False)
        if truncated:
            self.lbl_hint.configure(text="已显示 %d 个匹配（搜索超时或结果过多，可换个更精确的词）"
                                    % len(hits))
        else:
            self.lbl_hint.configure(text="%d 个匹配" % len(hits))

    # ---------- 打开 ----------

    def _focus_list(self, _e=None):
        kids = self.tree.get_children("")
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])
            self.tree.focus_set()
        return "break"

    def _on_enter(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return "break"
        iid = sel[0]
        vals = self.tree.item(iid, "values")
        text = self.tree.item(iid, "text")
        name = text.strip()
        # 从 info 列还原完整路径
        info = vals[0] if vals else ""
        if self.var_mode.get() == "name":
            # 文件名模式：info 是父目录相对路径
            rel = (info + "/" if info not in ("", "·") else "") + name
            full = os.path.join(self.root_dir, rel.replace("/", os.sep))
        else:
            # 内容模式：info 格式为 "相对路径:行号  预览"
            rel = info.split(":")[0] if ":" in info else info
            full = os.path.join(self.root_dir, rel.replace("/", os.sep))
        if os.path.isfile(full):
            from . import editor
            editor.open_editor(self.app, full)
            self._close()
        return "break"

def _subsequence(needle, haystack):
    """子序列匹配：needle 的每个字符按序出现在 haystack 中即可"""
    it = iter(haystack)
    return all(c in it for c in needle)

def _grep_file(path, needle):
    """在文件里搜 needle，返回 [(行号, 行文本)]，最多 3 行"""
    hits = []
    needle_l = needle.lower()
    try:
        with open(path, "rb") as f:
            raw = f.read()
        # 二进制检测：有 NUL 就跳过
        if b"\x00" in raw[:8192]:
            return []
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            return []
    except Exception:
        return []
    for i, line in enumerate(text.splitlines(), 1):
        if needle_l in line.lower():
            hits.append((i, line.strip()[:120]))
            if len(hits) >= 3:
                break
    return hits
