# -*- coding: utf-8 -*-
"""一次性诊断：设置窗口里快捷启动的按钮栏到底排到哪去了（跑完即删）"""
import os
import tempfile

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="vb_diag_")

import tkinter as tk                                    # noqa: E402
import tkinter.ttk as ttk                               # noqa: E402
from PIL import ImageGrab                               # noqa: E402
from vbhelper import core                               # noqa: E402
from vbhelper.app import VBHelper                       # noqa: E402

app = VBHelper()
for _ in range(8):
    app.root.update()
win = app._settings_window()
for _ in range(20):
    app.root.update()

print("设置窗口: %dx%d" % (win.winfo_width(), win.winfo_height()))

def find_all(w, cls):
    out = []
    for c in w.winfo_children():
        if isinstance(c, cls):
            out.append(c)
        out.extend(find_all(c, cls))
    return out

print("\n== 设置窗口里所有可见控件的位置（y 是否落在窗口内）==")
H = win.winfo_height()
for c in find_all(win, (tk.Widget,)):
    try:
        if not isinstance(c, (tk.Button, tk.Label, tk.Entry, tk.Checkbutton,
                              ttk.Treeview, tk.Frame)):
            continue
        txt = ""
        try:
            txt = c.cget("text")
        except Exception:
            pass
        y, h = c.winfo_y(), c.winfo_height()
        mgr = c.winfo_manager()
        flag = "" if (y + h <= H) else "   <<<<< 超出窗口底部，被挤没了"
        if isinstance(c, tk.Button):
            print("  按钮 %-10r y=%-4d h=%-4d 底部=%-4d manager=%s%s"
                  % (txt, y, h, y + h, mgr, flag))
    except tk.TclError:
        pass

print("\n== 快捷启动区块各层的几何 ==")
trees = find_all(win, ttk.Treeview)
for t in trees:
    print("  Treeview y=%d h=%d 底部=%d" % (t.winfo_y(), t.winfo_height(),
                                           t.winfo_y() + t.winfo_height()))
    p = t.master
    print("  它的父容器(box) y=%d h=%d 底部=%d"
          % (p.winfo_y(), p.winfo_height(), p.winfo_y() + p.winfo_height()))
    gp = p.master
    for sib in gp.winfo_children():
        print("     %s 兄弟 %-8s y=%-4d h=%-4d 底部=%-4d"
              % ("->" if sib is p else "  ", sib.winfo_class(),
                 sib.winfo_y(), sib.winfo_height(), sib.winfo_y() + sib.winfo_height()))

win.update_idletasks()
app.root.update()
box = (win.winfo_rootx(), win.winfo_rooty(),
       win.winfo_rootx() + win.winfo_width(), win.winfo_rooty() + win.winfo_height())
img = ImageGrab.grab(bbox=box)
out = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_diag_settings.png")
img.save(out)
print("\n截图:", out, img.size)

app._do_quit()
