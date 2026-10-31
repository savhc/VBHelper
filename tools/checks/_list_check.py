# -*- coding: utf-8 -*-
"""验证：加密 DNS 列表读写 / 编辑窗口 / 错误提示翻译（离线）"""
import os
import sys
import time
import tkinter as tk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from vbhelper import core, nettools as nt
from vbhelper.app import VBHelper

print("默认 DoH:", [r[0] for r in nt.load_doh_list()])
print("默认 DoT:", [r[0] for r in nt.load_dot_list()])

nt.save_lists([("测试DoH", "https://example.com/dns-query", "1.2.3.4")],
              [("测试DoT", "5.6.7.8", "dns.example.com")])
print("写入后读回:", nt.load_doh_list(), nt.load_dot_list())
cfg = core.load_cfg()
cfg.pop("doh_list", None)
cfg.pop("dot_list", None)
core.save_cfg(cfg)
print("清理后读回:", [r[0] for r in nt.load_doh_list()])

print("翻译示例1:", nt.friendly_err(Exception(
    "<urlopen error [WinError 10054] 远程主机强迫关闭了一个现有的连接。")))
print("翻译示例2:", nt.friendly_err(Exception("<urlopen error timed out>")))

app = VBHelper()
app.ball.withdraw()
app._ensure_panel()
app.panel.withdraw()
win = nt.open_window(app)
win.win.withdraw()
for _ in range(60):
    app.root.update()
    time.sleep(0.01)
win.edit_lists()
for _ in range(30):
    app.root.update()
    time.sleep(0.01)
tops = []
for parent in (app.root, win.win):
    tops += [w for w in parent.winfo_children()
             if isinstance(w, tk.Toplevel) and w.title().startswith("编辑加密")]
print("编辑窗口已弹出:", bool(tops))
if tops:
    texts = []
    stack = list(tops[0].winfo_children())
    while stack:
        x = stack.pop()
        if isinstance(x, tk.Text):
            texts.append(x.get("1.0", "end").strip().splitlines()[0] if x.get("1.0", "end").strip() else "")
        stack.extend(x.winfo_children())
    print("编辑窗内容首行:", [t for t in texts if t][:2])
    tops[0].destroy()
win.win.destroy()
app._do_quit()
print("完成")
