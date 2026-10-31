# -*- coding: utf-8 -*-
"""Web 服务包：正文提取（HTML -> Markdown）与多源搜索。

只依赖标准库 + 传入的 HttpClient；不 import Tk。
"""
from .extract import ExtractedPage, extract_url, html_to_markdown
from .search import (SearchHit, SearchPage, SearchResult, SearchService,
                     parse_baidu, parse_bing)

__all__ = [
    "ExtractedPage", "SearchHit", "SearchPage", "SearchResult",
    "SearchService", "extract_url", "html_to_markdown", "parse_baidu",
    "parse_bing",
]
