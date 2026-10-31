# -*- coding: utf-8 -*-
"""MCP 协议层自检：无 GUI、无 Tk、不碰真实鼠标键盘。

覆盖：
- registry：ToolResult/ToolError/ToolCall、启用规则、限流解析；
- tools：18 个工具 schema、截屏图片+坐标 JSON、输入校验、搜索 Markdown、
  剪贴板截断、system.info 不泄露 token、notify；
- server：401、initialize/session、通知 202、tools/list 过滤、成功/失败/超时/
  未启用/限流/确认策略、审计、batch、health/405/404、解析错误、stop 释放端口；
- stdio 桥：子进程 initialize/tools/list/通知无输出/session 透传/未知方法/
  错误应答模式/退出码 0。

运行：D:/python/python.exe _mcp_core_check.py
"""

from __future__ import annotations

import base64
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vbhelper.features.mcp.registry import (       # noqa: E402
    RateLimiter,
    ToolCall,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    parse_rate_limit,
    tool_enabled,
)
from vbhelper.features.mcp.tools import build_registry     # noqa: E402
from vbhelper.features.mcp.server import (                 # noqa: E402
    McpServer,
    PROTOCOL_VERSION,
    SESSION_HEADER,
)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhf"
    "DwAChwGA60e6kgAAAABJRU5ErkJggg==")

CTX = SimpleNamespace(app=SimpleNamespace(root_dir=ROOT))

PASSED: list[str] = []
FAILED: list[tuple[str, BaseException]] = []


def _run(name: str, fn) -> None:
    try:
        fn()
        PASSED.append(name)
        print("PASS  %s" % name)
    except BaseException as exc:        # 单个用例失败不阻断其余用例
        FAILED.append((name, exc))
        print("FAIL  %s -> %s: %s" % (name, type(exc).__name__, exc))
        traceback.print_exc()


def _assert(cond: Any, message: str) -> None:
    if not cond:
        raise AssertionError(message)


# ---------------------------------------------------------------- fake 服务


@dataclass
class FakeWindow:
    hwnd: int
    title: str
    class_name: str
    pid: int
    rect: tuple


@dataclass
class FakeCapture:
    image: Any
    png: bytes
    path: str | None
    width: int
    height: int
    scale: float
    origin: tuple
    mode: str
    window: Any = None


@dataclass
class FakeHit:
    title: str
    url: str
    snippet: str
    provider: str


@dataclass
class FakePage:
    title: str
    url: str
    markdown: str
    error: str = ""


@dataclass
class FakeSearchResult:
    query: str
    provider: str
    hits: list
    pages: list
    error: str = ""
    took_ms: int = 0


class FakeScreen:
    def __init__(self) -> None:
        self.calls: list = []
        self.capture_error = ""
        self.info_error = ""

    def info(self) -> dict:
        self.calls.append(("info",))
        if self.info_error:
            raise RuntimeError(self.info_error)
        return {
            "virtual_screen": {"x": -100, "y": 0, "w": 1920, "h": 1080},
            "monitors": [{"index": 0, "x": -100, "y": 0,
                          "width": 1920, "height": 1080, "primary": True}],
            "dpi": 96,
            "primary_size": [1920, 1080],
            "active_window": None,
        }

    def list_windows(self, limit: int = 50,
                     visible_only: bool = True) -> list:
        self.calls.append(("list_windows", limit, visible_only))
        windows = [
            FakeWindow(1, "记事本 - Notepad", "Notepad", 123,
                       (0, 0, 800, 600)),
            FakeWindow(2, "Chrome", "Chrome_WidgetWin_1", 456,
                       (-100, 0, 900, 700)),
        ]
        return windows[:max(0, int(limit))]

    def capture(self, *, mode: str = "full", monitor: int = 0,
                region: tuple | None = None, window_title: str | None = None,
                max_width: int | None = None, save: bool = True) -> FakeCapture:
        self.calls.append({
            "kind": "capture", "mode": mode, "monitor": monitor,
            "region": region, "window_title": window_title,
            "max_width": max_width,
        })
        if self.capture_error:
            raise RuntimeError(self.capture_error)
        return FakeCapture(image=None, png=PNG_1PX,
                           path="C:\\tmp\\capture.png" if save else None,
                           width=1280, height=720, scale=0.8,
                           origin=(-100, 20), mode=mode, window=None)


class FakeInput:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list = []

    def mouse_move(self, x: int, y: int, duration: float = 0.0) -> None:
        self.calls.append(("move", x, y, duration))

    def mouse_click(self, button: str = "left", count: int = 1,
                    interval: float = 0.05, x: int | None = None,
                    y: int | None = None) -> None:
        self.calls.append(("click", button, count, interval, x, y))

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int,
                   button: str = "left", duration: float = 0.4) -> None:
        self.calls.append(("drag", x1, y1, x2, y2, button, duration))

    def scroll(self, amount: int = -3, *, horizontal: bool = False,
               x: int | None = None, y: int | None = None) -> None:
        self.calls.append(("scroll", amount, horizontal, x, y))

    def type_text(self, text: str, *, method: str = "paste",
                  interval: float = 0.0) -> None:
        self.calls.append(("type", text, method, interval))

    def press(self, keys: str) -> None:
        self.calls.append(("press", keys))


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list = []
        self.error = ""
        self.empty = False
        self.page_error = ""

    def search(self, query: str, *, limit: int = 8, deep: bool = False,
               max_pages: int = 3, provider: str = "auto"
               ) -> FakeSearchResult:
        self.calls.append(("search", query, limit, deep, max_pages, provider))
        if self.error:
            return FakeSearchResult(query=query, provider=provider,
                                    hits=[], pages=[], error=self.error,
                                    took_ms=12)
        if self.empty:
            return FakeSearchResult(query=query, provider=provider,
                                    hits=[], pages=[], took_ms=7)
        hits = [
            FakeHit("结果一", "https://example.com/1", "摘要一", provider),
            FakeHit("结果二", "https://example.com/2", "摘要二", provider),
        ]
        pages = []
        if deep:
            pages = [FakePage("结果一", "https://example.com/1",
                              "# 正文\n\n这里是抓取到的正文。")]
        return FakeSearchResult(query=query, provider=provider,
                                hits=hits[:limit], pages=pages, took_ms=42)

    def fetch(self, url: str, *, max_chars: int = 20000) -> FakePage:
        self.calls.append(("fetch", url, max_chars))
        if self.page_error:
            return FakePage(title="", url=url, markdown="",
                            error=self.page_error)
        return FakePage(title="示例页面", url=url,
                        markdown="# 示例页面\n\n正文内容", error="")


