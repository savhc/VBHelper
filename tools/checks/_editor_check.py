# -*- coding: utf-8 -*-
"""临时回归：快捷修改（跑完即删）。"""
import os
import shutil
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="vb_editor_cfg_")

import tkinter as tk  # noqa: E402

import vbhelper.app as app_mod  # noqa: E402
from vbhelper import editor  # noqa: E402

FAILS = []
WARNINGS = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def span_text(txt, tag):
    ranges = txt.tag_ranges(tag)
    return [txt.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)]


app_mod.VBHelper._build_ball = lambda self: None
app_mod.VBHelper._place_ball = lambda self: None
app_mod.VBHelper._hover_poll = lambda self: None
app_mod.VBHelper._pump_results = lambda self: None

v = app_mod.VBHelper()
v.root.withdraw()

editor.messagebox.showerror = lambda *a, **k: WARNINGS.append(("error", a))
editor.messagebox.showwarning = lambda *a, **k: WARNINGS.append(("warning", a))
editor.messagebox.askyesno = lambda *a, **k: True
editor.messagebox.askyesnocancel = lambda *a, **k: False

base = tempfile.mkdtemp(prefix="vb_editor_")
opened = []


def write(name, text, enc):
    p = os.path.join(base, name)
    with open(p, "wb") as f:
        f.write(text.encode(enc))
    return p


