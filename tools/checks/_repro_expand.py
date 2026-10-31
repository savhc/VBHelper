# -*- coding: utf-8 -*-
"""回归探针：目录树展开链路（无窗口，root 全程 withdrawn）。

覆盖：
S1  你报的 bug——用户点开目录时，预热扫描正占着同一路径，
    修复前请求被静默丢弃、节点永远停在「正在读取…」；
    修复后请求挂单等待，扫描一完成就渲染，不需要收起再展开。
S2  缓存命中路径退化：预热完的目录点开要立刻渲染。
S3  _children_by_tag：Tk 8.6 已没有 getchildren 子命令，修复后按标签过滤要真的工作。
S4  下钻守卫：这一层有文件时不得继续下钻；纯文件夹层要顺着链展开。
S5  「全部展开」接力：未就绪的环节 _on_loaded 落地后续接，不能只扩一层。
S6  树代数守卫：换目录树后旧回调作废，不把旧内容灌进新树。
"""
import os
import sys
import time
import queue
import shutil
import tempfile
import threading
import traceback

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tkinter as tk
from tkinter import ttk

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "vbhelper_core", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "vbhelper.py"))
mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# --------- 让 __init__ 不造任何可见窗口、不让后台泵抢时序 ---------
mod.VBHelper._build_ball = lambda self: None
mod.VBHelper._place_ball = lambda self: None
mod.VBHelper._hover_poll = lambda self: None
mod.VBHelper._pump_results = lambda self: None   # 改为测试里手动泵，控制节奏

v = mod.VBHelper()
v._update_status = lambda *a, **k: None          # 面板不存在，掐掉状态栏
v._flash_status = lambda msg: None
v.tree = ttk.Treeview(v.root)                    # 真实 Treeview，只是不给几何
v._budget = 6000
v._loaded = 0
v.set["drill"] = True

FAILURES = []
_tempdirs = []


def mk(tmp):
    _tempdirs.append(tmp)
    return tmp


