# -*- coding: utf-8 -*-
"""网页正文提取：纯标准库 HTMLParser -> Markdown（mcp-block.md §3.4）。

流程：
1. 宽容的树构建器（忽略 script/style/nav/footer/header/aside/form/noscript/svg
   等噪声子树，实体自动反转义）；表结构按标签容错闭合，能处理多数残缺 HTML；
2. 候选正文打分：文本长度 + 标点/中文比例加分 - 链接密度扣分 +
   class/id 命中 content/article/main/post/body 加分；
3. Markdown 渲染：标题/段落/列表/代码块/引用/分隔线/链接/图片 alt；
   连续空行压到最多一个；
4. ``text`` 是纯文本（供搜索摘要/去重），``markdown`` 是渲染结果。

``extract_url`` 走传入的 HttpClient（``request`` 优先，兼容 ``get_text``），
非 HTML 返回截断文本；任何网络/HTTP 错误都转成 ``ok=False, error=中文``，不抛。
"""
from __future__ import annotations

import html as html_module
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

DEFAULT_MAX_BYTES = 5_242_880
DEFAULT_TIMEOUT = 15.0
NON_HTML_TRUNCATE = 20_000

VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# 整段丢弃：导航/页眉页脚/表单/脚本样式等
DROP_TAGS = frozenset({
    "script", "style", "nav", "footer", "header", "aside", "form",
    "noscript", "svg", "template", "iframe", "object", "canvas", "select",
    "textarea", "button", "dialog",
})

BLOCK_CONTAINERS = frozenset({
    "div", "section", "article", "main", "figure", "figcaption", "dl",
    "dd", "dt", "address", "details", "summary",
})

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# 遇到这些开始标签时，自动闭合栈顶的同类/段落
_AUTO_CLOSE = {
    "li": {"li"},
    "p": {"p"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr", "td", "th"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "option": {"option"},
}
_P_CLOSERS = frozenset({
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "main", "nav", "ol", "p", "pre", "section", "table", "ul",
})

_CONTENT_HINTS = ("articlecontent", "article-content", "article_body",
                  "articlebody", "post-content", "post_body", "postbody",
                  "entry-content", "entry_content", "content", "article",
                  "main", "markdown", "body")
_BAD_HINTS = ("comment", "sidebar", "footer", "navbar", "menu", "share",
              "related", "recommend", "breadcrumb")

_LANG_RE = re.compile(r"(?:language|lang)[-_]([A-Za-z0-9_+#.-]+)", re.I)


# ---------------- 极简 DOM ----------------

class _Text:
    __slots__ = ("data",)

    def __init__(self, data: str) -> None:
        self.data = data


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str],
                 parent: "_Node | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Any] = []
        self.parent = parent

    def classes(self) -> set[str]:
        raw = (self.attrs.get("class") or "") + " " + (self.attrs.get("id") or "")
        return {c.lower() for c in raw.split() if c}