try:
    py = write("sample.py", "# 注释\ndef hello(name):\n    return \"hi \" + name + 42\n", "utf-8")
    gbk = write("gbk.txt", "中文注释\r\n第二行\r\n", "gbk")
    bom = write("bom.py", "print('x')\n", "utf-8-sig")
    u16 = write("u16.txt", "第一行\n第二行\n", "utf-16")
    binary = os.path.join(base, "binary.dat")
    with open(binary, "wb") as f:
        f.write(b"\x00\x01\x02hello")

    # ---- Python 文件：编码、内容、着色 ----
    e = editor.open_editor(v, py)
    opened.append(e)
    check("打开 Python 文件", e is not None)
    e.win.withdraw()
    v.root.update()
    check("UTF-8 内容一致", e.text == "# 注释\ndef hello(name):\n    return \"hi \" + name + 42\n", repr(e.text))
    check("编码识别 UTF-8", e.encoding == "UTF-8", e.encoding)
    check("换行识别 LF", e.newline_name == "LF", e.newline_name)
    e._highlight()
    kw = span_text(e.txt, "kw")
    com = span_text(e.txt, "com")
    stri = span_text(e.txt, "str")
    num = span_text(e.txt, "num")
    check("关键字着色", "def" in kw and "return" in kw, repr(kw))
    check("注释着色", any("# 注释" in s for s in com), repr(com))
    check("字符串着色", any("hi " in s for s in stri), repr(stri))
    check("数字着色", "42" in num, repr(num))
    check("行号生成", e.gutter.get("1.0", "end-1c").startswith("1\n2\n3\n4"), repr(e.gutter.get("1.0", "end-1c")))

    # ---- GBK + CRLF ----
    g = editor.open_editor(v, gbk)
    opened.append(g)
    g.win.withdraw()
    v.root.update()
    check("GBK 内容", g.text == "中文注释\n第二行\n", repr(g.text))
    check("GBK 编码识别", g.encoding == "GBK", g.encoding)
    check("CRLF 保留", g.newline_name == "CRLF" and g.newline == "\r\n", g.newline_name)

    # ---- UTF-8 BOM ----
    b = editor.open_editor(v, bom)
    opened.append(b)
    b.win.withdraw()
    v.root.update()
    check("UTF-8 BOM 内容", b.text == "print('x')\n", repr(b.text))
    check("UTF-8 BOM 识别", b.encoding == "UTF-8 BOM", b.encoding)

    # ---- UTF-16 LE ----
    u = editor.open_editor(v, u16)
    opened.append(u)
    u.win.withdraw()
    v.root.update()
    check("UTF-16 内容", u.text == "第一行\n第二行\n", repr(u.text))
    check("UTF-16 识别", u.encoding == "UTF-16 LE", u.encoding)

    # ---- 二进制：默认进 Hex/PE 只读查看 ----
    before = len(WARNINGS)
    bin_ed = editor.open_editor(v, binary)
    check("二进制以查看器打开",
          bin_ed is not None and bin_ed.is_binary
          and bin_ed._mode in ("hex", "pe")
          and len(WARNINGS) == before,
          None if bin_ed is None else (bin_ed.is_binary, bin_ed._mode))
    if bin_ed is not None:
        opened.append(bin_ed)
        bin_ed.win.withdraw()
        v.root.update()
        check("二进制禁止保存", bin_ed.save() is False)

    # ---- 修改并保存（UTF-8） ----
    e.txt.insert("end", "# tail\n")
    v.root.update()
    check("修改标记", e._dirty is True)
    check("保存返回成功", e.save() is True)
    with open(py, "rb") as f:
        saved = f.read().decode("utf-8")
    check("保存内容写回", saved.endswith("# tail\n"), repr(saved[-20:]))
    check("保存后清除修改标记", e._dirty is False)

    # ---- GBK + CRLF 保存 ----
    g.txt.insert("end", "第三行\n")
    v.root.update()
    check("GBK 保存成功", g.save() is True)
    raw = open(gbk, "rb").read()
    check("GBK 字节保存", raw.decode("gbk") == "中文注释\r\n第二行\r\n第三行\r\n", repr(raw))
    check("GBK 换行未被改写", b"\n" in raw and b"\r\n" in raw)

    # ---- 编码切换 + BOM 保存 ----
    b.enc_var.set("UTF-8 BOM")
    b._on_encoding()
    v.root.update()
    check("切换编码后标签更新", b.encoding == "UTF-8 BOM", b.encoding)
    b.txt.insert("end", "y\n")
    v.root.update()
    check("BOM 文件保存成功", b.save() is True)
    raw = open(bom, "rb").read()
    check("保存保留 BOM", raw.startswith(b"\xef\xbb\xbf"), repr(raw[:6]))

    # ---- 不兼容编码切换被拒绝，仍保持原编码 ----
    warn_before = len(WARNINGS)
    u.enc_var.set("UTF-8")
    u._on_encoding()
    v.root.update()
    check("不兼容编码切换被拒绝", u.encoding == "UTF-16 LE"
          and len(WARNINGS) == warn_before + 1, u.encoding)
    check("编码下拉框回退", u.enc_var.get() == "UTF-16 LE", u.enc_var.get())

    # ---- 大文件着色性能 ----
    big = os.path.join(base, "big.py")
    block = "def f_%d(x):\n    # comment %d\n    return \"value\" + str(x) + 12345\n"
    with open(big, "w", encoding="utf-8") as f:
        for i in range(3000):
            f.write(block % (i, i))
    big_ed = editor.open_editor(v, big)
    opened.append(big_ed)
    big_ed.win.withdraw()
    v.root.update()
    t0 = time.time()
    big_ed._highlight()
    cost = time.time() - t0
    print("  300KB 着色耗时: %.2f 秒" % cost)
    check("大文件仍可着色", big_ed._highlight_on and cost < 8.0, "%.2fs" % cost)

    # ---- 右键菜单：文件有「快捷修改」，文件夹没有 ----
    from tkinter import ttk
    v.tree = ttk.Treeview(v.root)
    v.tree.insert("", "end", iid="f", values=("", py, "file"))
    v.tree.insert("", "end", iid="d", values=("", base, "dir"))
    v.tree.identify_row = lambda y: "f" if y == 10 else "d"
    menus = []
    posted = []       # 真正 post 出来的主菜单（颜色标记等是子菜单，不能拿 menus[-1] 当主菜单）
    orig_mk = v._mk_menu

    def mk_menu():
        m = orig_mk()

        def post(*a, **k):
            posted.append(m)

        m.post = post
        menus.append(m)
        return m

    v._mk_menu = mk_menu

    class Ev:
        pass

    ev = Ev()
    ev.x = ev.y = ev.x_root = ev.y_root = 10
    def menu_labels(m):
        out = []
        for i in range(int(m.index("end")) + 1):
            try:
                out.append(m.entrycget(i, "label"))
            except tk.TclError:
                out.append("")        # separator：Tk 9 没有 -label 选项
        return out

    v._tree_menu(ev)
    labels = menu_labels(posted[-1])
    check("文件右键有快捷修改", "快捷修改" in labels, repr(labels))
    ev.y = 30
    v._tree_menu(ev)
    labels = menu_labels(posted[-1])
    check("文件夹右键没有快捷修改", "快捷修改" not in labels, repr(labels))

    # ---- 关闭 ----
    for ed in opened:
        ed._dirty = False
        check("关闭窗口", ed.close() is True)
    opened = []

finally:
    for ed in opened:
        try:
            ed._destroy()
        except Exception:
            pass
    try:
        v.root.destroy()
    except Exception:
        pass
    shutil.rmtree(base, ignore_errors=True)

print("=" * 46)
if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("ALL OK")
