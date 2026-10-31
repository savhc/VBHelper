# -*- coding: utf-8 -*-
"""按钮级验证：退出确认框 / 注释窗 / 设置窗（invoke 等价于点一下）"""
import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import vbhelper

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_t3")
os.makedirs(BASE, exist_ok=True)
TGT = os.path.join(BASE, "a.txt")
open(TGT, "w").close()


def pump(app, n=30):
    for _ in range(n):
        app.root.update()
        time.sleep(0.01)


def find(w, cls, text=None):
    stack = [w]
    while stack:
        x = stack.pop()
        try:
            if isinstance(x, cls) and (text is None or str(x.cget("text")) == text):
                return x
            stack.extend(x.winfo_children())
        except tk.TclError:
            pass
    return None


def top(app, prefix):
    roots = [app.root]
    if app.panel is not None:
        roots.append(app.panel)
    for r in roots:
        for w in r.winfo_children():
            if isinstance(w, tk.Toplevel):
                try:
                    if w.title().startswith(prefix):
                        return w
                except tk.TclError:
                    pass
    return None


app = vbhelper.VBHelper()
app.ball.withdraw()
app._ensure_panel()
app.panel.withdraw()
app._load(BASE)
pump(app, 60)

app._quit()
pump(app)
d = app._quit_dialog
print("1 退出确认框弹出 :", bool(d and d.winfo_exists()))
cancel = find(d, tk.Button, "取消")
print("1 找到取消按钮   :", cancel is not None)
if cancel:
    cancel.invoke()
pump(app)
print("1 点取消后关闭   :", not d.winfo_exists())
print("1 程序仍在运行   :", bool(app.root.winfo_exists()))

app._note_dialog(TGT)
pump(app)
nw = top(app, "注释")
print("2 注释窗弹出     :", nw is not None)
if nw:
    txt = find(nw, tk.Text)
    txt.insert("1.0", "hello note")
    save = find(nw, tk.Button, "保存")
    print("2 找到保存按钮   :", save is not None)
    save.invoke()
    pump(app)
    print("2 保存后窗口关闭 :", not nw.winfo_exists())
    print("2 行第三列       :", app.tree.item(app._find_iid(TGT), "values")[2])
    print("2 注释已写入     :", app.notes.get("a.txt"))

app._note_dialog(TGT)
pump(app)
nw2 = top(app, "注释")
clr = find(nw2, tk.Button, "清除注释")
print("3 找到清除按钮   :", clr is not None)
clr.invoke()
pump(app)
print("3 清除后 notes   :", app.notes.get("a.txt"))

app._settings_window()
pump(app)
sw = top(app, "设置")
print("4 设置窗弹出     :", sw is not None)
if sw:
    cb = find(sw, tk.Checkbutton)
    print("4 开关文字       :", cb.cget("text"))
    cb.invoke()
    pump(app)
    print("4 点开关后 drill :", app.set.get("drill"))
    find(sw, tk.Button, "关闭").invoke()
    pump(app)
    print("4 设置窗已关闭   :", not sw.winfo_exists())

app._menu_window()
pump(app)
mw = top(app, "菜单")
print("5 菜单窗弹出     :", mw is not None)
if mw:
    find(mw, tk.Button, "收起面板").invoke()
    pump(app)
    print("5 点收起面板后窗口关闭:", not mw.winfo_exists())
    print("5 面板已收起     :", not app.panel.winfo_ismapped())

app._do_quit()
