# -*- coding: utf-8 -*-
"""MCP 端到端自检：python _mcp_check.py

不建 Tk、不发送真实鼠标键盘。覆盖：
- McpRuntime 启动/停止 + endpoint 生命周期
- 鉴权、initialize、tools/list、tools/call（截屏真实返回 PNG）
- 输入工具在关闭/开启/确认拒绝/审计下的行为（RecordingBackend）
- web.fetch 本地 HTTP、web.search 结果格式化
- stdio 桥子进程往返（--endpoint --no-autostart）
"""
import base64
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper.features.mcp import config as mcp_config
from vbhelper.features.mcp.runtime import McpRuntime
from vbhelper.services.win.input import InputBackend
from vbhelper.store.audit import AuditLog
from vbhelper.store.db import Database
from vbhelper.store.secrets import SecretStore
from vbhelper.ui.http import HttpClient

fails, oks = [], []


def check(name, cond, detail=""):
    (oks if cond else fails).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + ((" | " + str(detail)) if detail and not cond else ""))


# ---------------------------------------------------------------- fake ctx


class FakePaths:
    def __init__(self, root):
        self.local_dir = root
        self.mcp_dir = os.path.join(root, "mcp")
        self.captures_dir = os.path.join(root, "captures")
        self.config_file = os.path.join(root, "config.json")
        self.db_path = os.path.join(root, "db", "vbhelper.db")
        self.secrets_file = os.path.join(root, "secrets.json")
        for d in (self.mcp_dir, self.captures_dir, os.path.dirname(self.db_path)):
            os.makedirs(d, exist_ok=True)


class FakeCtx:
    def __init__(self, root):
        self.root = None
        self.cfg = {"features": {"mcp_bus": True}, "mcp": {}}
        self.paths = FakePaths(root)
        self.http = HttpClient()
        self.cache = None
        self.db = Database(self.paths.db_path)
        self.db.migrate()
        self.secrets = SecretStore(self.paths.secrets_file)
        self.audit = AuditLog(self.db)
        self.invoker = None
        self.saved = 0
        self.toasts = []

    def save_cfg(self):
        self.saved += 1

    def post(self, fn, timeout=30):
        return fn()

    def toast(self, text, duration=2500, **kwargs):
        self.toasts.append(str(text))
        return True


class RecordingBackend(InputBackend):
    def __init__(self):
        self.events = []

    def move(self, x, y):
        self.events.append(("move", int(x), int(y)))

    def click(self, button, count, interval):
        self.events.append(("click", button, int(count), float(interval)))

    def scroll(self, amount, horizontal):
        self.events.append(("scroll", int(amount), bool(horizontal)))

    def key(self, vk, down, extended=False):
        self.events.append(("key", int(vk), bool(down), bool(extended)))

    def unicode(self, text):
        self.events.append(("unicode", str(text)))

    def button(self, button, down):
        self.events.append(("button", str(button), bool(down)))


# ---------------------------------------------------------------- rpc


