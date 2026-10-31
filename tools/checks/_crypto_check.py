# -*- coding: utf-8 -*-
"""DoH / DoT / ECH 功能实测（真实网络）"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from vbhelper import nettools as nt

print("== DoH 测速（POST wire format）==")
for name, url, ip in nt.DOH_LIST:
    ms, note = nt.doh_test(url)
    print("  %-11s %s" % (name, ("%.0f ms  -> %s" % (ms, note)) if ms else "不可达: " + str(note)[:70]))

print("== DoT 测速（853，TLS 握手 + RFC7858 查询）——只测前 3 个，避免长时间等待 ==")
for name, ip, sni in nt.DOT_LIST[:3]:
    ms, note = nt.dot_test(ip, sni)
    print("  %-11s %s" % (name, ("%.0f ms  %s" % (ms, note)) if ms else "不可达: " + str(note)[:70]))

print("== ECH 检测（HTTPS 记录 ech 参数）==")
for d in ("cloudflare.com", "www.wikipedia.org", "www.baidu.com"):
    info = nt.ech_check(d)
    print("  %-22s ech=%s  alpn=%s  https记录=%d  esni=%s  err=%s"
          % (d, info["ech"], info["alpn"] or "-", len(info["https"]),
             info["esni"], info["error"]))

print("== 系统 DoH 配置 ==")
print(" ", nt.system_doh_list())

print("== 窗口与标签页 ==")
import tkinter as tk
from vbhelper.app import VBHelper

app = VBHelper()
app.ball.withdraw()
app._ensure_panel()
app.panel.withdraw()
win = nt.open_window(app)
win.win.withdraw()
for _ in range(80):
    app.root.update()
    time.sleep(0.01)
for t in ("dns", "crypto", "ech", "diag", "hosts"):
    win.show_tab(t)
print("  五个标签页切换正常:", bool(win.tab_crypto.winfo_exists()),
      bool(win.tab_ech.winfo_exists()))
win.show_tab("crypto")
win.bench_crypto("doh")
for _ in range(150):
    app.root.update()
    time.sleep(0.01)
rows = [win.crypto_tree.item(i, "values") for i in win.crypto_tree.get_children()]
print("  DoH 表格行数:", len(rows))
print("  示例行:", rows[0] if rows else "无")
win.win.destroy()
app._do_quit()
print("全部完成")
