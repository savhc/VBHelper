# -*- coding: utf-8 -*-
"""包版（vbhelper/）目录树展开链端到端回归探针 —— 用户报的「点开空目录、
没有『正在读取…』、反复展开也没反应」。

复现链条：scan_dir 曾把 OSError 吞成 []，工人标 ok=True，空结果被写入
_preheat_cache 并记录 mtime；此后每次展开都命中缓存"秒出空目录"，连重试
提示都不会出现。修复后：失败必须以 ok=False 上报，占位行给出重试提示，
缓存不落毒，下次展开真正重扫。

S1  全新目录树加载 + 用户展开子目录 → 内容渲染
S2  读失败（None 语义）→ 占位显示重试提示、不落缓存；恢复后重扫成功
S3  真·空目录 → 干净渲染成空（也必须秒开），不许弹重试提示
S4  同一路径挂单等待：多个回调全部收到通知
S5  磁盘变更侦测：mtime 变化触发重扫，新文件出现
S6  渲染后收起再展开（缓存命中）→ 内容原样秒出
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

import vbhelper.app as app
import vbhelper.scan as scan_mod

FAILURES = []
_tempdirs = []

# ---- 让 __init__ 和面板都不产生可见窗口、不写用户真实配置 ----
app.VBHelper._build_ball = lambda self: None
app.VBHelper._place_ball = lambda self: None
app.VBHelper._hover_poll = lambda self: None
app.load_cfg = lambda: {}                # 干净配置（root_expand 默认开、drill 默认关）
for _m in ("core", "tree", "floating", "meta"):
    mod = __import__("vbhelper." + _m, fromlist=[_m])
    if hasattr(mod, "save_cfg"):
        mod.save_cfg = lambda cfg: None   # 别碰用户 APPDATA

v = app.VBHelper()
v._set_ball_text = lambda name: None
v._ensure_panel()                        # 真面板（withdrawn，不可见）


def mk(p):
    _tempdirs.append(p)
    return p


def pump(seconds=8.0, until=None):
    """真实主循环：update() 驱动 after 队列（含 _pump_results 自续）"""
    end = time.time() + seconds
    while time.time() < end:
        v.root.update()
        if until and until():
            return True
        time.sleep(0.01)
    return bool(until and until())


def live(iid):
    """节点下非占位的真实子项"""
    out = []
    for k in v.tree.get_children(iid):
        try:
            tags = v.tree.item(k, "tags")
            if "loading" not in tags:
                out.append(k)
        except tk.TclError:
            pass
    return tuple(out)


def state(iid):
    return v._dir_state.get(iid)


def dump(label, iid):
    kids = []
    for k in v.tree.get_children(iid):
        try:
            kids.append((v.tree.item(k, "text"), state(k)))
        except tk.TclError:
            kids.append(("<gone>", "?"))
    print("  [%s] iid=%s state=%s kids=%s" % (label, iid, state(iid), kids))


def make_tree(base, files, dirs):
    for d in dirs:
        os.makedirs(os.path.join(base, d), exist_ok=True)
    for f in files:
        open(os.path.join(base, f), "w", encoding="utf-8").close()


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILURES.append(name)


def user_click_expander(iid):
    """模拟用户点展开箭头：Tk 先 toggle open，_tree_click 再调度 _user_expand"""
    try:
        was_open = v.tree.item(iid, "open")
        v.tree.item(iid, open=not was_open)
    except tk.TclError:
        return
    v._user_expand(iid)


def find_dir_iid(parent, full_path):
    for k in v.tree.get_children(parent):
        try:
            vals = v._vals(k)
        except tk.TclError:
            continue
        if vals and vals[1] == "dir" and vals[0] == full_path:
            return k
    return None


try:
    # ================= S1 全新加载 + 用户展开 =================
    P = mk(tempfile.mkdtemp(prefix="vbpkg_"))
    sub1 = os.path.join(P, "sub1")
    make_tree(P, ["a.txt", "b.txt"], ["sub1", "sub2"])
    open(os.path.join(sub1, "deep.txt"), "w", encoding="utf-8").close()
    open(os.path.join(P, "sub2", "inside2.txt"), "w", encoding="utf-8").close()

    v._load(P)
    root_iid = v.tree.get_children("")[0]
    ok = pump(6.0, until=lambda: len(live(root_iid)) >= 3)
    names = {os.path.basename(v._vals(k)[0]) for k in live(root_iid)}
    check("S1a 根目录自适应渲染（root_expand）", ok and {"sub1", "sub2", "a.txt", "b.txt"} <= names)

    sub_iid = find_dir_iid(root_iid, sub1)
    user_click_expander(sub_iid)             # 用户第一次点开：模拟点击展开箭头
    ok = pump(6.0, until=lambda: bool(live(sub_iid)))
    check("S1b 点开 sub1 渲染出 deep.txt",
          ok and {os.path.basename(v._vals(k)[0]) for k in live(sub_iid)} == {"deep.txt"}
          and state(sub_iid) == "loaded")

    # ================= S2 读失败 → 重试链 =================
    sub2 = os.path.join(P, "sub2")
    sub2_iid = find_dir_iid(root_iid, sub2)
    real_scan = scan_mod.scan_dir
    scan_mod.scan_dir = lambda p: None        # 模拟「这次没读成」（原实现这里是 []+ok=True）
    user_click_expander(sub2_iid)
    pump(3.0, until=lambda: False)
    ph = [v.tree.item(k, "text") for k in v.tree.get_children(sub2_iid)]
    check("S2a 失败给出重试占位、不落缓存",
          state(sub2_iid) == "idle" and sub2 not in v._preheat_cache
          and any("重试" in t for t in ph))

    scan_mod.scan_dir = real_scan             # 恢复：用户收起再展开就该读出来
    v.tree.item(sub2_iid, open=False)         # 收起
    user_click_expander(sub2_iid)             # 点箭头再展开 → Tk toggle 成 open 后进加载分支
    ok = pump(6.0, until=lambda: state(sub2_iid) == "loaded")
    names2 = {os.path.basename(v._vals(k)[0]) for k in live(sub2_iid)}
    check("S2b 重试后真正读出内容", ok and names2 == {"inside2.txt"} and state(sub2_iid) == "loaded")

    # ================= S3 真·空目录 =================
    empty = os.path.join(P, "empty_dir")
    os.makedirs(empty, exist_ok=True)
    v._load(P)                                # 重挂让它进根列表
    root2_iid = v.tree.get_children("")[0]
    pump(6.0, until=lambda: len(live(root2_iid)) >= 4)
    empty_iid = find_dir_iid(root2_iid, empty)
    user_click_expander(empty_iid)
    pump(3.0, until=lambda: state(empty_iid) == "loaded")
    check("S3 空目录干净渲染（无占位行、无重试提示）",
          state(empty_iid) == "loaded" and not v.tree.get_children(empty_iid))

    # ================= S4 同路径挂单：多个等待者一起收 =================
    got = []
    gate = threading.Event()
    scan_mod.scan_dir = lambda p: (gate.wait(6.0), real_scan(p))[1]
    v._submit_scan(sub1, lambda r, ok_: got.append(("w1", r)), priority=0)
    v._submit_scan(sub1, lambda r, ok_: got.append(("w2", r)), priority=1)
    # 第三个等待者：此时路径已在途中，必须挂单而不是丢弃
    okw = v._submit_scan(sub1, lambda r, ok_: got.append(("w3", r)), priority=1)
    check("S4a 撞车请求被挂单", okw is True and sub1 in v._inflight)
    gate.set()
    scan_mod.scan_dir = real_scan
    pump(4.0, until=lambda: len(got) >= 3)
    tags_set = {w for w, _ in got}
    check("S4b 三个等待者全部收到结果", tags_set == {"w1", "w2", "w3"})
    check("S4c 结果一致且正常", all(r is not None for _, r in got)
          and sub1 not in v._inflight)

    # ================= S5 磁盘变更侦测 =================
    m_iid = find_dir_iid(v.tree.get_children("")[0], sub1)
    user_click_expander(m_iid)                # 先展开到 loaded
    ok = pump(6.0, until=lambda: len(live(m_iid)) >= 1)
    check("S5a sub1 装载到 loaded", ok and state(m_iid) == "loaded")
    open(os.path.join(sub1, "brand_new.txt"), "w", encoding="utf-8").close()
    v._dir_mtime[sub1] = time.time() - 3600   # 记账造假：让侦测认为磁盘变过
    v._watch_changes()                        # 直接跑一轮抽查
    ok = pump(6.0, until=lambda: any(
        os.path.basename((v._vals(k))[0] or "") == "brand_new.txt" for k in live(m_iid)))
    check("S5b 变更侦测触发重扫，新文件出现", ok)

    # ================= S6 收起再展开，缓存秒出 =================
    v.tree.item(m_iid, open=False)            # 用户收起（Tk 已 toggle 成 False）
    v._user_expand(m_iid)                     # 收起分支：不发起扫描、撤销展开意图
    pump(1.0)
    check("S6a 收起分支不发起扫描", sub1 not in v._inflight)
    # 再展开（再点一次箭头：Tk toggle 成 True 后进加载分支，缓存命中应秒出）
    v.tree.item(m_iid, open=True)
    v._user_expand(m_iid)
    ok = pump(6.0, until=lambda: v.tree.item(m_iid, "open") and len(live(m_iid)) >= 2)
    names6 = {os.path.basename(v._vals(k)[0]) for k in live(m_iid)}
    check("S6b 再展开秒出全部内容", ok and {"deep.txt", "brand_new.txt"} <= names6)
    dump("final", m_iid)

    # ================= S7 真实点击路径（_tree_click 整链） =================
    # 历史教训：identify 双参会 TypeError，直接杀死 _tree_click，箭头点击永远不会
    # 调度 _user_expand（表现就是「点开永远空、右键全部展开倒正常」）。这里把面板
    # 挪到屏幕外拿到真实几何，用合成 Event 走完好几遍真实处理函数。
    from tkinter import Event

    def row_box(iid):
        b = v.tree.bbox(iid)
        if isinstance(b, str):
            b = b.split()
        try:
            t = tuple(int(x) for x in b)
        except Exception:
            return (0, 0, 0, 0)
        return t if len(t) == 4 else (0, 0, 0, 0)

    clicks = []
    real_expand = v._user_expand

    def recording_expand(iid):
        clicks.append(iid)
        real_expand(iid)

    v._user_expand = recording_expand
    try:
        v.panel.deiconify()
        v.panel.attributes("-topmost", False)
        v.panel.wm_geometry("+3000+3000")     # 不可见位置（屏外），但布局真实存在
        v.root.update()
        box = row_box(m_iid)                  # (x, y, w, h)；未滚进视野会是 (0,0,0,0)
        tries = 0
        while box[-1] == 0:
            tries += 1
            if tries > 40:
                print("  [S7] bbox 一直为 0，iid=%s mapped=%s geom=%s"
                      % (m_iid, v.panel.winfo_ismapped(), v.panel.winfo_geometry()))
                break
            v.tree.see(m_iid)
            v.root.update()
            box = row_box(m_iid)
        exp_x = None
        row_y = box[1] + max(1, box[3] // 2)
        for dx in range(0, 24):                     # 箭头是 Treeitem.indicator 元素
            if "indicator" in (v.tree.identify_element(box[0] + dx, row_y) or ""):
                exp_x = box[0] + dx
                break
        check("S7a 展开箭头可被命中", exp_x is not None)
        if exp_x is not None:
            ev = Event()
            ev.x, ev.y = exp_x, row_y
            v.tree.item(m_iid, open=False)    # 先收起，点击应重新展开
            v._tree_click(ev)                 # 真实处理函数：不允许再抛任何异常
            v.root.update()                   # 处理 after(1, _user_expand)
            pump(4.0, until=lambda: bool(live(m_iid)))
            check("S7b 点箭头 → 走 _user_expand → 内容渲染",
                  m_iid in clicks and bool(live(m_iid)))
            v.tree.item(m_iid, open=False)
            v.root.update()
        # ---- 注释列 i 图标：columns=("note",) 的 #1----
        note_hits = []
        real_note = v._open_note
        v._open_note = lambda iid: note_hits.append(iid)
        try:
            w = v.tree.winfo_width()
            note_x = None
            for x in range(w - 2, max(0, w - 70), -1):
                if (v.tree.identify_region(x, row_y) == "cell"
                        and v.tree.identify_column(x) == "#1"):
                    note_x = x
                    break
            check("S7c 注释列可被命中", note_x is not None)
            if note_x is not None:
                ev2 = Event()
                ev2.x, ev2.y = note_x, row_y
                v._tree_click(ev2)
                pump(0.5)                     # after(1) 需要跑事件泵才会到期
                check("S7d 点注释列 → 调度 _open_note", m_iid in note_hits)
        finally:
            v._open_note = real_note
    finally:
        v._user_expand = real_expand
        v.panel.withdraw()

except Exception:
    traceback.print_exc()
    FAILURES.append("X 异常中断")
finally:
    try:
        v.root.protocol("WM_DELETE_WINDOW", lambda: None)
        v.root.destroy()
    except Exception:
        pass
    for d in _tempdirs:
        shutil.rmtree(d, ignore_errors=True)

print("=" * 46)
if FAILURES:
    print("FAILED:", ", ".join(FAILURES))
    sys.exit(1)
print("ALL OK")
