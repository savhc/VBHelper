# -*- coding: utf-8 -*-
"""验证：工具栏按钮清单 / 菜单窗只含网络工具与设置 / 网络工具入口能开窗"""
import os
import sys
import time
import tkinter as tk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from vbhelper import elevate
from vbhelper.app import VBHelper

app = VBHelper()
app.ball.withdraw()
app._ensure_panel()
app.panel.withdraw()

texts = [w.cget("text") for w in app.title_bar.winfo_children()
         if isinstance(w, tk.Label)]
print("工具栏按钮:", texts)
print("工具栏已无网络工具入口:", all("🌐" not in str(t) for t in texts))

app._menu_window()
for _ in range(40):
    app.root.update()
    time.sleep(0.01)
mw = None
for w in app.root.winfo_children():
    if isinstance(w, tk.Toplevel) and w.title().startswith("菜单"):
        mw = w
print("菜单窗已弹出:", bool(mw))
labels = []
if mw:
    stack = list(mw.winfo_children())
    while stack:
        x = stack.pop()
        if isinstance(x, tk.Label):
            labels.append(str(x.cget("text")))
        stack.extend(x.winfo_children())
    print("菜单内的项:", [l for l in labels if l in ("网络工具", "设置")])
    print("没有旧的杂项:", not any(l.startswith(("切换目录", "刷新", "全部展开",
                                                  "全部折叠", "收起面板", "退出"))
                                    for l in labels))
    mw.destroy()
for _ in range(10):
    app.root.update()

orig = elevate.is_admin
elevate.is_admin = lambda: True          # 假装已是管理员，避免弹确认框
app._open_nettools()
for _ in range(80):
    app.root.update()
    time.sleep(0.01)
w2 = getattr(app, "_nettools_win", None)
print("网络工具窗口可从菜单打开:", bool(w2 and w2.win.winfo_exists()))
if w2:
    w2.win.destroy()
elevate.is_admin = orig

app._settings_window()
for _ in range(40):
    app.root.update()
    time.sleep(0.01)
sw = None
for w in app.root.winfo_children():
    if isinstance(w, tk.Toplevel) and w.title().startswith("设置"):
        sw = w
print("设置窗口仍可用:", bool(sw))
if sw:
    sw.destroy()
app._do_quit()
print("完成")
