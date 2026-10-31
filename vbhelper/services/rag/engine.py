# -*- coding: utf-8 -*-
"""项目 RAG 索引与检索（无 Tk，可在后台线程运行）。

- 扫描项目（跳过 .git/node_modules/dist 等与隐藏目录、二进制、超大文件）；
- 多线程读取 + 分块（60 行窗口 / 10 行重叠；Markdown 按标题分节）；
- 增量写 SQLite（rag_projects/rag_files/rag_chunks + FTS5 unicode + trigram）；
- 检索返回带 ``相对路径:起始行-结束行`` 的片段，供 AI 只读相关文件。

FTS 表由本模块手动同步（新增/删除 chunk 时同步写两张 FTS），
不用 SQL 触发器，避免迁移脚本按分号拆分 BEGIN...END。
"""
from __future__ import annotations

import concurrent.futures as cf
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

try:
    from ...store.db import Database
except Exception:       # 允许独立导入测试
    Database = Any      # type: ignore


class RagError(RuntimeError):
    """RAG 索引/检索的可预期错误。"""


# ---------------------------------------------------------------------------
# 扫描范围
# ---------------------------------------------------------------------------

DIR_IGNORES = {
    ".git", ".svn", ".hg", "node_modules", "bower_components",
    "dist", "build", "out", "target", "bin", "obj", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".next",
    ".nuxt", ".venv", "venv", "env", ".env", "coverage", "htmlcov",
    ".idea", ".vs", ".vscode", ".gradle", ".cache",
}

SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif",
    ".tiff", ".psd", ".svg", ".mp3", ".flac", ".wav", ".ogg", ".m4a",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".zip", ".rar", ".7z",
    ".gz", ".bz2", ".xz", ".tar", ".jar", ".war", ".ear", ".jmod",
    ".class", ".pyc", ".pyo", ".pyd", ".exe", ".dll", ".so", ".dylib",
    ".bin", ".dat", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf",
    ".db", ".sqlite", ".sqlite3", ".lock", ".iso", ".img",
    ".pem", ".key", ".pfx", ".p12", ".keystore", ".jks",
}

# 明确不索引的敏感文件（避免把密钥/口令送进 AI 上下文）。
SKIP_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    ".npmrc", ".netrc", ".pgpass", "id_rsa", "id_dsa", "id_ecdsa",
    "id_ed25519", "credentials.json", "secrets.json", "keystore",
}

MAX_FILE_BYTES = 1_048_576          # 1MB
MAX_FILES = 50_000
CHUNK_LINES = 60
CHUNK_OVERLAP = 10
MAX_CHUNK_CHARS = 6_000

_LANG = {
    ".py": "Python", ".pyw": "Python", ".js": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TSX", ".jsx": "JSX", ".java": "Java", ".kt": "Kotlin",
    ".kts": "Kotlin", ".go": "Go", ".rs": "Rust", ".c": "C", ".h": "C",
    ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".php": "PHP",
    ".rb": "Ruby", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell",
    ".ps1": "PowerShell", ".bat": "Batch", ".cmd": "Batch", ".sql": "SQL",
    ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".less": "Less", ".md": "Markdown", ".json": "JSON", ".yaml": "YAML",
    ".yml": "YAML", ".toml": "TOML", ".xml": "XML", ".ini": "INI",
    ".cfg": "INI", ".conf": "Config", ".txt": "Text", ".log": "Log",
    ".vue": "Vue", ".lua": "Lua", ".dart": "Dart", ".r": "R",
    ".proto": "Proto", ".gradle": "Gradle", ".properties": "Properties",
}


def norm_root(path: str) -> str:
    """绝对、规范化路径（Windows 用原大小写存储）。"""
    return os.path.abspath(os.path.normpath(os.path.expanduser(path or "")))


def _emit(progress: Callable[[dict], None] | None, **event: Any) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception:
        pass


def _lang_for(rel_path: str) -> str:
    return _LANG.get(os.path.splitext(rel_path)[1].lower(), "")


def _skip_dir(name: str) -> bool:
    low = name.lower()
    return low in DIR_IGNORES or low.startswith(".") or low.endswith(".egg-info")


