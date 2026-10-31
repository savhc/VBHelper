# -*- coding: utf-8 -*-
"""滚轮接线自检：python _ui_scroll_check.py

覆盖：
  通用 bind_canvas_wheel：指针在 Canvas 子控件上也能滚；
  Treeview 自己滚时页面不跟着滚；
  音乐 _ScrollPane（音源/下载/设置卡片页）滚轮生效；
  真实设置窗（floating._settings_window）滚轮生效。

窗口都是临时创建、用完销毁；不写用户配置。
"""
import os
import sys
import tkinter as tk
from tkinter import ttk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from vbhelper.ui.scroll import bind_canvas_wheel

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


def make_page(win):
    """Toplevel 里放一个可滚动的 Canvas 页，返回 (canvas, inner)。"""
    canvas = tk.Canvas(win, bg="#111111", highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg="#111111")
    canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    return canvas, inner


def wheel(widget, delta=-120):
    widget.event_generate("<MouseWheel>", delta=delta)


def find_descendant(widget, cls):
    for child in widget.winfo_children():
        if isinstance(child, cls):
            return child
        found = find_descendant(child, cls)
        if found is not None:
            return found
    return None


def test_standalone(root):
    win = tk.Toplevel(root)
    win.geometry("460x260+80+80")
    canvas, inner = make_page(win)
    for i in range(80):
        tk.Label(inner, text="row %d" % i, bg="#111111", fg="#ffffff").pack(anchor="w")
    tree = ttk.Treeview(inner, columns=("n",), show="headings", height=3)
    tree.heading("n", text="n")
    tree.pack(fill="x")
    for i in range(50):
        tree.insert("", "end", values=(i,))
    bind_canvas_wheel(win, canvas, area=win)
    root.update()

    label = find_descendant(inner, tk.Label)
    before = canvas.yview()[0]
    wheel(label)
    root.update()
    check("指针在 Label 上滚轮生效", canvas.yview()[0] > before,
          "%.4f -> %.4f" % (before, canvas.yview()[0]))

    canvas.yview_moveto(0)
    root.update()
    before = canvas.yview()[0]
    wheel(canvas)
    root.update()
    check("指针在 Canvas 空白处滚轮生效", canvas.yview()[0] > before)

    canvas.yview_moveto(0)
    tree.yview_moveto(0)
    root.update()
    c_before, t_before = canvas.yview()[0], tree.yview()[0]
    wheel(tree)
    root.update()
    check("Treeview 自己滚", tree.yview()[0] > t_before,
          "%.4f -> %.4f" % (t_before, tree.yview()[0]))
    check("Treeview 滚动时页面不动", canvas.yview()[0] == c_before,
          "%.4f -> %.4f" % (c_before, canvas.yview()[0]))

    win.destroy()

    # area 隔离：另一个窗口里，页面外的控件不触发滚动
    win2 = tk.Toplevel(root)
    win2.geometry("460x260+100+100")
    canvas2, _inner2 = make_page(win2)
    outside = tk.Label(win2, text="outside", bg="#111111", fg="#ffffff")
    outside.pack(side="bottom")
    bind_canvas_wheel(win2, canvas2, area=canvas2)
    root.update()
    before = canvas2.yview()[0]
    wheel(outside)
    root.update()
    check("页面外的滚轮不误滚", canvas2.yview()[0] == before)
    win2.destroy()


def test_music_scrollpane(root):
    from vbhelper.music import _ScrollPane
    win = tk.Toplevel(root)
    win.geometry("460x300+120+120")
    pane = _ScrollPane(win, "#101010")
    pane.frame.pack(fill="both", expand=True)
    for i in range(60):
        pane.add(tk.Label(pane.inner, text="card %d" % i, bg="#101010",
                          fg="#ffffff"))
    root.update()
    pane._sync()
    root.update()
    card = pane.inner.winfo_children()[0]
    before = pane.canvas.yview()[0]
    wheel(card)
    root.update()
    check("音乐卡片页滚轮生效", pane.canvas.yview()[0] > before,
          "%.4f -> %.4f" % (before, pane.canvas.yview()[0]))
    win.destroy()


def test_real_settings(app):
    try:
        app.root.update()
        app._settings_window()
        app.root.update()
        win = app._settings_win
        canvas = None
        for child in win.winfo_children():
            if isinstance(child, tk.Canvas):
                canvas = child
                break
        if canvas is None:
            check("找到设置页 Canvas", False)
            return
        inner = next((w for w in canvas.winfo_children()
                      if isinstance(w, tk.Frame)), None)
        if inner is None:
            check("找到设置页 inner", False)
            return
        # 内容可能刚好装得下，补一段高的填充确保可滚
        tk.Frame(inner, height=1200, width=10).pack(side="bottom")
        app.root.update()
        label = find_descendant(inner, tk.Label)
        before = canvas.yview()[0]
        wheel(label)
        app.root.update()
        check("真实设置窗滚轮生效", canvas.yview()[0] > before,
              "%.4f -> %.4f" % (before, canvas.yview()[0]))
    finally:
        try:
            app._settings_win.destroy()
        except Exception:
            pass


def main():
    from vbhelper.app import VBHelper

    # 只能有一个 Tk 根：PhotoImage 属于创建它的解释器，多 root 会互相不认
    app = VBHelper()
    try:
        test_standalone(app.root)
        test_music_scrollpane(app.root)
        test_real_settings(app)
    finally:
        try:
            app._do_quit()
        except Exception:
            pass
    print("\n结果：%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
