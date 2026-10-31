"""目录树：Treeview 渲染、展开/折叠、下钻、变更标记与右键菜单。

节点数据来自 ScanPool；展开时先渲染占位，扫描结果回主线程后替换。
目录树下钻与文件预览的入口也在这里。
"""
import subprocess

from . import core, editor, launcher
from .core import *
from .ui import icons, render

class TreeMixin:
    def _tree_icon(self, name, color=None):
        """目录树行图标：Treeview 的 #0 列支持 image，按类型着色。"""
        color = color or (C_DIR if name == "folder" else C_MUTED)
        ph = render.photo(icons.draw(name, 14, color),
                          key="tree:ico:%s:14:%s" % (name, color))
        self._tree_icon_cache = getattr(self, "_tree_icon_cache", {})
        self._tree_icon_cache[(name, color)] = ph
        return ph

    def _vals(self, iid):
        """取一行树数据，按 (路径, 类型, 图标) 的顺序返回。

        树里真正存的顺序是 (图标, 路径, 类型)——图标必须排第一位，右侧那一列才
        显示得出它（Treeview 是按位置取值的，不认列名）。这里统一换回来，别处
        就照老顺序用，既不用记元组顺序，也不会因为顺序变了而改错取值下标。
        """
        try:
            v = self.tree.item(iid, "values")
        except tk.TclError:
            return ("", "", "")
        if not v or len(v) < 3:
            return ("", "", "")
        return (v[1], v[2], v[0])

    def _set_note_icon(self, iid, icon):
        """只换右侧那一列的图标，路径与类型原地不动"""
        try:
            v = self.tree.item(iid, "values")
        except tk.TclError:
            return
        if not v or len(v) < 3:
            return
        self.tree.item(iid, values=(icon, v[1], v[2]))

    def _refresh(self):
        self._ensure_panel()          # 面板可能还没展开过，title_lbl 等控件还没建
        if self.root_dir and os.path.isdir(self.root_dir):
            # 主动刷新必须重读磁盘：缓存只服务于展开/切回，不能把 F5 也挡掉
            with self._preheat_lock:
                self._preheat_cache.clear()
                self._scan_failed.clear()
            self._load(self.root_dir)
        else:
            messagebox.showwarning(APP_NAME, "目录已不存在，请重新选择。", parent=self.root)

    def _load_git_status(self):
        """读 git status --porcelain，得到 {相对路径(小写): 状态字符}。

        只认 porcelain 格式前两列：M 改、A 加、D 删、?? 未跟踪、R 重命名。
        非 git 项目或 git 不可用就静默跳过，树照常渲染。
        """
        self._git_status = {}
        root = self.root_dir
        if not root or not os.path.isdir(os.path.join(root, ".git")):
            return
        try:
            out = subprocess.run(
                ["git", "-C", root, "status", "--porcelain", "-z"],
                capture_output=True, timeout=3, creationflags=0x08000000,
            )
            if out.returncode != 0:
                return
            data = out.stdout.decode("utf-8", "replace")
        except Exception:
            return
        for entry in data.split("\0"):
            if len(entry) < 4:
                continue
            xy, rel = entry[:2], entry[3:]
            # -z 模式重命名是 "XY new\0old"，只取新路径
            if xy[0] in "RC" and "\0" in entry:
                rel = entry[3:].split("\0")[0]
            rel = rel.strip()
            if not rel:
                continue
            # 未跟踪目录（?? dir/）会展开成整棵子树，这里标记该目录本身
            key = rel.rstrip("/").replace("\\", "/").lower()
            if "??" in xy:
                code = "?"
            elif "D" in xy:
                code = "D"
            elif "A" in xy:
                code = "A"
            elif "R" in xy:
                code = "R"
            else:
                code = "M"
            self._git_status[key] = code

    def _git_tag(self, path):
        """该路径的 Git 状态 tag，无状态返回 None。用项目内相对路径匹配。"""
        pre = self._root_prefix
        if not pre or not self._git_status:
            return None
        low = path.lower()
        if not low.startswith(pre):
            return None
        rel = low[len(pre):].replace("\\", "/")
        code = self._git_status.get(rel)
        return GIT_TAGS.get(code) if code else None

    def _load(self, path):
        self.root_dir = path
        self.cfg["last_dir"] = path
        save_cfg(self.cfg)
        self._root_prefix = (path.rstrip("\\/") + os.sep).lower()
        self._load_meta(path)          # 项目内的标记/注释优先于全局配置
        self._load_git_status()        # Git 状态标注
        self._budget = DRILL_BUDGET
        self._loaded = 0
        self._root_depth = path.rstrip("\\/").count("\\")
        self.truncated = False
        self._dir_state.clear()
        self._dir_has_files.clear()
        self._dir_kids.clear()
        self._auto_pending.clear()
        self._drilled.clear()
        self._want_open.clear()
        self._iid_by_path.clear()
        self._dir_mtime.clear()
        name = display_name(path)
        self.title_lbl.configure(text=name)
        self._set_ball_text(name)
        self.tree.delete(*self.tree.get_children(""))
        # 根目录也走后台读取：选到 C:\ 这类几万项的目录时，同步 scandir 会把
        # 主线程按住几百毫秒，和「点开就卡」是同一个毛病。
        # 值元组顺序固定为 (图标, 路径, 类型)：图标必须在第一位，右侧那一列才显示它
        root_iid = self.tree.insert("", "end", text=name,
                                    image=self._tree_icon("folder"),
                                    values=(self._note_icon(path), path, "dir"),
                                    tags=self._row_tags("dir", self._mark_tag(path)))
        self._dir_state[root_iid] = "idle"
        self.tree.insert(root_iid, "end", text="", tags=("loading",))
        if self.set.get("root_expand", True):
            self._open_item(root_iid)
        else:
            self._ensure_loaded(root_iid)
        self._update_status(path)

    def _open_item(self, iid, focus=True):
        """展开节点：缓存命中立刻渲染，否则后台读取后就绪再自动下钻"""
        if focus:
            self.tree.focus(iid)
        self.tree.item(iid, open=True)
        self._want_open.add(iid)       # 读取期间若被误折叠，完成后要撑回来
        if self._ensure_loaded(iid):
            self._drill(iid, focus)
            return True
        return False

    def _tree_click(self, e):
        row = self.tree.identify_row(e.y)
        self._click_row = row or None
        if not row:
            return
        region = self.tree.identify_region(e.x, e.y)   # identify 必须 (component,x,y) 三参
        col = self.tree.identify_column(e.x)           # 注释列是 columns=("note",) 的 #1
        if region == "cell" and col in ("#1", "#2", "note"):
            self.root.after(1, self._open_note, row)   # 点右侧 i 图标看注释
            return
        # 展开箭头不是 region（region 只有 heading/separator/cell/tree），
        # 而是树列里的 Treeitem.indicator 元素；用 identify_element 判定
        try:
            elem = self.tree.identify_element(e.x, e.y) or ""
        except Exception:
            elem = ""
        if "indicator" in elem or region == "expander":
            self.root.after(1, self._user_expand, row)

    def _key_expand(self):
        iid = self.tree.focus()
        if iid:
            self.root.after(1, self._user_expand, iid)

    def _user_expand(self, iid):
        """Tk 不为程序化 open 派发事件，用户侧交互统一走这里补加载与下钻。

        判断的是「处理时已展开」而不是点击前的状态——否则第一次点开只会读出
        内容却不再往下自动展开，看起来就是「点一次没反应」。
        折叠分支绝不能再发起扫描：那会把结果灌进闭合节点，表现就是「读了内容
        却看不见，必须再点一次展开」（超时重试正是踩进这条路的）。
        """
        try:
            if not self.tree.exists(iid):
                return
            if self.tree.item(iid, "open"):
                self._budget = DRILL_BUDGET      # 每次用户展开重新给下钻额度
                self._drilled.clear()
                self._open_item(iid, focus=False)
            else:
                self._want_open.discard(iid)     # 用户明确要收起：撤销展开意图
        except tk.TclError:
            pass

    def _set_placeholder(self, iid, text, icon=None):
        """占位行：撑出展开箭头，也在读取慢/超时时给个说法"""
        img = self._tree_icon(icon or "clock") if icon else ""
        kids = self.tree.get_children(iid)
        if kids and "loading" in self.tree.item(kids[0], "tags"):
            self.tree.item(kids[0], text=text, image=img)
        else:
            self.tree.insert(iid, "end", text=text, image=img,
                             tags=("loading",))

    def _ensure_loaded(self, iid):
        """确保目录内容已进树；返回 True 表示这一层已经能用（含正在分批渲染）"""
        vals = self._vals(iid)
        if not vals or len(vals) < 2 or vals[1] != "dir":
            return True
        path = vals[0]
        state = self._dir_state.get(iid)
        if state == "loading":
            return False
        if state in ("rendering", "loaded"):
            if self._dir_fresh(path):
                return True
            # 目录在磁盘上变过（mtime 对不上）：作废缓存和已渲染内容，就地重扫
            dbg_log("stale cache, rescan  %s" % path)
            with self._preheat_lock:
                self._preheat_cache.pop(path, None)
            self._dir_state[iid] = "idle"
            self._dir_has_files.pop(iid, None)
            self._dir_kids.pop(iid, None)
        rows = self._scan_cached(path)
        if rows is not None and self._dir_fresh(path):
            self._render_rows(iid, rows)
            return True
        self._dir_state[iid] = "loading"
        self._iid_by_path[path] = iid    # 迟到结果/变更侦测要按 path 找回节点
        self._set_placeholder(iid, "正在读取…", "clock")   # 占位提示：避免加载期间界面无反馈
        dbg_log("submit scan prio=0  %s" % path)
        self._submit_scan(path, lambda r, ok: self._on_loaded(iid, r, ok), priority=0)
        return False

    def _on_loaded(self, iid, rows, ok=True):
        """后台读取完成：回到主线程渲染，并接着自动下钻 / 接力全部展开"""
        try:
            if not self.tree.exists(iid):
                return
            if self._dir_state.get(iid) != "loading":
                return
            if rows is None:                  # 读取失败或超时：保留可重试占位，不能显示为空目录
                self._dir_state[iid] = "idle"
                self._auto_pending.discard(iid)
                self._set_placeholder(iid, "暂时读不出来（点击展开重试）",
                                      "alert-triangle")
                return
            self._render_rows(iid, rows or [])
        except tk.TclError:
            pass

    def _render_rows(self, iid, rows):
        """把扫描结果分批塞进树：每帧只干 RENDER_FRAME_MS 毫秒。

        以前是一次性插完，几万项的大目录（node_modules、WinSxS）会把主线程按住
        一两秒；分批之后界面照常响应，行是「刷」出来的，和资源管理器一个观感。
        """
        if len(rows) > POPULATE_LIMIT:
            rows = rows[:POPULATE_LIMIT]
            self.truncated = True
        try:
            kids = self.tree.get_children(iid)
            if kids:
                self.tree.delete(*kids)
        except tk.TclError:
            return
        self._dir_state[iid] = "rendering"
        self._dir_has_files[iid] = any(not r[4] for r in rows)
        dir_kids = []
        self._dir_kids[iid] = dir_kids
        marks, notes, mark_tags = self.marks, self.notes, MARK_TAGS
        git_status = self._git_status
        root_pre = self._root_prefix
        insert = self.tree.insert
        total = len(rows)
        pos, added = 0, 0
        budget_s = RENDER_FRAME_MS / 1000.0
        t_start = time.perf_counter()
        path_for_log = self._vals(iid)[0] if core.DEBUG_LOG_ON else ""

        def step():
            nonlocal pos, added
            try:
                if not self.tree.exists(iid) or self._dir_state.get(iid) != "rendering":
                    return
            except tk.TclError:
                return
            t0 = time.perf_counter()
            while pos < total:
                _, _, name, path_, is_dir = rows[pos]
                key = self._meta_key(path_)
                mark = mark_tags.get(marks.get(key))
                icon = NOTE_ICON if notes.get(key) else ""
                # Git 状态 tag：用相对路径匹配
                gtag = ""
                if git_status and root_pre and path_.lower().startswith(root_pre):
                    gc = git_status.get(path_.lower()[len(root_pre):].replace("\\", "/"))
                    gtag = GIT_TAGS.get(gc, "") if gc else ""
                base = "dir" if is_dir else "file"
                t = [base]
                if mark:
                    t.append(mark)
                if gtag:
                    t.append(gtag)
                if is_dir:
                    kid = insert(iid, "end", text=name,
                                 image=self._tree_icon("folder"),
                                 values=(icon, path_, "dir"), tags=tuple(t))
                    insert(kid, "end", text="", tags=("loading",))
                    self._dir_state[kid] = "idle"
                    dir_kids.append(kid)
                else:
                    insert(iid, "end", text=name,
                           image=self._tree_icon("file"),
                           values=(icon, path_, "file"), tags=tuple(t))
                pos += 1
                added += 1
                if pos % 32 == 0 and time.perf_counter() - t0 >= budget_s:
                    break
            if pos < total:
                self.root.after(1, step)
                return
            self._dir_state[iid] = "loaded"
            self._loaded += added
            if path_for_log:
                dbg_log("render %6d rows %6.2fs  %s"
                        % (added, time.perf_counter() - t_start, path_for_log))
            self._after_dir_loaded(iid)

        step()

    def _after_dir_loaded(self, iid):
        """一层渲染完：接上「全部展开」的队列，或者继续自动下钻"""
        if iid in self._want_open:
            self._want_open.discard(iid)
            try:
                # 读取期间用户折叠过节点：结果返回后恢复展开，慢加载不应丢失用户操作
                self.tree.item(iid, open=True)
            except tk.TclError:
                return
        if iid in self._auto_pending:
            self._auto_pending.discard(iid)
            self._expand_queue(deque(self._dir_kids.get(iid) or ()))
        else:
            try:
                if self.tree.item(iid, "open"):
                    self._drill(iid, focus=False)
            except tk.TclError:
                return
        self._update_status()

    def _drill(self, iid, focus=True):
        """无文件层自动下钻：这一层只有子文件夹，就顺着包名继续展开。

        这里以前用 Tcl 的 children 按 tag 过滤，那个子命令不存在，异常被吞掉后
        永远返回空——于是每一层都被当成「纯目录」一路展开，点一个文件夹等于把
        整个仓库读一遍。现在用插入时记下的 has_files 判断，语义才是对的。
        """
        if not self.set.get("drill") or self._budget <= 0:
            return
        if iid in self._drilled:        # 同一轮里可能从两条路进来（缓存命中 + 回调）
            return
        if self._dir_state.get(iid) != "loaded" or self._dir_has_files.get(iid):
            return
        vals = self._vals(iid)
        if not vals or len(vals) < 2 or vals[1] != "dir":
            return
        if vals[0].rstrip("\\/").count("\\") - self._root_depth >= DRILL_DEPTH:
            return
        kids = self._dir_kids.get(iid) or ()
        if not kids or len(kids) > DRILL_FANOUT:   # 扇出这么大就不是包名链了
            return
        self._drilled.add(iid)
        for k in kids:
            if self._budget <= 0:
                break
            self._budget -= 1        # 额度按「自动展开的目录数」算，不按行数算
            self._open_item(k, focus)

    def _expand_all(self):
        roots = self.tree.get_children("")
        if not roots:
            return
        self._budget = EXPAND_BUDGET
        self._expand_cancelled = False
        self._expand_total = EXPAND_BUDGET
        self._auto_pending.clear()
        self.status.configure(text="全部展开中… 点此停止", fg=C_ACCENT)
        self.status.bind("<Button-1>", lambda e: self._cancel_expand())
        self._expand_queue(deque(roots))

    def _cancel_expand(self):
        self._expand_cancelled = True
        self._auto_pending.clear()
        self.status.unbind("<Button-1>")
        self._update_status()

    def _expand_queue(self, q):
        """「全部展开」的广度推进：分帧处理，未就绪的等渲染完再接力"""
        if self._expand_cancelled:
            self.status.unbind("<Button-1>")
            self._update_status()
            return
        n = 0
        while q and self._budget > 0 and n < 40:
            iid = q.popleft()
            n += 1
            self._budget -= 1
            state = self._dir_state.get(iid)
            if state == "loaded":
                try:
                    self.tree.item(iid, open=True)
                except tk.TclError:
                    continue
                q.extend(self._dir_kids.get(iid) or ())
            elif state in ("loading", "rendering"):
                self._auto_pending.add(iid)
            else:
                self._auto_pending.add(iid)
                self._open_item(iid, focus=False)
        if q and self._budget > 0:
            left = self._expand_total - self._budget
            self.status.configure(text="全部展开中… 已处理 %d 项 · 点此停止" % left,
                                  fg=C_ACCENT)
            self.root.after(1, self._expand_queue, q)
        else:
            self.status.unbind("<Button-1>")
            self._update_status()

    def _collapse_all(self):
        """把所有已加载的目录收起来：只碰状态表里的目录节点，不递归整个树"""
        for iid in list(self._dir_state):
            try:
                self.tree.item(iid, open=False)
            except tk.TclError:
                pass
        self._auto_pending.clear()
        self._want_open.clear()

    def _tree_dclick(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        vals = self._vals(iid)
        if not vals:
            return
        path, kind = vals[0], vals[1]
        if kind == "file":
            ext = os.path.splitext(path)[1].lower()
            if ext in (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi",
                       ".scr", ".com", ".pif"):
                if not messagebox.askyesno(
                        APP_NAME,
                        "「%s」是可执行/脚本文件，确定要运行吗？" % display_name(path),
                        parent=self.root):
                    return
            try:
                os.startfile(path)
            except Exception:
                messagebox.showerror(APP_NAME, "无法打开该文件。", parent=self.root)
        else:
            if self.tree.item(iid, "open"):
                self.tree.item(iid, open=False)
            else:
                self._budget = DRILL_BUDGET      # 双击是用户动作，重新给下钻额度
                self._drilled.clear()
                self._open_item(iid)

    def _tree_menu(self, e):
        """右键菜单。

        快捷启动要按「右击的是谁」决定工作目录：右击文件用文件所在目录、右击文件夹
        用它自己、右击空白处用项目根——AI 编程工具全靠当前目录找项目，给错就白跑。
        """
        iid = self.tree.identify_row(e.y)
        m = self._mk_menu()
        if iid:
            vals = self._vals(iid)
            if vals and len(vals) >= 2 and "loading" not in self.tree.item(iid, "tags"):
                path, kind = vals[0], vals[1]
                is_open = bool(self.tree.item(iid, "open"))
                # 多选时右键不塌缩选区：右键已选项保留其余选中项
                if iid not in self.tree.selection():
                    self.tree.selection_set(iid)
                self.tree.focus(iid)

                def toggle():
                    if is_open:
                        self.tree.item(iid, open=False)
                    else:
                        self._open_item(iid, focus=False)

                m.add_command(label="折叠" if is_open else "展开", command=toggle)
                m.add_separator()
                if kind == "file":
                    m.add_command(label="打开文件",
                                  command=lambda: self._open_file(path))
                    m.add_command(label="快捷修改",
                                  command=lambda: editor.open_editor(self, path))
                    m.add_separator()
                elif kind == "dir" and self.features.get("rag") is not None:
                    # 仅文件夹显示：对选中的目录建立本地 RAG 索引
                    m.add_command(label="建立 RAG 索引…",
                                  command=lambda: self._open_rag(path))
                    m.add_separator()
                launcher.menu_cascade(self, m, path)
                m.add_separator()
                m.add_command(label="查看属性", command=lambda: self._show_props(path))
                m.add_command(label="复制文字", command=lambda: self._copy(display_name(path)))
                m.add_command(label="复制相对路径", command=lambda: self._copy_rel(path))
                m.add_command(label="完整路径", command=lambda: self._copy(path))
                m.add_command(label="重命名…", command=lambda: self._rename(path, kind, iid))
                m.add_command(label="在资源管理器中打开",
                              command=lambda: self._explorer(path, kind))
                m.add_cascade(label="标记颜色", menu=self._mark_menu(path))
                note = self.notes.get(self._meta_key(path))
                m.add_command(label="编辑注释…" if note else "添加注释…",
                              command=lambda: self._note_dialog(path))
                if note:
                    m.add_command(label="清除注释", command=lambda: self._del_note(path))
                m.add_command(label="用提示词工作台…",
                              command=lambda: self._promptlab(path))
                m.add_command(label="复制为 AI 上下文",
                              command=lambda: self._copy_ai_context())
                m.add_separator()
        else:
            # 空白处：没有选中项，快捷启动的工作目录就落到项目根
            launcher.menu_cascade(self, m, None)
            m.add_separator()
        m.add_command(label="快速搜索…", command=self._open_finder)
        m.add_separator()
        m.add_command(label="全部展开", command=self._expand_all)
        m.add_command(label="全部折叠", command=self._collapse_all)
        m.add_command(label="刷新", command=self._refresh)
        m.add_separator()
        m.add_command(label="切换目录…", command=self._pick_dir)
        m.post(e.x_root, e.y_root)

    def _mk_menu(self):
        return tk.Menu(self.panel or self.root, tearoff=0, bg=C_PANEL, fg=C_TEXT,
                       activebackground=C_ACCENT, activeforeground="white",
                       bd=0, relief="flat", font=(core.FONT, 9),
                       borderwidth=1, activeborderwidth=0)

    def _explorer(self, path, kind):
        try:
            if kind == "dir":
                os.startfile(path)
            else:
                subprocess.Popen(["explorer", "/select," + path])
        except Exception:
            messagebox.showerror(APP_NAME, "无法打开资源管理器。", parent=self.root)

    def _open_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi",
                   ".scr", ".com", ".pif"):
            if not messagebox.askyesno(
                    APP_NAME,
                    "「%s」是可执行/脚本文件，确定要运行吗？" % display_name(path),
                    parent=self.root):
                return
        try:
            os.startfile(path)
        except Exception:
            messagebox.showerror(APP_NAME, "无法打开该文件。", parent=self.root)

    def _copy_rel(self, path):
        """复制相对项目根的路径：发给别人时不需要带上你本地的盘符和前缀"""
        pre = getattr(self, "_root_prefix", "")
        if pre and path.lower().startswith(pre):
            self._copy(path[len(pre):])
        else:
            self._copy(display_name(path))

    def _rename(self, path, kind, iid):
        old = display_name(path)
        dlg = tk.Toplevel(self.root)
        dlg.title("重命名")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)
        try:
            dlg.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(dlg, text="新名称：" + ("（不含扩展名）" if kind == "file" else ""),
                 bg=C_BG, fg=C_MUTED, font=(core.FONT, 8)).pack(anchor="w", padx=16,
                                                                 pady=(14, 2))
        var = tk.StringVar(value=os.path.splitext(old)[0] if kind == "file" else old)
        entry = tk.Entry(dlg, textvariable=var, bg=C_PANEL, fg=C_TEXT,
                         insertbackground=C_TEXT, relief="flat", bd=0,
                         font=(core.FONT, 10), width=36)
        entry.pack(fill="x", padx=16, ipady=4)
        entry.select_range(0, "end")

        def do_rename():
            name = var.get().strip()
            if not name or name in (".", ".."):
                return
            if kind == "file":
                ext = os.path.splitext(old)[1]
                name += ext
            if name == old:
                dlg.destroy()
                return
            new = os.path.join(os.path.dirname(path), name)
            if os.path.exists(new):
                messagebox.showwarning(APP_NAME, "同名文件已存在。", parent=dlg)
                return
            try:
                os.rename(path, new)
            except OSError as e:
                messagebox.showerror(APP_NAME, "重命名失败：%s" % e, parent=dlg)
                return
            dlg.destroy()
            self._refresh()

        bar = tk.Frame(dlg, bg=C_BG)
        bar.pack(fill="x", padx=16, pady=(12, 14))
        tk.Button(bar, text="确定", command=do_rename, bg=C_ACCENT, fg="white",
                  activebackground=C_ACCENT_H, activeforeground="white",
                  relief="flat", bd=0, padx=16, pady=4,
                  font=(core.FONT, 9), cursor="hand2").pack(side="right")
        tk.Button(bar, text="取消", command=dlg.destroy, bg=C_BG, fg=C_TEXT,
                  activebackground=C_HOVER, activeforeground=C_TEXT,
                  relief="flat", bd=0, padx=14, pady=4,
                  font=(core.FONT, 9), cursor="hand2").pack(side="right", padx=6)
        dlg.bind("<Return>", lambda e: do_rename())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.after(60, entry.focus_set)

    def _copy(self, text):
        if not set_clipboard(text):
            try:
                w = self.panel or self.root
                w.clipboard_clear()
                w.clipboard_append(text)
            except Exception:
                messagebox.showerror(APP_NAME, "复制失败。", parent=self.root)

    def _copy_selected(self):
        sel = self.tree.selection()
        if sel:
            path = self._vals(sel[0])[0]
            if path:
                self._copy(path)

    def _copy_ai_context(self):
        """把选中文件/目录打包成 AI 友好的上下文文本，一键复制到剪贴板。

        格式：目录树概览 + 每个文件的完整内容（带相对路径头），
        直接粘给 AI 就能开工，不用再一个个说「请看这个文件」。
        """
        sel = self.tree.selection()
        if not sel:
            return
        root = self.root_dir or ""
        files = []       # (相对路径, 完整路径)
        dirs = []        # 相对路径
        for iid in sel:
            vals = self._vals(iid)
            if not vals or len(vals) < 2:
                continue
            path, kind = vals[0], vals[1]
            rel = self._rel(path)
            if kind == "dir":
                dirs.append(rel)
                for fp in self._walk_files(path):
                    files.append((self._rel(fp), fp))
            else:
                files.append((rel, path))
        if not files and not dirs:
            return
        # 去重、排序
        seen = set()
        uniq = []
        for rel, fp in sorted(files, key=lambda x: x[0].lower()):
            key = rel.lower()
            if key not in seen:
                seen.add(key)
                uniq.append((rel, fp))
        parts = []
        proj = display_name(root) if root else "项目"
        parts.append("# %s" % proj)
        parts.append("")
        parts.append("## 目录结构")
        for d in sorted(dirs, key=str.lower):
            parts.append("  %s/" % d)
        for rel, _fp in uniq:
            parts.append("  %s" % rel)
        parts.append("")
        for rel, fp in uniq:
            parts.append("## %s" % rel)
            content = self._read_text(fp)
            if content is None:
                parts.append("（二进制文件，跳过）")
            elif not content.strip():
                parts.append("（空文件）")
            else:
                ext = os.path.splitext(fp)[1].lstrip(".").lower()
                lang = {"py": "python", "js": "javascript", "ts": "typescript",
                        "java": "java", "c": "c", "h": "c", "cpp": "cpp",
                        "hpp": "cpp", "cs": "csharp", "go": "go", "rs": "rust",
                        "rb": "ruby", "php": "php", "sh": "bash", "bat": "bat",
                        "ps1": "powershell", "sql": "sql", "html": "html",
                        "css": "css", "xml": "xml", "json": "json",
                        "yaml": "yaml", "yml": "yaml", "toml": "toml",
                        "md": "markdown", "vue": "vue", "jsx": "jsx",
                        "tsx": "tsx", "kt": "kotlin", "swift": "swift",
                }.get(ext, "")
                parts.append("```%s" % lang)
                parts.append(content.rstrip("\n"))
                parts.append("```")
            parts.append("")
        text = "\n".join(parts)
        self._copy(text)
        n = len(uniq)
        try:
            self.status.configure(text="已复制 %d 个文件的 AI 上下文（%d 字符）"
                                  % (n, len(text)))
        except tk.TclError:
            pass

    def _rel(self, path):
        pre = getattr(self, "_root_prefix", "")
        if pre and path.lower().startswith(pre):
            return path[len(pre):].replace("\\", "/")
        return display_name(path)

    def _walk_files(self, path, limit=80):
        """收集目录下的文本文件（上限 limit 个，跳过常见二进制/依赖目录）"""
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
                ".svn", ".hg", "dist", "build", "target"}
        out = []
        for dirpath, dirs, files in os.walk(path, onerror=lambda e: None):
            dirs[:] = [d for d in dirs if d not in skip]
            for fn in sorted(files, key=str.lower):
                out.append(os.path.join(dirpath, fn))
                if len(out) >= limit:
                    return out
        return out

    def _read_text(self, path, cap=64 * 1024):
        """读文本文件；二进制返回 None；超大文件截断"""
        try:
            with open(path, "rb") as f:
                raw = f.read(cap + 1)
        except OSError:
            return None
        if b"\x00" in raw[:4096]:
            return None
        for enc in ("utf-8-sig", "gbk", "latin-1"):
            try:
                return raw[:cap].decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return None

    def _show_props(self, path):
        w = tk.Toplevel(self.root)
        w.title("属性")
        w.configure(bg=C_BG)
        w.resizable(False, False)
        try:
            w.attributes("-topmost", True)
        except Exception:
            pass

        try:
            st = os.stat(path)
        except OSError:
            messagebox.showerror(APP_NAME, "无法读取该项目的属性。", parent=self.root)
            w.destroy()
            return

        name = display_name(path)
        is_dir = os.path.isdir(path)
        ext = os.path.splitext(name)[1]
        kind = "文件夹" if is_dir else ("文件 %s" % (ext if ext else "(无扩展名)"))
        size_var = tk.StringVar(value="计算中…" if is_dir else human_size(st.st_size))
        if is_dir:
            self._dir_size(path, size_var, w)
        attrs = getattr(st, "st_file_attributes", 0)
        flags = []
        if not os.access(path, os.W_OK):
            flags.append("只读")
        if attrs & 0x2:
            flags.append("隐藏")
        if attrs & 0x4:
            flags.append("系统")
        if os.path.islink(path):
            flags.append("链接")

        rows = [
            ("名称", name),
            ("类型", kind),
            ("位置", os.path.dirname(path.rstrip("\\/")) or path),
            ("大小", size_var),
            ("修改时间", fmt_time(st.st_mtime)),
            ("创建时间", fmt_time(st.st_ctime)),
            ("属性", " ".join(flags) if flags else "无"),
        ]
        for i, (k, v) in enumerate(rows):
            tk.Label(w, text=k, bg=C_BG, fg=C_MUTED, font=(core.FONT, 9),
                     anchor="e").grid(row=i, column=0, sticky="e", padx=(14, 8), pady=4)
            if isinstance(v, tk.Variable):
                tk.Label(w, textvariable=v, bg=C_BG, fg=C_TEXT,
                         font=(core.FONT, 9), anchor="w").grid(row=i, column=1, sticky="w", pady=4)
            else:
                tk.Label(w, text=v, bg=C_BG, fg=C_TEXT, font=(core.FONT, 9),
                         anchor="w", justify="left", wraplength=320).grid(
                    row=i, column=1, sticky="w", pady=4)

        bar = tk.Frame(w, bg=C_BG)
        bar.grid(row=len(rows), column=0, columnspan=2, pady=(8, 12))
        self._pbtn(bar, "复制完整路径", lambda: self._copy(path)).pack(side="left", padx=(0, 8))
        self._pbtn(bar, "确定", w.destroy).pack(side="left")
        w.update_idletasks()
        w.geometry("+%d+%d" % (max(0, self.root.winfo_pointerxy()[0] - 260),
                               max(0, self.root.winfo_pointerxy()[1] - 120)))

    def _pbtn(self, parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd, bg=C_ACCENT, fg="white",
                      activebackground=C_ACCENT_H, activeforeground="white",
                  relief="flat", bd=0, padx=10, pady=4, font=(core.FONT, 9), cursor="hand2")
        return b

    def _dir_size(self, path, var, win):
        """文件夹大小后台统计，主线程轮询取结果（子线程一律不碰 tk）"""
        state = {"done": False, "text": ""}

        def calc():
            total, count = 0, 0
            try:
                for rp, dirs, files in os.walk(path, onerror=lambda e: None):
                    count += len(dirs)
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(rp, f))
                            count += 1
                        except OSError:
                            pass
            except Exception:
                pass
            state["text"] = "%s（%d 个项目）" % (human_size(total), count)
            state["done"] = True

        threading.Thread(target=calc, daemon=True).start()

        def poll():
            if state["done"]:
                try:
                    var.set(state["text"])
                except tk.TclError:
                    pass
                return
            try:
                win.after(200, poll)
            except tk.TclError:
                pass

        win.after(200, poll)
