# -*- coding: utf-8 -*-
"""联网搜索：多 provider + 网页解析 + deep 页面抓取（mcp-block.md §3.5）。

Provider（``auto`` 顺序）：
``tavily`` → ``brave`` → ``serper`` → ``searxng`` → ``bing_html`` → ``baidu_html``；
前四个需要 Key/URL，后两个直接解析公开搜索页。

- 请求统一走 ``HttpClient.request``（不走缓存）；超时默认 20s；
- 结果 URL 去重、过滤站内搜索链接与 ``javascript:``；Bing ``ck/a`` 跳转
  尽量从 ``u=a1<base64url>`` 还原；
- ``deep=True``：前 ``max_pages`` 条用 ``extract_url`` 抓正文，线程池 3，
  单页 10s，每页截 8000 字符；单页失败只写该页 error，不影响整体；
- 任何失败都写 ``SearchResult.error``（中文），不抛异常。
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable

from .extract import extract_url

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
BING_SEARCH_URL = "https://cn.bing.com/search?q=%s&setlang=zh-CN"
BAIDU_SEARCH_URL = "https://www.baidu.com/s?wd=%s"
TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SERPER_URL = "https://google.serper.dev/search"

MAX_FETCH_BYTES = 5 * 1024 * 1024
PAGE_TIMEOUT = 10.0
DEEP_PAGE_CHARS = 8000
SEARCH_TOTAL_BUDGET = 55.0
MAX_PAGE_WORKERS = 3

_PROVIDER_ALIASES = {
    "": "auto",
    "auto": "auto",
    "tavily": "tavily",
    "brave": "brave",
    "serper": "serper",
    "searxng": "searxng",
    "searx": "searxng",
    "bing": "bing_html",
    "bing_html": "bing_html",
    "baidu": "baidu_html",
    "baidu_html": "baidu_html",
}

_PROVIDER_LABELS = {
    "tavily": "Tavily",
    "brave": "Brave",
    "serper": "Serper",
    "searxng": "SearXNG",
    "bing_html": "必应",
    "baidu_html": "百度",
}


class SearchError(RuntimeError):
    """单个 provider 的搜索失败（内部使用，最终汇总到 SearchResult.error）。"""


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    provider: str


@dataclass
class SearchPage:
    title: str
    url: str
    markdown: str
    error: str = ""


@dataclass
class SearchResult:
    query: str
    provider: str
    hits: list[SearchHit] = field(default_factory=list)
    pages: list[SearchPage] = field(default_factory=list)
    error: str = ""
    took_ms: int = 0


# ---------------- HTML 解析：流式、只认结果块 ----------------

def _squash(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def _classes(attrs: dict[str, str]) -> set[str]:
    return {c.lower() for c in (attrs.get("class") or "").split() if c}


def _unwrap_bing(url: str) -> str:
    """Bing 跳转链接 ``/ck/a?...&u=a1<base64url>`` -> 真实 URL；失败保留原样。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if "bing.com/ck/a" not in low and "/ck/a" not in low:
        return raw
    try:
        query = urllib.parse.urlsplit(raw).query
        params = urllib.parse.parse_qs(query)
        value = (params.get("u") or [""])[0]
        if not value:
            return raw
        if value.startswith("a1"):
            value = value[2:]
        value = value.replace("-", "+").replace("_", "/")
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="replace")
        if decoded.startswith(("http://", "https://")):
            return decoded
    except Exception:
        pass
    return raw


