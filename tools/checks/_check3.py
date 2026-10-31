# -*- coding: utf-8 -*-
"""自测：异步加载 / 首次点开自动接力下钻 / 标记 / 大目录 / 预热缓存"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import vbhelper

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_t")
chain = os.path.join(BASE, "src", "main", "java", "com", "demo")
os.makedirs(chain, exist_ok=True)
open(os.path.join(chain, "App.java"), "w").close()
pure = os.path.join(BASE, "a", "b")
os.makedirs(pure, exist_ok=True)
open(os.path.join(pure, "x.txt"), "w").close()
big = os.path.join(BASE, "big")
os.makedirs(big, exist_ok=True)
for i in range(3000):
    open(os.path.join(big, "f%d.txt" % i), "w").close()

app = vbhelper.VBHelper()
app._ensure_panel()
t0 = time.time()
app._load(BASE)
app._expand()
print("主线程 _load 立即返回: %.0f ms (不应阻塞在读盘上)" % ((time.time() - t0) * 1000))


def dump(iid, depth=0):
    vals = app.tree.item(iid, "values")
    print("  " * depth + ("[开]" if app.tree.item(iid, "open") else "[关]") +
          os.path.basename(vals[0] if vals else "?"))
    for k in app.tree.get_children(iid):
        if "loading" not in app.tree.item(k, "tags"):
            dump(k, depth + 1)


def step1():
    print("① 后台读取完成后:")
    for r in app.tree.get_children(""):
        dump(r)
    a_iid = app._find_iid(os.path.join(BASE, "a"))
    app.tree.item(a_iid, open=True)
    app._user_expand(a_iid)
    b_iid = app._find_iid(pure)
    print("   刚点开时 b 是否已展开 =", bool(app.tree.item(b_iid, "open")), "(异步中应为 False)")
    app.root.after(800, step2)


def step2():
    b_iid = app._find_iid(pure)
    print("   800ms 后 b 是否已展开 =", bool(app.tree.item(b_iid, "open")),
          "(应自动接力下钻为 True)")
    tgt = os.path.join(pure, "x.txt")
    app._apply_mark(tgt, "红")
    print("② 标记 tags:", app.tree.item(app._find_iid(tgt), "tags"),
          "| 配置已存:", vbhelper.load_cfg().get("marks", {}).get(tgt))
    app._apply_mark(tgt, None)
    print("   清除后 tags:", app.tree.item(app._find_iid(tgt), "tags"))
    big_iid = app._find_iid(big)
    t = time.time()
    app.tree.item(big_iid, open=True)
    app._user_expand(big_iid)
    app.root.after(1600, lambda: step3(t))


def step3(t):
    kids = app.tree.get_children(app._find_iid(big))
    print("③ 3000 文件目录渲染完成: %.0f ms, 子项 %d" % ((time.time() - t) * 1000, len(kids)))
    print("④ 预热缓存目录数:", len(app._preheat_cache), "| 后台扫描中:", len(app._inflight))
    print("状态栏:", app.status.cget("text")[-46:])
    app._quit()


app.root.after(900, step1)
app.root.mainloop()