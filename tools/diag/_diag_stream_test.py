# -*- coding: utf-8 -*-
"""实验：MCI 能否流式播放 http 源（本地 server 限速喂 mp3）。

可行 → 直链经本地流代理边下边播；不可行 → 准流式走"增长文件"或放弃换引擎。
"""
import http.server
import socketserver
import subprocess
import sys
import threading
import time
import ctypes
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = 18765
MP3 = os.path.join(os.environ["TEMP"], "stream_test_src.mp3")
FF = None
for cand in ("ffmpeg", r"D:\ffmpeg\bin\ffmpeg.exe"):
    try:
        subprocess.run([cand, "-version"], capture_output=True, check=True)
        FF = cand
        break
    except Exception:
        continue
if not FF:
    print("NO_FFMPEG")
    sys.exit(1)
# 生成 60 秒有声音的 mp3（正弦波，比静音更接近真实码率行为）
subprocess.run([FF, "-y", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=60", "-b:a", "128k", MP3],
               capture_output=True, check=True)
size = os.path.getsize(MP3)
print("src mp3:", size, "bytes")

sent = threading.Event()
rate_lock = threading.Lock()


class SlowHandler(http.server.BaseHTTPRequestHandler):
    """限速发送，模拟慢网整轨缓冲：~400KB/s。"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        sent_total = 0
        with open(MP3, "rb") as f:
            while True:
                chunk = f.read(32 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    break
                sent_total += len(chunk)
                if sent_total > size * 0.05:
                    sent.set()          # 已送出 >5%，模拟"可以开播了"
                time.sleep(0.06)        # ~500KB/s
        print("server: done, sent", sent_total)

    def log_message(self, *a):
        pass


srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), SlowHandler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()

u32 = ctypes.windll.winmm
u32.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                               ctypes.c_uint, ctypes.c_void_p]
u32.mciGetErrorStringW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]


def mci(cmd):
    buf = ctypes.create_unicode_buffer(512)
    rc = u32.mciSendStringW(cmd, buf, 512, None)
    return rc, (buf.value or "").strip()


url = "http://127.0.0.1:%d/a.mp3" % PORT
rc, out = mci('open "%s" type mpegvideo alias streamtest' % url)
print("open rc =", rc, out)
if rc:
    # 不带 type 让系统自动挑 source filter
    rc, out = mci('open "%s" alias streamtest' % url)
    print("open(no type) rc =", rc, out)
if rc:
    print("VERDICT: FAIL (MCI 无法打开 http 流)")
    sys.exit(1)

time.sleep(0.3)
rc, _ = mci("play streamtest")
print("play rc =", rc)
time.sleep(2.0)
rc, mode = mci("status streamtest mode")
rc2, pos = mci("status streamtest position")
print("after 2s: mode =", mode, " position =", pos)
ok = (mode == "playing") and int(pos or 0) > 0
rc, _len = mci("status streamtest length")
print("length =", _len)
mci("close streamtest")
srv.shutdown()
print("VERDICT:", "PASS — MCI 支持流式播 http 源" if ok else "FAIL — 打开了但不前进")
