# -*- coding: utf-8 -*-
"""拆包后的回归验证：目录树 / 注释 / 网络工具窗口 / 提权检测"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from vbhelper import elevate, nettools
from vbhelper.app import VBHelper

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_t4")
SRC = os.path.join(BASE, "src")
os.makedirs(SRC, exist_ok=True)
TGT = os.path.join(SRC, "a.txt")
open(TGT, "w").close()


def pump(app, n=50):
    for _ in range(n):
        app.root.update()
        time.sleep(0.01)


app = VBHelper()
app.ball.withdraw()
app._ensure_panel()
app.panel.withdraw()
app._load(BASE)
pump(app)
print("1 树已加载项数 :", app._loaded)
print("1 根节点子项   :", [app.tree.item(i, "text") for i in app.tree.get_children("")])

app._set_note(TGT, "测试注释内容")
iid = app._find_iid(TGT)
print("2 注释写入     :", app.notes)
print("2 行第三列(i)  :", repr(app.tree.item(iid, "values")[2]) if iid else "行不可见")

win = nettools.open_window(app)
win.win.withdraw()
t0 = time.time()
pump(app, 900)
print("3 等待 %.1f 秒" % (time.time() - t0))
print("3 网络工具窗口 :", bool(win.win.winfo_exists()))
print("3 网卡下拉     :", repr(win.iface_var.get()), "| 缓存:", win.iface_dns)
print("3 状态栏文本   :", win.status.cget("text"))
print("3 hosts 字符数 :", len(win.hosts_txt.get("1.0", "end")))
print("3 管理员状态   :", elevate.is_admin())
print("4 DNS 探测(阿里):", nettools.dns_latency("223.5.5.5", "www.baidu.com"))
win.win.destroy()
app._do_quit()
print("全部检查完成")
