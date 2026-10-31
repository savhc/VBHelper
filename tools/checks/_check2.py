# -*- coding: utf-8 -*-
"""自测：① 第一次点开即展开下钻 ② 标记颜色 ③ 大目录性能"""
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
print("整树载入+自动下钻: %.0f ms, 共 %d 项" % ((time.time() - t0) * 1000, app._loaded))


def check():
    def dump(iid, depth=0):
        vals = app.tree.item(iid, "values")
        print("  " * depth + ("[开]" if app.tree.item(iid, "open") else "[关]") +
              os.path.basename(vals[0] if vals else "?"))
        for k in app.tree.get_children(iid):
            if "loading" not in app.tree.item(k, "tags"):
                dump(k, depth + 1)

    for r in app.tree.get_children(""):
        dump(r)

    # ① 第一次点开纯文件夹层：应立刻读出内容并自动下钻到含文件层
    a_iid = app._find_iid(os.path.join(BASE, "a"))
    app.tree.item(a_iid, open=True)      # 模拟 Tk 处理完点击箭头后的状态
    app._user_expand(a_iid)
    b_iid = app._find_iid(pure)
    print("① 第一次点开 'a' 后, 子层 'b' 是否已展开:", bool(app.tree.item(b_iid, "open")))

    # ② 标记
    tgt = os.path.join(pure, "x.txt")
    app._apply_mark(tgt, "红")
    print("② 标记后 tags:", app.tree.item(app._find_iid(tgt), "tags"),
          "| 配置已写入:", vbhelper.load_cfg().get("marks", {}).get(tgt))
    app._apply_mark(tgt, None)
    print("   清除后 tags:", app.tree.item(app._find_iid(tgt), "tags"))

    # ③ 大目录展开耗时
    b_iid2 = app._find_iid(big)
    app.tree.item(b_iid2, open=False)
    t1 = time.time()
    app._user_expand(b_iid2)
    print("③ 3000 文件目录展开: %.0f ms" % ((time.time() - t1) * 1000))
    print("状态栏:", app.status.cget("text")[-60:])
    app._quit()


app.root.after(300, check)
app.root.mainloop()