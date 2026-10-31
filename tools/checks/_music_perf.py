# -*- coding: utf-8 -*-
"""占用与启动开销实测：python _music_perf.py

量三件事：新模块 import 成本、开播放器带来的内存增量、音源 node 进程的增量。
"""
import os
import subprocess
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    import psutil
except Exception:
    psutil = None


def rss():
    if psutil is None:
        return 0
    return psutil.Process().memory_info().rss / 1048576.0


def pump(app, n=60):
    for _ in range(n):
        app.root.update()
        time.sleep(0.008)


t0 = time.time()
from vbhelper import core            # noqa: E402
t1 = time.time()
from vbhelper import music           # noqa: E402
from vbhelper import musicapi, musicnet, musiccrypto, music_boards  # noqa: E402
t2 = time.time()
print("import core   %.0f ms" % ((t1 - t0) * 1000))
print("import music+在线模块 %.0f ms" % ((t2 - t1) * 1000))
print("AES 自检向量 %s" % musiccrypto.self_test())

music_selftest_ms = (time.time() - t2) * 1000

from vbhelper.app import VBHelper     # noqa: E402
app = VBHelper()
app.root.update()
base = rss()
print("\n悬浮球本体 RSS %.1f MB" % base)

real_folders = [f for f in ((app.cfg.get("music") or {}).get("folders") or [])
                if os.path.isdir(f)]
app.cfg["music"] = {"folders": real_folders, "quality": "320k"}
pump(app, 20)
t3 = time.time()
win = music.MusicWindow(app)
t4 = time.time()
pump(app, 40)
print("建播放器窗口 %.0f ms（含首次绘制前的 pump %.0f ms）" % (
    (t4 - t3) * 1000, (time.time() - t4) * 1000))
win._scan_all()
deadline = time.time() + 20
while time.time() < deadline and not win.local_uids:
    pump(app, 5)
after_scan = rss()
print("扫描 %d 首后 RSS %.1f MB（增量 %.1f MB）" % (len(win.local_uids), after_scan,
                                                  after_scan - base))
win._select_view(("search", ""))
win.search.delete(0, "end")
win.search.insert(0, "周杰伦")
win._do_search()
deadline = time.time() + 25
while time.time() < deadline and not win._search_rows:
    pump(app, 5)
after_search = rss()
print("在线搜索 %d 条后 RSS %.1f MB（列表增量 %.1f MB）" % (
    len(win._search_rows), after_search, after_search - after_scan))

node = musicapi.find_node()
if node:
    item = {"id": "perf", "name": "自检", "script": musicapi._pack(
        musicapi.SELFTEST_SCRIPT)}
    t5 = time.time()
    win.api.load(item)
    deadline = time.time() + 25
    while time.time() < deadline and not win.api.inited:
        pump(app, 3)
    cold = (time.time() - t5) * 1000
    with_node = rss()
    child_mb = 0.0
    proc = getattr(win.api, "_proc", None)
    if psutil is not None and proc is not None:
        try:
            child_mb = psutil.Process(proc.pid).memory_info().rss / 1048576.0
        except Exception:
            pass
    print("音源冷启动（拉起 node + 脚本初始化）%.0f ms" % cold)
    print("     node 子进程自身 %.1f MB；本进程增量 %.1f MB" % (
        child_mb, with_node - after_search))
    win.api.stop()
    pump(app, 20)
    print("音源销毁后 RSS %.1f MB" % rss())
else:
    print("没找到 node.exe，跳过音源占用测量")

win._select_view(("all", ""))
for i in range(6):
    win._select_view(("search", ""))
    win._select_view(("all", ""))
    pump(app, 4)
print("反复切视图后 RSS %.1f MB（总增量 %.1f MB）" % (rss(), rss() - base))
win.close()
pump(app, 20)
print("关闭播放器后 RSS %.1f MB（回收 %.1f MB）" % (rss(), after_search - rss()))
app.root.destroy()
print("\n结论：播放器净增 %.1f MB（不含悬浮球本体 %.1f MB）" % (
    after_scan - base, base))