def _skip_file(name: str) -> bool:
    low = name.lower()
    if low.startswith(".") or low in SKIP_NAMES:
        return True
    return os.path.splitext(low)[1] in SKIP_EXTS


# ---------------------------------------------------------------------------
# 读取/分块
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    abs_path: str
    rel_path: str
    size: int
    mtime: float


@dataclass
class Chunk:
    start_line: int
    end_line: int
    text: str
    tokens: int


def _decode(raw: bytes) -> str | None:
    if b"\x00" in raw[:8192]:
        return None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _token_estimate(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = max(0, len(text) - cjk)
    return max(1, int(math.ceil(cjk * 0.7 + other / 4.0)))


def _window_chunks(lines: list[str], *, size: int = CHUNK_LINES,
                   overlap: int = CHUNK_OVERLAP) -> Iterator[Chunk]:
    n = len(lines)
    if n == 0:
        return
    step = max(1, size - overlap)
    i = 0
    while i < n:
        j = min(n, i + size)
        text = "\n".join(lines[i:j])
        if text.strip():
            if len(text) > MAX_CHUNK_CHARS:
                text = text[:MAX_CHUNK_CHARS]
            yield Chunk(i + 1, j, text, _token_estimate(text))
        if j >= n:
            break
        i += step


_MD_HEADING = re.compile(r"^#{1,6}\s+\S")


def _markdown_chunks(lines: list[str]) -> Iterator[Chunk]:
    """Markdown 按标题分节；单节过长再按行窗口切。"""
    starts = [i for i, line in enumerate(lines) if _MD_HEADING.match(line)]
    if not starts:
        yield from _window_chunks(lines)
        return
    bounds = sorted(set(starts + [len(lines)]))
    for k, start in enumerate(bounds[:-1]):
        end = bounds[k + 1]
        block = lines[start:end]
        if not block:
            continue
        if len(block) <= CHUNK_LINES:
            text = "\n".join(block)
            if text.strip():
                yield Chunk(start + 1, end, text[:MAX_CHUNK_CHARS],
                            _token_estimate(text))
        else:
            for chunk in _window_chunks(block):
                yield Chunk(chunk.start_line + start,
                            chunk.end_line + start,
                            chunk.text, chunk.tokens)


def chunk_text(text: str, rel_path: str) -> list[Chunk]:
    """把文件文本切成块；Markdown 按标题，其它按 60/10 行窗口。"""
    lines = text.splitlines()
    if not lines:
        return []
    if rel_path.lower().endswith((".md", ".markdown")):
        return list(_markdown_chunks(lines))
    return list(_window_chunks(lines))


def _parse_file(entry: FileEntry, max_bytes: int,
                stop: threading.Event) -> dict[str, Any]:
    """线程池任务：读文件 + 分块；失败返回 error。"""
    result: dict[str, Any] = {"entry": entry, "chunks": [], "error": ""}
    if stop.is_set():
        result["error"] = "cancelled"
        return result
    try:
        with open(entry.abs_path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            result["error"] = "文件超过大小上限"
            return result
        text = _decode(raw)
        if text is None:
            result["error"] = "二进制/无法解码"
            return result
        chunks = chunk_text(text, entry.rel_path)
        result["chunks"] = chunks
    except Exception as exc:
        result["error"] = str(exc or type(exc).__name__)
    return result


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def discover_files(root: str, *, max_file_bytes: int = MAX_FILE_BYTES,
                   max_files: int = MAX_FILES,
                   progress: Callable[[dict], None] | None = None,
                   stop: threading.Event | None = None,
                   ) -> tuple[list[FileEntry], int]:
    """遍历项目，返回 (可索引文件, 跳过数)。静默、不跟随符号链接。"""
    root = norm_root(root)
    if not os.path.isdir(root):
        raise RagError("不是文件夹：%s" % root)
    stop = stop or threading.Event()
    files: list[FileEntry] = []
    skipped = 0
    seen = 0
    last = 0.0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if stop.is_set():
            break
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        for name in sorted(filenames):
            if stop.is_set():
                break
            if _skip_file(name):
                skipped += 1
                continue
            abs_path = os.path.join(dirpath, name)
            try:
                if os.path.islink(abs_path):
                    skipped += 1
                    continue
                st = os.stat(abs_path, follow_symlinks=False)
            except OSError:
                skipped += 1
                continue
            if st.st_size <= 0 or st.st_size > max_file_bytes:
                skipped += 1
                continue
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            files.append(FileEntry(abs_path, rel, int(st.st_size),
                                   float(st.st_mtime)))
            seen += 1
            now = time.monotonic()
            if now - last >= 0.12:
                last = now
                _emit(progress, phase="scan", files=len(files),
                      skipped=skipped, message="扫描项目文件…")
            if len(files) >= max_files:
                skipped += 1
                dirnames[:] = []
                break
        if len(files) >= max_files:
            break
    return files, skipped


# ---------------------------------------------------------------------------
# 数据库同步
# ---------------------------------------------------------------------------


def _project_row(db: Database, root: str) -> sqlite3.Row | None:
    row = db.query_one("SELECT * FROM rag_projects WHERE path=?", (root,))
    if row is None:
        row = db.query_one(
            "SELECT * FROM rag_projects WHERE lower(path)=lower(?)", (root,))
    return row


def _ensure_project(db: Database, root: str) -> int:
    row = _project_row(db, root)
    if row is not None:
        return int(row["id"])
    cur = db.execute(
        "INSERT INTO rag_projects(path,name,status,created_at) "
        "VALUES(?,?,?,?)", (root, os.path.basename(root) or root,
                            "indexing", time.time()))
    return int(cur.lastrowid)


def _delete_fts_rows(db: Database, chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    rows = [(int(cid),) for cid in chunk_ids]
    db.executemany("DELETE FROM rag_fts WHERE rowid=?", rows)
    db.executemany("DELETE FROM rag_fts_tri WHERE rowid=?", rows)


def _delete_file_chunks(db: Database, file_id: int) -> None:
    ids = [int(r["id"]) for r in db.query(
        "SELECT id FROM rag_chunks WHERE file_id=?", (file_id,))]
    _delete_fts_rows(db, ids)
    db.execute("DELETE FROM rag_chunks WHERE file_id=?", (file_id,))


def _delete_project_data(db: Database, project_id: int) -> None:
    ids = [int(r["id"]) for r in db.query(
        "SELECT id FROM rag_chunks WHERE project_id=?", (project_id,))]
    _delete_fts_rows(db, ids)
    db.execute("DELETE FROM rag_chunks WHERE project_id=?", (project_id,))
    db.execute("DELETE FROM rag_files WHERE project_id=?", (project_id,))


def _insert_fts(db: Database, rows: list[tuple[int, str, str, int]]) -> None:
    if not rows:
        return
    db.executemany(
        "INSERT INTO rag_fts(rowid,text,path,project_id) VALUES(?,?,?,?)",
        rows)
    db.executemany(
        "INSERT INTO rag_fts_tri(rowid,text,path,project_id) VALUES(?,?,?,?)",
        rows)


def _write_file_result(db: Database, project_id: int, result: dict[str, Any],
                       existing: dict[str, sqlite3.Row]) -> int:
    """写入一个文件的 file 行 + chunks + 两张 FTS；返回 chunk 数。"""
    entry: FileEntry = result["entry"]
    chunks: list[Chunk] = result["chunks"]
    rel = entry.rel_path
    old = existing.get(rel)
    if old is not None:
        _delete_file_chunks(db, int(old["id"]))
        db.execute(
            "UPDATE rag_files SET mtime=?,size=?,lang=?,chunk_count=?,"
            "indexed_at=? WHERE id=?",
            (entry.mtime, entry.size, _lang_for(rel), len(chunks),
             time.time(), int(old["id"])))
        file_id = int(old["id"])
    else:
        cur = db.execute(
            "INSERT INTO rag_files(project_id,path,mtime,size,lang,"
            "chunk_count,indexed_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, rel, entry.mtime, entry.size, _lang_for(rel),
             len(chunks), time.time()))
        file_id = int(cur.lastrowid)
    if chunks:
        db.executemany(
            "INSERT INTO rag_chunks(project_id,file_id,path,start_line,"
            "end_line,text,token_estimate) VALUES(?,?,?,?,?,?,?)",
            [(project_id, file_id, rel, c.start_line, c.end_line, c.text,
              c.tokens) for c in chunks])
        # FTS 列顺序是 (rowid, text, path, project_id)；text 必须放正文，
        # path 是 UNINDEXED 的展示列。插反会导致只按路径命中。
        rows = [(int(r["id"]), str(r["text"]), str(r["path"]), project_id)
                for r in db.query(
                    "SELECT id,path,text FROM rag_chunks WHERE file_id=?",
                    (file_id,))]
        _insert_fts(db, rows)
    return len(chunks)


def _update_project_stats(db: Database, project_id: int,
                          status: str = "ready") -> dict[str, Any]:
    frow = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS b "
        "FROM rag_files WHERE project_id=?", (project_id,))
    crow = db.query_one(
        "SELECT COUNT(*) AS n FROM rag_chunks WHERE project_id=?",
        (project_id,))
    files = int(frow["n"] if frow else 0)
    chunks = int(crow["n"] if crow else 0)
    total = int(frow["b"] if frow else 0)
    now = time.time()
    db.execute(
        "UPDATE rag_projects SET file_count=?,chunk_count=?,total_bytes=?,"
        "status=?,indexed_at=? WHERE id=?",
        (files, chunks, total, status, now, project_id))
    return {"files": files, "chunks": chunks, "bytes": total,
            "indexed_at": now, "status": status}


# ---------------------------------------------------------------------------
# 索引主流程
# ---------------------------------------------------------------------------


def index_project(db: Database, root: str, *, full: bool = False,
                  threads: int | None = None,
                  max_file_bytes: int = MAX_FILE_BYTES,
                  max_files: int = MAX_FILES,
                  progress: Callable[[dict], None] | None = None,
                  stop: threading.Event | None = None) -> dict[str, Any]:
    """建立/更新项目索引。``progress`` 在索引线程里调用（不要碰 Tk）。"""
    root = norm_root(root)
    if db is None:
        raise RagError("数据库不可用")
    if not os.path.isdir(root):
        raise RagError("不是文件夹：%s" % root)
    stop = stop or threading.Event()
    started = time.monotonic()
    stats: dict[str, Any] = {
        "project": root, "files": 0, "chunks": 0, "skipped": 0,
        "errors": 0, "kept": 0, "deleted": 0, "cancelled": False,
    }
    last_emit = [0.0]

    def emit(phase: str, *, force: bool = False, **kw: Any) -> None:
        now = time.monotonic()
        if not force and now - last_emit[0] < 0.09:
            return
        last_emit[0] = now
        payload = {"phase": phase, "elapsed": round(now - started, 2),
                   "project": root}
        payload.update(kw)
        _emit(progress, **payload)

    emit("scan_start", force=True, message="扫描项目文件…")
    files, skipped = discover_files(
        root, max_file_bytes=max_file_bytes, max_files=max_files,
        progress=progress, stop=stop)
    stats["skipped"] = skipped
    if stop.is_set():
        stats["cancelled"] = True
        stats["elapsed"] = round(time.monotonic() - started, 2)
        emit("cancelled", force=True, **stats)
        return stats

    emit("scan_done", force=True, total=len(files), skipped=skipped)
    project_id = _ensure_project(db, root)
    db.execute("UPDATE rag_projects SET status=? WHERE id=?",
               ("indexing", project_id))
    existing_rows = db.query(
        "SELECT id,path,mtime,size,chunk_count FROM rag_files "
        "WHERE project_id=?", (project_id,))
    existing: dict[str, sqlite3.Row] = {str(r["path"]): r
                                       for r in existing_rows}
    if full:
        _delete_project_data(db, project_id)
        existing = {}

    changed: list[FileEntry] = []
    kept = 0
    for entry in files:
        old = existing.get(entry.rel_path)
        if old is not None \
                and abs(float(old["mtime"]) - entry.mtime) < 0.0001 \
                and int(old["size"]) == entry.size:
            kept += 1
        else:
            changed.append(entry)
    current_paths = {entry.rel_path for entry in files}
    deleted_paths = [p for p in existing if p not in current_paths]
    if deleted_paths:
        for rel in deleted_paths:
            _delete_file_chunks(db, int(existing[rel]["id"]))
            db.execute("DELETE FROM rag_files WHERE id=?",
                       (int(existing[rel]["id"]),))
    stats["kept"] = kept
    stats["deleted"] = len(deleted_paths)

    if not changed:
        info = _update_project_stats(db, project_id, "ready")
        stats.update(info)
        stats["elapsed"] = round(time.monotonic() - started, 2)
        emit("done", force=True, **stats)
        return stats

    if threads:
        # 允许自定义到 256（UI 会对 128/256 弹性能警告），但硬顶 256，
        # 防止手改配置写出离谱值把线程池撑爆。
        worker_count = max(1, min(256, int(threads)))
    else:
        worker_count = max(2, min(8, os.cpu_count() or 4))
    total = len(changed)
    emit("parse_start", force=True, total=total, kept=kept,
         deleted=len(deleted_paths), message="多线程读取并分块…")

    done = 0
    chunk_total = 0
    errors = 0
    batch: list[dict[str, Any]] = []
    batch_chunks = 0
    error_samples: list[str] = []

    def flush() -> None:
        nonlocal batch, batch_chunks, chunk_total
        if not batch:
            return
        with db.transaction():
            for result in batch:
                count = _write_file_result(db, project_id, result, existing)
                chunk_total += count
        batch = []
        batch_chunks = 0
        emit("db", force=True, done=done, total=total,
             chunks=chunk_total, errors=errors, message="写入数据库…")

    executor = cf.ThreadPoolExecutor(max_workers=worker_count,
                                     thread_name_prefix="rag-index")
    futures = {executor.submit(_parse_file, entry, max_file_bytes, stop): entry
               for entry in changed}
    try:
        for future in cf.as_completed(futures):
            entry = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:
                errors += 1
                if len(error_samples) < 12:
                    error_samples.append("%s: %r" % (entry.rel_path, exc))
                continue
            if result.get("error"):
                if result["error"] != "cancelled":
                    errors += 1
                    if len(error_samples) < 12:
                        error_samples.append("%s: %s"
                                             % (entry.rel_path,
                                                result["error"]))
                continue
            batch.append(result)
            batch_chunks += len(result["chunks"])
            emit("parse", done=done, total=total, chunks=chunk_total
                 + batch_chunks, errors=errors, kept=kept,
                 file=entry.rel_path, message="多线程读取并分块…")
            if len(batch) >= 24 or batch_chunks >= 2400:
                flush()
            if stop.is_set():
                break
    finally:
        if stop.is_set():
            for f in futures:
                f.cancel()
        executor.shutdown(wait=not stop.is_set(), cancel_futures=stop.is_set())
        flush()

    if stop.is_set():
        info = _update_project_stats(db, project_id, "partial")
        stats.update(info)
        stats["errors"] = errors
        stats["cancelled"] = True
        stats["error_samples"] = error_samples
        stats["elapsed"] = round(time.monotonic() - started, 2)
        emit("cancelled", force=True, **stats)
        return stats

    info = _update_project_stats(db, project_id, "ready")
    stats.update(info)
    stats["errors"] = errors
    stats["error_samples"] = error_samples
    stats["elapsed"] = round(time.monotonic() - started, 2)
    emit("done", force=True, **stats)
    return stats


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

_FTS_LATIN = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _quote_fts(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _fts_latin_query(query: str) -> str:
    tokens = list(dict.fromkeys(_FTS_LATIN.findall(query)))[:8]
    return " OR ".join(_quote_fts(tok) for tok in tokens)


def _fts_trigram_query(query: str) -> str:
    parts = [p for p in re.split(r"\s+", query.strip()) if len(p) >= 3]
    if not parts:
        if len(query.strip()) >= 3:
            parts = [query.strip()]
    return " OR ".join(_quote_fts(p) for p in parts[:6])


def _snippet(text: str, query: str, width: int = 220) -> str:
    low = text.casefold()
    needle = ""
    for part in re.split(r"\s+", (query or "").strip()):
        if part and part.casefold() in low:
            needle = part
            break
    if not needle:
        return text[:width].replace("\n", " ")
    idx = low.find(needle.casefold())
    start = max(0, idx - width // 3)
    end = min(len(text), start + width)
    return text[start:end].replace("\n", " ")


def _query_table(db: Database, table: str, match: str, project_id: int,
                 limit: int) -> list[dict[str, Any]]:
    if not match:
        return []
    sql = (
        "SELECT c.id, c.path, c.start_line, c.end_line, c.text, "
        "snippet(%s, 0, '[', ']', '…', 20) AS snip, bm25(%s) AS score "
        "FROM %s JOIN rag_chunks c ON c.id=%s.rowid "
        "WHERE %s MATCH ? AND c.project_id=? "
        "ORDER BY score LIMIT ?" % (table, table, table, table, table))
    try:
        rows = db.query(sql, (match, project_id, max(1, limit)))
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        out.append({
            "id": int(row["id"]), "path": str(row["path"]),
            "start_line": int(row["start_line"]),
            "end_line": int(row["end_line"]),
            "text": str(row["text"]),
            "snippet": str(row["snip"] or ""),
            "score": float(row["score"] or 0.0),
            "source": table,
        })
    return out


def _like_search(db: Database, project_id: int, query: str,
                 limit: int) -> list[dict[str, Any]]:
    parts = [p for p in re.split(r"\s+", query.strip()) if p][:3]
    if not parts:
        return []
    needle = max(parts, key=len)
    rows = db.query(
        "SELECT id,path,start_line,end_line,text FROM rag_chunks "
        "WHERE project_id=? AND (text LIKE ? OR path LIKE ?) "
        "ORDER BY length(text) LIMIT ?",
        (project_id, "%" + needle + "%", "%" + needle + "%", max(1, limit)))
    return [{
        "id": int(r["id"]), "path": str(r["path"]),
        "start_line": int(r["start_line"]), "end_line": int(r["end_line"]),
        "text": str(r["text"]), "snippet": _snippet(str(r["text"]), query),
        "score": 999.0, "source": "like",
    } for r in rows]


def search(db: Database, project_path: str, query: str, *,
           limit: int = 8) -> dict[str, Any]:
    """在指定项目索引里检索；返回项目信息 + 命中片段。"""
    root = norm_root(project_path)
    row = _project_row(db, root)
    if row is None:
        raise RagError("项目尚未建立索引：%s" % root)
    q = (query or "").strip()
    if not q:
        raise RagError("检索词不能为空")
    limit = max(1, min(int(limit or 8), 20))
    project_id = int(row["id"])
    results: dict[int, dict[str, Any]] = {}
    order: list[int] = []

    latin = _fts_latin_query(q)
    trigram = _fts_trigram_query(q) if (len(q) >= 3) else ""
    for table, match, penalty in (
            ("rag_fts", latin, 0.0),
            ("rag_fts_tri", trigram, 0.0)):
        for hit in _query_table(db, table, match, project_id, limit * 2):
            cid = hit["id"]
            if cid not in results:
                results[cid] = hit
                order.append(cid)
            elif hit["score"] < results[cid]["score"]:
                results[cid] = hit
    if not results:
        for hit in _like_search(db, project_id, q, limit * 2):
            results[hit["id"]] = hit
            order.append(hit["id"])

    hits = [results[cid] for cid in order]
    hits.sort(key=lambda h: h["score"])
    hits = hits[:limit]
    for hit in hits:
        hit["rel_path"] = hit["path"]
        hit["abs_path"] = os.path.join(root, hit["path"].replace("/", os.sep))
        if not hit["snippet"]:
            hit["snippet"] = _snippet(hit["text"], q)
        hit["text"] = hit["text"][:2000]
        hit.pop("source", None)
    files = []
    for hit in hits:
        if hit["rel_path"] not in files:
            files.append(hit["rel_path"])
    return {
        "project": root,
        "name": str(row["name"] or os.path.basename(root)),
        "files_indexed": int(row["file_count"] or 0),
        "chunks_indexed": int(row["chunk_count"] or 0),
        "indexed_at": float(row["indexed_at"] or 0),
        "status": str(row["status"] or ""),
        "query": q,
        "hits": hits,
        "files": files,
    }


def list_projects(db: Database) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT id,path,name,file_count,chunk_count,total_bytes,status,"
        "created_at,indexed_at FROM rag_projects ORDER BY indexed_at DESC")
    return [dict(r) for r in rows]


def project_stats(db: Database, root: str) -> dict[str, Any] | None:
    row = _project_row(db, norm_root(root))
    return dict(row) if row is not None else None
