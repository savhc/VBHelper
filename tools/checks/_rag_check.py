# -*- coding: utf-8 -*-
"""项目 RAG 自检：python _rag_check.py（无 GUI）。

覆盖：数据库迁移、扫描忽略规则、二进制/超大/密钥跳过、Markdown/代码分块、
多线程索引、增量更新/删除、全文/中文/短词检索、重建、取消、Feature 登记。
"""
import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper.services.rag import engine
from vbhelper.store.db import Database

fails, oks = [], []


def check(name, cond, detail=""):
    (oks if cond else fails).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + ((" | " + str(detail)) if detail and not cond else ""))


def write(root, rel, text, encoding="utf-8"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding) as fh:
        fh.write(text)
    return path


def main():
    base = tempfile.mkdtemp(prefix="vbrag_")
    root = os.path.join(base, "proj")
    os.makedirs(root)
    write(root, "src/app.py", "def hello_rag():\n"
          "    return 'hello world'\n\n" +
          "\n".join("value_%d = %d" % (i, i) for i in range(140)))
    write(root, "docs/guide.md", "# 项目指南\n\n这是中文索引测试。\n\n"
          "## 安装\n\npip install demo\n")
    write(root, "src/util.js", "export function greet(name) {\n"
          "  return `hi ${name}`;\n}\n")
    write(root, "node_modules/pkg/index.js", "SHOULD_NOT_INDEX_NODE\n")
    write(root, ".git/config", "SECRET_GIT\n")
    write(root, ".env", "API_KEY=SHOULD_NOT_INDEX_ENV\n")
    with open(os.path.join(root, "logo.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    with open(os.path.join(root, "huge.txt"), "w", encoding="utf-8") as fh:
        fh.write("big_marker\n" * 120000)

    db = Database(os.path.join(base, "rag.db"))
    db.migrate()
    tables = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("迁移 v2 表", {"rag_projects", "rag_files", "rag_chunks",
                         "rag_fts", "rag_fts_tri"} <= tables, sorted(tables))

    events = []
    stats = engine.index_project(db, root, threads=4,
                                 progress=events.append)
    check("索引 3 个有效文件", stats["files"] == 3, stats)
    check("块数 > 0", stats["chunks"] > 0, stats["chunks"])
    check("跳过二进制/密钥/超大", stats["skipped"] >= 3, stats["skipped"])
    check("索引事件含扫描/分块/完成",
          {"scan_start", "scan_done", "parse_start", "done"}
          <= {e.get("phase") for e in events},
          [e.get("phase") for e in events])
    check("FTS 行数与 chunks 一致",
          db.query_one("SELECT COUNT(*) n FROM rag_fts")["n"]
          == db.query_one("SELECT COUNT(*) n FROM rag_chunks")["n"]
          == db.query_one("SELECT COUNT(*) n FROM rag_fts_tri")["n"])
    fts_row = db.query_one(
        "SELECT text FROM rag_fts WHERE rag_fts MATCH 'hello_rag' LIMIT 1")
    check("FTS text 存正文（不是路径）",
          fts_row is not None and "hello_rag" in str(fts_row["text"]),
          dict(fts_row) if fts_row is not None else None)
    check("FTS MATCH 能命中正文",
          db.query_one("SELECT COUNT(*) n FROM rag_fts "
                       "WHERE rag_fts MATCH 'hello_rag'")["n"] > 0)

    r = engine.search(db, root, "hello world")
    check("英文检索命中 app.py", r["hits"] and r["hits"][0]["rel_path"]
          == "src/app.py", [(h["rel_path"], h["start_line"]) for h in r["hits"]])
    check("命中带行号/绝对路径", r["hits"]
          and r["hits"][0]["start_line"] >= 1
          and r["hits"][0]["abs_path"].endswith("app.py"), r["hits"][:1])
    check("相关文件列表", "src/app.py" in r["files"], r["files"])

    r = engine.search(db, root, "中文索引")
    check("中文检索命中 guide.md", r["hits"] and r["hits"][0]["rel_path"]
          == "docs/guide.md", [(h["rel_path"], h["snippet"]) for h in r["hits"]])
    r = engine.search(db, root, "索引")
    check("两字中文检索（LIKE 兜底）", r["hits"]
          and any(h["rel_path"] == "docs/guide.md" for h in r["hits"]),
          [(h["rel_path"], h["snippet"]) for h in r["hits"]])
    r = engine.search(db, root, "greet")
    check("JS 检索命中 util.js", r["hits"] and r["hits"][0]["rel_path"]
          == "src/util.js", [(h["rel_path"]) for h in r["hits"]])
    for needle in ("SHOULD_NOT_INDEX_NODE", "SHOULD_NOT_INDEX_ENV",
                   "SECRET_GIT", "big_marker"):
        check("不索引：%s" % needle,
              not engine.search(db, root, needle)["hits"])

    stats2 = engine.index_project(db, root, threads=4)
    check("增量索引保持不变", stats2["kept"] == 3 and stats2["chunks"]
          == stats["chunks"], stats2)
    write(root, "src/app.py", "def changed_marker():\n    return 'new'\n")
    stats3 = engine.index_project(db, root, threads=4)
    check("修改文件后重索引", engine.search(db, root, "changed_marker")["hits"]
          and not engine.search(db, root, "hello world")["hits"], stats3)
    check("修改后 FTS 不重复",
          db.query_one("SELECT COUNT(*) n FROM rag_fts")["n"]
          == db.query_one("SELECT COUNT(*) n FROM rag_chunks")["n"])
    os.remove(os.path.join(root, "docs", "guide.md"))
    stats4 = engine.index_project(db, root, threads=4)
    check("删除文件后清理索引", stats4["deleted"] >= 1
          and not engine.search(db, root, "中文索引")["hits"], stats4)
    stats5 = engine.index_project(db, root, full=True, threads=4)
    check("full 重建", stats5["files"] == 2 and stats5["chunks"] > 0,
          stats5)
    check("重建后 FTS 一致",
          db.query_one("SELECT COUNT(*) n FROM rag_fts")["n"]
          == db.query_one("SELECT COUNT(*) n FROM rag_chunks")["n"])

    # 取消：预先 set stop，索引应尽快返回 cancelled
    stop = threading.Event()
    stop.set()
    stats6 = engine.index_project(db, root, threads=2, stop=stop)
    check("取消返回 cancelled", stats6.get("cancelled") is True, stats6)

    try:
        engine.search(db, os.path.join(base, "not-indexed"), "x")
        check("未索引项目报错", False)
    except engine.RagError:
        check("未索引项目报错", True)

    # MAX 上限：最多文件数会截断扫描
    cap_root = os.path.join(base, "cap-project")
    os.makedirs(cap_root, exist_ok=True)
    for i in range(8):
        write(cap_root, "m%02d.py" % i, "cap_marker_%d = %d\n" % (i, i))
    cap_stats = engine.index_project(db, cap_root, threads=2, max_files=3)
    check("max_files 截断文件数", cap_stats["files"] == 3
          and cap_stats["skipped"] >= 1, cap_stats)
    small = engine.index_project(db, cap_root, threads=2,
                                 max_file_bytes=10)
    check("max_file_bytes 跳过超限文件", small["files"] == 0, small)

    from vbhelper.features import catalog
    ids = []
    for cls in catalog():
        try:
            ids.append(getattr(cls, "id", ""))
        except Exception:
            pass
    check("Feature 登记 rag", "rag" in ids, ids)

    from vbhelper.context import ensure_feature_flags
    old = {"features": {"rag": False}}
    ensure_feature_flags(old)
    check("旧配置 rag=false 一次性升级为 true", old["features"]["rag"] is True
          and old.get("features_version") == 2, old)
    new = {"features": {"rag": False}, "features_version": 2}
    ensure_feature_flags(new)
    check("新版本用户关闭 rag 被尊重", new["features"]["rag"] is False, new)

    db.close()
    import shutil
    shutil.rmtree(base, ignore_errors=True)
    print("\n%s" % ("=" * 52))
    print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
