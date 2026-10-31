# -*- coding: utf-8 -*-
"""审计日志：落 ``audit_log`` 表，写入前递归脱敏，永不把异常抛给工具链路。

脱敏规则（:func:`redact`）：
- dict 键命中敏感名（``authorization``/``token``/``password``/``secret``/
  ``api_key``/``cookie``/``bearer`` 等，或 ``*_token`` / ``*_key`` 后缀）时值换成 ``***``；
- 文本里 ``Bearer xxx``、``sk-xxx``、``dp1:...``、``token=...`` 等键值对打码；
- 普通 ``keys`` / ``monkey`` 不受影响（精确键名 + 下划线后缀规则，不用“包含 key”）。

用法::

    from vbhelper.store.audit import AuditLog

    audit = AuditLog(db)
    audit.log("mcp", "input.type", {"text": "hello"}, ok=True, elapsed_ms=12)
    audit.recent(20, scope="mcp")
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from typing import Any

# 精确键名（小写、连字符归一成下划线后比较）
_SENSITIVE_KEYS = {
    "authorization", "token", "password", "passwd", "secret", "api_key",
    "apikey", "cookie", "bearer", "access_token", "refresh_token",
    "client_secret", "set_cookie", "x_api_key", "auth",
}
# 后缀规则：token_ref / api_key_ref 这类“引用名”不命中
_SENSITIVE_SUFFIXES = ("_token", "_key", "_secret", "_password", "_passwd")

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s\"',;}]+")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_DP1_RE = re.compile(r"\bdp1:[A-Za-z0-9+/=]+")
# token=... / "api_key": "..." / password: ... 等键值对（允许 access_token 之类前缀）
_KV_RE = re.compile(
    r"(?i)([A-Za-z0-9_-]*(?:token|password|passwd|secret|api[-_]?key|apikey"
    r"|authorization|cookie|bearer|x[-_]api[-_]key|set[-_]cookie))"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',;}\]]+)")


def _kv_mask(match: re.Match[str]) -> str:
    return match.group(1) + match.group(2) + "***"


def _redact_text(text: str) -> str:
    """纯文本脱敏（顺序：Header -> 已知前缀密钥 -> 键值对）。"""
    if not text:
        return text
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _SK_RE.sub("sk-***", text)
    text = _DP1_RE.sub("dp1:***", text)
    text = _KV_RE.sub(_kv_mask, text)
    return text


def _is_sensitive_key(key: Any) -> bool:
    name = str(key).strip().lower().replace("-", "_")
    if not name:
        return False
    if name in _SENSITIVE_KEYS:
        return True
    return name.endswith(_SENSITIVE_SUFFIXES)


def redact(value: Any) -> Any:
    """递归脱敏：str/dict/list 结构化处理，其它类型 ``str()`` 后按文本处理。"""
    if value is None:
        return None
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, val in value.items():
            if _is_sensitive_key(key):
                out[key] = "***"
            else:
                out[key] = redact(val)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return _redact_text(str(value))


def _stderr(msg: str) -> None:
    try:
        print("[audit] " + msg, file=sys.stderr)
    except Exception:
        pass


class AuditLog:
    """审计写入/查询。单条 detail 截断到 ``max_detail`` 字符。"""

    def __init__(self, db: Any, *, max_detail: int = 2000) -> None:
        self.db = db
        self.max_detail = int(max_detail)

    # ------------------------------------------------------------ 内部

    def _detail_text(self, detail: Any) -> str:
        """dict/list 先结构化脱敏再序列化；最后整体再按文本脱敏一次。"""
        if detail is None:
            text = ""
        elif isinstance(detail, (dict, list, tuple)):
            try:
                text = json.dumps(redact(detail), ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(redact(detail))
        else:
            text = str(detail)
        text = _redact_text(text)
        if self.max_detail > 0 and len(text) > self.max_detail:
            text = text[:self.max_detail]
        return text

    def _insert(self, row: tuple[Any, ...]) -> None:
        self.db.execute(
            "INSERT INTO audit_log (ts, scope, action, detail, ok, elapsed_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)", row)

    # ------------------------------------------------------------ 写

    def log(self, scope: str, action: str, detail: Any = None, *,
            ok: bool = True, elapsed_ms: int = 0) -> None:
        """记录一条审计。任何异常都吞掉（只 stderr），不影响工具调用。"""
        try:
            text = self._detail_text(detail)
            row = (time.time(), str(scope), str(action), text,
                   1 if ok else 0, int(elapsed_ms or 0))
            try:
                self._insert(row)
            except Exception:
                # 表还没建（调用方忘了 migrate）——补一次迁移后重试，仍失败就放弃。
                self.db.migrate()
                self._insert(row)
        except Exception as exc:
            _stderr("写入审计失败：%r" % (exc,))

    # ------------------------------------------------------------ 读

    def recent(self, limit: int = 200, scope: str | None = None) -> list[dict]:
        """最近的审计记录，``ts`` 倒序；detail 保持落库时的字符串。"""
        try:
            limit = max(1, int(limit))
            if scope is None:
                rows = self.db.query(
                    "SELECT id, ts, scope, action, detail, ok, elapsed_ms"
                    " FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
            else:
                rows = self.db.query(
                    "SELECT id, ts, scope, action, detail, ok, elapsed_ms"
                    " FROM audit_log WHERE scope = ?"
                    " ORDER BY ts DESC, id DESC LIMIT ?", (scope, limit))
        except Exception as exc:
            _stderr("读取审计失败：%r" % (exc,))
            return []
        items = []
        for row in rows:
            item = dict(row)
            item["ok"] = bool(item.get("ok"))
            items.append(item)
        return items

    def clear(self, scope: str | None = None) -> None:
        """清空审计（可按 scope）。"""
        if scope is None:
            self.db.execute("DELETE FROM audit_log")
        else:
            self.db.execute("DELETE FROM audit_log WHERE scope = ?", (scope,))

    def export(self, path: str) -> int:
        """导出全部审计为 JSONL（ts 升序），返回条数。失败向上抛。"""
        rows = self.db.query(
            "SELECT id, ts, scope, action, detail, ok, elapsed_ms"
            " FROM audit_log ORDER BY ts ASC, id ASC")
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        count = 0
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for row in rows:
                    item = dict(row)
                    item["ok"] = bool(item.get("ok"))
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    count += 1
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return count
