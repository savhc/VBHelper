# -*- coding: utf-8 -*-
"""最小 SQLite 缓存：http_cache 表。

特点：
- 幂等建表（CREATE TABLE IF NOT EXISTS），重复初始化安全；
- WAL + busy_timeout=3000；
- 连接按线程创建（threading.local），写操作用全局锁串行化；
- 独立可复用，不 import 任何 Tk 相关模块。

用法::

    from vbhelper.store.cache import HttpCache, default_db_path

    cache = HttpCache()                 # 默认 %LOCALAPPDATA%\\VBHelper\\db\\vbhelper.db
    cache.put("https://example.com", etag='"v1"', last_modified=None,
              status=200, headers={"Content-Type": "text/html"},
              body=b"<html></html>", ttl=3600)
    row = cache.get("https://example.com")   # dict 或 None
    if row and not cache.expired("https://example.com"):
        print(row["body"])
"""

import json
import os
import sqlite3
import threading
import time
from typing import Any


APP_DIR_NAME = "VBHelper"


def default_db_path() -> str:
    """返回默认数据库路径 %LOCALAPPDATA%\\VBHelper\\db\\vbhelper.db。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME, "db", "vbhelper.db")


# 幂等建表：表结构需与 store/db.py 的迁移定义保持一致
_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    status INTEGER,
    headers TEXT,
    body BLOB,
    fetched_at REAL,
    ttl REAL
)
"""

_COLUMNS = ("key", "etag", "last_modified", "status", "headers",
            "body", "fetched_at", "ttl")


class HttpCache:
    """SQLite 键值缓存（http_cache 表），线程安全，按线程持有连接。

    用法::

        cache = HttpCache("D:\\tmp\\test.db")
        cache.put("k", body=b"v", ttl=60)
        row = cache.get("k")            # {"key": "k", "body": b"v", ...}
        cache.expired("k")              # False（60 秒内）
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._ensure_table()

    # ---------------- 连接管理 ----------------

    def _conn(self) -> sqlite3.Connection:
        """取当前线程的连接；没有则新建并设置 PRAGMA。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=3.0)
            conn.execute("PRAGMA busy_timeout=3000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass          # 某些文件系统不支持 WAL，忽略即可
            self._local.conn = conn
        return conn

    def _ensure_table(self) -> None:
        """幂等建表。"""
        with self._write_lock:
            conn = self._conn()
            conn.executescript(_SCHEMA)
            conn.commit()

    # ---------------- 读写 ----------------

    def get(self, key: str) -> dict[str, Any] | None:
        """按 key 取缓存行；不存在返回 None。headers 反序列化为 dict。"""
        conn = self._conn()
        row = conn.execute(
            "SELECT key, etag, last_modified, status, headers, body,"
            " fetched_at, ttl FROM http_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        item = dict(zip(_COLUMNS, row))
        try:
            item["headers"] = json.loads(item["headers"]) if item["headers"] else {}
        except (ValueError, TypeError):
            item["headers"] = {}
        item["body"] = bytes(item["body"] or b"")
        return item

    def put(self, key: str, *, etag: str | None = None,
            last_modified: str | None = None, status: int = 200,
            headers: dict[str, str] | None = None, body: bytes = b"",
            ttl: float = 0.0, fetched_at: float | None = None) -> None:
        """写入/覆盖一条缓存。ttl<=0 表示不过期。"""
        conn = self._conn()
        with self._write_lock:
            conn.execute(
                "INSERT OR REPLACE INTO http_cache"
                " (key, etag, last_modified, status, headers, body, fetched_at, ttl)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, etag, last_modified, status,
                 json.dumps(headers or {}, ensure_ascii=False),
                 sqlite3.Binary(body),
                 fetched_at if fetched_at is not None else time.time(),
                 float(ttl)),
            )
            conn.commit()

    def touch(self, key: str, *, ttl: float | None = None) -> bool:
        """刷新 fetched_at（304 命中后使用），可选更新 ttl。返回是否有此行。"""
        conn = self._conn()
        with self._write_lock:
            if ttl is None:
                cur = conn.execute(
                    "UPDATE http_cache SET fetched_at = ? WHERE key = ?",
                    (time.time(), key))
            else:
                cur = conn.execute(
                    "UPDATE http_cache SET fetched_at = ?, ttl = ? WHERE key = ?",
                    (time.time(), float(ttl), key))
            conn.commit()
            return cur.rowcount > 0

    def delete(self, key: str) -> None:
        """删除一条缓存。"""
        conn = self._conn()
        with self._write_lock:
            conn.execute("DELETE FROM http_cache WHERE key = ?", (key,))
            conn.commit()

    def clear(self) -> None:
        """清空 http_cache 表。"""
        conn = self._conn()
        with self._write_lock:
            conn.execute("DELETE FROM http_cache")
            conn.commit()

    def close(self) -> None:
        """关闭当前线程的连接（其他线程的连接由各自线程负责）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    # ---------------- 过期判定 ----------------

    @staticmethod
    def entry_expired(entry: dict[str, Any] | None) -> bool:
        """判断一行缓存是否过期：无行、ttl<=0（永不过期）除外。"""
        if entry is None:
            return True
        ttl = entry.get("ttl") or 0.0
        if ttl <= 0:
            return False
        return time.time() >= float(entry.get("fetched_at") or 0.0) + ttl

    def expired(self, key: str) -> bool:
        """按 key 判断缓存是否过期（无缓存视为过期，需重新获取）。"""
        return self.entry_expired(self.get(key))
