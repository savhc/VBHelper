# -*- coding: utf-8 -*-
"""存储层自检：python _store_check.py（无 GUI，纯标准库）。

覆盖：Database 迁移幂等/并发/事务回滚、与 HttpCache 共用同一 DB 文件、
DPAPI 往返与密钥库落盘、审计脱敏/截断/查询/导出、MCP 配置深合并、
token 生成/复用/轮换、endpoint 原子读写与损坏文件容错。

退出码：全部通过 0，有失败 1。
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper.features.mcp import config as mcfg
from vbhelper.store import secrets as sec
from vbhelper.store.audit import AuditLog, redact
from vbhelper.store.cache import HttpCache
from vbhelper.store.cache import default_db_path as cache_default_db_path
from vbhelper.store.db import Database, default_db_path

fails = []
oks = []
root = tempfile.mkdtemp(prefix="vbhelper_store_check_")


def check(name, cond, detail=""):
    (oks if cond else fails).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + ((" | " + str(detail)) if detail and not cond else ""))


def section(title, fn):
    print("== %s ==" % title)
    try:
        fn()
    except Exception:
        fails.append(title + "（异常）")
        traceback.print_exc()


# ==================================================================== 1
def t_database():
    path = os.path.join(root, "db1", "vbhelper.db")
    db = Database(path)
    check("Database.path 正确", db.path == path, db.path)
    check("default_db_path 与 HttpCache 一致",
          default_db_path() == cache_default_db_path(),
          (default_db_path(), cache_default_db_path()))
    db.migrate()
    db.migrate()                                  # 第二次必须无副作用
    tables = {r["name"] for r in
              db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    check("audit_log / schema_version 建表",
          {"audit_log", "schema_version"} <= tables, tables)
    row = db.query_one("SELECT version FROM schema_version")
    check("schema_version 只有一行 v2",
          row is not None and int(row["version"]) == 2
          and len(db.query("SELECT version FROM schema_version")) == 1, row)
    check("RAG 表已建立",
          {"rag_projects", "rag_files", "rag_chunks",
           "rag_fts", "rag_fts_tri"} <= tables,
          sorted(tables))
    check("row_factory=sqlite3.Row",
          isinstance(db.query_one("SELECT 1 AS x"), sqlite3.Row))
    conn = db.connection()
    check("journal_mode=WAL",
          str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal",
          conn.execute("PRAGMA journal_mode").fetchone()[0])
    check("busy_timeout=3000",
          int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 3000)
    check("foreign_keys=ON",
          int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1)

    db.executemany(
        "INSERT INTO audit_log (ts, scope, action, detail, ok, elapsed_ms)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(time.time(), "many", "a%d" % i, "", 1, 0) for i in range(3)])
    check("executemany 写入 3 条",
          db.query_one("SELECT COUNT(*) AS c FROM audit_log")["c"] == 3)

    # 事务回滚（直接 conn.execute）
    try:
        with db.transaction() as c:
            c.execute("INSERT INTO audit_log (ts, scope, action, detail,"
                      " ok, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?)",
                      (time.time(), "tx", "boom", "", 1, 0))
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    check("事务异常回滚",
          db.query_one("SELECT COUNT(*) AS c FROM audit_log")["c"] == 3)
    # 事务内 db.execute 不得提前提交
    try:
        with db.transaction():
            db.execute("INSERT INTO audit_log (ts, scope, action, detail,"
                       " ok, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?)",
                       (time.time(), "tx", "boom2", "", 1, 0))
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    check("事务内 execute 不提前提交",
          db.query_one("SELECT COUNT(*) AS c FROM audit_log")["c"] == 3)
    with db.transaction() as c:
        c.execute("INSERT INTO audit_log (ts, scope, action, detail,"
                  " ok, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?)",
                  (time.time(), "tx", "commit", "", 1, 0))
    check("事务提交生效",
          db.query_one("SELECT COUNT(*) AS c FROM audit_log")["c"] == 4)

    # 并发：8 线程 × 50 条
    audit = AuditLog(db)
    errors = []

    def worker(i):
        try:
            for j in range(50):
                audit.log("concurrent", "w%d" % i, {"i": i, "j": j}, elapsed_ms=j)
        except Exception as exc:                  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    alive = [t for t in threads if t.is_alive()]
    count = db.query_one(
        "SELECT COUNT(*) AS c FROM audit_log WHERE scope='concurrent'")["c"]
    check("并发 8×50=400 条审计", count == 400 and not errors and not alive,
          (count, errors[:1], len(alive)))
    db.close()


# ==================================================================== 2
def t_httpcache_share():
    path = os.path.join(root, "shared", "vbhelper.db")
    cache = HttpCache(path)
    cache.put("https://example.com/a", body=b"hello", headers={"X-Test": "1"},
              ttl=60)
    db = Database(path)
    db.migrate()
    row = cache.get("https://example.com/a")
    check("Database.migrate 后 HttpCache 仍可读",
          row is not None and row["body"] == b"hello", row)
    check("http_cache 表未被破坏",
          db.query_one("SELECT COUNT(*) AS c FROM http_cache")["c"] == 1)
    check("audit_log 与 http_cache 共存",
          db.query_one("SELECT name FROM sqlite_master WHERE type='table'"
                       " AND name='audit_log'") is not None)
    cache.put("https://example.com/b", body=b"world")
    check("迁移后 HttpCache 仍可写",
          cache.get("https://example.com/b") is not None)
    db.execute("INSERT INTO audit_log (ts, scope, action, detail, ok,"
               " elapsed_ms) VALUES (?, ?, ?, ?, ?, ?)",
               (time.time(), "shared", "x", "", 1, 0))
    check("HttpCache 可读到 Database 的连接不受影响",
          cache.get("https://example.com/a")["headers"] == {"X-Test": "1"})
    cache.close()
    db.close()


# ==================================================================== 3
def t_secrets():
    enc = sec.dpapi_encrypt("s3cr3t-token-中文-123")
    check("密文带 dp1: 前缀", enc.startswith("dp1:"), enc[:12])
    check("密文不含明文", "s3cr3t-token" not in enc)
    check("DPAPI 往返还原",
          sec.dpapi_decrypt(enc) == "s3cr3t-token-中文-123")
    check("空值透传",
          sec.dpapi_encrypt("") == "" and sec.dpapi_decrypt("") == "")
    check("兼容旧明文", sec.dpapi_decrypt("legacy-plain") == "legacy-plain")
    masked = sec.mask_key("sk-abcdef1234567890xyz")
    check("mask 前 5 后 4",
          masked.startswith("sk-ab") and masked.endswith("0xyz")
          and "cdef" not in masked and "•" in masked, masked)
    check("mask 短 key 全打码", sec.mask_key("sk-1") == "••••")
    check("mask 9 字符全打码", sec.mask_key("123456789") == "•" * 9)

    path = os.path.join(root, "secrets", "secrets.json")
    store = sec.SecretStore(path)
    check("空密钥库 get 返回默认值", store.get("mcp.token", "d") == "d")
    store.set("mcp.token", "plain-secret-value")
    raw = open(path, encoding="utf-8").read()
    check("落盘文件不含明文", "plain-secret-value" not in raw, raw[:80])
    check("落盘文件含 dp1:", "dp1:" in raw)
    check("同一实例读回", store.get("mcp.token") == "plain-secret-value")
    check("新实例读回", sec.SecretStore(path).get("mcp.token")
          == "plain-secret-value")
    check("has()", store.has("mcp.token") is True)
    store.set("", "x")                       # 空 name 等于删除，无副作用
    check("set('') 不影响已有项",
          store.get("mcp.token") == "plain-secret-value")
    store.set("tmp.key", "v")
    store.delete("tmp.key")
    check("删除生效", store.get("tmp.key") == "")
    store.delete("不存在")
    check("删除不存在的 key 不报错", True)
    check("新实例看不到已删项",
          sec.SecretStore(path).get("tmp.key") == "")

    bad_path = os.path.join(root, "secrets", "bad.json")
    with open(bad_path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "items": {"broken": "dp1:AAAA"}}, f)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        bad = sec.SecretStore(bad_path)
        got = bad.get("broken", "fallback")
        has_bad = bad.has("broken")
    check("解密失败返回默认值", got == "fallback", got)
    check("解密失败 has=False", has_bad is False)
    check("解密失败写 stderr 提示", "解密" in buf.getvalue(), buf.getvalue())

    broken_path = os.path.join(root, "secrets", "broken.json")
    with open(broken_path, "w", encoding="utf-8") as f:
        f.write("{not json")
    with contextlib.redirect_stderr(io.StringIO()):
        broken = sec.SecretStore(broken_path)
    check("损坏 JSON 视为空库", broken.get("x", "") == "" and len(broken) == 0)

    default_store = sec.SecretStore()
    check("无参构造默认路径",
          default_store.path.endswith(os.path.join("VBHelper", "secrets.json")),
          default_store.path)


# ==================================================================== 4
def t_redact():
    data = {
        "headers": {"Authorization": "Bearer abcdefgh123456",
                    "Accept": "application/json"},
        "api_key": "sk-abcdefgh12345678",
        "authorization": "raw-secret-value",
        "nested": [{"token": "tok-secret-1"},
                   {"monkey": "banana", "keys": ["a", "b"]}],
    }
    red = redact(data)
    check("Authorization 头脱敏",
          red["headers"]["Authorization"] == "***")
    check("敏感字段 api_key 脱敏", red["api_key"] == "***")
    check("敏感字段 authorization 脱敏", red["authorization"] == "***")
    check("嵌套 dict/list 脱敏", red["nested"][0]["token"] == "***")
    check("monkey 不误伤", red["nested"][1]["monkey"] == "banana")
    check("keys 不误伤", red["nested"][1]["keys"] == ["a", "b"])
    check("普通值不动", red["headers"]["Accept"] == "application/json")
    check("入参未被原地修改", data["api_key"] == "sk-abcdefgh12345678")

    text = "Authorization: Bearer abcdefgh123456"
    out = redact(text)
    check("文本 Bearer 脱敏", "abcdefgh" not in out and "***" in out, out)
    out = redact("key=sk-abcdefgh12345678")
    check("文本 sk- 脱敏", "abcdefgh" not in out and "***" in out, out)
    out = redact("密文 dp1:QUJDREVGRw== 泄露")
    check("文本 dp1: 脱敏", "QUJDREVGRw" not in out and "dp1:***" in out, out)
    out = redact('{"password": "hunter2", "access_token": "tok-abc"}')
    check("文本 password/access_token 脱敏",
          "hunter2" not in out and "tok-abc" not in out, out)
    out = redact('{"bearer": "bearer-secret", "x-api-key": "xsecret-1"}')
    check("文本 bearer/x-api-key 脱敏",
          "bearer-secret" not in out and "xsecret-1" not in out, out)
    check("文本 monkey/keys/tokenizer 不误伤",
          redact("monkey key keys tokenizer") == "monkey key keys tokenizer")
    check("未知对象 str 后处理", redact(42) == "42" and redact(None) is None)
    nested_list = redact(["Bearer abcdefgh123456", {"secret": "s"}])
    check("列表脱敏", "abcdefgh" not in nested_list[0]
          and nested_list[1]["secret"] == "***", nested_list)


# ==================================================================== 5
def t_audit():
    db = Database(os.path.join(root, "audit", "vbhelper.db"))
    db.migrate()
    audit = AuditLog(db)
    audit.log("mcp", "screen.capture", {
        "mode": "full",
        "headers": {"Authorization": "Bearer abcdefgh12345678"},
        "api_key": "sk-abcdefgh12345678",
        "note": "ok",
    }, ok=True, elapsed_ms=33)
    audit.log("mcp", "input.type", {"text_len": 5}, ok=False, elapsed_ms=7)
    audit.log("other", "system.info", "plain detail")
    rows = audit.recent(10)
    check("recent 返回 3 条", len(rows) == 3, len(rows))
    check("recent 按 ts 倒序",
          rows[0]["action"] == "system.info"
          and rows[-1]["action"] == "screen.capture", rows)
    item = rows[-1]
    check("detail 是字符串", isinstance(item["detail"], str), type(item["detail"]))
    check("落库 detail 已脱敏",
          "abcdefgh" not in item["detail"] and "***" in item["detail"],
          item["detail"])
    check("ok/elapsed_ms 映射",
          item["ok"] is True and item["elapsed_ms"] == 33
          and rows[1]["ok"] is False and rows[1]["elapsed_ms"] == 7)
    raw = db.query_one("SELECT detail FROM audit_log WHERE action='screen.capture'")
    check("数据库里也没有明文", "abcdefgh" not in raw["detail"], raw["detail"])
    check("scope 过滤",
          len(audit.recent(10, scope="mcp")) == 2
          and len(audit.recent(10, scope="other")) == 1
          and len(audit.recent(10, scope="none")) == 0)
    check("detail 原样（未含密钥的短字符串）",
          audit.recent(1)[0]["detail"] == "plain detail")

    audit.log("mcp", "long", "x" * 5000)
    long_row = audit.recent(1)[0]
    check("detail 默认截断 2000",
          len(long_row["detail"]) == 2000, len(long_row["detail"]))
    small = AuditLog(db, max_detail=50)
    small.log("mcp", "small", "y" * 500)
    check("max_detail 可配置",
          len(small.recent(1)[0]["detail"]) == 50)

    export_path = os.path.join(root, "audit", "export.jsonl")
    n = audit.export(export_path)
    lines = [json.loads(line) for line in
             open(export_path, encoding="utf-8").read().splitlines()]
    check("export 条数与文件行数", n == 5 and len(lines) == 5, (n, len(lines)))
    check("export 内容可解析且已脱敏",
          all("detail" in item for item in lines)
          and all("abcdefgh" not in (item.get("detail") or "")
                  for item in lines))

    audit.clear(scope="other")
    check("clear(scope) 只清指定范围",
          len(audit.recent(50, scope="other")) == 0
          and len(audit.recent(50, scope="mcp")) == 4)
    audit.clear()
    check("clear() 清空全部", audit.recent(10) == [])
    db.close()

    # 未显式 migrate 时首写自动补迁移
    db2 = Database(os.path.join(root, "audit", "lazy.db"))
    AuditLog(db2).log("mcp", "lazy", "x")
    check("未 migrate 也能写审计",
          len(AuditLog(db2).recent(10)) == 1)
    db2.close()


# ==================================================================== 6
class FakeCtx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.saved = 0

    def save_cfg(self):
        self.saved += 1


class BadCtx:
    def save_cfg(self):
        raise RuntimeError("disk full")


def t_config():
    snapshot = json.loads(json.dumps(mcfg.DEFAULT_CONFIG))
    user = {"mcp": {"server": {"port": 52341},
                    "tools": {"input.type": {"enabled": False}},
                    "extra": {"a": 1}}}
    ctx = FakeCtx({"mcp": user["mcp"], "features": {}})
    cfg = mcfg.get(ctx)
    check("用户已有值优先", cfg["server"]["port"] == 52341)
    check("缺失默认值补齐",
          cfg["server"]["max_sessions"] == 8
          and cfg["server"]["bind"] == "127.0.0.1"
          and cfg["server"]["auto_start"] is True
          and cfg["server"]["token_ref"] == "mcp.token"
          and cfg["confirm"]["mode"] == "ask"
          and cfg["input"]["enabled"] is False
          and cfg["screen"]["allow"] is True
          and cfg["web"]["max_fetch_bytes"] == 5242880
          and cfg["search"]["provider"] == "auto")
    check("嵌套 tools 保留", cfg["tools"]["input.type"]["enabled"] is False)
    check("未知键保留", cfg["extra"] == {"a": 1})
    check("结果写回 ctx.cfg['mcp']", ctx.cfg["mcp"] is cfg)
    check("DEFAULT_CONFIG 未被修改",
          json.dumps(mcfg.DEFAULT_CONFIG, sort_keys=True) == json.dumps(
              snapshot, sort_keys=True))
    cfg["server"]["port"] = 1
    check("合并不共享 DEFAULT 引用", mcfg.DEFAULT_CONFIG["server"]["port"] == 0)

    ctx2 = FakeCtx({})
    cfg2 = mcfg.get(ctx2)
    check("无 mcp 键时创建默认配置",
          isinstance(ctx2.cfg["mcp"], dict) and cfg2["enabled"] is False
          and ctx2.cfg["mcp"] is cfg2)
    check("get 不自动落盘", ctx.saved == 0 and ctx2.saved == 0)

    mcfg.save(ctx)
    check("save 调用 ctx.save_cfg()", ctx.saved == 1)
    try:
        mcfg.save(BadCtx())
        check("save 异常向上抛", False)
    except RuntimeError:
        check("save 异常向上抛", True)


# ==================================================================== 7
class FakeSecrets:
    def __init__(self):
        self.data = {}

    def get(self, name, default=""):
        return self.data.get(name, default)

    def set(self, name, value):
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)


def t_token():
    fake = FakeSecrets()
    t1 = mcfg.ensure_token(fake)
    check("token 自动创建", bool(t1) and len(t1) >= 40, len(t1))
    check("token 写入 mcp.token", fake.data.get("mcp.token") == t1)
    check("ensure 复用已有 token", mcfg.ensure_token(fake) == t1)
    t2 = mcfg.rotate_token(fake)
    check("rotate 换新 token", t2 != t1 and fake.data.get("mcp.token") == t2)
    try:
        mcfg.ensure_token(None)
        check("secrets=None 抛 ValueError", False)
    except ValueError:
        check("secrets=None 抛 ValueError", True)
    try:
        mcfg.rotate_token(object())
        check("secrets 无 get/set 抛 ValueError", False)
    except ValueError:
        check("secrets 无 get/set 抛 ValueError", True)

    path = os.path.join(root, "secrets", "token.json")
    store = sec.SecretStore(path)
    tok = mcfg.ensure_token(store)
    check("真实密钥库 token 可读回", store.get("mcp.token") == tok)
    check("真实密钥库 ensure 复用", mcfg.ensure_token(store) == tok)
    rotated = mcfg.rotate_token(store)
    check("真实密钥库 rotate 生效",
          rotated != tok and sec.SecretStore(path).get("mcp.token") == rotated)
    raw = open(path, encoding="utf-8").read()
    check("token 文件无明文", tok not in raw and rotated not in raw
          and "dp1:" in raw)


# ==================================================================== 8
def t_endpoint():
    local = os.path.join(root, "local")
    paths = SimpleNamespace(local_dir=local, mcp_dir=os.path.join(local, "mcp"))
    ep = mcfg.endpoint_path(paths)
    check("endpoint_path 用 mcp_dir",
          ep == os.path.join(local, "mcp", "endpoint.json"), ep)
    fallback = SimpleNamespace(local_dir=local)
    check("无 mcp_dir 时回退 local_dir/mcp",
          mcfg.endpoint_path(fallback) == os.path.join(
              local, "mcp", "endpoint.json"))

    info = {"host": "127.0.0.1", "port": 52341,
            "url": "http://127.0.0.1:52341/mcp", "token": "tok123",
            "pid": 1234, "started": 1760000000.0, "app_version": "1.0.0",
            "version": 9}
    written = mcfg.write_endpoint(paths, info)
    check("write_endpoint 返回路径且落盘",
          written == ep and os.path.isfile(ep))
    data = mcfg.read_endpoint(paths)
    check("endpoint 读取",
          isinstance(data, dict) and data.get("port") == 52341
          and data.get("token") == "tok123", data)
    check("endpoint version 强制为 1", data.get("version") == 1, data)
    check("read_endpoint_file 与 read_endpoint 一致",
          mcfg.read_endpoint_file(ep) == data)

    mcfg.write_endpoint(paths, {"port": 1, "url": "http://127.0.0.1:1/mcp"})
    check("重复写覆盖旧内容",
          mcfg.read_endpoint(paths).get("port") == 1)

    missing = os.path.join(root, "endpoint", "missing.json")
    check("缺失文件返回 None", mcfg.read_endpoint_file(missing) is None)
    bad = os.path.join(root, "endpoint", "bad.json")
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{not json")
    check("坏 JSON 返回 None", mcfg.read_endpoint_file(bad) is None)
    for name, payload in (
            ("list", [1, 2, 3]),
            ("port_str", {"port": "52341", "url": "http://127.0.0.1:1/mcp"}),
            ("port_zero", {"port": 0, "url": "http://127.0.0.1:1/mcp"}),
            ("port_big", {"port": 70000, "url": "http://127.0.0.1:1/mcp"}),
            ("url_missing", {"port": 52341}),
            ("url_bad", {"port": 52341, "url": "ftp://x"})):
        p = os.path.join(root, "endpoint", name + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        check("非法 endpoint(%s) 返回 None" % name,
              mcfg.read_endpoint_file(p) is None)

    mcfg.clear_endpoint(paths)
    check("clear_endpoint 删除文件", not os.path.exists(ep))
    mcfg.clear_endpoint(paths)
    check("重复 clear 不报错", True)


# ==================================================================== main
try:
    section("1. Database 迁移/并发/事务", t_database)
    section("2. 与 HttpCache 共用 DB 文件", t_httpcache_share)
    section("3. DPAPI 与密钥库", t_secrets)
    section("4. redact 脱敏", t_redact)
    section("5. AuditLog 写入/查询/导出", t_audit)
    section("6. MCP 配置深合并", t_config)
    section("7. token 创建/复用/轮换", t_token)
    section("8. endpoint 读写/容错", t_endpoint)
finally:
    shutil.rmtree(root, ignore_errors=True)

print("\n" + "=" * 52)
print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
for f in fails:
    print("  - " + f)
sys.exit(1 if fails else 0)