class _TreeBuilder(HTMLParser):
    """宽容树构建器：丢弃噪声子树、自动闭合常见残缺标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {}, None)
        self._stack: list[_Node] = [self.root]
        self._drop_stack: list[str] = []
        self.failed = False

    # -- 内部 --

    def _current(self) -> _Node:
        return self._stack[-1]

    def _pop_to(self, tag: str) -> bool:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return True
        return False

    # -- HTMLParser 回调 --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._drop_stack:
            if tag in DROP_TAGS:
                self._drop_stack.append(tag)
            return
        if tag in DROP_TAGS:
            self._drop_stack.append(tag)
            return
        # 自动闭合
        closes = _AUTO_CLOSE.get(tag)
        if closes and self._current().tag in closes:
            self._stack.pop()
        if tag in _P_CLOSERS and self._current().tag == "p":
            self._stack.pop()
        attr_map: dict[str, str] = {}
        for key, value in attrs:
            attr_map[key.lower()] = value if value is not None else ""
        node = _Node(tag, attr_map, self._current())
        self._current().children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str,
                           attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._drop_stack:
            return
        if tag in DROP_TAGS:
            return
        attr_map = {(k or "").lower(): (v if v is not None else "")
                    for k, v in attrs}
        self._current().children.append(_Node(tag, attr_map, self._current()))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop_stack:
            if tag in self._drop_stack:
                while self._drop_stack:
                    popped = self._drop_stack.pop()
                    if popped == tag:
                        break
            return
        self._pop_to(tag)

    def handle_data(self, data: str) -> None:
        if self._drop_stack or not data:
            return
        node = self._current()
        if node.children and isinstance(node.children[-1], _Text):
            node.children[-1].data += data
        else:
            node.children.append(_Text(data))


# ---------------- 树遍历工具 ----------------

def _iter_nodes(node: _Node, depth: int = 0):
    for child in node.children:
        if isinstance(child, _Node):
            yield child, depth
            yield from _iter_nodes(child, depth + 1)


def _find_tag(node: _Node, tag: str) -> _Node | None:
    for child, _depth in _iter_nodes(node):
        if child.tag == tag:
            return child
    return None


def _node_text(node: _Node, depth: int = 0) -> str:
    """节点内全部文本（忽略噪声标签），不做空白压缩。"""
    if depth > 400:
        return ""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, _Text):
            parts.append(child.data)
        elif child.tag not in DROP_TAGS:
            parts.append(_node_text(child, depth + 1))
            if child.tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3",
                             "h4", "h5", "h6", "pre"):
                parts.append("\n")
    return "".join(parts)


def _squash(text: str) -> str:
    """折叠空白：行内连续空白变一个空格，换行变空格（段落另起 block）。"""
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", " ", text)
    return text.strip()


def _plain_text(node: _Node) -> str:
    raw = _node_text(node)
    raw = raw.replace("\xa0", " ").replace("\u3000", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.splitlines()]
    out: list[str] = []
    blank = False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


# ---------------- 打分 ----------------

def _measure(node: _Node, memo: dict[int, tuple[int, int, int]],
             depth: int = 0) -> tuple[int, int, int]:
    """返回 (文本长度, 链接内文本长度, 标点数)；结果缓存。"""
    key = id(node)
    cached = memo.get(key)
    if cached is not None:
        return cached
    if depth > 400 or node.tag in DROP_TAGS:
        memo[key] = (0, 0, 0)
        return 0, 0, 0
    text_len = 0
    link_len = 0
    punct = 0
    for child in node.children:
        if isinstance(child, _Text):
            data = child.data
            text_len += len(data.strip())
            punct += sum(data.count(c) for c in
                         "。，、；：！？,.;:!?…—")
        else:
            c_len, c_link, c_punct = _measure(child, memo, depth + 1)
            text_len += c_len
            punct += c_punct
            link_len += c_link
            if child.tag == "a":
                link_len += c_len
    memo[key] = (text_len, link_len, punct)
    return memo[key]


def _score(node: _Node, memo: dict[int, tuple[int, int, int]]) -> float:
    text_len, link_len, punct = _measure(node, memo)
    if text_len < 60:
        return -1.0
    density = 1.0 if text_len <= 0 else min(1.0, link_len / text_len)
    score = float(text_len)
    score += min(400.0, punct * 6.0)          # 标点/句子密度加分
    score -= text_len * density * 0.9         # 链接密度扣分
    hints = " ".join(node.classes())
    if any(h in hints for h in _CONTENT_HINTS):
        score += 250.0
    if any(h in hints for h in _BAD_HINTS):
        score -= 300.0
    return score


def _choose_content(body: _Node) -> _Node:
    """在 article/main/div/section 候选里选分最高者，失败回退 body。"""
    memo: dict[int, tuple[int, int, int]] = {}
    best: _Node | None = None
    best_score = 0.0
    for node, _depth in _iter_nodes(body):
        if node.tag not in ("article", "main", "div", "section"):
            continue
        value = _score(node, memo)
        if value > best_score:
            best_score, best = value, node
    if best is None or best_score <= 0:
        return body
    # 选中的块文本太少时宁可回退整页，避免抓到侧栏小卡片
    text_len, _link, _punct = _measure(best, memo)
    if text_len < 80:
        return body
    return best


# ---------------- Markdown 渲染 ----------------

def _absolute(base_url: str, href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    low = href.lower()
    if low.startswith(("javascript:", "data:", "mailto:", "tel:", "#")):
        return ""
    if base_url:
        try:
            return urllib.parse.urljoin(base_url, href)
        except Exception:
            return href
    return href


def _inline(node: _Node, base_url: str, depth: int = 0) -> str:
    """把节点渲染成行内 Markdown 文本（跳过嵌套列表/噪声标签）。"""
    if depth > 200:
        return ""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, _Text):
            parts.append(child.data)
            continue
        tag = child.tag
        if tag in DROP_TAGS or tag in ("ul", "ol"):
            continue
        if tag == "br":
            parts.append(" ")
        elif tag == "img":
            alt = _squash(child.attrs.get("alt", ""))
            src = _absolute(base_url, child.attrs.get("src", ""))
            if src:
                parts.append("![%s](%s)" % (alt, src))
            elif alt:
                parts.append("![%s]" % alt)
        elif tag == "a":
            text = _squash(_inline(child, base_url, depth + 1))
            href = _absolute(base_url, child.attrs.get("href", ""))
            if href and text:
                parts.append("[%s](%s)" % (text, href))
            elif text:
                parts.append(text)
        elif tag == "code":
            raw = _squash(_node_text(child, depth + 1))
            if raw:
                parts.append("`%s`" % raw.replace("`", "'"))
        elif tag in ("strong", "b"):
            inner = _squash(_inline(child, base_url, depth + 1))
            if inner:
                parts.append("**%s**" % inner)
        elif tag in ("em", "i"):
            inner = _squash(_inline(child, base_url, depth + 1))
            if inner:
                parts.append("*%s*" % inner)
        else:
            parts.append(_inline(child, base_url, depth + 1))
    return "".join(parts)


def _raw_text(node: _Node, depth: int = 0) -> str:
    """保留原始空白（pre/code 用）。"""
    if depth > 200:
        return ""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, _Text):
            parts.append(child.data)
        elif child.tag not in DROP_TAGS:
            parts.append(_raw_text(child, depth + 1))
            if child.tag == "br":
                parts.append("\n")
    return "".join(parts)


def _code_lang(node: _Node) -> str:
    candidates = [node.attrs.get("class", "")]
    for child, _depth in _iter_nodes(node):
        if child.tag == "code":
            candidates.append(child.attrs.get("class", ""))
            break
    for raw in candidates:
        match = _LANG_RE.search(raw or "")
        if match:
            return match.group(1).lower()
    return ""


class _Renderer:
    """块级 Markdown 渲染器：段落/列表/代码/引用/标题/表格纯文本。"""

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url
        self.blocks: list[str] = []
        self.buf: list[str] = []

    def flush(self) -> None:
        text = _squash("".join(self.buf))
        self.buf.clear()
        if text:
            self.blocks.append(text)

    def add_block(self, text: str) -> None:
        self.flush()
        text = (text or "").strip("\n").rstrip()
        if text.strip():
            self.blocks.append(text)

    def render(self, node: _Node) -> str:
        self.walk(node)
        self.flush()
        return _cleanup("\n\n".join(self.blocks))

    def walk(self, node: _Node, depth: int = 0) -> None:
        if depth > 300:
            return
        for child in node.children:
            if isinstance(child, _Text):
                self.buf.append(child.data)
            else:
                self.element(child, depth + 1)

    def element(self, node: _Node, depth: int = 0) -> None:
        tag = node.tag
        if tag in DROP_TAGS:
            return
        if tag in HEADINGS:
            text = _squash(_inline(node, self.base_url))
            if text:
                self.add_block("#" * HEADINGS[tag] + " " + text)
        elif tag == "p":
            self.flush()
            self.walk(node, depth)
            self.flush()
        elif tag == "pre":
            raw = _raw_text(node).strip("\n")
            if raw.strip():
                lang = _code_lang(node)
                self.add_block("```%s\n%s\n```" % (lang, raw.rstrip()))
        elif tag == "code":
            raw = _squash(_raw_text(node))
            if raw:
                self.buf.append("`%s`" % raw.replace("`", "'"))
        elif tag in ("strong", "b"):
            inner = _squash(_inline(node, self.base_url))
            if inner:
                self.buf.append("**%s**" % inner)
        elif tag in ("em", "i"):
            inner = _squash(_inline(node, self.base_url))
            if inner:
                self.buf.append("*%s*" % inner)
        elif tag == "a":
            text = _squash(_inline(node, self.base_url))
            href = _absolute(self.base_url, node.attrs.get("href", ""))
            if href and text:
                self.buf.append("[%s](%s)" % (text, href))
            elif text:
                self.buf.append(text)
        elif tag == "img":
            alt = _squash(node.attrs.get("alt", ""))
            src = _absolute(self.base_url,
                            node.attrs.get("src")
                            or node.attrs.get("data-src", ""))
            if src:
                self.buf.append("![%s](%s)" % (alt, src))
            elif alt:
                self.buf.append("![%s]" % alt)
        elif tag in ("ul", "ol"):
            self.flush()
            self._render_list(node, ordered=(tag == "ol"), indent=0)
        elif tag == "li":
            self.flush()
            text = _squash(_inline(node, self.base_url))
            if text:
                self.blocks.append("- " + text)
        elif tag == "blockquote":
            self.flush()
            sub = _Renderer(self.base_url)
            sub.walk(node)
            sub.flush()
            body = "\n\n".join(sub.blocks).strip()
            if body:
                self.blocks.append("\n".join(
                    "> " + line if line.strip() else ">"
                    for line in body.splitlines()))
        elif tag == "hr":
            self.flush()
            self.blocks.append("---")
        elif tag == "br":
            self.buf.append(" ")
        elif tag == "table":
            self.flush()
            text = _plain_text(node)
            if text:
                self.blocks.append(text)
        elif tag in ("tr", "td", "th", "thead", "tbody", "tfoot"):
            return                          # 表格整体处理
        elif tag in BLOCK_CONTAINERS:
            self.flush()
            self.walk(node, depth)
            self.flush()
        else:
            self.walk(node, depth)

    def _render_list(self, node: _Node, *, ordered: bool, indent: int) -> None:
        lines = self._list_lines(node, ordered=ordered, indent=indent)
        if lines:
            self.blocks.append("\n".join(lines))

    def _list_lines(self, node: _Node, *, ordered: bool,
                    indent: int) -> list[str]:
        """列表整体作为一个 block（行间不插空行，嵌套列表才能正确缩进）。"""
        lines: list[str] = []
        index = 1
        for child in node.children:
            if not isinstance(child, _Node):
                continue
            if child.tag == "li":
                content = _squash(_inline(child, self.base_url))
                prefix = "%d. " % index if ordered else "- "
                if content:
                    lines.append("  " * indent + prefix + content)
                for grand in child.children:
                    if (isinstance(grand, _Node)
                            and grand.tag in ("ul", "ol")):
                        lines.extend(self._list_lines(
                            grand, ordered=(grand.tag == "ol"),
                            indent=indent + 1))
                index += 1
            elif child.tag in ("ul", "ol"):
                lines.extend(self._list_lines(child,
                                              ordered=(child.tag == "ol"),
                                              indent=indent + 1))
        return lines


def _cleanup(markdown: str) -> str:
    """行尾去空白；代码块外连续空行压到最多一个。"""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_fence = False
    blank = False
    for line in lines:
        stripped = line.rstrip()
        if stripped.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and not stripped.strip():
            if not blank and out:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(stripped)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out).strip()


# ---------------- 对外 API ----------------

@dataclass
class ExtractedPage:
    url: str
    title: str
    markdown: str
    text: str
    ok: bool
    error: str = ""
    byline: str = ""


def _meta_content(root: _Node, names: tuple[str, ...]) -> str:
    for node, _depth in _iter_nodes(root):
        if node.tag != "meta":
            continue
        key = (node.attrs.get("name") or "").lower()
        prop = (node.attrs.get("property") or "").lower()
        if key in names or prop in names:
            value = _squash(node.attrs.get("content", ""))
            if value:
                return value
    return ""


def _page_title(root: _Node) -> str:
    title_node = _find_tag(root, "title")
    if title_node is not None:
        title = _squash(_node_text(title_node))
        if title:
            return title
    meta = _meta_content(root, ("og:title", "twitter:title"))
    if meta:
        return meta
    for level in ("h1", "h2"):
        for node, _depth in _iter_nodes(root):
            if node.tag == level:
                text = _squash(_node_text(node))
                if text:
                    return text
    return ""


def html_to_markdown(html: str, base_url: str = "") -> ExtractedPage:
    """HTML 字符串 -> ExtractedPage（正文 Markdown + 纯文本）。

    找不到正文时返回 ``ok=True, markdown="", error="empty"``，不抛异常。
    """
    builder = _TreeBuilder()
    try:
        builder.feed(html or "")
        builder.close()
    except Exception:
        pass                                    # 残缺输入尽量已解析部分
    root = builder.root
    title = _page_title(root)
    body = _find_tag(root, "body") or root
    content = _choose_content(body)
    renderer = _Renderer(base_url)
    try:
        markdown = renderer.render(content)
    except RecursionError:
        markdown = ""
    text = _plain_text(content)
    byline = (_meta_content(root, ("author", "article:author",
                                   "og:article:author"))
              or _meta_content(root, ("byline",)))
    error = ""
    if not markdown.strip() and not text.strip():
        error = "empty"
    return ExtractedPage(url=base_url, title=title, markdown=markdown,
                         text=text, ok=True, error=error, byline=byline)


_META_CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.I)


def _decode_body(resp: Any) -> str:
    """解码响应体。

    注意：共享的 ``HttpClient.request`` 目前丢失响应头（content_type 为空），
    所以这里在 content_type 缺失时先嗅探前 4KB 的 ``charset=``（meta/XML 声明），
    再回退 ``resp.text()`` / UTF-8。
    """
    raw = getattr(resp, "body", b"") or b""
    content_type = (getattr(resp, "content_type", "") or "").lower()
    encoding = ""
    if "charset=" in content_type:
        encoding = content_type.split("charset=", 1)[1].split(";")[0].strip()
    if not encoding and raw:
        match = _META_CHARSET_RE.search(raw[:4096])
        if match:
            encoding = match.group(1).decode("ascii", errors="ignore").lower()
    if encoding:
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            pass
    try:
        return resp.text()
    except Exception:
        return raw.decode("utf-8", errors="replace")


def extract_url(url: str, http: Any, *, max_bytes: int = DEFAULT_MAX_BYTES,
                allow_private: bool = False,
                timeout: float = DEFAULT_TIMEOUT) -> ExtractedPage:
    """抓取 URL 并提取正文；网络/HTTP 错误转 ok=False，不抛异常。"""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        request_fn = getattr(http, "request", None)
        if callable(request_fn):
            resp = request_fn(url, method="GET", headers=headers,
                              timeout=timeout, allow_private=allow_private,
                              max_bytes=max_bytes)
        else:                                   # 兼容只实现 get_text 的桩
            resp = http.get_text(url, headers=headers, timeout=timeout,
                                 cache_ttl=0, allow_private=allow_private)
    except Exception as exc:
        return ExtractedPage(url=url, title="", markdown="", text="",
                             ok=False, error="抓取失败：%s" % exc)
    status = int(getattr(resp, "status", 0) or 0)
    if status >= 400:
        return ExtractedPage(url=url, title="", markdown="", text="",
                             ok=False, error="请求失败：HTTP %d" % status)
    final_url = getattr(resp, "url", "") or url
    content_type = (getattr(resp, "content_type", "") or "").lower()
    body = getattr(resp, "body", b"") or b""
    body_text = _decode_body(resp)
    is_html = ("html" in content_type or "xml" in content_type
               or (not content_type and body_text.lstrip()[:1] == "<"))
    if not is_html:
        text = body_text[:NON_HTML_TRUNCATE]
        title = urllib.parse.urlsplit(final_url).path.rsplit("/", 1)[-1] or final_url
        return ExtractedPage(url=final_url, title=title, markdown=text,
                             text=text, ok=True)
    page = html_to_markdown(body_text, base_url=final_url)
    if not page.title:
        page.title = urllib.parse.urlsplit(final_url).netloc or final_url
    return page


def html_unescape(text: str) -> str:
    """实体反转义（供外部/测试使用；解析器内部已自动反转义）。"""
    return html_module.unescape(text)
