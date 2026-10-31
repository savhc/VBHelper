# -*- coding: utf-8 -*-
"""统一 SQLite 底座：线程内连接、WAL、串行写锁、版本化迁移。

与 ``store/cache.py:HttpCache`` 共用同一个 ``vbhelper.db`` 文件：两边都用
``CREATE TABLE IF NOT EXISTS`` 各自建表，WAL 下读写互不阻塞；本模块**不碰**
``http_cache`` 表。

要点：
- 连接按线程创建（``threading.local``），``row_factory=sqlite3.Row``；
- ``PRAGMA journal_mode=WAL``、``busy_timeout=3000``、``foreign_keys=ON``；
- 写操作（``execute`` / ``executemany`` / 事务）用 ``RLock`` 串行化；
- ``transaction()`` 用 ``BEGIN IMMEDIATE``，异常回滚；支持同线程嵌套（外层负责提交）；
- 迁移表 ``schema_version(version INTEGER)``，``_MIGRATIONS`` 按版本递增、可追加，
  重复调用无副作用。

用法::

    from vbhelper.store.db import Database

    db = Database()                  # 默认 %LOCALAPPDATA%\\VBHelper\\db\\vbhelper.db
    db.migrate()
    db.execute("INSERT INTO audit_log ... VALUES (?, ...)", (value,))
    rows = db.query("SELECT * FROM audit_log ORDER BY ts DESC")
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from typing import Any, Iterator, Sequence

APP_DIR_NAME = "VBHelper"


def default_db_path() -> str:
    """默认数据库路径，与 ``store/cache.py:default_db_path`` 保持一致。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME, "db", "vbhelper.db")


# 版本化迁移：只能追加，不要改历史条目（旧库已按旧版本执行过）。
# v1：审计日志表（MCP 块）
_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  scope TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  ok INTEGER NOT NULL DEFAULT 1,
  elapsed_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""),
    # v2：项目 RAG 索引（chunks 与两张 FTS 表；FTS 同步由 engine 手动维护，
    # 不用触发器，避免迁移脚本按分号拆分时破坏 BEGIN...END）。
    (2, """
CREATE TABLE IF NOT EXISTS rag_projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  name TEXT,
  file_count INTEGER NOT NULL DEFAULT 0,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  total_bytes INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  created_at REAL NOT NULL,
  indexed_at REAL
);
CREATE TABLE IF NOT EXISTS rag_files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  mtime REAL NOT NULL,
  size INTEGER NOT NULL,
  lang TEXT,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  indexed_at REAL,
  UNIQUE(project_id, path)
);
CREATE INDEX IF NOT EXISTS idx_rag_files_project ON rag_files(project_id);
CREATE TABLE IF NOT EXISTS rag_chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  file_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_estimate INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_project ON rag_chunks(project_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_file ON rag_chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
  text, path UNINDEXED, project_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts_tri USING fts5(
  text, path UNINDEXED, project_id UNINDEXED,
  tokenize='trigram'
);
"""),
]


def _split_sql(script: str) -> list[str]:
    """按 ``;`` 拆分 DDL 脚本（迁移语句里没有字符串字面量分号）。

    不用 ``executescript``：它会隐式提交当前事务，破坏迁移的原子性。
    """
    return [stmt.strip() for stmt in script.split(";") if stmt.strip()]


class Database:
    """SQLite 访问入口：每线程一条连接，写操作全局串行化。

    ``path=None`` 时使用 :func:`default_db_path`；目录不存在会自动创建。
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or default_db_path()
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    @property
    def path(self) -> str:
        """数据库文件路径。"""
        return self._path

    # ------------------------------------------------------------ 连接

    def connection(self) -> sqlite3.Connection:
        """取当前线程的连接；没有则新建并设置 PRAGMA。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=3.0)
            conn.row_factory = sqlite3.Row
            # 关闭驱动层隐式事务控制：事务边界统一由 transaction() 显式管理，
            # 也避免 BEGIN IMMEDIATE 撞上驱动自动开启的事务。
            conn.isolation_level = None
            conn.execute("PRAGMA busy_timeout=3000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass          # 某些文件系统不支持 WAL，退化为默认日志模式即可
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _in_transaction(self) -> bool:
        return bool(getattr(self._local, "tx_depth", 0))

    # ------------------------------------------------------------ 迁移

    def migrate(self) -> None:
        """按 ``_MIGRATIONS`` 升级到最新版本；幂等、原子（整体一个事务）。"""
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version(version INTEGER)")
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = int(row[0] or 0) if row is not None else 0
            pending = [(ver, sql) for ver, sql in _MIGRATIONS if ver > current]
            for _ver, sql in pending:
                for stmt in _split_sql(sql):
                    conn.execute(stmt)
            if pending:
                # schema_version 只保留一行：当前版本。
                conn.execute("DELETE FROM schema_version")
                conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)",
                    (max(ver for ver, _sql in _MIGRATIONS),))

    # ------------------------------------------------------------ 写

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """执行一条写语句；不在事务中时立即提交。"""
        with self._write_lock:
            conn = self.connection()
            cur = conn.execute(sql, params)
            if not self._in_transaction():
                conn.commit()
            return cur

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        """批量写；不在事务中时立即提交。"""
        with self._write_lock:
            conn = self.connection()
            conn.executemany(sql, seq)
            if not self._in_transaction():
                conn.commit()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：``BEGIN IMMEDIATE`` 抢写锁，异常回滚，成功提交。

        同线程嵌套时复用外层事务（只有最外层提交/回滚），方便
        ``migrate()``/组合操作复用。
        """
        with self._write_lock:
            conn = self.connection()
            depth = int(getattr(self._local, "tx_depth", 0))
            if depth:
                # 嵌套：交给外层收尾，避免中间提交破坏原子性。
                self._local.tx_depth = depth + 1
                try:
                    yield conn
                finally:
                    self._local.tx_depth = depth
                return
            self._local.tx_depth = 1
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            else:
                conn.commit()
            finally:
                self._local.tx_depth = 0

    # ------------------------------------------------------------ 读

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """查询多行（Row 对象）。"""
        return list(self.connection().execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        """查询一行；不存在返回 None。"""
        return self.connection().execute(sql, params).fetchone()

    # ------------------------------------------------------------ 生命周期

    def close(self) -> None:
        """关闭当前线程的连接（其它线程的连接由各自线程负责）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def __repr__(self) -> str:                 # pragma: no cover - 调试用
        return "Database(%r)" % (self._path,)
