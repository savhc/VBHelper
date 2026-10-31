"""Agent 快捷启动：设置窗口里增删改，目录树右键菜单里一键跑。

跟目录树的配合是重点：右击文件就在**文件所在目录**里跑，右击文件夹就在**该文件夹**
里跑，右击空白处就在项目根跑。AI 编程工具（opencode / claude / aider 之类）都是按
当前工作目录找项目的，目录给错了等于白跑。

启动方式分两种，对应两类工具：
- 命令行：优先在 Windows Terminal 里跑（没装才退回 cmd.exe 控制台窗口）；TUI 工具必须有窗口，不然什么都看不到
- 程序：直接跑指定路径的 exe，图形程序不弹黑框，命令行程序才要控制台
"""
import os
import shlex
import shutil
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import core
from . import platform as plat
from .core import *
from .ui import icons, render

CREATE_NEW_CONSOLE = 0x00000010

KINDS = (("cmd", "运行命令行"), ("exe", "运行程序"))
CWD_MODES = (("target", "目标所在位置"), ("root", "项目根目录"), ("fixed", "固定目录"))
KIND_LABEL = dict(KINDS)
CWD_LABEL = dict(CWD_MODES)

# 这些后缀直接 Popen 是起不来的，得交给 cmd 解释
SCRIPT_EXT = (".bat", ".cmd", ".ps1")

def _parent(app):
    return app.panel or app.root

def _cmd_exe():
    """cmd.exe 的绝对路径：PATH 被改坏时也要能启动"""
    return os.environ.get("COMSPEC") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")

def _wt_exe():
    """Windows Terminal 的位置；没装返回 None（自动退回老控制台）"""
    p = shutil.which("wt.exe")
    if p:
        return p
    # PATH 可能不含 WindowsApps，但应用执行别名仍可用
    p = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Microsoft", "WindowsApps", "wt.exe")
    return p if os.path.isfile(p) else None

def _wt_escape(s):
    """wt 把分号当多标签分隔符，命令里的分号要转义，不然会被拆成两条命令"""
    return s.replace(";", "\\;")

def _terminal(cwd, argv, use_wt=True):
    """把 argv 放进 Windows Terminal 的新标签页；没装终端就开老控制台。

    返回 (真正要 Popen 的 argv, creationflags)。
    """
    wt = _wt_exe() if use_wt else None
    if not wt:
        return argv, CREATE_NEW_CONSOLE
    pre = [wt]
    if cwd:
        pre += ["-d", _wt_escape(cwd)]
    return pre + [_wt_escape(a) for a in argv], 0

