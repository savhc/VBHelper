# -*- coding: utf-8 -*-
"""Git 服务：封装系统 git.exe（R2 Git 管理器底座，见 plan 02 §4）。

设计要点：
- 参数列表调用，绝不 shell=True；Windows 下 CREATE_NO_WINDOW；
- 固定 GIT_TERMINAL_PROMPT=0 / GIT_PAGER=cat / LC_ALL=C.UTF-8，
  只读命令再加 GIT_OPTIONAL_LOCKS=0，避免和用户自己的 git 抢索引锁；
- 输出按 UTF-8 解码，失败回退系统编码；超时 kill 并抛 GitError；
- 不 import Tk，可以在 TaskRunner 的后台线程里直接调用。

本文件先覆盖本地只读操作与 stage/unstage/commit；远程与 discard 等
危险操作在 UI 落地时再补，避免没有确认入口就暴露写能力。
"""
from __future__ import annotations

import locale
import os
import shutil
import subprocess
from dataclasses import dataclass, field

CREATE_NO_WINDOW = 0x08000000
MAX_DIFF_BYTES = 2 * 1024 * 1024


class GitError(RuntimeError):
    """git 命令失败：带命令行、退出码、stderr 原文（供 UI 显示/复制诊断）。"""

    def __init__(self, message: str, *, command: list[str] | None = None,
                 code: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.command = command or []
        self.code = code
        self.stderr = stderr


@dataclass(frozen=True)
class GitInfo:
    version: str
    path: str


@dataclass(frozen=True)
class GitFile:
    """一条变更记录。status 是 porcelain 的单个状态字符（M/A/D/R/C/U/?）。"""

    path: str
    status: str
    orig_path: str | None = None

    @property
    def is_rename(self) -> bool:
        return self.orig_path is not None


@dataclass
class RepoStatus:
    branch: str | None = None
    detached: bool = False
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: list[GitFile] = field(default_factory=list)
    unstaged: list[GitFile] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked
                    or self.conflicted)

    @property
    def total(self) -> int:
        return (len(self.staged) + len(self.unstaged) + len(self.untracked)
                + len(self.conflicted))


@dataclass(frozen=True)
class Commit:
    hash: str
    short: str
    author: str
    email: str
    timestamp: int
    subject: str


@dataclass(frozen=True)
class Branch:
    name: str
    hash: str
    current: bool
    upstream: str | None
    timestamp: int