class FakeClipboard:
    def __init__(self) -> None:
        self.text = ""
        self.calls: list = []
        self.fail = False

    def read_text(self) -> str:
        self.calls.append(("read",))
        if self.fail:
            raise RuntimeError("剪贴板被占用")
        return self.text

    def write_text(self, text: str) -> bool:
        self.calls.append(("write", len(text)))
        if self.fail:
            raise RuntimeError("剪贴板被占用")
        self.text = text
        return True

    def foreground_app(self) -> dict:
        return {"title": "", "class_name": "", "pid": 0, "hwnd": 0}


class FakeServices:
    def __init__(self) -> None:
        self.screen = FakeScreen()
        self.input = FakeInput(enabled=True)
        self.search = FakeSearch()
        self.clipboard = FakeClipboard()


class FakeAudit:
    def __init__(self) -> None:
        self.records: list = []
        self.lock = threading.Lock()

    def log(self, scope: str, action: str, detail: Any = None, *,
            ok: bool = True, elapsed_ms: int = 0) -> None:
        with self.lock:
            self.records.append({"scope": scope, "action": action,
                                 "detail": detail, "ok": bool(ok),
                                 "elapsed_ms": int(elapsed_ms)})


class FakeConfirm:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.calls: list = []
        self.lock = threading.Lock()

    def __call__(self, title: str, summary: str) -> bool:
        with self.lock:
            self.calls.append((title, summary))
        return self.answer


def base_cfg() -> dict:
    return {
        "enabled": True,
        "server": {"bind": "127.0.0.1", "port": 0,
                   "token": "SEKRET-TOKEN-SHOULD-NOT-LEAK"},
        "confirm": {"mode": "ask", "session_minutes": 5, "timeout": 30},
        "tools": {},
        "input": {"enabled": False, "panic_seconds": 60},
        "screen": {"allow": True, "max_width": 1600},
        "web": {"max_fetch_bytes": 5242880, "allow_private": False},
        "search": {"provider": "auto"},
    }


def server_cfg() -> dict:
    cfg = base_cfg()
    cfg["input"]["enabled"] = True
    cfg["tools"] = {"screen.info": {"enabled": False}}
    return cfg


# ---------------------------------------------------------------- 工具辅助


def call_tool(registry: ToolRegistry, name: str, args: dict, services: Any,
              cfg: dict | None = None, notify=None) -> ToolResult:
    spec = registry.get(name)
    _assert(spec is not None, "工具不存在：%s" % name)
    return spec.handler(ToolCall(ctx=CTX, services=services, args=args,
                                 cfg=cfg if cfg is not None else base_cfg(),
                                 notify=notify))


def expect_tool_error(fn, contains: str = "") -> ToolError:
    try:
        fn()
    except ToolError as exc:
        if contains and contains not in str(exc):
            raise AssertionError("错误信息缺少 %r：%s" % (contains, exc))
        return exc
    raise AssertionError("预期抛出 ToolError，但没有")


def _text(result: ToolResult) -> str:
    for item in result.content:
        if item.get("type") == "text":
            return str(item.get("text") or "")
    raise AssertionError("结果里没有 text content")


# ---------------------------------------------------------------- 用例 1：registry


def check_registry() -> None:
    image = ToolResult.image(PNG_1PX, text="")
    _assert(len(image.content) == 1, "空 text 不应追加 content")
    _assert(image.content[0]["type"] == "image", "图片 content 类型错误")
    _assert(image.content[0]["mimeType"] == "image/png", "mime 错误")
    _assert(base64.b64decode(image.content[0]["data"]).startswith(
        b"\x89PNG\r\n\x1a\n"), "PNG base64 解码失败")

    both = ToolResult.image(PNG_1PX, text="说明")
    _assert([c["type"] for c in both.content] == ["image", "text"],
            "content 顺序必须是 image 在前")
    text = ToolResult.text("你好", structured={"k": 1})
    _assert(text.content == [{"type": "text", "text": "你好"}], "text 结果错误")
    _assert(text.structured == {"k": 1}, "structured 丢失")

    err = ToolError("坏了", code="bad", detail="d")
    _assert(str(err) == "坏了" and err.code == "bad" and err.detail == "d",
            "ToolError 字段错误")

    _assert(parse_rate_limit("60/min") == (60, 60.0), "限流解析 60/min")
    _assert(parse_rate_limit("2/min") == (2, 60.0), "限流解析 2/min")
    _assert(parse_rate_limit("100/10s") == (100, 10.0), "限流解析 100/10s")
    _assert(parse_rate_limit("bad") is None, "无效限流应返回 None")

    limiter = RateLimiter()
    _assert(limiter.allow("k", "2/min"), "第 1 次应放行")
    _assert(limiter.allow("k", "2/min"), "第 2 次应放行")
    _assert(not limiter.allow("k", "2/min"), "第 3 次应限流")
    _assert(limiter.allow("other", "2/min"), "不同 key 互不影响")

    services = FakeServices()
    registry = build_registry(services)
    input_spec = registry.get("input.type")
    capture_spec = registry.get("screen.capture")
    info_spec = registry.get("screen.info")
    _assert(not tool_enabled(input_spec, {"input": {"enabled": False}}),
            "input 总闸关闭时工具必须禁用")
    _assert(tool_enabled(input_spec, {"input": {"enabled": True}}),
            "input 总闸开启且默认开")
    _assert(not tool_enabled(input_spec, {
        "input": {"enabled": True},
        "tools": {"input.type": {"enabled": False}},
    }), "单项覆盖优先")
    _assert(not tool_enabled(capture_spec, {"screen": {"allow": False}}),
            "screen.capture 受 allow 总闸限制")
    _assert(tool_enabled(info_spec, {"screen": {"allow": False}}),
            "screen.info 不受 allow 限制")
    _assert(not tool_enabled(info_spec,
                             {"tools": {"screen.info": {"enabled": False}}}),
            "单项关闭 screen.info 生效")
    _assert(tool_enabled(info_spec, None), "cfg 缺失时安全默认（screen 开）")
    _assert(not tool_enabled(input_spec, {"input": "broken"}),
            "cfg 畸形时 input 安全默认关闭")

    errors = 0
    try:
        registry.register(info_spec)
    except ValueError:
        errors += 1
    _assert(errors == 1, "重复注册应报错")


# ---------------------------------------------------------------- 用例 2：schema


EXPECTED_TOOLS = {
    "screen.capture", "screen.info", "screen.list_windows",
    "input.mouse_move", "input.mouse_click", "input.mouse_drag",
    "input.scroll", "input.type", "input.key",
    "web.search", "web.fetch", "clipboard.read", "clipboard.write",
    "system.info", "notify.toast",
    "rag.search", "rag.projects", "project.read_file",
}