# ---------------- 配置读写 ----------------
def shortcuts(app):
    """读快捷启动列表，顺手把坏数据过滤掉（配置文件是手改得动的）"""
    items = []
    for raw in (app.cfg.get("quick_launch") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        items.append({
            "name": name,
            "kind": raw.get("kind") if raw.get("kind") in dict(KINDS) else "cmd",
            "command": str(raw.get("command") or ""),
            "path": str(raw.get("path") or ""),
            "args": str(raw.get("args") or ""),
            "cwd_mode": raw.get("cwd_mode") if raw.get("cwd_mode") in dict(CWD_MODES) else "target",
            "cwd": str(raw.get("cwd") or ""),
            "console": bool(raw.get("console", True)),
        })
    return items

def save_shortcuts(app, items):
    app.cfg["quick_launch"] = items
    save_cfg(app.cfg)

def split_args(s):
    """按空格拆参数，成对的引号原样去掉（不引 shell，纯粹方便用户手写）"""
    out, cur, quote = [], "", None
    for ch in (s or ""):
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out

# ---------------- 启动引擎 ----------------
def target_dir(item, target_path, root_dir):
    """算出该在哪个目录里跑"""
    mode = item.get("cwd_mode") or "target"
    if mode == "root":
        return root_dir or ""
    if mode == "fixed":
        return item.get("cwd") or ""
    # 目标所在位置：文件夹用它自己，文件用它的上级目录
    if target_path:
        return target_path if os.path.isdir(target_path) else os.path.dirname(target_path)
    return root_dir or ""

def build(item, target_path, root_dir, use_wt=True):
    """把一条快捷启动翻译成 Popen 参数，返回 (argv, cwd, creationflags, 错误信息)。

    单独拆出来是为了能脱离界面直接验证命令行拼得对不对；use_wt=False 用于
    终端起不来时退回老控制台。
    """
    cwd = target_dir(item, target_path, root_dir)
    if not cwd:
        cwd = root_dir or None
    if cwd and not os.path.isdir(cwd):
        return None, cwd, 0, "工作目录不存在：%s" % cwd

    if not plat.IS_WINDOWS:
        return _build_posix(item, cwd)

    if item.get("kind") == "exe":
        exe = (item.get("path") or "").strip()
        if not exe:
            return None, cwd, 0, "请先选择要运行的程序。"
        if not os.path.isfile(exe):
            return None, cwd, 0, "程序不存在：%s" % exe
        argv = [exe] + split_args(item.get("args"))
        if exe.lower().endswith(SCRIPT_EXT):
            # 脚本得让 cmd 解释，跑完窗口留着才能看到报错
            run, flags = _terminal(cwd, [_cmd_exe(), "/k", _join(argv)], use_wt)
            return run, cwd, flags, ""
        if not item.get("console", True):
            # 图形程序不要终端窗口
            return argv, cwd, 0, ""
        run, flags = _terminal(cwd, argv, use_wt)
        return run, cwd, flags, ""

    # 命令行：整个字符串交给 cmd，/k 让窗口跑完不消失（命令没装时能看到报错）
    cmd = (item.get("command") or "").strip()
    if not cmd:
        return None, cwd, 0, "请先填写要运行的命令。"
    extra = (item.get("args") or "").strip()
    line = cmd + (" " + extra if extra else "")
    run, flags = _terminal(cwd, [_cmd_exe(), "/k", line], use_wt)
    return run, cwd, flags, ""

def _terminal_posix(cwd, argv):
    """在图形终端里跑一条命令，返回 (argv, creationflags)。

    优先用发行版自带的终端模拟器；都找不到时直接执行，TUI 程序会因为
    没有 tty 报错，但至少不会卡住调用方。
    """
    inner = "cd %s && exec %s" % (
        shlex.quote(cwd or os.path.expanduser("~")),
        " ".join(shlex.quote(a) for a in argv))
    for name in ("x-terminal-emulator", "gnome-terminal", "konsole",
                 "xfce4-terminal", "mate-terminal", "xterm"):
        exe = shutil.which(name)
        if not exe:
            continue
        if name == "gnome-terminal":
            return [exe, "--", "sh", "-c", inner], 0
        if name in ("konsole", "xfce4-terminal", "mate-terminal"):
            return [exe, "-e", "sh", "-c", inner], 0
        return [exe, "-e", "sh", "-c", inner], 0
    return ["sh", "-c", inner], 0


def _build_posix(item, cwd):
    """Linux/macOS 的启动参数：没有 cmd.exe / wt.exe，改用终端模拟器。"""
    if item.get("kind") == "exe":
        exe = (item.get("path") or "").strip()
        if not exe:
            return None, cwd, 0, "请先选择要运行的程序。"
        if not os.path.isfile(exe):
            return None, cwd, 0, "程序不存在：%s" % exe
        argv = [exe] + split_args(item.get("args"))
        if item.get("console", True):
            run, flags = _terminal_posix(cwd, argv)
            return run, cwd, flags, ""
        return argv, cwd, 0, ""

    cmd = (item.get("command") or "").strip()
    if not cmd:
        return None, cwd, 0, "请先填写要运行的命令。"
    extra = (item.get("args") or "").strip()
    line = cmd + (" " + extra if extra else "")
    run, flags = _terminal_posix(cwd, ["sh", "-lc", line])
    return run, cwd, flags, ""


def _join(argv):
    """把 argv 拼成一条命令行，带空格的加引号（只用于交给 cmd 的场合）"""
    return " ".join('"%s"' % a if (" " in a or "\t" in a) else a for a in argv)

def _is_wt(argv):
    return bool(argv) and os.path.basename(argv[0]).lower() == "wt.exe"

def launch(app, item, target_path=None):
    """按一条快捷启动拉起进程；出错给提示而不是静默失败"""
    argv, cwd, flags, err = build(item, target_path, app.root_dir)
    if err:
        messagebox.showerror(APP_NAME, err, parent=_parent(app))
        return False

    def start(a, c, f):
        try:
            subprocess.Popen(a, cwd=c or None, creationflags=f)
            return None
        except Exception as ex:
            return ex

    e = start(argv, cwd, flags)
    # 终端别名启动失败（管理员权限下常见），回退 conhost
    if e is not None and _is_wt(argv):
        argv2, cwd2, flags2, err2 = build(item, target_path, app.root_dir, use_wt=False)
        if err2:
            e = RuntimeError(err2)
        else:
            e = start(argv2, cwd2, flags2)
            if e is None:
                cwd = cwd2
    if e is not None:
        messagebox.showerror(APP_NAME, "启动失败：%s" % e, parent=_parent(app))
        return False
    try:
        app._flash_status("已启动 %s（工作目录 %s）" % (item["name"], cwd or "默认"))
    except Exception:
        pass
    return True

# ---------------- 目录树右键菜单 ----------------
def menu_cascade(app, menu, target_path=None):
    """把快捷启动挂成级联菜单；target_path 为 None 表示右击空白处（用项目根）"""
    sub = tk.Menu(menu, tearoff=0, bg=C_PANEL, fg=C_TEXT, activebackground=C_ACCENT,
                  activeforeground="white", bd=0, relief="flat", font=(core.FONT, 9),
                  borderwidth=1, activeborderwidth=0)
    items = shortcuts(app)
    if not items:
        sub.add_command(label="（还没配置，去「设置」里添加）", state="disabled")
    else:
        for it in items:
            sub.add_command(label=it["name"],
                            command=lambda i=it: launch(app, i, target_path))
        sub.add_separator()
    sub.add_command(label="管理快捷启动…", command=app._settings_window)
    menu.add_cascade(label="快捷启动", menu=sub)
    return sub

# ---------------- 设置窗口里的区块 ----------------
def section(app, parent):
    """设置窗口里的「快捷启动」：列表 + 添加/编辑/删除/排序/测试"""
    tk.Frame(parent, bg=C_BORDER, height=1).pack(fill="x", padx=18, pady=(12, 0))
    head = tk.Frame(parent, bg=C_BG)
    head.pack(fill="x", padx=18, pady=(10, 0))
    tk.Label(head, text="快捷启动", bg=C_BG, fg=C_TEXT,
             font=(core.FONT, 10, "bold")).pack(side="left")
    tk.Label(head, text="右击目录树即可直接跑", bg=C_BG, fg=C_MUTED,
             font=(core.FONT, 8)).pack(side="right")

    state = {"items": shortcuts(app)}

    box = tk.Frame(parent, bg=C_BG)
    box.pack(fill="both", expand=True, padx=18, pady=(6, 0))
    tree = ttk.Treeview(box, columns=("name", "kind", "cwd"), show="headings",
                        style="VB.Treeview", height=5, selectmode="browse")
    for col, title, width, anchor in (("name", "名称", 150, "w"),
                                      ("kind", "启动方式", 80, "center"),
                                      ("cwd", "工作目录", 130, "w")):
        tree.heading(col, text=title)
        tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))
    vsb = ttk.Scrollbar(box, style="VB.Vertical.TScrollbar", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)

    def fill_list(keep=None):
        tree.delete(*tree.get_children(""))
        for i, it in enumerate(state["items"]):
            tree.insert("", "end", iid=str(i),
                        values=(it["name"], KIND_LABEL.get(it["kind"], ""),
                                CWD_LABEL.get(it["cwd_mode"], "")))
        if keep is not None and 0 <= keep < len(state["items"]):
            tree.selection_set(str(keep))
            tree.focus(str(keep))

    def selected():
        sel = tree.selection()
        return int(sel[0]) if sel else None

    def commit():
        save_shortcuts(app, state["items"])

    def editing():
        """编辑窗还开着就别再开一个。

        必须查 winfo_exists：编辑窗有时是被连坐销毁的（比如设置窗口没了），
        这时残留的句柄还在，只看它是不是 None 会让「添加」永久点不动。
        """
        w = getattr(app, "_ql_edit_win", None)
        if w is None:
            return False
        try:
            return bool(w.winfo_exists())
        except tk.TclError:
            return False

    def on_add():
        if editing():
            return

        def saved(data):
            state["items"].append(data)
            commit()
            fill_list(len(state["items"]) - 1)
            app._flash_status("已添加快捷启动：%s" % data["name"])

        edit_dialog(app, parent.winfo_toplevel(), None, saved)

    def on_edit():
        i = selected()
        if i is None:
            messagebox.showinfo(APP_NAME, "先在上面选中一条再编辑。", parent=_parent(app))
            return
        if editing():
            return

        def saved(data):
            state["items"][i] = data
            commit()
            fill_list(i)

        edit_dialog(app, parent.winfo_toplevel(), state["items"][i], saved)

    def on_del():
        i = selected()
        if i is None:
            messagebox.showinfo(APP_NAME, "先在上面选中一条再删除。", parent=_parent(app))
            return
        it = state["items"][i]
        if not messagebox.askyesno(APP_NAME, "删除快捷启动「%s」？" % it["name"],
                                   parent=_parent(app)):
            return
        state["items"].pop(i)
        commit()
        fill_list(min(i, len(state["items"]) - 1))

    def on_move(step):
        i = selected()
        if i is None:
            return
        j = i + step
        if not (0 <= j < len(state["items"])):
            return
        items = state["items"]
        items[i], items[j] = items[j], items[i]
        commit()
        fill_list(j)

    def on_test():
        i = selected()
        if i is None:
            messagebox.showinfo(APP_NAME, "先在上面选中一条再测试。", parent=_parent(app))
            return
        # 按「目标所在位置」测：拿项目根当目标，效果就是在这个项目里跑一次
        launch(app, state["items"][i], app.root_dir)

    def on_copy():
        i = selected()
        if i is None:
            messagebox.showinfo(APP_NAME, "先在上面选中一条再复制。", parent=_parent(app))
            return
        import copy as _copy
        data = _copy.deepcopy(state["items"][i])
        data["name"] = data["name"] + " 副本"
        state["items"].insert(i + 1, data)
        commit()
        fill_list(i + 1)

    def on_export():
        if not state["items"]:
            messagebox.showinfo(APP_NAME, "没有可导出的快捷启动。", parent=_parent(app))
            return
        from tkinter import filedialog
        p = filedialog.asksaveasfilename(
            parent=_parent(app), defaultextension=".json",
            filetypes=[("JSON", "*.json")], title="导出快捷启动")
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(state["items"], f, ensure_ascii=False, indent=1)
            app._flash_status("已导出 %d 条到 %s" % (len(state["items"]), p))
        except Exception as e:
            messagebox.showerror(APP_NAME, "导出失败：%s" % e, parent=_parent(app))

    def on_import():
        from tkinter import filedialog
        p = filedialog.askopenfilename(
            parent=_parent(app), filetypes=[("JSON", "*.json")], title="导入快捷启动")
        if not p:
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("格式不对，应该是列表")
            valid = [x for x in data if isinstance(x, dict) and x.get("name") and x.get("cmd")]
            if not valid:
                messagebox.showinfo(APP_NAME, "文件里没有有效的快捷启动条目。", parent=_parent(app))
                return
            state["items"].extend(valid)
            commit()
            fill_list(len(state["items"]) - len(valid))
            app._flash_status("已导入 %d 条" % len(valid))
        except Exception as e:
            messagebox.showerror(APP_NAME, "导入失败：%s" % e, parent=_parent(app))

    bar = tk.Frame(parent, bg=C_BG)
    bar.pack(fill="x", padx=18, pady=(8, 0))
    _qbtn(bar, "添加…", on_add, accent=True).pack(side="left")
    _qbtn(bar, "编辑…", on_edit).pack(side="left", padx=6)
    _qbtn(bar, "删除", on_del).pack(side="left")
    _qbtn(bar, "复制一条", on_copy).pack(side="left", padx=6)
    _qbtn(bar, "导入…", on_import).pack(side="left")
    _qbtn(bar, "导出…", on_export).pack(side="left", padx=6)
    _qbtn(bar, "测试启动", on_test).pack(side="right")
    _qbtn(bar, "", lambda: on_move(1), icon="arrow-down").pack(side="right", padx=2)
    _qbtn(bar, "", lambda: on_move(-1), icon="arrow-up").pack(side="right")
    tree.bind("<Double-Button-1>", lambda e: on_edit())

    fill_list()
    return tree

