# -*- coding: utf-8 -*-
"""诊断：加载两个真实音源各 2 次（复现 "already been declared"），并测 kw musicUrl。"""
import json
import os
import queue
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper import musicapi as M

KW_TRACK = {
    "name": "Hey, Somebody", "singer": "肖俊 (XIAOJUN)", "source": "kw",
    "songmid": "657209598", "interval": "03:01",
    "albumName": "Hey, Somebody - The 1st Mini Album", "img": "",
    "typeUrl": {}, "albumId": "104715874",
    "types": [{"type": "128k", "size": None}],
    "_types": {"128k": {"size": None}},
}


def drain(q, rt, timeout, want=("ready", "error", "res", "alert")):
    """收集事件直到命中 want 之一或超时；把 log/error 打出来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            e, p = q.get(timeout=0.2)
        except queue.Empty:
            rt.poll(now=time.time())
            continue
        if e == "log":
            print("      [log] %s" % p.get("text", "")[:200])
            continue
        if e in want:
            return e, p
        if e == "error":
            print("      [error] %s" % p.get("message", ""))
            return e, p
    return None, None


def main():
    node = M.find_node()
    print("node =", node)
    if not node:
        print("没找到 node，退出")
        return 1
    d = json.load(open(os.path.join(
        os.environ["APPDATA"], "VBHelper", "music_sources.json"), encoding="utf-8"))
    items = d.get("items", [])
    print("音源数 =", len(items))
    fail = 0
    for it in items:
        name = it.get("name")
        print("\n" + "=" * 66)
        print("音源: %s  v%s" % (name, it.get("version")))
        q = queue.Queue()
        rt = M.LxRuntime(q)
        rt._node = node
        # 第 1 次加载
        ok = rt.load(it)
        print("  load#1 提交:", ok)
        e, p = drain(q, rt, 30)
        if e == "ready":
            kw = (p.get("sources") or {}).get("kw")
            print("  load#1 ready，kw 能力:", kw)
        else:
            print("  load#1 结果:", e, (p or {}).get("message"))
            fail += 1
            rt.stop()
            continue
        # 第 2 次加载（复现点：同一 context 二次声明）
        ok = rt.load(it)
        print("  load#2 提交:", ok)
        e, p = drain(q, rt, 30)
        if e == "ready":
            kw = (p.get("sources") or {}).get("kw")
            print("  load#2 ready，kw 能力:", kw)
        else:
            print("  load#2 结果:", e, (p or {}).get("message"))
            fail += 1
            rt.stop()
            continue
        # musicUrl 实测
        if not rt.supports("kw", "musicUrl", "128k"):
            print("  !! 音源不支持 kw/musicUrl/128k，跳过")
        else:
            key = rt.request("musicUrl", "kw",
                             {"type": "128k", "musicInfo": KW_TRACK})
            print("  musicUrl 请求 key:", key)
            e, p = drain(q, rt, 60, want=("res", "error"))
            if e == "res":
                url = (((p or {}).get("data") or {}).get("data") or {}).get("url")
                if url:
                    print("  musicUrl -> %s" % url[:180])
                else:
                    print("  musicUrl 失败: %s" % (p or {}).get("error"))
            else:
                print("  musicUrl 无回执:", e, p)
        rt.stop()
    print("\n" + "=" * 66)
    print("失败音源数:", fail)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