EXPECTED_REQUIRED = {
    "input.mouse_move": ["x", "y"],
    "input.mouse_drag": ["x1", "y1", "x2", "y2"],
    "input.type": ["text"],
    "input.key": ["keys"],
    "web.search": ["query"],
    "web.fetch": ["url"],
    "clipboard.write": ["text"],
    "notify.toast": ["title", "message"],
    "rag.search": ["query"],
    "project.read_file": ["path"],
}


def check_schemas() -> None:
    registry = build_registry(FakeServices())
    specs = registry.all()
    names = [spec.name for spec in specs]
    _assert(len(names) == 18, "必须恰好注册 18 个工具，当前 %d" % len(names))
    _assert(set(names) == EXPECTED_TOOLS, "工具名与契约不一致：%r"
            % (set(names) ^ EXPECTED_TOOLS))
    _assert(len(names) == len(set(names)), "工具名重复")
    for spec in specs:
        schema = spec.schema
        _assert(schema.get("type") == "object",
                "%s schema type 必须是 object" % spec.name)
        _assert(schema.get("additionalProperties") is False,
                "%s schema 必须 additionalProperties=false" % spec.name)
        required = schema.get("required")
        _assert(required == EXPECTED_REQUIRED.get(spec.name, []),
                "%s required 不正确：%r" % (spec.name, required))
        _assert(isinstance(schema.get("properties"), dict),
                "%s 缺少 properties" % spec.name)
        _assert(isinstance(schema.get("description"), str)
                and schema["description"].strip(),
                "%s 缺少 description" % spec.name)


# ---------------------------------------------------------------- 用例 3：截屏


def check_screen_tools() -> None:
    services = FakeServices()
    registry = build_registry(services)

    result = call_tool(registry, "screen.capture", {}, services)
    _assert(not result.is_error, "截屏不应失败")
    _assert([c["type"] for c in result.content] == ["image", "text"],
            "截屏 content 顺序错误")
    png = base64.b64decode(result.content[0]["data"])
    _assert(png.startswith(b"\x89PNG\r\n\x1a\n"), "图片不是 PNG")
    text = _text(result)
    _assert("screen_x = origin_x + img_x / scale" in text,
            "截屏文本缺少坐标换算说明")
    first_line, _, rest = text.partition("\n")
    meta = json.loads(rest)
    _assert(meta["origin"] == [-100, 20], "origin 错误：%r" % (meta["origin"],))
    _assert(abs(meta["scale"] - 0.8) < 1e-9, "scale 错误")
    _assert(meta["virtual_screen"] == [-100, 0, 1920, 1080],
            "virtual_screen 错误：%r" % (meta["virtual_screen"],))
    _assert(meta["width"] == 1280 and meta["height"] == 720, "尺寸错误")
    _assert(meta["mode"] == "full", "mode 错误")
    _assert(result.structured == meta, "structuredContent 与文本 JSON 不一致")
    capture_calls = [call for call in services.screen.calls
                     if isinstance(call, dict)]
    _assert(capture_calls[-1]["max_width"] == 1600,
            "缺省 max_width 应取 cfg")

    call_tool(registry, "screen.capture",
              {"mode": "region", "region": [0, 0, 100, 100]}, services)
    call_tool(registry, "screen.capture",
              {"mode": "window", "window_title": "记事本", "max_width": 0},
              services)
    capture_calls = [call for call in services.screen.calls
                     if isinstance(call, dict)]
    _assert(capture_calls[-1]["max_width"] == 0,
            "max_width=0 应透传")
    expect_tool_error(
        lambda: call_tool(registry, "screen.capture", {"mode": "region"},
                          services), "region")
    expect_tool_error(
        lambda: call_tool(registry, "screen.capture", {"mode": "window"},
                          services), "window_title")
    services.screen.capture_error = "找不到窗口：xx"
    expect_tool_error(
        lambda: call_tool(registry, "screen.capture",
                          {"mode": "window", "window_title": "xx"}, services),
        "截屏失败")
    services.screen.capture_error = ""

    info = call_tool(registry, "screen.info", {}, services)
    info_data = json.loads(_text(info))
    _assert(info_data["virtual_screen"]["w"] == 1920, "screen.info 内容错误")

    windows = call_tool(registry, "screen.list_windows", {"limit": 1}, services)
    window_data = json.loads(_text(windows))
    _assert(len(window_data) == 1, "limit 未生效")
    _assert(set(window_data[0]) == {"hwnd", "title", "class_name", "pid",
                                    "rect"}, "窗口字段错误")
    _assert(window_data[0]["rect"] == [0, 0, 800, 600], "rect 未转 list")
    _assert(services.screen.calls[-1] == ("list_windows", 1, True),
            "list_windows 参数错误")


# ---------------------------------------------------------------- 用例 4：输入


def check_input_tools() -> None:
    services = FakeServices()
    services.input.enabled = False
    registry = build_registry(services)
    expect_tool_error(
        lambda: call_tool(registry, "input.type", {"text": "hi"}, services),
        "鼠标键盘自动化未启用")
    _assert(services.input.calls == [], "未启用时不应触碰输入后端")

    services.input.enabled = True
    result = call_tool(registry, "input.mouse_move",
                       {"x": -5, "y": 300, "duration": 0.1}, services)
    _assert("(-5, 300)" in _text(result), "移动结果缺少坐标")
    _assert(services.input.calls[-1] == ("move", -5, 300, 0.1),
            "mouse_move 调用参数错误")

    result = call_tool(registry, "input.mouse_click",
                       {"x": 10, "y": 20, "button": "right", "count": 2},
                       services)
    _assert("(10, 20)" in _text(result) and "右键" in _text(result),
            "点击结果文本错误")
    _assert(services.input.calls[-1][0] == "click", "mouse_click 未调用")

    result = call_tool(registry, "input.mouse_drag",
                       {"x1": 1, "y1": 2, "x2": 3, "y2": 4}, services)
    _assert("(1, 2)" in _text(result) and "(3, 4)" in _text(result),
            "拖拽结果文本错误")

    result = call_tool(registry, "input.scroll", {"amount": 5}, services)
    _assert("向上" in _text(result), "滚动方向文本错误")

    result = call_tool(registry, "input.type",
                       {"text": "你好", "method": "unicode"}, services)
    _assert("2 个字符" in _text(result), "输入字数文本错误")
    _assert(services.input.calls[-1][0] == "type", "type_text 未调用")

    result = call_tool(registry, "input.key", {"keys": "ctrl+shift+t"},
                       services)
    _assert("ctrl+shift+t" in _text(result), "按键结果文本错误")

    expect_tool_error(
        lambda: call_tool(registry, "input.mouse_click", {"count": 11},
                          services), "count")
    expect_tool_error(
        lambda: call_tool(registry, "input.mouse_click", {"x": 1}, services),
        "同时")
    expect_tool_error(
        lambda: call_tool(registry, "input.type", {"text": ""}, services),
        "text")
    expect_tool_error(
        lambda: call_tool(registry, "input.type", {"text": "a" * 20001},
                          services), "20000")
    expect_tool_error(
        lambda: call_tool(registry, "input.key", {"keys": "a" * 65},
                          services), "64")


