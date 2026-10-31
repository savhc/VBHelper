# -*- coding: utf-8 -*-
"""Git 服务包（纯标准库 + 系统 git.exe，不依赖 Tk）。"""
from .service import (MAX_DIFF_BYTES, Branch, Commit, DiffResult, GitError,
                      GitFile, GitInfo, GitService, RepoStatus, parse_status)

__all__ = [
    "MAX_DIFF_BYTES", "Branch", "Commit", "DiffResult", "GitError",
    "GitFile", "GitInfo", "GitService", "RepoStatus", "parse_status",
]
