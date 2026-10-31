# -*- coding: utf-8 -*-
"""Web 服务无 GUI 自检：正文提取 + 多源搜索（本地 HTTP fixture，不外网）。

运行：D:/python/python.exe _web_services_check.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from vbhelper.services.web import (SearchHit, SearchService,  # noqa: E402
                                   extract_url, html_to_markdown, parse_baidu,
                                   parse_bing)
from vbhelper.ui.http import HttpClient  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASSED.append(name)
        print("PASS %s%s" % (name, (" - " + detail) if detail else ""))
        return True
    FAILED.append((name, detail))
    print("FAIL %s - %s" % (name, detail))
    return False


def skip(name: str, detail: str = "") -> None:
    print("SKIP %s%s" % (name, (" - " + detail) if detail else ""))


ARTICLE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>本地测试文章 - 服务层</title>
<meta name="author" content="测试作者">
<style>.nav{color:red}</style>
<script>console.log("脚本垃圾")</script>
</head><body>
<nav><a href="/">导航垃圾</a></nav>
<header><h1>页眉垃圾</h1></header>
<aside>侧栏垃圾</aside>
<article class="article-content">
<h1>本地文章标题</h1>
<p>这是第一段正文，包含<strong>加粗</strong>、<em>斜体</em>和
<a href="/next?x=1&amp;y=2">站内链接</a>，以及实体 &amp; &lt;tag&gt; 与中文标点：你好，世界！</p>
<p>第二段继续补充文本，让正文打分稳定通过阈值。这里还有一段较长的中文内容，
用于验证段落之间只会保留一个空行，不会出现三连换行。</p>
<h2>列表与代码</h2>
<ul><li>项目一</li><li>项目二</li></ul>
<ol><li>第一步</li><li>第二步</li></ol>
<pre><code class="language-python">def hello():
    print("你好")
    return 42
</code></pre>
<blockquote>这是引用文字</blockquote>
<hr>
<p><img alt="示意图" src="/img/a.png"> 图片说明</p>
<p>第三段正文，继续增加字符数量并保持标点密度，确保正文节点得分最高。
这里再补充一些中文句子，以便选择正文时不会误选导航或侧栏。</p>
</article>
<footer>页脚垃圾</footer>
<form><input value="表单垃圾"><button>提交垃圾</button></form>
</body></html>
"""

DEEP_HTML = """<html><head><title>%(title)s</title></head><body>
<article class="post-content"><h1>%(title)s</h1>
<p>%(body)s</p><p>第二段内容，用于截断测试。%(body)s</p>
</article></body></html>"""


def _deep_html(title: str) -> str:
    body = ("这是一个用于 deep 搜索抓取的本地页面，包含足够长的正文内容。\n"
            * 30)
    return DEEP_HTML % {"title": title, "body": body}


