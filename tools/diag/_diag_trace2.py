# -*- coding: utf-8 -*-
"""只读诊断（第二版）：跳过源自身的版本检查竞态，直接打 musicUrl 看处理链。

读 music_sources.json、起临时宿主、发一次真实请求，全程不写配置、不碰 app 代码。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from vbhelper import music, musicapi, musicnet

HOST = os.path.join(HERE, "_diag", "host_diag.js")


def main():
    store = json.load(open(os.path.join(os.environ["APPDATA"], "VBHelper",
                                        "music_sources.json"), encoding="utf-8"))
    item = next(i for i in store["items"] if i["id"] == store["active"])
    script = musicapi._unpack(item["script"])
    node = musicapi.find_node()
    proc = subprocess.Popen([node, HOST], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1, creationflags=0x08000000)
    box = queue.Queue()
    threading.Thread(target=lambda: [box.put(l) for l in proc.stdout], daemon=True).start()

    def send(obj):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def drain(seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                line = box.get(timeout=0.2)
            except queue.Empty:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                print("  [raw] " + line[:300])
                continue
            t = msg.get("t")
            if t == "diag":
                print("  [%s] %s" % (msg.get("tag"),
                                     json.dumps(msg.get("info"), ensure_ascii=False)[:1100]))
            elif t == "inited":
                print("  [inited] %s" % json.dumps(msg.get("sources"), ensure_ascii=False))
            elif t == "res":
                print("  [res] ok=%s error=%r data=%s" % (
                    msg.get("ok"), msg.get("error"),
                    json.dumps(msg.get("data"), ensure_ascii=False)[:400]))
            else:
                print("  [%s] %s" % (t, json.dumps(msg, ensure_ascii=False)[:400]))

    send({"t": "load", "script": script, "name": item.get("name") or ""})
    drain(3.0)

    row = musicnet.search("kw", "晴天", 1)["list"][0]
    track = music.make_online(row)
    track["kind"] = "online"
    info = {"type": "128k", "musicInfo": music.api_music_info(track)}
    print("\n== 直接发 musicUrl（不等 inited）==")
    print("   musicInfo = %s" % json.dumps(info["musicInfo"], ensure_ascii=False)[:400])
    send({"t": "req", "id": "r1", "action": "musicUrl", "source": "kw", "info": info})
    drain(40.0)
    proc.kill()


if __name__ == "__main__":
    main()