class Client:
    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.session = ""
        self._id = 0

    def raw(self, payload, *, auth=True):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = resp.read()
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session = sid
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def call(self, method, params=None, *, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            msg["id"] = self._id
        status, payload = self.raw(msg)
        return status, payload


def _content_text(result):
    for item in (result or {}).get("content") or []:
        if item.get("type") == "text":
            return str(item.get("text") or "")
    return ""


# ---------------------------------------------------------------- local web


class _PageHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/page"):
            body = """<!doctype html><html><head><meta charset="utf-8">
<title>测试文档</title></head><body>
<nav>导航垃圾</nav>
<article><h1>MCP 测试页面</h1>
<p>这是一个用于正文提取的<b>测试</b>页面，包含中文和 English 以及标点。</p>
<h2>代码</h2><pre><code>print("hello")</code></pre>
<ul><li>第一项</li><li>第二项</li></ul>
<p><a href="/next">下一节</a></p></article>
<footer>页脚垃圾</footer></body></html>"""
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


def start_local_web():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# ---------------------------------------------------------------- main


def main():
    tmp = tempfile.mkdtemp(prefix="vbmcp_")
    web = None
    runtime = None
    try:
        ctx = FakeCtx(tmp)
        runtime = McpRuntime(ctx)
        runtime.cfg["input"]["enabled"] = True
        runtime.cfg["screen"]["allow"] = True
        runtime.cfg["confirm"]["mode"] = "never"
        info = runtime.start(persist=False)
        port, token = info["port"], info["token"]
        check("服务已启动", port > 0 and bool(token), info)
        check("endpoint 已写入", os.path.isfile(
            mcp_config.endpoint_path(ctx.paths)))
        ep = mcp_config.read_endpoint(ctx.paths)
        check("endpoint 内容", ep and ep.get("port") == port
              and ep.get("token") == token, ep)

        # 注入 RecordingBackend（不碰真实输入）
        runtime._ensure_services()
        backend = RecordingBackend()
        runtime._services.input._backend = backend
        runtime._services.input.start_watch(runtime._on_panic)

        client = Client(info["url"], token)
        status, payload = client.raw({"jsonrpc": "2.0", "id": 1,
                                      "method": "ping"}, auth=False)
        check("无 token 401", status == 401, status)
        bad = Client(info["url"], "wrong-token")
        status, payload = bad.raw({"jsonrpc": "2.0", "id": 1,
                                   "method": "ping"})
        check("错误 token 401", status == 401, status)

        status, payload = client.call("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "check", "version": "1"}})
        check("initialize 成功", status == 200 and payload
              and payload.get("result", {}).get("protocolVersion"), payload)
        check("session 头", bool(client.session), client.session)
        client.call("notifications/initialized", notify=True)

        status, payload = client.call("tools/list")
        names = [t["name"] for t in payload["result"]["tools"]]
        check("tools/list 18 个", len(names) == 18, names)
        for need in ("screen.capture", "input.mouse_click", "web.search",
                     "web.fetch", "clipboard.read", "system.info",
                     "rag.search", "rag.projects", "project.read_file"):
            check("包含 %s" % need, need in names)

        # 截屏：真实返回 PNG
        status, payload = client.call("tools/call", {
            "name": "screen.capture",
            "arguments": {"mode": "full", "max_width": 800}})
        result = payload["result"]
        img = next((c for c in result["content"] if c.get("type") == "image"),
                   None)
        png = base64.b64decode(img["data"]) if img else b""
        check("截屏返回 image", png.startswith(b"\x89PNG\r\n\x1a\n"), len(png))
        meta = result.get("structuredContent") or {}
        check("截屏元数据", meta.get("width", 0) <= 800
              and meta.get("scale", 0) > 0 and meta.get("origin") == [0, 0],
              meta)
        check("截屏文本含换算", "screen_x = origin_x + img_x / scale"
              in _content_text(result))

        # 区域截图：[x,y,w,h] 语义必须得到 w*h 的图
        status, payload = client.call("tools/call", {
            "name": "screen.capture",
            "arguments": {"mode": "region",
                          "region": [100, 100, 400, 300],
                          "max_width": 0}})
        result = payload["result"]
        meta = result.get("structuredContent") or {}
        check("区域截图尺寸 w*h", meta.get("width") == 400
              and meta.get("height") == 300, meta)
        check("区域截图 origin", meta.get("origin") == [100, 100], meta)

        # 窗口截图：拿当前可见窗口里标题最长的一个（跳过自身）
        status, payload = client.call("tools/call", {
            "name": "screen.list_windows", "arguments": {"limit": 10}})
        windows = _content_text(payload["result"])
        try:
            win_list = json.loads(windows)
        except Exception:
            win_list = []
        cand = None
        for item in win_list:
            rect = item.get("rect") or [0, 0, 0, 0]
            if item.get("title") and rect[2] - rect[0] > 300 \
                    and rect[3] - rect[1] > 200:
                cand = item
                break
        if cand is None:
            check("窗口截图", True, "skip（没有合适窗口）")
        else:
            status, payload = client.call("tools/call", {
                "name": "screen.capture",
                "arguments": {"mode": "window",
                              "window_title": cand["title"],
                              "max_width": 0}})
            result = payload["result"]
            img = next((c for c in result["content"]
                        if c.get("type") == "image"), None)
            meta = result.get("structuredContent") or {}
            check("窗口截图", not result.get("isError") and img is not None
                  and meta.get("mode") == "window", (cand.get("title"), meta))
        status, payload = client.call("tools/call", {"name": "screen.info",
                                                     "arguments": {}})
        check("screen.info", "virtual_screen" in _content_text(payload["result"]))

        # 输入：真实走 RecordingBackend
        status, payload = client.call("tools/call", {
            "name": "input.mouse_move", "arguments": {"x": 123, "y": 456}})
        check("input.mouse_move", not payload["result"]["isError"]
              and ("move", 123, 456) in backend.events, backend.events[-3:])
        status, payload = client.call("tools/call", {
            "name": "input.type",
            "arguments": {"text": "测试A😀", "method": "unicode"}})
        joined = "".join(str(e[1]) for e in backend.events
                         if e[0] == "unicode")
        check("input.type unicode", "测试A😀" in joined, backend.events[-5:])
        status, payload = client.call("tools/call", {
            "name": "input.mouse_click",
            "arguments": {"x": 10, "y": 20, "count": 2}})
        check("input.mouse_click", any(e[0] == "click" and e[2] == 2
                                       for e in backend.events),
              backend.events[-3:])

        # 单项关闭后不再可用
        runtime.set_tool_enabled("input.mouse_click", False)
        status, payload = client.call("tools/call", {
            "name": "input.mouse_click", "arguments": {}})
        check("禁用工具被拒", payload is None and status in (200, 400) or
              (payload and "error" in payload), (status, payload))
        names2 = [t["name"] for t in client.call("tools/list")[1]
                  ["result"]["tools"]]
        check("tools/list 同步移除", "input.mouse_click" not in names2)
        runtime.set_tool_enabled("input.mouse_click", True)

        # 确认拒绝：ask + 回调 False
        runtime.cfg["confirm"]["mode"] = "ask"
        runtime._server._confirm = lambda _t, _s: False
        status, payload = client.call("tools/call", {
            "name": "input.mouse_move", "arguments": {"x": 1, "y": 2}})
        check("确认拒绝 isError", payload["result"]["isError"]
              and "拒绝" in _content_text(payload["result"]), payload)
        runtime._server._confirm = lambda _t, _s: True
        status, payload = client.call("tools/call", {
            "name": "input.mouse_move", "arguments": {"x": 3, "y": 4}})
        check("确认允许成功", not payload["result"]["isError"])
        runtime.cfg["confirm"]["mode"] = "never"

        # 本地网页抓取：默认拒绝私网，打开 allow_private 后才能抓
        web, base = start_local_web()
        status, payload = client.call("tools/call", {
            "name": "web.fetch",
            "arguments": {"url": base + "/page", "max_chars": 5000}})
        text = _content_text(payload["result"])
        check("web.fetch 默认拒绝私网", payload["result"]["isError"]
              and ("私有" in text or "private" in text.lower()), text[:160])
        runtime._services.search.allow_private = True
        status, payload = client.call("tools/call", {
            "name": "web.fetch",
            "arguments": {"url": base + "/page", "max_chars": 5000}})
        text = _content_text(payload["result"])
        check("web.fetch 正文", "MCP 测试页面" in text
              and "print" in text and "导航垃圾" not in text, text[:160])
        check("web.fetch 列表/链接", "第一项" in text and "下一节" in text)

        # web.search 格式化（假搜索服务，避免联网不稳定）
        from vbhelper.services.web.search import (SearchHit, SearchPage,
                                                  SearchResult)

        def fake_search(query, **kwargs):
            return SearchResult(
                query=query, provider="fake",
                hits=[SearchHit("标题 A", "https://example.com/a",
                                "摘要 A", "fake")],
                pages=[SearchPage("标题 A", "https://example.com/a",
                                  "# 正文 A\n\n内容", "")],
                took_ms=12)
        runtime._services.search.search = fake_search
        status, payload = client.call("tools/call", {
            "name": "web.search",
            "arguments": {"query": "测试", "deep": True, "max_pages": 1}})
        text = _content_text(payload["result"])
        check("web.search Markdown", "## 搜索：测试" in text
              and "https://example.com/a" in text
              and "正文 A" in text, text[:200])

        # RAG：真实建一个临时项目索引，再用 MCP 工具检索
        from vbhelper.services.rag import engine
        proj = os.path.join(tmp, "rag-project")
        os.makedirs(os.path.join(proj, "src"))
        with open(os.path.join(proj, "src", "demo.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("def greet(name):\n"
                     "    return 'hello rag ' + name\n")
        with open(os.path.join(proj, "README.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# RAG 演示\n\n这是本地索引的中文说明。\n")
        engine.index_project(ctx.db, proj)
        status, payload = client.call("tools/call", {
            "name": "rag.projects", "arguments": {}})
        text = _content_text(payload["result"])
        check("rag.projects 列出项目", "rag-project" in text
              and not payload["result"]["isError"], text[:160])
        status, payload = client.call("tools/call", {
            "name": "rag.search",
            "arguments": {"project_path": proj, "query": "hello rag"}})
        text = _content_text(payload["result"])
        check("rag.search 命中文件/行号", "demo.py:1-2" in text
              and "hello rag" in text and not payload["result"]["isError"],
              text[:300])
        status, payload = client.call("tools/call", {
            "name": "project.read_file",
            "arguments": {"project_path": proj, "path": "src/demo.py",
                          "start_line": 1, "end_line": 2}})
        text = _content_text(payload["result"])
        check("project.read_file 行范围", "def greet" in text
              and "行 1-2" in text and not payload["result"]["isError"],
              text[:200])
        status, payload = client.call("tools/call", {
            "name": "project.read_file",
            "arguments": {"project_path": proj, "path": "../../outside.txt"}})
        check("project.read_file 拒绝逃逸",
              payload["result"]["isError"], payload)

        # 通知：非阻塞入队，长文本/标题拼接
        status, payload = client.call("tools/call", {
            "name": "notify.toast",
            "arguments": {"title": "完成", "message": "测试通知内容"}})
        check("notify.toast 成功", not payload["result"]["isError"]
              and any("测试通知内容" in t for t in ctx.toasts), ctx.toasts[-3:])

        # 审计
        rows = runtime.recent_audit(50)
        actions = [r["action"] for r in rows]
        check("审计有截屏记录", "screen.capture" in actions, actions[:10])
        check("审计有输入记录", "input.mouse_move" in actions, actions[:10])
        check("审计无 token", all(token not in str(r.get("detail") or "")
                                  for r in rows))
        check("审计有拒绝记录", any(not r["ok"] for r in rows))

        # stdio 桥
        ep_path = mcp_config.endpoint_path(ctx.paths)
        reqs = [
            {"jsonrpc": "2.0", "id": 101, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "bridge", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 102, "method": "tools/list",
             "params": {}},
        ]
        stdin_text = "\n".join(json.dumps(r, ensure_ascii=False)
                               for r in reqs) + "\n"
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-m", "vbhelper.mcp_stdio", "--endpoint",
             ep_path, "--no-autostart"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")
        out, err = proc.communicate(stdin_text, timeout=40)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        check("stdio 只输出两条响应", len(lines) == 2, (out[:400], err[-400:]))
        parsed = [json.loads(ln) for ln in lines] if len(lines) == 2 else []
        check("stdio initialize", parsed and parsed[0].get("id") == 101
              and parsed[0].get("result", {}).get("serverInfo"), parsed[:1])
        check("stdio tools/list", len(parsed) > 1 and parsed[1].get("id") == 102
              and "tools" in parsed[1].get("result", {}), parsed[1:])

        # 停止：端口释放 + endpoint 删除
        old_port = port
        runtime.stop(persist=False)
        check("停止后 endpoint 删除",
              not os.path.exists(mcp_config.endpoint_path(ctx.paths)))
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % old_port,
                                   timeout=2)
            closed = False
        except Exception:
            closed = True
        check("停止后端口关闭", closed)
        runtime.cfg["enabled"] = True
        runtime.stop(persist=True)
        check("停止不关闭 enabled（桥仍可拉起）",
              runtime.cfg.get("enabled") is True)
        runtime.set_auto_start(False)
        check("自动启动开关可持久化",
              (runtime.cfg.get("server") or {}).get("auto_start") is False)
    finally:
        if runtime is not None:
            try:
                runtime.shutdown()
            except Exception:
                pass
        if web is not None:
            try:
                web.shutdown()
                web.server_close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("=" * 56))
    print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