# ---------------------------------------------------------------- 用例 5：联网


def check_web_tools() -> None:
    services = FakeServices()
    registry = build_registry(services)

    result = call_tool(registry, "web.search",
                       {"query": "vbhelper", "deep": True}, services)
    text = _text(result)
    _assert("## 搜索：vbhelper" in text, "搜索标题缺失")
    _assert("1. [结果一](https://example.com/1)" in text, "命中列表缺失")
    _assert("### 来源 1" in text, "deep 页面标题缺失")
    _assert("这里是抓取到的正文" in text, "deep 正文缺失")
    _assert(services.search.calls[-1][0] == "search", "search 未调用")

    services.search.error = "所有搜索引擎都失败了"
    result = call_tool(registry, "web.search", {"query": "x"}, services)
    _assert(result.is_error and "搜索失败" in _text(result),
            "搜索错误应 isError")
    services.search.error = ""

    services.search.empty = True
    result = call_tool(registry, "web.search", {"query": "x"}, services)
    _assert(not result.is_error and "没有找到结果" in _text(result),
            "0 结果应是成功但说明")
    services.search.empty = False

    result = call_tool(registry, "web.fetch",
                       {"url": "https://example.com/a"}, services)
    text = _text(result)
    _assert("示例页面" in text and "https://example.com/a" in text
            and "正文内容" in text, "fetch 输出不完整")
    expect_tool_error(
        lambda: call_tool(registry, "web.fetch", {"url": "ftp://x"},
                          services), "http")

    services.search.page_error = "HTTP 500"
    result = call_tool(registry, "web.fetch",
                       {"url": "https://example.com/b"}, services)
    _assert(result.is_error and "抓取失败" in _text(result),
            "fetch 错误应 isError")


# ---------------------------------------------------------------- 用例 6：剪贴板等


def check_clipboard_system_notify() -> None:
    services = FakeServices()
    registry = build_registry(services)
    services.clipboard.text = "你好剪贴板"
    result = call_tool(registry, "clipboard.read", {}, services)
    _assert(_text(result) == "你好剪贴板", "剪贴板读取错误")

    services.clipboard.text = "字" * 25000
    result = call_tool(registry, "clipboard.read", {}, services)
    text = _text(result)
    _assert("已截断" in text and len(text) < 22000, "剪贴板截断失败")

    result = call_tool(registry, "clipboard.write", {"text": "hello"},
                       services)
    _assert("5 个字符" in _text(result), "写入字数错误")
    _assert(services.clipboard.text == "hello", "剪贴板未写入")
    expect_tool_error(
        lambda: call_tool(registry, "clipboard.write", {"text": ""},
                          services), "text")

    services.clipboard.fail = True
    expect_tool_error(
        lambda: call_tool(registry, "clipboard.read", {}, services),
        "读取剪贴板失败")
    services.clipboard.fail = False

    cfg = base_cfg()
    result = call_tool(registry, "system.info", {}, services, cfg=cfg)
    text = _text(result)
    _assert("SEKRET-TOKEN-SHOULD-NOT-LEAK" not in text,
            "system.info 泄露 token")
    data = json.loads(text)
    _assert(data["app_version"], "缺少 app_version")
    _assert(data["project_root"] == ROOT, "project_root 错误")
    _assert("virtual_screen" in data.get("screen", {}), "缺少屏幕信息")
    _assert("token" not in (data.get("mcp_server") or {}),
            "mcp_server 不应包含 token")

    sent: list = []
    result = call_tool(registry, "notify.toast",
                       {"title": "标题", "message": "内容"}, services,
                       notify=lambda title, message: sent.append(
                           (title, message)))
    _assert(sent == [("标题", "内容")], "通知回调参数错误")
    _assert("已发送" in _text(result), "通知结果文本错误")
    result = call_tool(registry, "notify.toast",
                       {"title": "标题", "message": "内容"}, services)
    _assert("通知通道不可用" in _text(result), "无回调时应提示通道不可用")


# ---------------------------------------------------------------- server 用例


def toy_specs(danger_calls: list) -> list[ToolSpec]:
    schema = {"type": "object", "properties": {},
              "required": [], "additionalProperties": False}

    def echo(call: ToolCall) -> ToolResult:
        return ToolResult.text("echo:%s" % call.args.get("msg"))

    def image(call: ToolCall) -> ToolResult:
        return ToolResult.image(PNG_1PX, text="图像结果")

    def boom(call: ToolCall) -> ToolResult:
        raise ToolError("测试失败", code="boom")

    def slow(call: ToolCall) -> ToolResult:
        time.sleep(0.5)
        return ToolResult.text("slow done")

    def limited(call: ToolCall) -> ToolResult:
        return ToolResult.text("limited ok")

    def danger(call: ToolCall) -> ToolResult:
        danger_calls.append(dict(call.args))
        return ToolResult.text("danger ok")

    def session_tool(call: ToolCall) -> ToolResult:
        return ToolResult.text("session:%s" % (call.session or ""))

    return [
        ToolSpec("toy.echo", "回声", "测试", schema, echo,
                 timeout=10.0),
        ToolSpec("toy.image", "图片", "测试", schema, image,
                 timeout=10.0),
        ToolSpec("toy.boom", "爆炸", "测试", schema, boom,
                 timeout=10.0),
        ToolSpec("toy.sleep", "睡眠", "测试", schema, slow,
                 timeout=0.2),
        ToolSpec("toy.limited", "限流", "测试", schema, limited,
                 timeout=10.0, rate_limit="2/min"),
        ToolSpec("toy.danger", "危险操作", "测试", schema, danger,
                 permission="confirm", timeout=10.0),
        ToolSpec("toy.session", "会话", "测试", schema, session_tool,
                 timeout=10.0),
    ]


def hget(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name)
        if value is not None:
            return str(value)
    except Exception:
        pass
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
    except Exception:
        pass
    return None