def pump(seconds=5.0, until=None):
    """手动泵.v._results + 处理 after 队列，直到条件 near 或超时"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            while True:
                path, rows, req = v._results.get_nowait()
                v._scan_done(path, rows, req)
        except queue.Empty:
            pass
        v.root.update()
        if until and until():
            return True
        time.sleep(0.01)
    return bool(until and until())


def live(iid):
    """拿到该节点的真实子项（不含 loading 占位）"""
    kids = v.tree.get_children(iid)
    live_kids = []
    for k in kids:
        try:
            if "loading" not in v.tree.item(k, "tags"):
                live_kids.append(k)
        except tk.TclError:
            pass
    return tuple(live_kids)


def make_dir_node(path, tags=("dir",)):
    """按 _insert_rows 的形状造一个「已可见但内容未读」的目录节点"""
    iid = v.tree.insert("", "end", text="📁 " + os.path.basename(path) or path,
                        values=(path, "dir", ""), tags=tags)
    v.tree.insert(iid, "end", text="", tags=("loading",))
    return iid


def _find_chain(root_iid, chain_paths):
    """顺着链路径找出 l1/l2/l3... 对应的树节点 iid"""
    def walk(node, i):
        if i >= len(chain_paths):
            return [node]
        out = []
        for k in v.tree.get_children(node):
            try:
                vals = v.tree.item(k, "values")
            except tk.TclError:
                continue
            if not vals or vals[1] != "dir":
                continue
            if os.path.normcase(os.path.normpath(vals[0])) == os.path.normcase(
                    os.path.normpath(chain_paths[i])):
                out.extend(walk(k, i + 1))
            else:
                out.extend(walk(k, i))
        return out
    return walk(root_iid, 0)


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILURES.append(name)


def make_tree(base, files, dirs):
    for d in dirs:
        os.makedirs(os.path.join(base, d), exist_ok=True)
    for f in files:
        open(os.path.join(base, f), "w", encoding="utf-8").close()


try:
    # ============ S1: 预热占路时点开目录（本次报告的 bug） ============
    P1 = mk(tempfile.mkdtemp(prefix="vb_s1_"))
    make_tree(P1, ["a.txt", "b.txt"], ["sub1", "sub2"])
    gate = threading.Event()
    org_scan = mod.scan_dir
    mod.scan_dir = lambda p: (gate.wait(8.0), org_scan(p))[1]   # 卡住让撞车必现

    iid1 = make_dir_node(P1)
    v._submit_scan(P1, None, True)          # 预热先占住 P1
    assert P1 in v._inflight
    ok = v._ensure_loaded(iid1)             # 用户此刻点开 → 修复后挂单；旧码直接丢
    check("S1a 点开时撞上预热，节点进入等待", ok is False and iid1 in v.tree.get_children(""))
    check("S1b 请求被挂单而不是丢弃", len(v._scan_waiters.get(P1, ())) == 1)

    gate.set()                                # 放行，连接下来
    mod.scan_dir = org_scan                   # 之后的扫描不再阻塞
    rend = pump(6.0, until=lambda: len(live(iid1)) >= 4)
    names1 = {os.path.basename(v.tree.item(k, "values")[0]) for k in live(iid1)}
    check("S1c 扫描完成即渲染，无需收起再展开", rend and {"sub1", "sub2", "a.txt", "b.txt"} <= names1)
    check("S1d 预热缓存也正常落账", P1 in v._preheat_cache)
    check("S1e 挂单清空、不在途中", P1 not in v._scan_waiters and P1 not in v._inflight)

    # ============ S2: 缓存命中立即渲染 ============
    P2 = mk(tempfile.mkdtemp(prefix="vb_s2_"))
    make_tree(P2, ["x.txt"], ["sub_a"])
    v._submit_scan(P2, None, True)
    pump(5.0, until=lambda: P2 in v._preheat_cache)
    iid2 = make_dir_node(P2)
    ok2 = v._ensure_loaded(iid2)
    names2 = {os.path.basename(v.tree.item(k, "values")[0]) for k in live(iid2)}
    check("S2 预热完的目录点开立即渲染", ok2 is True and names2 == {"x.txt", "sub_a"})
    check("S2b 缓存命中后路径入站即清", P2 not in v._scan_waiters and P2 not in v._inflight)

    # ============ S3: _children_by_tag 真的按标签过滤 ============
    rows3 = org_scan(P2)
    parent3 = v.tree.insert("", "end", text="P", values=(P2, "dir", ""))
    v._insert_rows(parent3, rows3)
    d3 = v._children_by_tag(parent3, "dir")
    f3 = v._children_by_tag(parent3, "file")
    kinds3a = [v.tree.item(k, "values")[1] for k in d3]
    kinds3b = [v.tree.item(k, "values")[1] for k in f3]
    check("S3 目录/文件按标签分流",
          kinds3a == ["dir"] and kinds3b == ["file"] and
          len(v.tree.get_children(parent3)) == 2)

    # ============ S4: 下钻守卫（有文件的层不下钻） ============
    mod.scan_dir = org_scan                   # 恢复正常扫描
    mixed = mk(tempfile.mkdtemp(prefix="vb_s4_"))
    make_tree(mixed, ["z.txt"], ["child"])
    mixed_iid = make_dir_node(mixed)
    v._submit_scan(mixed, lambda r, ok_, i_=mixed_iid: v._on_loaded(
        i_, r, v._tree_gen), False)
    pump(5.0, until=lambda: bool(live(mixed_iid)))
    child_iid = [k for k in live(mixed_iid)
                 if v.tree.item(k, "values")[1] == "dir"][0]
    v._root_depth = mixed.rstrip("\\/").count("\\")
    v._budget = 600
    v._drill(mixed_iid, focus=False)
    check("S4a 这一层有文件 → 不自动下钻",
          not v.tree.item(child_iid, "open"))

    pure = mk(tempfile.mkdtemp(prefix="vb_s4b_"))
    chain = [os.path.join(pure, s) for s in ("l1", "l1\\l2", "l1\\l2\\l3")]
    for c in chain:
        os.makedirs(c, exist_ok=True)
    open(os.path.join(chain[2], "deep.txt"), "w", encoding="utf-8").close()
    pure_iid = make_dir_node(pure)
    v._budget = 600
    v._drill(pure_iid, focus=False)
    pump(6.0, until=lambda: all(v.tree.item(i, "open") for i in _find_chain(pure_iid, chain)))
    check("S4b 纯文件夹层顺着链自动展开",
          all(v.tree.item(i, "open") for i in _find_chain(pure_iid, chain)))

    # ============ S5: 全部展开的接力（跨后台读取续链） ============
    # fresh 目录 + drill 关闭 + 逐层都要后台读 → 全部展开必须自己走到 l3
    fz = mk(tempfile.mkdtemp(prefix="vb_s5_"))
    chain2 = [os.path.join(fz, s) for s in ("m1", "m1\\m2", "m1\\m2\\m3")]
    for c in chain2:
        os.makedirs(c, exist_ok=True)
    open(os.path.join(chain2[2], "leaf.txt"), "w", encoding="utf-8").close()
    fz_iid = make_dir_node(fz)
    v.set["drill"] = False                    # 关掉下钻，只看全部展开本身
    v._budget = 600
    v._expand_all()
    got = pump(12.0, until=lambda: all(v.tree.item(i, "open") for i in _find_chain(fz_iid, chain2))
               and not v._auto_q and not v._auto_expand)
    check("S5 全部展开贯穿未读好的深层",
          got and set(v._auto_expand) == set() and not v._auto_q)
    v.set["drill"] = True

    # ============ S6: 缓存复用 + 换树后代数守卫 ============
    iid6 = make_dir_node(P1)                     # 旧目录，预热缓存还在
    okc = v._ensure_loaded(iid6)
    check("S6c 同目录再点开走缓存秒出", okc is True and len(live(iid6)) >= 4)

    P6 = mk(tempfile.mkdtemp(prefix="vb_s6_"))
    make_tree(P6, ["p.txt"], ["psub"])
    gate2 = threading.Event()
    mod.scan_dir = lambda p: (gate2.wait(8.0), org_scan(p))[1]
    iid6b = make_dir_node(P6)
    v._ensure_loaded(iid6b)                     # 真读盘，回调按当前代挂上
    v._tree_gen += 1                            # 模拟 _load 重建树
    gate2.set()
    mod.scan_dir = org_scan
    pump(4.0)
    check("S6a 旧代回调不动新树",
          not live(iid6b) and len(v.tree.get_children(iid6b)) == 1)
    v._on_loaded(iid6b, org_scan(P6), v._tree_gen)
    pump(2.0)
    check("S6b 新代回调正常渲染", bool(live(iid6b)))

    # ============ S7: 真实点击路径（_tree_click 整链） ============
    # 历史教训：identify 两参签名抛 TypeError + region=="expander" 永远不成立
    # （箭头其实是树列里的 Treeitem.indicator 元素），箭头点击从未调度过
    # _user_expand → 表现就是「点开永远空、右键全部展开倒正常」。
    cr = tk.Tk()
    cr.wm_geometry("+3000+3000")          # 屏外即不可见；若 withdraw，则树不布局，bbox 全 0
    ct = ttk.Treeview(cr, show="tree", columns=("note",))
    ct.column("#0", width=220)
    ct.column("note", width=26, stretch=False)
    ct.pack(fill="both", expand=True)
    sid = ct.insert("", "end", text="somedir")
    ct.insert(sid, "end", text="placeholder")
    ct.item(sid, open=True)
    cr.update()

    exps2, notes2 = [], []

    class _ClickShim(object):            # 只带上 _tree_click 需要的最小属性
        pass

    sh = _ClickShim()
    sh.root = cr
    sh.tree = ct
    sh._click_row = None
    sh._user_expand = lambda iid: exps2.append(iid)
    sh._open_note = lambda iid: notes2.append(iid)

    def _row_bbox(t_, id_):
        b = t_.bbox(id_)
        if isinstance(b, str):
            b = b.split()
        try:
            t = tuple(int(x) for x in b)
        except Exception:
            return (0, 0, 0, 0)
        return t if len(t) == 4 else (0, 0, 0, 0)

    class _Ev(object):
        pass

    box = _row_bbox(ct, sid)
    row_y = box[1] + max(1, box[3] // 2)
    arrow_x = None                        # 箭头 = Treeitem.indicator 元素
    for dx in range(0, 24):
        if "indicator" in (ct.identify_element(box[0] + dx, row_y) or ""):
            arrow_x = box[0] + dx
            break
    check("S7a 箭头元素可被命中", arrow_x is not None)
    if arrow_x is not None:
        ev = _Ev()
        ev.x, ev.y = arrow_x, row_y
        mod.VBHelper._tree_click(sh, ev)
        cr.update()
        pump(0.5)
        check("S7b 点箭头 → 调度 _user_expand", sid in exps2)
    nbefore = len(exps2)
    ev = _Ev()
    ev.x, ev.y = box[0] + 60, row_y       # 正文文字区
    mod.VBHelper._tree_click(sh, ev)
    cr.update()
    check("S7c 点正文不误触展开", len(exps2) == nbefore)
    note_x = None                         # 注释列 = columns=("note",) 的 #1
    w = ct.winfo_width()
    for x in range(w - 2, max(0, w - 70), -1):
        if ct.identify_region(x, row_y) == "cell" and ct.identify_column(x) == "#1":
            note_x = x
            break
    check("S7d 注释列可被命中", note_x is not None)
    if note_x is not None:
        ev = _Ev()
        ev.x, ev.y = note_x, row_y
        mod.VBHelper._tree_click(sh, ev)
        cr.update()
        pump(0.5)
        check("S7e 点注释列 → 调度 _open_note", sid in notes2)
    cr.destroy()

except Exception:
    traceback.print_exc()
    FAILURES.append("S-ex 异常中断")
finally:
    v.root.destroy()
    for d in _tempdirs:
        shutil.rmtree(d, ignore_errors=True)

print("=" * 46)
if FAILURES:
    print("FAILED:", ", ".join(FAILURES))
    sys.exit(1)
print("ALL OK")