@dataclass(frozen=True)
class DiffResult:
    text: str
    truncated: bool = False
    size: int = 0


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    """UTF-8 优先，失败回退系统编码；再做不了就替换坏字节。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            enc = locale.getpreferredencoding(False) or "utf-8"
            return raw.decode(enc, errors="replace")
        except Exception:
            return raw.decode("utf-8", errors="replace")


def _add_change(out: RepoStatus, xy: str, path: str,
                orig: str | None) -> None:
    if len(xy) != 2:
        return
    x, y = xy[0], xy[1]
    if x != ".":
        out.staged.append(GitFile(path, x, orig))
    if y != ".":
        out.unstaged.append(GitFile(path, y, orig))


def parse_status(raw: bytes) -> RepoStatus:
    """解析 ``git status --porcelain=v2 -z --branch`` 的原始输出。"""
    text = _decode(raw)
    tokens = text.split("\0")
    out = RepoStatus()
    i, n = 0, len(tokens)
    while i < n:
        rec = tokens[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("# branch.head "):
            head = rec[len("# branch.head "):]
            if head == "(detached)":
                out.detached = True
                out.branch = None
            else:
                out.branch = head
        elif rec.startswith("# branch.upstream "):
            out.upstream = rec[len("# branch.upstream "):]
        elif rec.startswith("# branch.ab "):
            for part in rec[len("# branch.ab "):].split():
                try:
                    if part.startswith("+"):
                        out.ahead = int(part[1:])
                    elif part.startswith("-"):
                        out.behind = int(part[1:])
                except ValueError:
                    continue
        elif rec.startswith("1 "):
            parts = rec.split(" ", 8)       # 1 XY sub mH mI mW hH hI path
            if len(parts) >= 9:
                _add_change(out, parts[1], parts[8], None)
        elif rec.startswith("2 "):
            parts = rec.split(" ", 9)       # 2 XY ... Xscore path
            if len(parts) >= 10:
                orig = tokens[i] if i < n else ""
                i += 1
                _add_change(out, parts[1], parts[9], orig or None)
        elif rec.startswith("u "):
            parts = rec.split(" ", 9)       # u XY sub m1 m2 m3 mW h1 h2 path
            if len(parts) >= 10:
                out.conflicted.append(parts[9])
        elif rec.startswith("? "):
            out.untracked.append(rec[2:])
        # "! " ignored / "# branch.oid" 忽略
    return out


def _clean_paths(paths) -> list[str]:
    out = []
    for p in paths or ():
        p = str(p).strip()
        if p and p not in out:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class GitService:
    """系统 git.exe 的薄封装。实例无状态，可跨线程复用。"""

    def __init__(self, git_path: str | None = None, *,
                 timeout: float = 15.0) -> None:
        self._git = git_path
        self.timeout = timeout

    # ---------------- 环境 ----------------

    @property
    def git_path(self) -> str | None:
        return self._find_git()

    def _find_git(self) -> str | None:
        if self._git:
            return self._git
        return shutil.which("git")

    def detect(self) -> GitInfo | None:
        """返回 git 版本与路径；未安装返回 None。"""
        exe = self._find_git()
        if not exe:
            return None
        try:
            out = self._run(["--version"], None, exe=exe, timeout=5.0)
        except Exception:
            return None
        return GitInfo(version=out.strip(), path=exe)

    def is_repo(self, repo: str) -> bool:
        """目录是否是 git 工作区（不要求必须是仓库根）。"""
        if not repo or not os.path.isdir(repo):
            return False
        try:
            out = self._run(["rev-parse", "--is-inside-work-tree"], repo,
                            timeout=5.0)
        except GitError:
            return False
        return out.strip() == "true"

    # ---------------- 执行 ----------------

    def _run_bytes(self, args, repo: str | None, *, exe: str | None = None,
                   timeout: float | None = None,
                   env_extra: dict[str, str] | None = None) -> bytes:
        git = exe or self._find_git()
        if not git:
            raise GitError("未找到 git.exe，请先安装 Git 或配置路径")
        cmd = [git] + [str(a) for a in args]
        env = os.environ.copy()
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "LC_ALL": "C.UTF-8",
            "GIT_OPTIONAL_LOCKS": "0",
        })
        if env_extra:
            env.update(env_extra)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                cmd, cwd=repo or None, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        except OSError as exc:
            raise GitError("启动 git 失败：%s" % exc) from exc
        limit = timeout if timeout is not None else self.timeout
        try:
            out, err = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=3)
            except Exception:
                pass
            raise GitError("git 命令超时（%.0fs）：%s"
                           % (limit, " ".join(cmd)))
        if proc.returncode != 0:
            text = _decode(err).strip() or _decode(out).strip()
            raise GitError(
                "git %s 失败（%d）：%s"
                % (args[0] if args else "", proc.returncode, text),
                command=cmd, code=proc.returncode, stderr=text)
        return out

    def _run(self, args, repo: str | None, *, exe: str | None = None,
             timeout: float | None = None,
             env_extra: dict[str, str] | None = None) -> str:
        return _decode(self._run_bytes(args, repo, exe=exe, timeout=timeout,
                                       env_extra=env_extra))

    # ---------------- 只读 ----------------

    def status(self, repo: str, *, timeout: float = 15.0) -> RepoStatus:
        """读取工作区状态（porcelain v2，路径不转义）。"""
        raw = self._run_bytes(
            ["status", "--porcelain=v2", "-z", "--branch"], repo,
            timeout=timeout)
        return parse_status(raw)

    def log(self, repo: str, limit: int = 200,
            branch: str | None = None, *, timeout: float = 30.0) -> list[Commit]:
        """最近提交（新到旧）。"""
        fmt = "%H%x1f%h%x1f%an%x1f%ae%x1f%at%x1f%s"
        args = ["log", "--no-color", "--format=" + fmt,
                "-n", str(max(1, int(limit)))]
        if branch:
            args.append(str(branch))
        out = self._run(args, repo, timeout=timeout)
        rows: list[Commit] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 6:
                continue
            h, short, author, email, ts, subject = parts
            try:
                stamp = int(ts)
            except ValueError:
                stamp = 0
            rows.append(Commit(h, short, author, email, stamp, subject))
        return rows

    def diff(self, repo: str, path: str | None = None, *,
             staged: bool = False, context: int = 3,
             max_bytes: int = MAX_DIFF_BYTES,
             timeout: float = 30.0) -> DiffResult:
        """统一 diff；超过 max_bytes 截断并标记（UI 提示用外部编辑器）。"""
        args = ["diff", "--no-color", "--no-ext-diff",
                "--unified=%d" % max(0, int(context))]
        if staged:
            args.append("--cached")
        if path:
            args += ["--", str(path)]
        raw = self._run_bytes(args, repo, timeout=timeout)
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        return DiffResult(_decode(raw), truncated, len(raw))

    def branches(self, repo: str, *, include_remote: bool = False,
                 timeout: float = 15.0) -> list[Branch]:
        """本地分支（可选远程），按提交时间倒序。"""
        fmt = ("%(refname:short)\x1f%(objectname:short)\x1f%(HEAD)\x1f"
               "%(upstream:short)\x1f%(committerdate:unix)")
        refs = ["refs/heads"]
        if include_remote:
            refs.append("refs/remotes")
        out = self._run(["for-each-ref", "--sort=-committerdate",
                         "--format=" + fmt] + refs, repo, timeout=timeout)
        rows: list[Branch] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 5:
                continue
            name, h, head, upstream, ts = parts
            try:
                stamp = int(ts)
            except ValueError:
                stamp = 0
            rows.append(Branch(name, h, head.strip() == "*",
                               upstream or None, stamp))
        return rows

    # ---------------- 本地写操作（UI 需二次确认） ----------------

    def stage(self, repo: str, paths) -> None:
        """git add -- <paths>。"""
        clean = _clean_paths(paths)
        if not clean:
            return
        self._run(["add", "--"] + clean, repo, timeout=30.0)

    def unstage(self, repo: str, paths) -> None:
        """取消暂存；没有 HEAD 的新仓库回退到 git reset。"""
        clean = _clean_paths(paths)
        if not clean:
            return
        try:
            self._run(["restore", "--staged", "--"] + clean, repo, timeout=30.0)
        except GitError:
            self._run(["reset", "--quiet", "HEAD", "--"] + clean, repo,
                      timeout=30.0)

    def commit(self, repo: str, summary: str, description: str = "",
               *, amend: bool = False, timeout: float = 60.0) -> str:
        """提交暂存区；summary 为空直接拒绝（不弹编辑器）。"""
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("提交消息不能为空")
        args = ["commit", "-m", summary]
        desc = (description or "").strip()
        if desc:
            args += ["-m", desc]
        if amend:
            args.append("--amend")
        return self._run(args, repo, timeout=timeout)