class Handler(BaseHTTPRequestHandler):
    """本地 fixture 服务器：正常页 / 文本 / GBK / 5MB / 慢响应 / 404。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:          # 静音
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:                       # noqa: N802
        path = urlsplit(self.path).path
        if path == "/article":
            self._send(200, ARTICLE_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/plain.txt":
            self._send(200, "纯文本内容 plain-text-marker".encode("utf-8"),
                       "text/plain; charset=utf-8")
        elif path == "/gbk":
            body = ("<html><head><meta charset=\"gbk\"><title>GBK 编码页</title>"
                    "</head>"
                    "<body><p>中文编码内容，用于验证 charset 解码。</p>"
                    "</body></html>").encode("gbk")
            self._send(200, body, "text/html; charset=gbk")
        elif path == "/big":
            body = b"<html><body><p>" + b"x" * (5 * 1024 * 1024 + 200_000)
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/slow":
            time.sleep(3)
            self._send(200, b"<html><body><p>slow</p></body></html>",
                       "text/html; charset=utf-8")
        elif path == "/deep1":
            self._send(200, _deep_html("深抓页面一").encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/deep2":
            self._send(200, _deep_html("深抓页面二").encode("utf-8"),
                       "text/html; charset=utf-8")
        else:
            self._send(404, b"<html><body><p>not found</p></body></html>",
                       "text/html; charset=utf-8")


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeResponse:
    def __init__(self, payload: object, content_type: str = "application/json"):
        self.status = 200
        self.content_type = content_type
        self.body = (payload if isinstance(payload, bytes)
                     else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.url = ""

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class FakeHttp:
    """按 URL 片段返回固定 JSON，用于测试 API provider 的字段映射。"""

    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def request(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        for key, payload in self.payloads.items():
            if key in url:
                return FakeResponse(payload)
        raise AssertionError("unexpected url: %s" % url)


def test_html_to_markdown() -> None:
    page = html_to_markdown(ARTICLE_HTML, "http://example.com/a/b")
    check("extract.title", page.title == "本地测试文章 - 服务层", page.title)
    check("extract.byline", page.byline == "测试作者", page.byline)
    check("extract.ok", page.ok and page.error == "", page.error)
    markdown = page.markdown
    checks = {
        "extract.h1": "# 本地文章标题" in markdown,
        "extract.h2": "## 列表与代码" in markdown,
        "extract.strong_em": "**加粗**" in markdown and "*斜体*" in markdown,
        "extract.link":
            "[站内链接](http://example.com/next?x=1&y=2)" in markdown,
        "extract.entity": "& <tag>" in markdown,
        "extract.list_ul": "- 项目一\n- 项目二" in markdown,
        "extract.list_ol": "1. 第一步\n2. 第二步" in markdown,
        "extract.code_lang": "```python" in markdown,
        "extract.code_indent": '    print("你好")' in markdown,
        "extract.blockquote": "> 这是引用文字" in markdown,
        "extract.hr": "\n---\n" in markdown,
        "extract.img": "![示意图](http://example.com/img/a.png)" in markdown,
        "extract.plain": "本地文章标题" in page.text
                         and "导航垃圾" not in page.text,
        "extract.no_nav": all(word not in markdown for word in
                              ("导航垃圾", "页眉垃圾", "侧栏垃圾", "页脚垃圾",
                               "表单垃圾", "提交垃圾", "脚本垃圾",
                               "console.log", "color:red")),
        "extract.no_blank_runs": "\n\n\n" not in markdown,
    }
    for name, ok in checks.items():
        check(name, ok, "" if ok else repr(markdown[:300]))

    empty = html_to_markdown("<html><body></body></html>")
    check("extract.empty", empty.ok and empty.markdown == ""
          and empty.error == "empty", "%r/%r" % (empty.markdown, empty.error))

    simple = html_to_markdown("<p>只有一段正文，但长度足够让正文提取正常工作，"
                              "包含中文标点，测试段落输出。</p>")
    check("extract.simple_body", simple.ok and simple.markdown != ""
          and "只有一段正文" in simple.markdown, simple.markdown)

    pre = html_to_markdown("<pre><code>x = 1\ny = 2</code></pre>")
    check("extract.pre_no_lang", "```\nx = 1\ny = 2\n```" in pre.markdown,
          repr(pre.markdown))

    blanks = html_to_markdown("<div>段落一<br><br><br><br>段落二</div>"
                              "<div><br></div><div>段落三</div>")
    check("extract.blank_collapse", "\n\n\n" not in blanks.markdown,
          repr(blanks.markdown))


def test_extract_url(base: str, client: HttpClient) -> None:
    page = extract_url(base + "/article", client, allow_private=True)
    check("extract_url.ok", page.ok and "本地文章标题" in page.markdown
          and page.text, page.error)
    check("extract_url.link_absolute",
          ("%s/next?x=1&y=2" % base) in page.markdown,
          repr(page.markdown[:300]))

    plain = extract_url(base + "/plain.txt", client, allow_private=True)
    check("extract_url.plain", plain.ok
          and "plain-text-marker" in plain.markdown, plain.error)

    gbk = extract_url(base + "/gbk", client, allow_private=True)
    check("extract_url.gbk", gbk.ok and "中文编码内容" in gbk.text
          and "GBK" in gbk.title, "%s / %s" % (gbk.title, gbk.error))

    missing = extract_url(base + "/404", client, allow_private=True)
    check("extract_url.404", (not missing.ok)
          and "404" in missing.error, missing.error)

    slow = extract_url(base + "/slow", client, allow_private=True,
                       timeout=0.4)
    check("extract_url.timeout", (not slow.ok) and "超时" in slow.error,
          slow.error)

    big = extract_url(base + "/big", client, allow_private=True)
    check("extract_url.size", (not big.ok) and "上限" in big.error,
          big.error)

    guard = extract_url(base + "/article", client, allow_private=False)
    check("extract_url.ssrf_guard", (not guard.ok) and "私网" in guard.error,
          guard.error)


def _read_fixture(name: str) -> str:
    path = os.path.join(ROOT, "_t", "mcp_fixtures", name)
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def test_parsers() -> None:
    bing_html = _read_fixture("bing.html")
    baidu_html = _read_fixture("baidu.html")
    if bing_html:
        hits = parse_bing(bing_html)
        check("parse_bing.count", len(hits) >= 5, "%d 条" % len(hits))
        check("parse_bing.fields", all(
            h.title.strip() and h.url.strip() and h.snippet.strip()
            for h in hits[:5]), str([(bool(h.title), bool(h.url),
                                     bool(h.snippet)) for h in hits[:5]]))
        check("parse_bing.urls", all(
            h.url.startswith(("http://", "https://"))
            and "bing.com/search" not in h.url
            and not h.url.startswith("javascript:") for h in hits),
            str([h.url for h in hits[:3]]))
        check("parse_bing.unique",
              len({h.url for h in hits}) == len(hits), "%d 条" % len(hits))
    else:
        skip("parse_bing", "_t/mcp_fixtures/bing.html 不存在")

    if baidu_html:
        hits = parse_baidu(baidu_html)
        check("parse_baidu.count", len(hits) >= 5, "%d 条" % len(hits))
        check("parse_baidu.fields", all(
            h.title.strip() and h.url.strip() and h.snippet.strip()
            for h in hits[:5]), str([(bool(h.title), bool(h.url),
                                     bool(h.snippet)) for h in hits[:5]]))
        check("parse_baidu.urls", all(
            h.url.startswith(("http://", "https://"))
            and "baidu.com/s" not in h.url for h in hits), str(
            [h.url for h in hits[:3]]))
        check("parse_baidu.unique",
              len({h.url for h in hits}) == len(hits), "%d 条" % len(hits))
    else:
        skip("parse_baidu", "_t/mcp_fixtures/baidu.html 不存在")

    check("parse_bing.empty", parse_bing("") == [])
    check("parse_baidu.empty", parse_baidu("") == [])
    check("parse_bing.malformed", isinstance(
        parse_bing("<li class='b_algo'><h2><a href='x'>t"), list))

    encoded = base64.urlsafe_b64encode(
        b"https://example.com/a?b=1").decode().rstrip("=")
    jump = ("<li class=\"b_algo\"><h2><a href=\"https://www.bing.com/ck/a"
            "?!!&amp;&amp;p=x&amp;u=a1%s&amp;ntb=1\">跳转标题</a></h2>"
            "<p class=\"b_lineclamp2\">跳转摘要</p></li>" % encoded)
    jump_hits = parse_bing(jump)
    check("parse_bing.unwrap", len(jump_hits) == 1
          and jump_hits[0].url == "https://example.com/a?b=1",
          str(jump_hits))
    broken = parse_bing(
        "<li class='b_algo'><h2><a href='https://www.bing.com/ck/a"
        "?u=notbase64!!!'>坏链接</a></h2><p>摘要</p></li>")
    check("parse_bing.unwrap_fallback", len(broken) == 1
          and broken[0].url.startswith("https://www.bing.com/ck/a"),
          str(broken))


def test_api_providers() -> None:
    http = FakeHttp({
        "tavily": {"results": [{"title": "T 标题", "url": "https://e.example/t",
                                "content": "T 摘要"}]},
        "brave": {"web": {"results": [{"title": "B 标题",
                                       "url": "https://e.example/b",
                                       "description": "B 摘要"}]}},
        "serper": {"organic": [{"title": "S 标题",
                                "link": "https://e.example/s",
                                "snippet": "S 摘要"}]},
        "searx": {"results": [{"title": "X 标题",
                               "url": "https://e.example/x",
                               "content": "X 摘要"}]},
    })
    service = SearchService(http, api_key="fake-key",
                            searxng_url="https://searx.example",
                            allow_private=True, timeout=5.0)
    cases = [
        ("tavily", service._search_tavily("q", 5), "T 标题",
         "https://e.example/t", "T 摘要", "tavily"),
        ("brave", service._search_brave("q", 5), "B 标题",
         "https://e.example/b", "B 摘要", "brave"),
        ("serper", service._search_serper("q", 5), "S 标题",
         "https://e.example/s", "S 摘要", "serper"),
        ("searxng", service._search_searxng("q", 5), "X 标题",
         "https://e.example/x", "X 摘要", "searxng"),
    ]
    for name, hits, title, url, snippet, provider in cases:
        ok = (len(hits) == 1 and hits[0].title == title
              and hits[0].url == url and hits[0].snippet == snippet
              and hits[0].provider == provider)
        check("search.api.%s" % name, ok, str(hits))


def test_search_local(base: str, client: HttpClient) -> None:
    class LocalSearch(SearchService):
        def _search_bing_html(self, query: str, limit: int):
            return [
                SearchHit("深抓页面一", base + "/deep1", "本地一", "bing_html"),
                SearchHit("404 页面", base + "/404", "本地二", "bing_html"),
                SearchHit("深抓页面二", base + "/deep2", "本地三", "bing_html"),
            ][:limit]

        def _search_baidu_html(self, query: str, limit: int):
            return []

    service = LocalSearch(client, provider="bing_html", allow_private=True,
                          timeout=5.0)
    result = service.search("本地深搜", limit=5)
    check("search.local.hits", len(result.hits) == 3
          and result.provider == "bing_html" and result.error == "",
          "%d hits / %r" % (len(result.hits), result.error))
    check("search.local.took_ms", isinstance(result.took_ms, int)
          and result.took_ms >= 0, str(result.took_ms))

    deep = service.search("本地深搜", limit=3, deep=True, max_pages=3,
                          provider="bing_html")
    check("search.deep.pages", len(deep.pages) == 3,
          "%d pages" % len(deep.pages))
    if len(deep.pages) == 3:
        ok_pages = [p for p in deep.pages if not p.error]
        failed = [p for p in deep.pages if p.error]
        check("search.deep.content", len(ok_pages) == 2
              and all("本地页面" in p.markdown or "深抓页面" in p.markdown
                      for p in ok_pages)
              and all("已截断" not in p.markdown for p in ok_pages),
              str([(p.url, p.error, len(p.markdown))
                   for p in deep.pages]))
        check("search.deep.page_error", len(failed) == 1
              and "404" in failed[0].error,
              str([(p.url, p.error) for p in deep.pages]))

    short = service.fetch(base + "/deep1", max_chars=120)
    check("search.fetch.truncate", "已截断" in short.markdown
          and len(short.markdown) < 400, str(len(short.markdown)))

    auto = service.search("自动", limit=2, provider="auto")
    check("search.auto.fallback", len(auto.hits) == 2
          and auto.provider == "bing_html" and auto.error == "",
          "%r/%d" % (auto.provider, len(auto.hits)))

    class FilterSearch(SearchService):
        def _search_bing_html(self, query: str, limit: int):
            return [
                SearchHit("js", "javascript:void(0)", "", "bing_html"),
                SearchHit("站内", "https://cn.bing.com/search?q=x", "", "bing_html"),
                SearchHit("重复", base + "/deep1", "a", "bing_html"),
                SearchHit("重复2", base + "/deep1#frag", "b", "bing_html"),
                SearchHit("正常", base + "/deep2", "c", "bing_html"),
            ]

        def _search_baidu_html(self, query: str, limit: int):
            return []

    filtered = FilterSearch(client, provider="auto", allow_private=True,
                             timeout=5.0).search("过滤", limit=10)
    check("search.filter", [h.url for h in filtered.hits]
          == [base + "/deep1", base + "/deep2"],
          str([h.url for h in filtered.hits]))

    class FailSearch(SearchService):
        def _search_bing_html(self, query: str, limit: int):
            raise RuntimeError("模拟 Bing 失败")

        def _search_baidu_html(self, query: str, limit: int):
            raise RuntimeError("模拟百度失败")

    failed = FailSearch(client, provider="auto", allow_private=True,
                        timeout=5.0).search("无结果")
    check("search.all_fail", failed.hits == [] and "搜索失败" in failed.error
          and "模拟 Bing 失败" in failed.error, failed.error)

    empty = service.search("   ")
    check("search.empty_query", "不能为空" in empty.error, empty.error)
    unknown = service.search("x", provider="nope")
    check("search.unknown_provider", "未知搜索源" in unknown.error,
          unknown.error)

    limited = service.search("限制", limit=1, provider="bing_html")
    check("search.limit", len(limited.hits) == 1, "%d" % len(limited.hits))


def test_live_bing() -> None:
    """可选真实网络：失败打印 SKIP，不计 FAIL。"""
    try:
        result = SearchService(HttpClient(), provider="bing_html",
                               timeout=8.0).search("python asyncio", limit=3)
    except Exception as exc:
        skip("search.live_bing", "网络不可用：%r" % exc)
        return
    if result.hits:
        check("search.live_bing(optional)", True, "%d hits, %d ms"
              % (len(result.hits), result.took_ms))
    else:
        skip("search.live_bing", result.error or "无结果")


def main() -> int:
    print("== web services check ==")
    print("python", sys.version.split()[0], "root", ROOT)
    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = "http://127.0.0.1:%d" % port
    client = HttpClient()
    try:
        test_html_to_markdown()
        test_extract_url(base, client)
        test_parsers()
        test_api_providers()
        test_search_local(base, client)
        test_live_bing()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        try:
            client.close()
        except Exception:
            pass
    print("\n== result: %d passed, %d failed ==" % (len(PASSED), len(FAILED)))
    for name, detail in FAILED:
        print("FAILED:", name, "-", detail)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