def make_http(state: dict):
    base = state["base"]

    def post(payload: Any = None, *, token: Any = "test-token",
             sid: str | None = None, path: str = "/mcp",
             raw: str | None = None) -> tuple[int, Any, str]:
        if raw is not None:
            data = raw.encode("utf-8")
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer %s" % token
        if sid is not None:
            headers[SESSION_HEADER] = sid
        request = urllib.request.Request(base + path, data=data,
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return (int(resp.status), resp.headers,
                        resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return (int(exc.code), exc.headers,
                    exc.read().decode("utf-8", "replace"))

    def get(path: str, *, token: Any = "test-token"
            ) -> tuple[int, Any, str]:
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer %s" % token
        request = urllib.request.Request(base + path, headers=headers,
                                         method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return (int(resp.status), resp.headers,
                        resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return (int(exc.code), exc.headers,
                    exc.read().decode("utf-8", "replace"))

    def rpc(method: str, params: Any = None, *, request_id: Any = 1,
            sid: str | None = None, token: Any = "test-token"):
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        status, headers, body = post(payload, token=token, sid=sid)
        data = json.loads(body) if body else None
        return status, headers, data

    return post, get, rpc


# ---------------------------------------------------------------- main


def main() -> int:
    print("== MCP 协议层自检 ==")
    _run("registry 结果封装/启停规则/限流", check_registry)
    _run("tools 15 个工具 schema", check_schemas)
    _run("tools 截屏与窗口", check_screen_tools)
    _run("tools 输入自动化与参数校验", check_input_tools)
    _run("tools 联网搜索与抓取", check_web_tools)
    _run("tools 剪贴板/系统信息/通知", check_clipboard_system_notify)

    state: dict = {}
    test_services = FakeServices()
    test_services.input.enabled = True
    audit = FakeAudit()
    confirm = FakeConfirm(True)
    danger_calls: list = []
    registry = build_registry(test_services)
    for spec in toy_specs(danger_calls):
        registry.register(spec)
    cfg = server_cfg()
    srv = McpServer(CTX, registry, test_services, host="127.0.0.1",
                    port=0, token="test-token", max_sessions=8,
                    confirm=confirm, audit=audit, cfg=cfg)
    state.update({
        "srv": srv, "registry": registry, "services": test_services,
        "audit": audit, "confirm": confirm, "cfg": cfg,
        "danger_calls": danger_calls, "sid": None,
    })
    try:
        port = srv.start()
        state["port"] = port
        state["base"] = "http://127.0.0.1:%d" % port
        _assert(srv.running, "服务未进入 running")
        _assert(srv.url == state["base"] + "/mcp", "url 错误：%r" % srv.url)
        post, get, rpc = make_http(state)
        state.update({"post": post, "get": get, "rpc": rpc})

        def check_auth() -> None:
            status, headers, _ = post({"jsonrpc": "2.0", "id": 1,
                                       "method": "ping"}, token=None)
            _assert(status == 401, "无 token 应 401，实际 %d" % status)
            _assert("Bearer" in (hget(headers, "WWW-Authenticate") or ""),
                    "401 缺少 WWW-Authenticate: Bearer")
            status, _, _ = post({"jsonrpc": "2.0", "id": 1,
                                 "method": "ping"}, token="wrong")
            _assert(status == 401, "错误 token 应 401")
            status, _, body = post({"jsonrpc": "2.0", "id": 1,
                                    "method": "ping"})
            _assert(status == 200 and json.loads(body)["result"] == {},
                    "正确 token 的 ping 失败")

        _run("HTTP 认证 401/正确 token", check_auth)

        def check_initialize() -> None:
            status, headers, body = post({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18",
                           "clientInfo": {"name": "check", "version": "1"}},
            })
            data = json.loads(body)
            _assert(status == 200, "initialize HTTP %d" % status)
            _assert(data["result"]["protocolVersion"] == PROTOCOL_VERSION,
                    "协议版本错误")
            _assert(data["result"]["serverInfo"]["name"] == "vbhelper",
                    "serverInfo.name 错误")
            sid = hget(headers, SESSION_HEADER)
            _assert(sid and len(sid) == 16, "缺少 Mcp-Session-Id：%r" % sid)
            state["sid"] = sid

            _, headers2, body2 = post({
                "jsonrpc": "2.0", "id": 2, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            })
            _assert(json.loads(body2)["result"]["protocolVersion"]
                    == "2024-11-05", "白名单版本应原样返回")
            _assert(hget(headers2, SESSION_HEADER), "第二次 initialize 无会话")

            _, _, body3 = post({
                "jsonrpc": "2.0", "id": 3, "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            })
            _assert(json.loads(body3)["result"]["protocolVersion"]
                    == PROTOCOL_VERSION, "未知版本应回退本端最新")

        _run("initialize 协议协商与会话头", check_initialize)

        def check_notifications() -> None:
            sid = state["sid"]
            status, _, body = post({"jsonrpc": "2.0",
                                    "method": "notifications/initialized"},
                                   sid=sid)
            _assert(status == 202 and body == "", "通知应 202 空体")
            status, _, body = post({
                "jsonrpc": "2.0", "method": "notifications/cancelled",
                "params": {"requestId": 1}}, sid=sid)
            _assert(status == 202 and body == "", "未知通知也应 202 空体")

        _run("notifications/* 202 空体", check_notifications)

        def check_tools_list() -> None:
            sid = state["sid"]
            status, _, data = rpc("tools/list", {}, sid=sid)
            _assert(status == 200, "tools/list HTTP %d" % status)
            tools = data["result"]["tools"]
            names = [tool["name"] for tool in tools]
            _assert("screen.info" not in names,
                    "被覆盖禁用的 screen.info 不应出现在列表")
            _assert("screen.capture" in names, "screen.capture 应启用")
            _assert("toy.echo" in names and "toy.danger" in names,
                    "玩具工具缺失")
            _assert("input.type" in names, "input 总闸开启后应列出输入工具")
            _assert(len(tools) == 24, "启用工具数量应为 24，实际 %d"
                    % len(tools))
            by_name = {tool["name"]: tool for tool in tools}
            _assert(by_name["toy.echo"]["annotations"]["readOnlyHint"] is True,
                    "safe 工具 readOnlyHint 应为 true")
            _assert(by_name["toy.danger"]["annotations"]["readOnlyHint"]
                    is False, "confirm 工具 readOnlyHint 应为 false")
            _assert(by_name["toy.echo"]["inputSchema"]["type"] == "object",
                    "inputSchema 错误")

        _run("tools/list 只含启用工具", check_tools_list)

        def check_calls() -> None:
            sid = state["sid"]
            _, _, data = rpc("tools/call",
                             {"name": "toy.echo",
                              "arguments": {"msg": "hi"}}, sid=sid)
            result = data["result"]
            _assert(not result["isError"], "echo 不应失败")
            _assert(result["content"][0]["text"] == "echo:hi",
                    "echo 结果错误")

            _, _, data = rpc("tools/call",
                             {"name": "toy.session", "arguments": {}}, sid=sid)
            session_text = data["result"]["content"][0]["text"]
            _assert(session_text == "session:%s" % sid,
                    "工具拿到的 session 不正确：%r" % session_text)

            _, _, data = rpc("tools/call",
                             {"name": "toy.image", "arguments": {}}, sid=sid)
            content = data["result"]["content"]
            _assert(content[0]["type"] == "image"
                    and content[0]["mimeType"] == "image/png",
                    "图片 content 错误")
            _assert(base64.b64decode(content[0]["data"]).startswith(
                b"\x89PNG\r\n\x1a\n"), "图片 base64 无效")
            _assert(content[1]["text"] == "图像结果", "图片文本错误")

        _run("tools/call 文本/图片/会话", check_calls)

        def check_errors() -> None:
            sid = state["sid"]
            status, _, data = rpc("tools/call",
                                  {"name": "toy.boom", "arguments": {}},
                                  sid=sid)
            _assert(status == 200, "工具错误应是 HTTP 200")
            _assert(data["result"]["isError"] is True, "boom 应 isError")
            text = data["result"]["content"][0]["text"]
            _assert("测试失败" in text and "boom" in text,
                    "ToolError 文本缺少内容/类型：%s" % text)

            started = time.monotonic()
            _, _, data = rpc("tools/call",
                             {"name": "toy.sleep", "arguments": {}}, sid=sid)
            elapsed = time.monotonic() - started
            _assert(data["result"]["isError"] is True, "超时应 isError")
            _assert("超时" in data["result"]["content"][0]["text"],
                    "超时文本错误")
            _assert(elapsed < 2.0, "超时返回太慢：%.2f 秒" % elapsed)

            _, _, data = rpc("tools/call",
                             {"name": "no.such", "arguments": {}}, sid=sid)
            _assert(data.get("error", {}).get("code") == -32602,
                    "未知工具应 -32602")
            _, _, data = rpc("tools/call",
                             {"name": "screen.info", "arguments": {}}, sid=sid)
            _assert(data.get("error", {}).get("code") == -32602,
                    "被禁用的工具应 -32602")
            _, _, data = rpc("tools/call",
                             {"name": "screen.info",
                              "arguments": {"window_title": "不该执行"}},
                             sid=sid)
            _assert(data.get("error", {}).get("code") == -32602,
                    "禁用工具带参数也应 -32602，不能变成内部错误")

        _run("工具失败/超时/未知禁用工具", check_errors)

        def check_rate_limit() -> None:
            sid = state["sid"]
            for index in range(2):
                _, _, data = rpc("tools/call",
                                 {"name": "toy.limited", "arguments": {}},
                                 sid=sid)
                _assert(not data["result"]["isError"],
                        "限流第 %d 次不应失败" % (index + 1))
            _, _, data = rpc("tools/call",
                             {"name": "toy.limited", "arguments": {}},
                             sid=sid)
            _assert(data["result"]["isError"] is True, "第 3 次应被限流")
            _assert("频繁" in data["result"]["content"][0]["text"],
                    "限流文本错误")

        _run("滑动窗口限流 2/min", check_rate_limit)

        def check_confirm() -> None:
            sid = state["sid"]
            _assert(state["cfg"]["confirm"]["mode"] == "ask", "初始应为 ask")
            state["confirm"].calls.clear()
            state["confirm"].answer = False
            _, _, data = rpc("tools/call",
                             {"name": "toy.danger", "arguments": {"a": 1}},
                             sid=sid)
            _assert(data["result"]["isError"] is True, "拒绝应 isError")
            _assert("拒绝" in data["result"]["content"][0]["text"],
                    "拒绝文本错误")
            _assert(len(state["confirm"].calls) == 1, "ask 模式应确认一次")
            title, summary = state["confirm"].calls[0]
            _assert("危险操作" in title, "确认标题缺少工具标题")
            _assert("toy.danger" in summary and "参数" in summary,
                    "确认摘要缺少工具名/参数")
            _assert(state["danger_calls"] == [], "拒绝后不应执行")

            state["confirm"].answer = True
            _, _, data = rpc("tools/call",
                             {"name": "toy.danger", "arguments": {}},
                             sid=sid)
            _assert(not data["result"]["isError"], "允许后应执行")
            _assert(len(state["confirm"].calls) == 2, "ask 模式每次都要确认")

            # input.type 的确认摘要只允许出现长度，不允许出现正文
            state["confirm"].calls.clear()
            state["confirm"].answer = False
            secret = "ABCABC" * 50
            _, _, data = rpc("tools/call",
                             {"name": "input.type",
                              "arguments": {"text": secret}}, sid=sid)
            _assert(data["result"]["isError"] is True, "输入确认拒绝应 isError")
            summary = state["confirm"].calls[0][1]
            _assert("字符" in summary, "确认摘要应显示文本长度")
            _assert("ABCABC" not in summary, "确认摘要泄露了输入正文")

            # session 模式：同一会话只确认一次
            state["cfg"]["confirm"]["mode"] = "session"
            state["confirm"].calls.clear()
            state["confirm"].answer = True
            state["danger_calls"].clear()
            for _ in range(2):
                _, _, data = rpc("tools/call",
                                 {"name": "toy.danger", "arguments": {}},
                                 sid=sid)
                _assert(not data["result"]["isError"], "session 模式应执行")
            _assert(len(state["confirm"].calls) == 1,
                    "session 模式应只确认一次，实际 %d"
                    % len(state["confirm"].calls))
            _assert(len(state["danger_calls"]) == 2, "session 模式应执行两次")

            # input 组共享授权：确认一次后 input.key 不再确认
            state["services"].input.enabled = True
            state["confirm"].calls.clear()
            _, _, data = rpc("tools/call",
                             {"name": "input.type",
                              "arguments": {"text": "x"}}, sid=sid)
            _assert(not data["result"]["isError"], "input.type 应执行")
            _, _, data = rpc("tools/call",
                             {"name": "input.key",
                              "arguments": {"keys": "enter"}}, sid=sid)
            _assert(not data["result"]["isError"], "input.key 应执行")
            _assert(len(state["confirm"].calls) == 1,
                    "input 组授权应共享，实际确认 %d 次"
                    % len(state["confirm"].calls))

            # 不同 session 需要重新确认
            state["confirm"].calls.clear()
            _, _, data = rpc("tools/call",
                             {"name": "toy.danger", "arguments": {}},
                             sid="aaaabbbbccccdddd")
            _assert(not data["result"]["isError"], "新会话允许后应执行")
            _assert(len(state["confirm"].calls) == 1,
                    "新会话应重新确认")

            # never 模式不弹确认
            state["cfg"]["confirm"]["mode"] = "never"
            state["confirm"].calls.clear()
            _, _, data = rpc("tools/call",
                             {"name": "toy.danger", "arguments": {}},
                             sid=sid)
            _assert(not data["result"]["isError"], "never 模式应执行")
            _assert(len(state["confirm"].calls) == 0, "never 模式不应确认")

            # confirm=None：ask/session 视为拒绝，never 放行
            cfg2 = {"confirm": {"mode": "ask"}, "input": {"enabled": False},
                    "screen": {"allow": True}}
            srv2 = McpServer(CTX, registry, state["services"],
                             token="t", confirm=None, cfg=cfg2)
            response, _ = srv2.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "toy.danger", "arguments": {}}}, "s1")
            _assert(response["result"]["isError"] is True,
                    "confirm=None + ask 应拒绝")
            cfg2["confirm"]["mode"] = "never"
            response, _ = srv2.handle({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "toy.danger", "arguments": {}}}, "s1")
            _assert(not response["result"]["isError"],
                    "confirm=None + never 应放行")

            # 会话数上限
            srv3 = McpServer(CTX, registry, state["services"], token="t",
                             max_sessions=1, cfg=cfg2)
            first, sid3 = srv3.handle({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION}}, None)
            _assert("result" in first and sid3, "首个会话应创建成功")
            second, _ = srv3.handle({
                "jsonrpc": "2.0", "id": 2, "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION}}, None)
            _assert(second.get("error", {}).get("code") == -32600,
                    "超过 max_sessions 应 -32600")

            state["cfg"]["confirm"]["mode"] = "ask"
            state["confirm"].answer = True

        _run("确认 ask/session/never 与授权缓存", check_confirm)

        def check_audit() -> None:
            records = state["audit"].records
            _assert(records, "审计没有记录")
            _assert(all(record["scope"] == "mcp" for record in records),
                    "审计 scope 错误")
            _assert(any(record["action"] == "toy.echo" and record["ok"]
                        for record in records), "成功调用未审计")
            _assert(any(record["action"] == "toy.boom" and not record["ok"]
                        for record in records), "失败调用未审计")
            _assert(any(record["action"] == "no.such" and not record["ok"]
                        for record in records), "未知工具未审计")
            _assert(all(isinstance(record["elapsed_ms"], int)
                        and record["elapsed_ms"] >= 0 for record in records),
                    "elapsed_ms 非整数")
            input_records = [record for record in records
                             if record["action"] == "input.type"]
            _assert(input_records, "input.type 没有审计记录")
            dumped = json.dumps([record["detail"] for record in input_records],
                                ensure_ascii=False)
            _assert("ABCABC" not in dumped, "审计泄露了 input.type 正文")
            _assert("字符" in dumped, "审计应记录 input.type 长度")
            all_dump = json.dumps([record["detail"] for record in records],
                                  ensure_ascii=False)
            _assert("test-token" not in all_dump, "审计不应包含 token")
            _assert("SEKRET" not in all_dump, "审计不应包含配置 token")

        _run("审计记录与脱敏", check_audit)

        def check_health_routes() -> None:
            status, _, body = get("/health")
            data = json.loads(body)
            _assert(status == 200 and data.get("ok") is True, "health 失败")
            _assert(data.get("name") == "vbhelper", "health name 错误")
            _assert("test-token" not in body, "health 泄露 token")
            status, _, _ = get("/health", token=None)
            _assert(status == 401, "health 无 token 应 401")

            status, headers, body = get("/mcp")
            _assert(status == 405, "GET /mcp 应 405，实际 %d" % status)
            _assert(hget(headers, "Allow") == "POST", "405 缺少 Allow: POST")
            _assert("SSE" in body, "405 应说明不支持 SSE")

            status, _, _ = get("/nope")
            _assert(status == 404, "未知 GET 路径应 404")
            status, _, _ = post({"jsonrpc": "2.0", "id": 1,
                                 "method": "ping"}, path="/nope")
            _assert(status == 404, "未知 POST 路径应 404")

            status_info = state["srv"].status()
            _assert("token" not in status_info, "status 不应包含 token")
            _assert("test-token" not in json.dumps(status_info),
                    "status 泄露 token")
            for key in ("running", "port", "sessions", "calls", "errors",
                        "started_at"):
                _assert(key in status_info, "status 缺少 %s" % key)
            _assert(status_info["calls"] > 0, "status calls 计数为 0")

        _run("health/405/404/status 无 token", check_health_routes)

        def check_batch_resources() -> None:
            sid = state["sid"]
            status, _, body = post([
                {"jsonrpc": "2.0", "id": 10, "method": "ping"},
                {"jsonrpc": "2.0", "id": 11, "method": "tools/list",
                 "params": {}},
            ], sid=sid)
            data = json.loads(body)
            _assert(status == 200 and isinstance(data, list)
                    and [item["id"] for item in data] == [10, 11],
                    "batch 响应错误")
            status, _, body = post([
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ], sid=sid)
            _assert(status == 202 and body == "", "纯通知 batch 应 202 空体")

            _, _, data = rpc("resources/list", {}, sid=sid)
            _assert(data["result"] == {"resources": []}, "resources/list 错误")
            _, _, data = rpc("prompts/list", {}, sid=sid)
            _assert(data["result"] == {"prompts": []}, "prompts/list 错误")
            _, _, data = rpc("resources/read",
                             {"uri": "file:///x"}, sid=sid)
            _assert(data.get("error", {}).get("code") == -32601,
                    "resources/read 应 -32601")
            _, _, data = rpc("completely/unknown", {}, sid=sid)
            _assert(data.get("error", {}).get("code") == -32601,
                    "未知方法应 -32601")

        _run("batch/resources/prompts/未知方法", check_batch_resources)

        def check_parse_errors() -> None:
            status, _, body = post(raw="{oops", sid=state["sid"])
            data = json.loads(body)
            _assert(status == 200 and data["error"]["code"] == -32700,
                    "非法 JSON 应 -32700，实际 %r" % data)
            status, _, body = post(raw="42", sid=state["sid"])
            data = json.loads(body)
            _assert(data["error"]["code"] == -32600,
                    "非对象消息应 -32600")
            _, _, data = rpc("tools/call",
                             {"name": "toy.echo", "arguments": []},
                             sid=state["sid"])
            _assert(data["error"]["code"] == -32602, "arguments 非对象应 -32602")
            _, _, data = rpc("tools/call", {"arguments": {}},
                             sid=state["sid"])
            _assert(data["error"]["code"] == -32602, "缺少 name 应 -32602")

        _run("JSON 解析/无效请求/参数错误", check_parse_errors)

        def check_stdio() -> None:
            srv = state["srv"]
            endpoint = {
                "version": 1, "host": "127.0.0.1", "port": srv.port,
                "url": srv.url, "token": "test-token", "pid": os.getpid(),
                "started": time.time(), "app_version": "test",
            }
            tmp = tempfile.mkdtemp(prefix="vbhelper_mcp_")
            endpoint_path = os.path.join(tmp, "endpoint.json")
            with open(endpoint_path, "w", encoding="utf-8") as handle:
                json.dump(endpoint, handle)
            env = os.environ.copy()
            env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
            env.pop("VBHELPER_MCP_ENDPOINT", None)
            stderr_path = os.path.join(tmp, "stderr.log")
            stderr_handle = open(stderr_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-m", "vbhelper.mcp_stdio",
                 "--endpoint", endpoint_path, "--no-autostart"],
                cwd=ROOT, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=stderr_handle,
                text=True, encoding="utf-8", bufsize=1)
            reader = _LineReader(proc.stdout)
            try:
                _send(proc, {"jsonrpc": "2.0", "id": 1,
                             "method": "initialize",
                             "params": {"protocolVersion": PROTOCOL_VERSION,
                                        "clientInfo": {"name": "stdio-check",
                                                       "version": "1"}}})
                obj = json.loads(reader.read())
                _assert(obj["id"] == 1
                        and obj["result"]["protocolVersion"]
                        == PROTOCOL_VERSION, "stdio initialize 失败")
                _send(proc, {"jsonrpc": "2.0",
                             "method": "notifications/initialized"})
                _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
                obj = json.loads(reader.read())
                _assert(obj["id"] == 2 and obj["result"] == {},
                        "通知产生了输出或 ping 失败：%r" % obj)
                _send(proc, {"jsonrpc": "2.0", "id": 3,
                             "method": "tools/list"})
                obj = json.loads(reader.read())
                names = [tool["name"] for tool in obj["result"]["tools"]]
                _assert("toy.echo" in names, "stdio tools/list 错误")
                _send(proc, {"jsonrpc": "2.0", "id": 5,
                             "method": "tools/call",
                             "params": {"name": "toy.session",
                                        "arguments": {}}})
                obj = json.loads(reader.read())
                session_text = obj["result"]["content"][0]["text"]
                _assert(session_text.startswith("session:")
                        and len(session_text) == len("session:") + 16,
                        "stdio 未沿用 Mcp-Session-Id：%r" % session_text)
                _send(proc, {"jsonrpc": "2.0", "id": 4,
                             "method": "bogus/method"})
                obj = json.loads(reader.read())
                _assert(obj["error"]["code"] == -32601,
                        "stdio 未知方法应 -32601")
                proc.stdin.close()
                code = proc.wait(timeout=20)
                _assert(code == 0, "stdio 桥退出码应为 0，实际 %r" % code)
            finally:
                if proc.poll() is None:
                    proc.kill()
                stderr_handle.close()
            log_text = ""
            try:
                with open(stderr_path, "r", encoding="utf-8",
                          errors="replace") as handle:
                    log_text = handle.read()
            except OSError:
                pass
            _assert("Traceback" not in log_text,
                    "stdio 桥 stderr 出现异常：%s" % log_text[-500:])

        _run("stdio 桥 initialize/通知/session/退出码", check_stdio)

        def check_stdio_error_mode() -> None:
            tmp = tempfile.mkdtemp(prefix="vbhelper_mcp_err_")
            missing = os.path.join(tmp, "endpoint.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
            env.pop("VBHELPER_MCP_ENDPOINT", None)
            stderr_path = os.path.join(tmp, "stderr.log")
            stderr_handle = open(stderr_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-m", "vbhelper.mcp_stdio",
                 "--endpoint", missing, "--no-autostart"],
                cwd=ROOT, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=stderr_handle,
                text=True, encoding="utf-8", bufsize=1)
            reader = _LineReader(proc.stdout)
            try:
                _send(proc, {"jsonrpc": "2.0", "id": 7, "method": "ping"})
                obj = json.loads(reader.read())
                _assert(obj["id"] == 7
                        and obj["error"]["code"] == -32000, "错误模式响应错误")
                _assert("启用 MCP" in obj["error"]["message"],
                        "错误模式缺少中文提示")
                proc.stdin.close()
                code = proc.wait(timeout=20)
                _assert(code == 0, "错误模式退出码应为 0，实际 %r" % code)
            finally:
                if proc.poll() is None:
                    proc.kill()
                stderr_handle.close()

        _run("stdio 错误应答模式不退出", check_stdio_error_mode)

    finally:
        port = state.get("port")
        try:
            state["srv"].stop()
        except Exception:
            traceback.print_exc()

    def check_stop() -> None:
        srv = state["srv"]
        _assert(not srv.running, "stop 后仍 running")
        _assert(state["srv"].status()["running"] is False,
                "status 仍报告 running")
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d/health" % port, timeout=2)
        except Exception:
            pass
        else:
            raise AssertionError("stop 后仍能连接端口 %d" % port)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()
        srv.stop()
        _assert(not srv.running, "重复 stop 不应报错")

    _run("stop 释放端口/无残留线程/幂等", check_stop)

    def check_guards() -> None:
        raised = False
        try:
            McpServer(CTX, registry, state["services"],
                      host="0.0.0.0", token="x")
        except ValueError:
            raised = True
        _assert(raised, "非 loopback host 应抛 ValueError")

        srv = McpServer(CTX, registry, state["services"], token="")
        raised = False
        try:
            srv.start()
        except ValueError:
            raised = True
        finally:
            srv.stop()
        _assert(raised, "空 token 应拒绝启动")

    _run("非环回/空 token 启动保护", check_guards)

    total = len(PASSED) + len(FAILED)
    print("\n结果：%d 项，PASS %d，FAIL %d"
          % (total, len(PASSED), len(FAILED)))
    if FAILED:
        for name, exc in FAILED:
            print("  FAIL %s -> %r" % (name, exc))
        return 1
    return 0


# ---------------------------------------------------------------- stdio 读取


class _LineReader:
    def __init__(self, stream) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, args=(stream,),
                                        name="stdio-reader", daemon=True)
        self._thread.start()

    def _run(self, stream) -> None:
        try:
            for line in stream:
                self._queue.put(line)
        except Exception:
            pass
        finally:
            self._queue.put(None)

    def read(self, timeout: float = 20.0) -> str:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("等待 stdio 输出超时（%.1f 秒）" % timeout)
        if item is None:
            raise AssertionError("stdio 进程提前结束（stdout EOF）")
        return item


def _send(proc: subprocess.Popen, payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()


if __name__ == "__main__":
    sys.exit(main())