class _BingParser(HTMLParser):
    """解析 ``li.b_algo``：h2>a 标题/href + 首个 p 摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[SearchHit] = []
        self._active = False
        self._li_depth = 0
        self._in_h2 = False
        self._title: list[str] = []
        self._href = ""
        self._captures: list[tuple[int, list[str]]] = []
        self._p = False
        self._p_prio = 9
        self._p_parts: list[str] = []

    def _finalize(self) -> None:
        title = _squash("".join(self._title))
        href = _unwrap_bing(self._href)
        snippet = ""
        for _prio, parts in sorted(self._captures, key=lambda x: x[0]):
            candidate = _squash("".join(parts))
            if candidate:
                snippet = candidate
                break
        if title and href:
            self.hits.append(SearchHit(title=title, url=href,
                                       snippet=snippet, provider="bing_html"))
        self._active = False
        self._li_depth = 0
        self._in_h2 = False
        self._title = []
        self._href = ""
        self._captures = []
        self._p = False
        self._p_parts = []

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {(k or "").lower(): (v if v is not None else "")
                    for k, v in attrs}
        if not self._active:
            if tag == "li" and "b_algo" in _classes(attr_map):
                self._active = True
                self._li_depth = 1
            return
        if tag == "li":
            self._li_depth += 1
            return
        if tag in ("script", "style", "svg"):
            return
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._href:
            href = attr_map.get("href", "")
            if href:
                self._href = urllib.parse.urljoin("https://cn.bing.com/", href)
        elif tag == "p" and not self._p:
            classes = _classes(attr_map)
            self._p = True
            self._p_parts = []
            if any("lineclamp" in c or "snippet" in c for c in classes):
                self._p_prio = 0
            elif any("b_caption" in c for c in classes):
                self._p_prio = 1
            else:
                self._p_prio = 2

    def handle_startendtag(self, tag: str,
                           attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if not self._active:
            return
        if tag == "li":
            self._li_depth -= 1
            if self._li_depth <= 0:
                self._finalize()
                return
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "p" and self._p:
            self._captures.append((self._p_prio, self._p_parts))
            self._p = False
            self._p_parts = []

    def handle_data(self, data: str) -> None:
        if not self._active or not data:
            return
        if self._in_h2:
            self._title.append(data)
        if self._p:
            self._p_parts.append(data)


def parse_bing(html: str) -> list[SearchHit]:
    """解析 Bing 搜索结果页 HTML，返回 SearchHit 列表（失败返回 []）。"""
    parser = _BingParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return parser.hits
    return parser.hits


class _BaiduParser(HTMLParser):
    """解析 ``div.c-container``：h3>a 标题/href + abstract 摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[SearchHit] = []
        self._active = False
        self._div_depth = 0
        self._in_h3 = False
        self._title: list[str] = []
        self._href = ""
        # 摘要抓取：{"tag":..., "depth":..., "prio":..., "parts":[...]}
        self._cap: dict[str, Any] | None = None
        self._snippets: list[tuple[int, list[str]]] = []
        self._texts: list[str] = []

    def _reset(self) -> None:
        self._active = False
        self._div_depth = 0
        self._in_h3 = False
        self._title = []
        self._href = ""
        self._cap = None
        self._snippets = []
        self._texts = []

    def _finalize(self) -> None:
        title = _squash("".join(self._title))
        href = (self._href or "").strip()
        if href:
            href = urllib.parse.urljoin("https://www.baidu.com/", href)
        snippet = ""
        for _prio, parts in sorted(self._snippets, key=lambda x: x[0]):
            candidate = _squash("".join(parts))
            if candidate:
                snippet = candidate
                break
        if not snippet:
            # 特殊卡片（百科/视频）摘要由 JS 渲染：退回块内可见文本
            fallback = _squash("".join(self._texts))[:300]
            snippet = fallback
        if title and href:
            self.hits.append(SearchHit(title=title, url=href,
                                       snippet=snippet, provider="baidu_html"))
        self._reset()

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {(k or "").lower(): (v if v is not None else "")
                    for k, v in attrs}
        if not self._active:
            if tag == "div" and "c-container" in _classes(attr_map):
                self._active = True
                self._div_depth = 1
            return
        if tag == "div":
            self._div_depth += 1
        if self._cap is not None:
            if tag == self._cap["tag"]:
                self._cap["depth"] += 1
            return
        if tag in ("script", "style", "svg"):
            return
        if tag == "h3":
            self._in_h3 = True
        elif tag == "a" and self._in_h3 and not self._href:
            href = attr_map.get("href", "")
            if href and not href.lower().startswith("javascript:"):
                self._href = href
        # 摘要容器：新版 data-module=abstract / summary-text，旧版 c-abstract
        priority: int | None = None
        if tag == "div" and attr_map.get("data-module") == "abstract":
            priority = 0
        elif tag == "div" and any("c-abstract" in c for c in _classes(attr_map)):
            priority = 1
        elif tag == "span" and any("summary-text" in c
                                   for c in _classes(attr_map)):
            priority = 0
        elif tag == "div" and any(c in _classes(attr_map) for c in
                                  ("content-right", "abstract")):
            priority = 2
        if priority is not None:
            self._cap = {"tag": tag, "depth": 1, "prio": priority,
                         "parts": []}

    def handle_startendtag(self, tag: str,
                           attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if not self._active:
            return
        if self._cap is not None and tag == self._cap["tag"]:
            self._cap["depth"] -= 1
            if self._cap["depth"] <= 0:
                self._snippets.append((self._cap["prio"],
                                       self._cap["parts"]))
                self._cap = None
        if tag == "h3":
            self._in_h3 = False
        if tag == "div":
            self._div_depth -= 1
            if self._div_depth <= 0:
                self._finalize()

    def handle_data(self, data: str) -> None:
        if not self._active or not data:
            return
        if self._in_h3:
            self._title.append(data)
        else:
            self._texts.append(data)
        if self._cap is not None:
            self._cap["parts"].append(data)


def parse_baidu(html: str) -> list[SearchHit]:
    """解析百度搜索结果页 HTML（含跳转链接），返回 SearchHit 列表。"""
    parser = _BaiduParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return parser.hits
    return parser.hits


# ---------------- 结果清理 ----------------

def _is_search_internal(url: str) -> bool:
    """过滤搜索引擎自身的搜索页/广告跳转页。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return True
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return True
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if host.endswith("bing.com"):
        if path.startswith("/search") or path.startswith("/images/"):
            return True
        if path.startswith(("/aclick", "/aclk", "/adwise")):
            return True
    if host.endswith("baidu.com"):
        if path.startswith("/s") or path.startswith("/baidu"):
            return True
    if host.endswith("google.com") and path.startswith("/search"):
        return True
    return False


def _clean_hits(hits: list[SearchHit], provider: str,
                limit: int) -> list[SearchHit]:
    out: list[SearchHit] = []
    seen: set[str] = set()
    for hit in hits:
        title = _squash(hit.title)
        url = (hit.url or "").strip()
        if not title or not url:
            continue
        if url.lower().startswith("javascript:"):
            continue
        if _is_search_internal(url):
            continue
        key = url.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(SearchHit(title=title, url=key,
                             snippet=_squash(hit.snippet),
                             provider=hit.provider or provider))
        if 0 < limit <= len(out):
            break
    return out


# ---------------- 服务 ----------------

class SearchService:
    """多源搜索 + deep 抓取；无 GUI，可在任意线程调用。"""

    def __init__(self, http: Any, *, provider: str = "auto", api_key: str = "",
                 searxng_url: str = "", allow_private: bool = False,
                 timeout: float = 20.0) -> None:
        self._http = http
        self.provider = provider or "auto"
        self.api_key = api_key or ""
        self.searxng_url = searxng_url or ""
        self.allow_private = bool(allow_private)
        self.timeout = float(timeout)

    # -------- 底层请求 --------

    def _request(self, url: str, *, headers: dict[str, str] | None = None,
                 method: str = "GET", body: bytes | None = None,
                 timeout: float | None = None) -> str:
        hdrs = dict(BASE_HEADERS)
        if headers:
            hdrs.update(headers)
        request_fn = getattr(self._http, "request", None)
        if callable(request_fn):
            resp = request_fn(url, method=method, headers=hdrs, body=body,
                              timeout=timeout if timeout is not None
                              else self.timeout,
                              allow_private=self.allow_private,
                              max_bytes=MAX_FETCH_BYTES)
        else:
            resp = self._http.get_text(url, headers=hdrs,
                                       timeout=timeout if timeout is not None
                                       else self.timeout,
                                       cache_ttl=0,
                                       allow_private=self.allow_private)
        status = int(getattr(resp, "status", 0) or 0)
        if status >= 400:
            raise SearchError("HTTP %d" % status)
        return resp.text()

    def _request_json(self, url: str, *, headers: dict[str, str] | None = None,
                      method: str = "GET", payload: Any = None,
                      timeout: float | None = None) -> Any:
        hdrs = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        text = self._request(url, headers=hdrs, method=method, body=body,
                             timeout=timeout)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise SearchError("返回内容不是有效 JSON") from exc

    # -------- provider 实现 --------

    def _search_tavily(self, query: str, limit: int) -> list[SearchHit]:
        data = self._request_json(
            TAVILY_URL, method="POST",
            headers={"Authorization": "Bearer %s" % self.api_key},
            payload={"api_key": self.api_key, "query": query,
                     "max_results": limit, "search_depth": "basic"})
        hits: list[SearchHit] = []
        for item in (data.get("results") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            hits.append(SearchHit(title=str(item.get("title") or ""),
                                  url=str(item.get("url") or ""),
                                  snippet=str(item.get("content") or ""),
                                  provider="tavily"))
        return hits

    def _search_brave(self, query: str, limit: int) -> list[SearchHit]:
        url = "%s?%s" % (BRAVE_URL, urllib.parse.urlencode(
            {"q": query, "count": max(1, min(limit, 20))}))
        data = self._request_json(
            url, headers={"X-Subscription-Token": self.api_key,
                          "Accept": "application/json"})
        hits: list[SearchHit] = []
        web = data.get("web") if isinstance(data, dict) else None
        results = (web or {}).get("results") if isinstance(web, dict) else []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            hits.append(SearchHit(title=str(item.get("title") or ""),
                                  url=str(item.get("url") or ""),
                                  snippet=str(item.get("description") or ""),
                                  provider="brave"))
        return hits

    def _search_serper(self, query: str, limit: int) -> list[SearchHit]:
        data = self._request_json(
            SERPER_URL, method="POST",
            headers={"X-API-KEY": self.api_key,
                     "Accept": "application/json"},
            payload={"q": query, "num": limit})
        hits: list[SearchHit] = []
        for item in (data.get("organic") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            hits.append(SearchHit(title=str(item.get("title") or ""),
                                  url=str(item.get("link") or ""),
                                  snippet=str(item.get("snippet") or ""),
                                  provider="serper"))
        return hits

    def _search_searxng(self, query: str, limit: int) -> list[SearchHit]:
        base = self.searxng_url.rstrip("/")
        if not base:
            raise SearchError("未配置 SearXNG 地址")
        endpoint = base if base.endswith("/search") else base + "/search"
        url = "%s?%s" % (endpoint, urllib.parse.urlencode(
            {"q": query, "format": "json", "language": "zh-CN"}))
        data = self._request_json(url, headers={"Accept": "application/json"})
        hits: list[SearchHit] = []
        for item in (data.get("results") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            hits.append(SearchHit(title=str(item.get("title") or ""),
                                  url=str(item.get("url") or ""),
                                  snippet=str(item.get("content") or ""),
                                  provider="searxng"))
        return hits

    def _search_bing_html(self, query: str, _limit: int) -> list[SearchHit]:
        url = BING_SEARCH_URL % urllib.parse.quote_plus(query)
        return parse_bing(self._request(url))

    def _search_baidu_html(self, query: str, _limit: int) -> list[SearchHit]:
        url = BAIDU_SEARCH_URL % urllib.parse.quote_plus(query)
        return parse_baidu(self._request(url))

    # -------- 调度 --------

    def _resolve_provider(self, provider: str) -> str:
        name = (provider or "auto").strip().lower()
        if name not in _PROVIDER_ALIASES:
            raise SearchError("未知搜索源：%s" % provider)
        return _PROVIDER_ALIASES[name]

    def _provider_chain(self, provider: str, query: str = ""
                        ) -> tuple[list[str], list[str]]:
        """返回 (可尝试的 provider 列表, 提前跳过的原因列表)。

        顺序：tavily → brave → serper → searxng → 网页兜底。网页兜底里
        中文查询优先百度（必应中文长尾词经常回落到泛化结果），英文/代码查询
        优先必应；一家失败/无结果自动试下一家。
        """
        if provider != "auto":
            return [provider], []
        chain: list[str] = []
        skipped: list[str] = []
        if self.api_key:
            chain.extend(["tavily", "brave", "serper"])
        else:
            skipped.append("未配置搜索 API Key（Tavily/Brave/Serper 已跳过）")
        if self.searxng_url:
            chain.append("searxng")
        else:
            skipped.append("未配置 SearXNG 地址")
        cjk = sum(1 for ch in query if "\u4e00" <= ch <= "\u9fff")
        if cjk >= max(2, len(query) // 6):
            chain.extend(["baidu_html", "bing_html"])
        else:
            chain.extend(["bing_html", "baidu_html"])
        return chain, skipped

    def _run_provider(self, name: str, query: str, limit: int) -> list[SearchHit]:
        fn: Callable[[str, int], list[SearchHit]] = {
            "tavily": self._search_tavily,
            "brave": self._search_brave,
            "serper": self._search_serper,
            "searxng": self._search_searxng,
            "bing_html": self._search_bing_html,
            "baidu_html": self._search_baidu_html,
        }[name]
        return fn(query, limit)

    def search(self, query: str, *, limit: int = 8, deep: bool = False,
               max_pages: int = 3, provider: str = "auto") -> SearchResult:
        """搜索；失败时 ``error`` 写中文原因，不抛异常。"""
        started = time.monotonic()
        query = (query or "").strip()
        result = SearchResult(query=query, provider="")
        if not query:
            result.error = "搜索关键词不能为空"
            return result
        limit = max(1, int(limit))
        # 调用方默认传 "auto" 时回落到服务实例配置（面板里选的 provider）
        requested = (provider or "").strip().lower()
        if requested in ("", "auto"):
            requested = (self.provider or "auto").strip().lower()
        try:
            name = self._resolve_provider(requested)
        except SearchError as exc:
            result.error = str(exc)
            return result

        chain, skipped = self._provider_chain(name, query)
        errors: list[str] = list(skipped)
        hits: list[SearchHit] = []
        chosen = ""
        for candidate in chain:
            label = _PROVIDER_LABELS.get(candidate, candidate)
            try:
                raw = self._run_provider(candidate, query, limit)
            except Exception as exc:
                detail = str(exc).strip() or exc.__class__.__name__
                errors.append("%s：%s" % (label, detail))
                continue
            cleaned = _clean_hits(raw, candidate, limit)
            if cleaned:
                hits = cleaned
                chosen = candidate
                break
            errors.append("%s：无结果" % label)

        result.hits = hits
        result.provider = chosen
        if not hits:
            result.error = "搜索失败：" + "；".join(errors)
            result.took_ms = int((time.monotonic() - started) * 1000)
            return result

        if deep and max_pages > 0:
            deadline = started + SEARCH_TOTAL_BUDGET
            result.pages = self._deep_pages(hits, int(max_pages), deadline)
        result.took_ms = int((time.monotonic() - started) * 1000)
        return result

    # -------- deep 抓取 --------

    def _deep_pages(self, hits: list[SearchHit], max_pages: int,
                    deadline: float) -> list[SearchPage]:
        targets = [h for h in hits[:max(0, max_pages)]
                   if h.url.lower().startswith(("http://", "https://"))]
        if not targets:
            return []
        pages: list[SearchPage | None] = [None] * len(targets)
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_PAGE_WORKERS, thread_name_prefix="vbhelper-search")
        futures = {
            executor.submit(self.fetch, hit.url, max_chars=DEEP_PAGE_CHARS): idx
            for idx, hit in enumerate(targets)}
        try:
            remaining = max(0.1, deadline - time.monotonic())
            done, not_done = concurrent.futures.wait(futures, timeout=remaining)
            for future in done:
                idx = futures[future]
                try:
                    pages[idx] = future.result()
                except Exception as exc:
                    pages[idx] = SearchPage(title=targets[idx].title,
                                            url=targets[idx].url,
                                            markdown="",
                                            error="抓取失败：%s" % exc)
            for future in not_done:
                future.cancel()
                idx = futures[future]
                pages[idx] = SearchPage(title=targets[idx].title,
                                        url=targets[idx].url, markdown="",
                                        error="抓取超时")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return [page for page in pages if page is not None]

    def fetch(self, url: str, *, max_chars: int = 20000) -> SearchPage:
        """抓单页正文（deep 用）；失败写 error 不抛。"""
        page = extract_url(url, self._http, max_bytes=MAX_FETCH_BYTES,
                           allow_private=self.allow_private,
                           timeout=PAGE_TIMEOUT)
        markdown = page.markdown or page.text
        if not page.ok:
            return SearchPage(title=page.title or url, url=url,
                              markdown="", error=page.error or "抓取失败")
        total = len(markdown)
        if total > max_chars > 0:
            markdown = (markdown[:max_chars]
                        + "\n\n…（已截断，原文 %d 字符）" % total)
        error = "正文为空" if not markdown.strip() else ""
        return SearchPage(title=page.title or url, url=url,
                          markdown=markdown, error=error)
