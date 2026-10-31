# -*- coding: utf-8 -*-
"""UI 运行时层质检：python _ui_runtime_check.py

覆盖：
  TaskRunner：正常任务 / 异常进 on_error / cancel_group 生效 / shutdown 干净；
  store.cache：幂等建表、读写、TTL 过期判定；
  HttpClient：本地 http.server 靶子测 GET/POST/缓存(ETag 304)/重定向/
              响应体超限/SSE/下载/SSRF 拦截(127.0.0.1 默认拒绝、allow_private 放行)。
"""
import os
import shutil
import sys
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from vbhelper.ui.async_runner import TaskRunner
from vbhelper.ui.http import HttpClient, HttpError
from vbhelper.store.cache import HttpCache

TMP = os.path.join(HERE, "_ui_runtime_tmp")
DB = os.path.join(TMP, "vbhelper.db")
fails, oks = [], []


def check(name, cond, detail=""):
    (oks if cond else fails).append(name)
    print("%s%-40s %s" % ("  ok  " if cond else "  FAIL ", name, detail))
    return cond


def pump(root, n=6, ms=0.02):
    """手动推进 Tk 主循环（不开 mainloop）。"""
    for _ in range(n):
        try:
            root.update()
        except tk.TclError:
            return
        time.sleep(ms)


