# -*- coding: utf-8 -*-
"""重测：展开到 a.txt、加标记、刷新，检查可见耗时 + 标记是否保留"""
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


def find(suffix):
    stack = list(app.tree.get_children(""))
    while stack:
        i = stack.pop()
        v = app.tree.item(i, "values")
        if v and v[0].endswith(suffix):
            return i
        stack.extend(app.tree.get_children(i))
    return None


def pump_until(cond, limit=600):
    t0 = time.time()
    for _ in range(limit):
        app.root.update()
        if cond():
            return (time.time() - t0) * 1000
        time.sleep(0.004)
    return None


def info(label):
    i = find("a.txt")
    if not i:
        print("%s -> 看不到 a.txt" % label)
        return
    v = app.tree.item(i, "values")
    print("%s -> i 图标=%r  颜色标签=%s" % (label, v[2] if len(v) > 2 else "", app.tree.item(i, "tags")))


app._load(BASE)
print("根层可见耗时 %s ms" % pump_until(lambda: app._loaded >= 1, 120))
src_iid = find("src")
print("src 节点:", "找到" if src_iid else "没找到")
app._open_item(src_iid)
print("展开 src 到 a.txt 出现耗时 %s ms" % pump_until(lambda: find("a.txt") is not None, 120))
info("首次展开")

app._set_note(TGT, "用户的注释")
app._apply_mark(TGT, "红")
info("加注释+颜色后")

for n in (1, 2):
    app._refresh()
    ms = pump_until(lambda: app._loaded >= 1, 120)
    print("第 %d 次刷新：根层可见 %s ms" % (n, ms))
    src_iid = find("src")
    if src_iid:
        app._open_item(src_iid)
    print("  展开 src 到 a.txt 出现耗时 %s ms" % pump_until(lambda: find("a.txt") is not None, 120))
    info("  刷新后标记")

print("内存 notes:", app.notes)
print("内存 marks:", app.marks)
print("meta 文件:", open(META, encoding="utf-8").read().replace("\n", " ")
      if os.path.exists(META) else "没有")
app._do_quit()
