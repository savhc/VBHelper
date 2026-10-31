# -*- coding: utf-8 -*-
"""复现：加注释+颜色 -> 刷新 -> 展开，看 i 图标/颜色是否丢失、内容是否延迟出现"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from vbhelper import core
from vbhelper.app import VBHelper

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_t5")
SRC = os.path.join(BASE, "src")
os.makedirs(SRC, exist_ok=True)
TGT = os.path.join(SRC, "a.txt")
open(TGT, "w").close()
META = os.path.join(BASE, core.META_NAME)
if os.path.exists(META):
    os.remove(META)

app = VBHelper()
app.ball.withdraw()
app._ensure_panel()
app.panel.withdraw()


def pump(n=60):
    for _ in range(n):
        app.root.update()
        time.sleep(0.01)


def state(label):
    found = []
    stack = list(app.tree.get_children(""))
    while stack:
        i = stack.pop()
        v = app.tree.item(i, "values")
        t = app.tree.item(i, "tags")
        if v and v[0].endswith("a.txt"):
            found.append(("icon=%r" % (v[2] if len(v) > 2 else ""), "tags=%s" % (t,)))
        stack.extend(app.tree.get_children(i))
    print("%s -> %s" % (label, found or "树里没有 a.txt"))


t0 = time.time()
app._load(BASE)
app._expand()
pump()
print("首次加载耗时 %.0f ms, 项数 %d" % ((time.time() - t0) * 1000, app._loaded))
state("首次加载后")

app._set_note(TGT, "用户的注释")
app._apply_mark(TGT, "红")
state("加注释+颜色后")
print("meta 文件内容:", open(META, encoding="utf-8").read().replace("\n", " ")
      if os.path.exists(META) else "没有 meta 文件")

t1 = time.time()
app._refresh()
pump(30)
print("刷新后 0.3 秒, 已加载项数 %d" % app._loaded)
state("刷新后 0.3 秒")
print("  内存 notes:", app.notes)
print("  内存 marks:", app.marks)
pump(120)
print("刷新耗时 %.0f ms, 项数 %d" % ((time.time() - t1) * 1000, app._loaded))
state("刷新完全稳定后")

app._collapse_all()
pump(20)
app._expand_all()
pump(150)
state("折叠再展开后")
app._do_quit()
