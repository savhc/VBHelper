# -*- coding: utf-8 -*-
"""只读诊断：用带日志的临时宿主跑一遍用户导入的音源，定位 musicUrl 失败原因。

不改 app 代码、不改配置；只读 music_sources.json，跑完杀掉 node。
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from vbhelper import music, musicapi, musicnet

HOST = os.path.join(HERE, "_diag", "host_diag.js")


def main():
    store_path = os.path.join(os.environ["APPDATA"], "VBHelper", "music_sources.json")
    store = json.load(open(store_path, encoding="utf-8"))
    item = next(i for i in store["items"] if i["id"] == store["active"])
    script = musicapi._unpack(item["script"])
    print("音源：%s  脚本 %d 字" % (item.get("name"), len(script)))

    node = musicapi.find_node()
    proc = subprocess.Popen([node, HOST], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1,
                            creationflags=0x08000000)
    events = []

    def send(obj):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            line = proc.stdout.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                print("  [raw] " + line[:300])
                continue
            events.append(msg)
            show(msg)

    def show(msg):
        t = msg.get("t")
        if t == "diag":
            print("  [%s] %s" % (msg.get("tag"), json.dumps(msg.get("info"),
                                                           ensure_ascii=False)[:900]))
        elif t == "inited":
            print("  [inited] %s" % json.dumps(msg.get("sources"), ensure_ascii=False))
        elif t == "res":
            print("  [res] ok=%s error=%r" % (msg.get("ok"), msg.get("error")))
            print("        data=%s" % json.dumps(msg.get("data"), ensure_ascii=False)[:400])
        else:
            print("  [%s] %s" % (t, json.dumps(msg, ensure_ascii=False)[:400]))

    send({"t": "load", "script": script, "name": item.get("name") or ""})
    pump(20)
    # 这个源的初始化本身带竞态（两条 npm 版本请求谁先回谁生效），失败就重来
    for attempt in range(1, 5):
        if any(e.get("t") == "inited" for e in events):
            break
        print("-- 第 %d 次重试初始化" % attempt)
        events.clear()
        send({"t": "load", "script": script, "name": item.get("name") or ""})
        pump(15)
    if not any(e.get("t") == "inited" for e in events):
        print("!! 4 次都没完成 inited，诊断到此为止")
        proc.kill()
        return

    row = musicnet.search("kw", "晴天", 1)["list"][0]
    track = music.make_online(row)
    track["kind"] = "online"
    info = {"type": "128k", "musicInfo": music.api_music_info(track)}
    print("\n发出 musicUrl 请求，musicInfo = %s" % json.dumps(info["musicInfo"],
                                                             ensure_ascii=False)[:400])
    send({"t": "req", "id": "r1", "action": "musicUrl", "source": "kw", "info": info})
    pump(45)
    proc.kill()


if __name__ == "__main__":
    main()
