# -*- coding: utf-8 -*-
"""提权验证：仪表盘能不能把每块硬盘的温度都列出来"""
import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from vbhelper import elevate, hud
from vbhelper.app import VBHelper

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_hud_admin.txt")
lines = []


def log(msg):
    lines.append(str(msg))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


log("管理员: %s" % elevate.is_admin())

app = VBHelper()
app.ball.withdraw()
if hud.is_open(app):
    hud.close_hud(app)
app._open_hud()
w = app._hud_win

snap = {}
t0 = time.time()
while time.time() - t0 < 45:
    app.root.update()
    time.sleep(0.2)
    snap = w.collector.snapshot()
    if (snap["ready"] and snap["probed"] and snap["admin"]
            and any(n.startswith("硬盘") for n, _ in snap["temps"])):
        break

log("等了 %.1f 秒" % (time.time() - t0))
log("采集: ready=%s probed=%s admin=%s" % (snap.get("ready"), snap.get("probed"), snap.get("admin")))
log("读到温度: %s" % (snap.get("temps"),))

# 采集有数不等于界面已经画出来：界面是 press 自己的 2 秒节奏刷的，得再推一把
t1 = time.time()
while time.time() - t1 < 5:
    app.root.update()
    time.sleep(0.1)

box = []
for c in w.temp_box.winfo_children():
    if isinstance(c, tk.Label):
        box.append(c.cget("text"))
    else:
        box.append([x.cget("text") for x in c.winfo_children()])
log("界面温度区: %s" % box)
log("提示行还在吗: %s" % (w.temp_hint is not None))
log("窗口尺寸: %s" % w.win.geometry())

app._do_quit()
log("完成")
