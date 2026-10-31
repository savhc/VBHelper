"""扫描编排：目录索引 -> 解析清单 -> 核对远端最新版/漏洞。

在后台线程跑，通过 ``progress(event)`` 把进度回调给 UI（调用方通常塞队列），
``stop()`` 为真时协作式取消。不写任何项目文件。

事件格式 ``(kind, payload)``：

- ``("walk", {"dirs": n})``               浏览目录计数（每 200 个一次）
- ``("manifests", {"count": n})``         清单发现完毕
- ``("file", FileEntry)``                 解析完一个清单
- ``("resolve_start", {"total": n})``     开始联网核对
- ``("resolve", {"done", "total", "name"})``
- ``("phase", {"name": "vulns"})``        开始查漏洞

"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import parsers, versions
from .parsers import Dependency

NET_ERROR_KINDS = {"dns", "network", "timeout", "tls"}

@dataclass
class FileEntry:
    """清单索引里的一行（给「扫描索引」树用）。"""

    path: str                     # 相对项目根
    eco: str
    kind: str
    count: int = 0
    state: str = "parsed"         # parsed / empty / warn / error
    note: str = ""

@dataclass
class ScanResult:
    root: str
    files: list[FileEntry] = field(default_factory=list)
    deps: list[Dependency] = field(default_factory=list)
    dirs_scanned: int = 0
    elapsed: float = 0.0
    truncated: bool = False
    online: bool = True
    checked: int = 0              # 成功联网核对的包数
    skipped: int = 0              # 超出上限没核对的包数
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.deps)

def _emit(progress, kind: str, payload) -> None:
    if not callable(progress):
        return
    try:
        progress((kind, payload))
    except Exception:
        pass

def merge_deps(deps: list[Dependency]) -> list[Dependency]:
    """同一目录下「声明 + 锁定」合并成一行。

    ``package.json`` 与 ``package-lock.json`` 各自解析出一条 react，
    合并后是一条：declared=^18.2.0、resolved=18.2.0。不同子目录的同名包
    不合并，避免把两个子项目混成一条。
    """
    order: list[tuple[str, str, str]] = []
    merged: dict[tuple[str, str, str], Dependency] = {}
    for dep in deps:
        folder = os.path.dirname(dep.source_file)
        key = (dep.ecosystem, dep.name.lower(), folder)
        current = merged.get(key)
        if current is None:
            merged[key] = dep
            order.append(key)
            continue
        if not dep.transitive:
            current.transitive = False       # 有直接声明就不是间接依赖
        if dep.declared and not current.declared:
            current.declared = dep.declared
            current.line = current.line or dep.line
            current.note = current.note or dep.note
        if dep.scope != "runtime" and current.scope == "runtime":
            current.scope = dep.scope
        if dep.resolved and not current.resolved:
            current.resolved = dep.resolved
            current.transitive = False
        elif dep.source_file != current.source_file and not current.note:
            current.note = "同时见 %s" % dep.source_file
    return [merged[key] for key in order]

class Scanner:
    """一次项目依赖扫描。实例一次性使用。"""

    def __init__(self, root: str, *, registry=None, resolve: bool = True,
                 workers: int = 6, max_lookup: int = 300,
                 osv_limit: int = 500, include_transitive: bool = False,
                 stop=None) -> None:
        self.root = os.path.abspath(root)
        self.registry = registry
        self.resolve = bool(resolve) and registry is not None
        self.workers = max(1, int(workers))
        self.max_lookup = max(0, int(max_lookup))
        self.osv_limit = max(0, int(osv_limit))
        self.include_transitive = bool(include_transitive)
        self._stop = stop if callable(stop) else (lambda: False)

    # ---------------- 主流程 ----------------

    def run(self, progress=None) -> ScanResult:
        if not os.path.isdir(self.root):
            raise ValueError("目录不存在：%s" % self.root)
        started = time.time()
        result = ScanResult(root=self.root)

        def emit(kind, payload):
            _emit(progress, kind, payload)

        manifests, dirs, truncated = parsers.discover(
            self.root, on_dir=lambda n: emit("walk", {"dirs": n}))
        result.dirs_scanned = dirs
        result.truncated = truncated
        emit("manifests", {"count": len(manifests)})

        # ---- 解析（纯本地，快） ----
        for manifest in manifests:
            if self._stop():
                result.cancelled = True
                break
            deps, note = parsers.parse_file(manifest)
            entry = FileEntry(path=manifest.rel, eco=manifest.eco,
                              kind=manifest.kind, count=len(deps),
                              note=note)
            if note and (note.startswith("解析失败") or "无法读取" in note
                         or "过大" in note):
                entry.state = "error"
            elif note:
                entry.state = "warn"
            elif not deps:
                entry.state = "empty"
            result.files.append(entry)
            result.deps.extend(deps)
            emit("file", entry)
        if not result.cancelled:
            result.deps = merge_deps(result.deps)
            per_file: dict[str, int] = {}
            for dep in result.deps:
                per_file[dep.source_file] = per_file.get(
                    dep.source_file, 0) + 1
            for entry in result.files:      # 计数按合并后的行数算
                entry.count = per_file.get(entry.path, 0)

        # ---- 联网核对（慢，可取消） ----
        latest: dict[tuple[str, str], str | None] = {}
        if self.resolve and not result.cancelled:
            targets, seen = [], set()
            for dep in result.deps:
                if dep.transitive and not self.include_transitive:
                    continue
                if not dep.current:
                    continue
                key = dep.key
                if key in seen:
                    continue
                seen.add(key)
                targets.append((dep.ecosystem, dep.name))
            if len(targets) > self.max_lookup:
                result.skipped = len(targets) - self.max_lookup
                targets = targets[:self.max_lookup]
            emit("resolve_start", {"total": len(targets)})
            if targets:
                latest, result.online, result.checked = self._resolve(
                    targets, emit, result)
        # ---- 回填最新版与状态 ----
        for dep in result.deps:
            found = latest.get(dep.key)
            if found:
                dep.latest = found
            dep.status = versions.status_for(dep.current, dep.latest)

        # ---- 漏洞（OSV 批量，缓存 6h） ----
        if self.resolve and result.online and not result.cancelled:
            emit("phase", {"name": "vulns"})
            candidates = [d for d in result.deps if d.current and d.is_exact]
            try:
                found = self.registry.vulns(candidates, limit=self.osv_limit)
            except Exception as exc:              # 漏洞查询失败不影响版本结果
                result.errors.append("漏洞查询失败：%s" % exc)
                found = {}
            for dep in result.deps:
                key = (dep.ecosystem, dep.name.lower(),
                       (dep.current or "").strip())
                hit = found.get(key)
                if hit:
                    dep.cves = hit
                    dep.status = "vulnerable"
            if len(candidates) > self.osv_limit:
                result.errors.append(
                    "漏洞查询只核对前 %d 个依赖" % self.osv_limit)

        result.elapsed = time.time() - started
        if result.skipped:
            result.errors.append(
                "%d 个包超过单次核对上限，未联网检查最新版" % result.skipped)
        if not result.online:
            result.errors.append("网络不可用，只展示本地解析结果")
        if result.truncated:
            result.errors.append("目录项太多，扫描已截断")
        emit("done", result)
        return result

    # ---------------- 联网 ----------------

    def _resolve(self, targets, emit, result: ScanResult):
        """并发查询最新版本；连续网络错误时判定离线并提前收工。"""
        found: dict[tuple[str, str], str | None] = {}
        total = len(targets)
        done = ok = net_fail = 0
        online = True
        pool = ThreadPoolExecutor(max_workers=self.workers)
        futures = {pool.submit(self.registry.latest, eco, name): (eco, name)
                   for eco, name in targets}
        try:
            for future in as_completed(futures):
                if self._stop():
                    result.cancelled = True
                    break
                eco, name = futures[future]
                version, error = None, None
                try:
                    version, error = future.result()
                except BaseException as exc:       # noqa: BLE001
                    error = exc
                done += 1
                if error is None and version:
                    ok += 1
                elif error is not None:
                    kind = getattr(error, "kind", "")
                    if kind in NET_ERROR_KINDS:
                        net_fail += 1
                    if net_fail >= 5 and ok == 0:
                        online = False
                        break
                found[(eco, name.lower())] = version
                emit("resolve", {"done": done, "total": total, "name": name,
                                 "error": "" if version else "未获取"})
        finally:
            for pending in futures:
                pending.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
        return found, online, ok