def wait_for(root, cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pump(root)
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------------- 本地 HTTP 靶子 ----------------

class _Target(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):      # 静音
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8",
              extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path
        if p.startswith("/text"):
            self._send(200, "你好，VBHelper")
        elif p.startswith("/etag"):
            etag = '"v1"'
            if self.headers.get("If-None-Match") == etag:
                self._send(304, b"", extra={"ETag": etag})
            else:
                self._send(200, "etag-body", extra={"ETag": etag})
        elif p.startswith("/redirect"):
            if p.startswith("/redirect2"):
                self._send(302, b"", extra={"Location": "/text"})
            else:
                self._send(302, b"", extra={"Location": "/redirect2"})
        elif p.startswith("/big"):
            self._send(200, b"x" * (2 * 1024 * 1024))
        elif p.startswith("/sse"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                chunk = ("data: 行%d\n\n" % i).encode("utf-8")
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self._send(404, "not found")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(n)
        import json
        try:
            obj = json.loads(payload.decode("utf-8"))
        except Exception:
            self._send(400, "bad json")
            return
        self._send(200, json.dumps({"echo": obj, "ok": True}),
                   ctype="application/json")


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# ---------------- 1. store.cache ----------------

def test_cache():
    if os.path.exists(DB):
        os.remove(DB)
    os.makedirs(TMP, exist_ok=True)
    c1 = HttpCache(DB)
    c2 = HttpCache(DB)                      # 第二个实例：幂等建表
    c1.put("k1", etag='"e1"', last_modified="Mon", status=200,
           headers={"content-type": "text/html"}, body=b"<html>", ttl=60)
    row = c2.get("k1")
    check("cache/读写一致", row is not None and row["body"] == b"<html>"
          and row["etag"] == '"e1"' and row["headers"]["content-type"] == "text/html")
    check("cache/未过期", not c1.expired("k1"))
    # TTL 过期：把 fetched_at 拨回过去
    c1.put("k2", body=b"old", ttl=30)
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE http_cache SET fetched_at = ? WHERE key='k2'",
                 (time.time() - 60,))
    conn.commit(); conn.close()
    check("cache/TTL 过期判定", HttpCache(DB).expired("k2"))
    check("cache/无记录视为过期", HttpCache(DB).expired("nope"))
    check("cache/touch 刷新", HttpCache(DB).touch("k1", ttl=99)
          and not HttpCache(DB).expired("k1"))
    check("cache/touch 无行返回 False", not HttpCache(DB).touch("ghost"))


# ---------------- 2. TaskRunner ----------------

def test_taskrunner():
    root = tk.Tk()
    root.withdraw()
    r = TaskRunner(root)

    got = {}

    def on_done(res):
        got["res"] = res

    r.submit("ok", lambda: 40 + 2, group="g1", on_done=on_done, kind="net")
    check("runner/正常任务回调", wait_for(root, lambda: "res" in got) and got["res"] == 42)

    errs = {}

    def on_error(err):
        errs["e"] = err

    def boom():
        raise ValueError("任务内部炸了")

    r.submit("bad", boom, group="g1", on_error=on_error)
    check("runner/异常进 on_error",
          wait_for(root, lambda: "e" in errs) and isinstance(errs["e"], ValueError))

    # cancel_group：长任务 + 分队列，取消后不再回调
    fired = []

    def slow():
        time.sleep(1.5)
        return "slow"

    h1 = r.submit("s1", slow, group="winA", on_done=lambda v: fired.append(v),
                  kind="disk")
    h2 = r.submit("s2", slow, group="winA", on_done=lambda v: fired.append(v),
                  kind="proc")
    pump(root, 2)
    r.cancel_group("winA")
    pump(root, 40, 0.05)
    time.sleep(1.8)
    pump(root, 10)
    check("runner/cancel_group 标记 cancelled",
          h1.cancelled and h2.cancelled)
    check("runner/取消后不触发回调", not fired)
    check("runner/取消句柄有 error", h1.error is not None and h2.error is not None)

    # 单任务 cancel
    h3 = r.submit("s3", lambda: (time.sleep(1.0), "x")[1], group="winB",
                  kind="default")
    r.cancel("s3")
    pump(root, 4)
    check("runner/cancel(key)", h3.cancelled)

    # shutdown：轮询停、无线程残留
    r.shutdown(wait=True)
    check("runner/shutdown 后 alive 线程=0", r.alive_thread_count == 0,
          "alive=%d" % r.alive_thread_count)
    try:
        r.submit("after-shutdown", lambda: 1)
        check("runner/shutdown 后 submit 报错", False)
    except RuntimeError:
        check("runner/shutdown 后 submit 报错", True)
    root.destroy()


# ---------------- 3. HttpClient ----------------

def test_http():
    srv, base = start_server()
    client = HttpClient(cache=HttpCache(DB), max_bytes=1024 * 1024)

    # GET
    resp = client.get_text(base + "/text", allow_private=True)
    check("http/GET 文本", resp.status == 200 and resp.text() == "你好，VBHelper")

    # POST JSON
    data, resp = client.post_json(base + "/post", {"num": 7}, allow_private=True)
    check("http/POST JSON 回显", resp.status == 200 and data["ok"] is True
          and data["echo"]["num"] == 7)

    # 缓存 + ETag 304
    r1 = client.get_text(base + "/etag", cache_ttl=60, allow_private=True)
    check("http/首次拉取", r1.status == 200 and r1.text() == "etag-body"
          and not r1.from_cache)
    r2 = client.get_text(base + "/etag", cache_ttl=60, allow_private=True)
    check("http/TTL 内命中缓存", r2.from_cache and not r2.stale
          and r2.text() == "etag-body")
    # TTL 已过 → 条件请求 → 304 → 仍回缓存体
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE http_cache SET fetched_at = ? WHERE key LIKE '%/etag'",
                 (time.time() - 120,))
    conn.commit(); conn.close()
    r3 = client.get_text(base + "/etag", cache_ttl=60, allow_private=True)
    check("http/过期后条件请求 304 命中", r3.from_cache and not r3.stale
          and r3.text() == "etag-body")

    # 离线回退：请求一个没人监听的端口（模拟网络失败），
    # 本地有（已过期的）缓存时回退缓存并标记 stale=True
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    dead_port = srv2.server_address[1]
    srv2.server_close()                  # 立刻关掉：端口保证没人听
    dead_url = "http://127.0.0.1:%d/dead" % dead_port
    client._cache.put(dead_url, status=200, headers={}, body=b"cached-dead", ttl=60)
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE http_cache SET fetched_at = ? WHERE key = ?",
                 (time.time() - 3600, dead_url))
    conn.commit(); conn.close()
    r4 = client.get_text(dead_url, cache_ttl=60, allow_private=True)
    check("http/离线回退缓存 stale=True",
          r4.stale and r4.from_cache and r4.body == b"cached-dead",
          "stale=%s from_cache=%s" % (r4.stale, r4.from_cache))
    # 离线且无缓存：抛网络错误
    try:
        client.get_text("http://127.0.0.1:%d/nothing" % dead_port,
                        cache_ttl=60, allow_private=True)
        check("http/离线无缓存抛错", False)
    except HttpError as e:
        check("http/离线无缓存抛错", e.kind == "network", "kind=%s" % e.kind)

    # 重定向（两级）
    r5 = client.get_text(base + "/redirect", allow_private=True)
    check("http/重定向跟随", r5.status == 200 and r5.text() == "你好，VBHelper"
          and r5.url.endswith("/text"))

    # 响应体超限（2MB > 1MB 上限）
    try:
        client.get_text(base + "/big", allow_private=True)
        check("http/响应体超限报错", False)
    except HttpError as e:
        check("http/响应体超限报错", e.kind == "size", "kind=%s" % e.kind)

    # SSE
    events = list(client.stream_sse(base + "/sse", allow_private=True))
    check("http/SSE 逐事件", [e["data"] for e in events] == ["行0", "行1", "行2"],
          str(events))

    # 下载
    path = client.download(base + "/text", TMP, allow_private=True)
    with open(path, "rb") as f:
        check("http/download 写文件", f.read().decode("utf-8") == "你好，VBHelper")
    os.remove(path)

    # SSRF：默认拦 127.0.0.1
    try:
        client.get_text(base + "/text")
        check("http/SSRF 默认拦截 127.0.0.1", False)
    except HttpError as e:
        check("http/SSRF 默认拦截 127.0.0.1", e.kind == "ssrf", "kind=%s" % e.kind)
    # 放行
    r6 = client.get_text(base + "/text", allow_private=True)
    check("http/allow_private 放行", r6.status == 200)

    # HTTP 状态错误分类
    try:
        client.get_text(base + "/none", allow_private=True)
        check("http/404 分类", False)
    except HttpError as e:
        check("http/404 分类", e.kind == "http" and e.status == 404)

    # DNS 错误分类
    try:
        client.get_text("http://nonexistent.invalid/x")
        check("http/DNS 错误分类", False)
    except HttpError as e:
        check("http/DNS 错误分类", e.kind == "dns", "kind=%s" % e.kind)

    # JSON 解析失败分类
    try:
        client.get_json(base + "/text", allow_private=True)
        check("http/JSON 解析失败分类", False)
    except HttpError as e:
        check("http/JSON 解析失败分类", e.kind == "parse", "kind=%s" % e.kind)

    client.close()
    srv.shutdown()
    srv.server_close()


def main():
    os.makedirs(TMP, exist_ok=True)
    test_cache()
    test_taskrunner()
    test_http()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
    if fails:
        print("失败：", ", ".join(fails))
        sys.exit(1)


if __name__ == "__main__":
    main()