def _qbtn(parent, text, cmd, accent=False, icon=None):
    kw = {}
    if icon:
        color = "white" if accent else C_TEXT
        ph = render.photo(icons.draw(icon, 14, color),
                          key="launcher:ico:%s:14:%s" % (icon, color))
        kw = {"image": ph, "compound": "left" if text else "center"}
    b = tk.Button(parent, text=text, command=cmd,
                  bg=C_ACCENT if accent else C_PANEL, fg="white" if accent else C_TEXT,
                  activebackground=C_ACCENT_H if accent else C_HOVER,
                  activeforeground="white", relief="flat", bd=0, padx=10, pady=4,
                  font=(core.FONT, 9), cursor="hand2", **kw)
    if icon:
        b._icon_photo = kw["image"]
    return b

# ---------------- 添加 / 编辑对话框 ----------------
def edit_dialog(app, parent, data, on_save):
    """新增或修改一条快捷启动；确定后回调 on_save(dict)

    挂到传进来的设置窗口下，这样设置窗口关了它也跟着走。
    """
    win = tk.Toplevel(parent or app.root)
    app._ql_edit_win = win
    win.title(("编辑" if data else "添加快捷启动") + " - " + APP_NAME)
    win.configure(bg=C_BG)
    win.geometry("560x470")
    win.resizable(False, False)
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass

    def close():
        app._ql_edit_win = None
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Escape>", lambda e: close())

    src = dict(data or {})
    name_var = tk.StringVar(value=src.get("name") or "")
    kind_var = tk.StringVar(value=src.get("kind") or "cmd")
    cmd_var = tk.StringVar(value=src.get("command") or "")
    path_var = tk.StringVar(value=src.get("path") or "")
    args_var = tk.StringVar(value=src.get("args") or "")
    mode_var = tk.StringVar(value=src.get("cwd_mode") or "target")
    cwd_var = tk.StringVar(value=src.get("cwd") or "")
    con_var = tk.BooleanVar(value=bool(src.get("console", True)))

    tk.Label(win, text="编辑快捷启动" if data else "添加快捷启动", bg=C_BG, fg=C_TEXT,
             font=(core.FONT, 11, "bold")).pack(anchor="w", padx=18, pady=(16, 0))
    tk.Label(win, text="命令行能用的地方，填名字就行；程序请选到具体文件。",
             bg=C_BG, fg=C_MUTED, font=(core.FONT, 8)).pack(anchor="w", padx=18)
    tk.Frame(win, bg=C_BORDER, height=1).pack(fill="x", padx=18, pady=(8, 0))

    body = tk.Frame(win, bg=C_BG)
    body.pack(fill="both", expand=True, padx=18, pady=(8, 0))
    body.columnconfigure(1, weight=1)

    def row_label(r, text, hint=""):
        f = tk.Frame(body, bg=C_BG)
        f.grid(row=r, column=0, sticky="ne", padx=(0, 10), pady=5)
        tk.Label(f, text=text, bg=C_BG, fg=C_TEXT, font=(core.FONT, 9)).pack(anchor="e")
        if hint:
            tk.Label(f, text=hint, bg=C_BG, fg=C_MUTED, font=(core.FONT, 7)).pack(anchor="e")

    def entry(r, var, width=40):
        e = tk.Entry(body, textvariable=var, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                     relief="flat", bd=0, highlightthickness=0, font=(core.FONT, 9), width=width)
        e.grid(row=r, column=1, sticky="we", pady=5, ipady=4)
        return e

    def hint_line(r, text):
        tk.Label(body, text=text, bg=C_BG, fg=C_MUTED, font=(core.FONT, 7),
                 anchor="w", justify="left").grid(row=r, column=1, sticky="w", pady=(0, 4))

    row_label(0, "名称")
    name_entry = entry(0, name_var)
    hint_line(1, "显示在右键菜单里的名字，留空就自动用命令名")

    row_label(2, "启动方式")
    pick = tk.Frame(body, bg=C_BG)
    pick.grid(row=2, column=1, sticky="w", pady=5)
    for key, label in KINDS:
        tk.Radiobutton(pick, text=label, variable=kind_var, value=key, bg=C_BG, fg=C_TEXT,
                       selectcolor=C_PANEL, activebackground=C_BG, activeforeground=C_TEXT,
                       bd=0, highlightthickness=0, font=(core.FONT, 9), cursor="hand2"
                       ).pack(side="left", padx=(0, 14))

    row_label(3, "命令行")
    cmd_entry = entry(3, cmd_var)
    hint_line(4, "例：opencode　claude　aider --model gpt-4　git status")

    row_label(5, "程序路径")
    prow = tk.Frame(body, bg=C_BG)
    prow.grid(row=5, column=1, sticky="we", pady=5)
    prow.columnconfigure(0, weight=1)
    path_entry = tk.Entry(prow, textvariable=path_var, bg=C_PANEL, fg=C_TEXT,
                          insertbackground=C_TEXT, relief="flat", bd=0,
                          highlightthickness=0, font=(core.FONT, 9))
    path_entry.grid(row=0, column=0, sticky="we", ipady=4)
    browse_btn = _qbtn(prow, "浏览…", lambda: pick_file())
    browse_btn.grid(row=0, column=1, padx=(6, 0))
    hint_line(6, "支持 .exe / .bat / .cmd / .ps1")

    row_label(7, "附加参数")
    entry(7, args_var)
    hint_line(8, "可留空；带空格就用英文引号包起来")

    tk.Frame(body, bg=C_BORDER, height=1).grid(row=9, column=0, columnspan=2,
                                              sticky="we", pady=(8, 2))
    row_label(10, "工作目录")
    modes = tk.Frame(body, bg=C_BG)
    modes.grid(row=10, column=1, sticky="w", pady=2)
    tips = {"target": "右击文件＝文件所在文件夹，右击文件夹＝它自己（推荐）",
            "root": "永远用当前项目的最外层根目录",
            "fixed": "每次都固定用同一个目录"}
    for key, label in CWD_MODES:
        line = tk.Frame(modes, bg=C_BG)
        line.pack(anchor="w", pady=1)
        tk.Radiobutton(line, text=label, variable=mode_var, value=key, bg=C_BG, fg=C_TEXT,
                       selectcolor=C_PANEL, activebackground=C_BG, activeforeground=C_TEXT,
                       bd=0, highlightthickness=0, font=(core.FONT, 9), cursor="hand2"
                       ).pack(side="left")
        tk.Label(line, text=tips[key], bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 7)).pack(side="left", padx=(6, 0))
    fdir = tk.Frame(body, bg=C_BG)
    fdir.grid(row=11, column=1, sticky="we", pady=(2, 6))
    fdir.columnconfigure(0, weight=1)
    cwd_entry = tk.Entry(fdir, textvariable=cwd_var, bg=C_PANEL, fg=C_TEXT,
                         insertbackground=C_TEXT, relief="flat", bd=0,
                         highlightthickness=0, font=(core.FONT, 9))
    cwd_entry.grid(row=0, column=0, sticky="we", ipady=4)
    cwd_browse = _qbtn(fdir, "浏览…", lambda: pick_dir())
    cwd_browse.grid(row=0, column=1, padx=(6, 0))

    tk.Frame(win, bg=C_BORDER, height=1).pack(fill="x", padx=18, pady=(10, 0))
    opt = tk.Frame(win, bg=C_BG)
    opt.pack(fill="x", padx=18, pady=(8, 0))
    con_check = tk.Checkbutton(opt, text="以命令行方式启动（新开一个终端窗口）",
                               variable=con_var, bg=C_BG, fg=C_TEXT, selectcolor=C_PANEL,
                               activebackground=C_BG, activeforeground=C_TEXT, bd=0,
                               highlightthickness=0, font=(core.FONT, 9), anchor="w",
                               cursor="hand2")
    con_check.pack(anchor="w")
    tk.Label(opt, text="opencode / claude / aider 这类终端里的工具必须勾上，否则窗口一闪就没；"
                       "VSCode 这类图形程序不要勾。装了 Windows Terminal 时优先用它，"
                       "没装才用老控制台。",
             bg=C_BG, fg=C_MUTED, font=(core.FONT, 7), anchor="w",
             justify="left", wraplength=500).pack(anchor="w", padx=24)

    def pick_file():
        p = filedialog.askopenfilename(
            parent=win, title="选择要运行的程序",
            filetypes=[("程序", "*.exe;*.bat;*.cmd;*.ps1"), ("所有文件", "*.*")])
        if p:
            path_var.set(os.path.normpath(p))
            if not name_var.get().strip():
                name_var.set(os.path.splitext(os.path.basename(p))[0])

    def pick_dir():
        d = filedialog.askdirectory(parent=win, title="选择固定工作目录",
                                    initialdir=cwd_var.get() or app.root_dir or os.path.expanduser("~"))
        if d:
            cwd_var.set(os.path.normpath(d))

    def sync(*_e):
        """按启动方式开关对应的输入框，别让用户对着灰框猜"""
        is_cmd = kind_var.get() == "cmd"
        for w in (path_entry, browse_btn):
            w.configure(state="disabled" if is_cmd else "normal")
        for w in (cmd_entry,):
            w.configure(state="normal" if is_cmd else "disabled")
        if is_cmd:
            con_var.set(True)
        con_check.configure(state="disabled" if is_cmd else "normal")
        is_fixed = mode_var.get() == "fixed"
        for w in (cwd_entry, cwd_browse):
            w.configure(state="normal" if is_fixed else "disabled")

    kind_var.trace_add("write", sync)
    mode_var.trace_add("write", sync)

    bar = tk.Frame(win, bg=C_BG)
    bar.pack(fill="x", padx=18, pady=(12, 14))

    def ok():
        kind = kind_var.get()
        name = name_var.get().strip()
        command = cmd_var.get().strip()
        path = path_var.get().strip()
        if kind == "cmd" and not command:
            messagebox.showwarning(APP_NAME, "请填写要运行的命令。", parent=win)
            return
        if kind == "exe" and not path:
            messagebox.showwarning(APP_NAME, "请选择要运行的程序。", parent=win)
            return
        if not name:
            # 名称为空时回退命令名/程序名，避免因必填项阻塞保存
            base = command or path
            name = os.path.splitext(os.path.basename(split_args(base)[0] if command else base))[0]
            name = name or "未命名"
        on_save({
            "name": name,
            "kind": kind,
            "command": command,
            "path": path,
            "args": args_var.get().strip(),
            "cwd_mode": mode_var.get(),
            "cwd": cwd_var.get().strip(),
            "console": bool(con_var.get()) if kind == "exe" else True,
        })
        close()

    _qbtn(bar, "确定", ok, accent=True).pack(side="right")
    _qbtn(bar, "取消", close).pack(side="right", padx=6)

    sync()
    app._center_on_panel(win)
    win.after(80, name_entry.focus_set)
    return win
