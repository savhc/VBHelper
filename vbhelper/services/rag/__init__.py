# -*- coding: utf-8 -*-
"""项目 RAG 服务：本地索引、增量更新与检索（无 Tk）。"""
from .engine import (
    Chunk,
    FileEntry,
    MAX_FILE_BYTES,
    RagError,
    chunk_text,
    discover_files,
    index_project,
    list_projects,
    norm_root,
    project_stats,
    search,
)

__all__ = [
    "Chunk", "FileEntry", "MAX_FILE_BYTES", "RagError", "chunk_text",
    "discover_files", "index_project", "list_projects", "norm_root",
    "project_stats", "search",
]
