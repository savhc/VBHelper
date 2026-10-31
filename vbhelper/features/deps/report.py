"""依赖体检报告：统计、升级命令、Markdown/JSON/AI 提示词。

纯函数，不碰 Tk，可以单独在自检脚本里生成。

"""
from __future__ import annotations

import json
import os
import time

from . import parsers, versions
from .scanner import ScanResult

_STATUS_ORDER = {name: i for i, name in enumerate(versions.STATUS_ORDER)}
_SCOPE_LABEL = {"runtime": "运行时", "dev": "开发", "optional": "可选",
                "peer": "对等", "build": "构建", "provided": "容器提供"}

def counts(deps) -> dict[str, int]:
    """按状态统计（固定包含全部键，UI 直接取）。"""
    out = {name: 0 for name in versions.STATUS_ORDER}
    for dep in deps:
        out[dep.status] = out.get(dep.status, 0) + 1
    return out

def eco_counts(deps) -> dict[str, int]:
    out: dict[str, int] = {}
    for dep in deps:
        out[dep.ecosystem] = out.get(dep.ecosystem, 0) + 1
    return out

def sort_deps(deps) -> list:
    """按「严重度 -> 生态 -> 名称」排序，需要处理的排前面。"""
    return sorted(
        deps,
        key=lambda d: (_STATUS_ORDER.get(d.status, 9), d.ecosystem,
                       d.name.lower()))

def upgrade_command(dep) -> str | None:
    """按生态生成升级命令；信息不足时返回 None。"""
    if not dep.latest:
        return None
    name = dep.name
    if dep.ecosystem == "pypi":
        return "pip install -U %s==%s" % (name, dep.latest)
    if dep.ecosystem == "npm":
        flag = " -D" if dep.scope == "dev" else ""
        return "npm install%s %s@%s" % (flag, name, dep.latest)
    if dep.ecosystem == "maven":
        return "# pom.xml: <version>%s</version>  (group:artifact = %s)" % (
            dep.latest, name)
    if dep.ecosystem == "go":
        return "go get %s@%s" % (name, dep.latest)
    return None

def _source(dep) -> str:
    """来源展示：有行号才带冒号。"""
    return ("%s:%s" % (dep.source_file, dep.line) if dep.line
            else dep.source_file)

def _fmt_time(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))

def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(cell).replace("|", "\\|")
                                     for cell in row) + " |")
    return out

def build_markdown(result: ScanResult, *, generated: float | None = None) -> str:
    """生成 Markdown 报告（含结论、待处理项、全量表格与清单索引）。"""
    deps = result.deps
    stats = counts(deps)
    generated = generated or time.time()
    lines = ["# 依赖体检报告", ""]

    lines.append("- 项目：`%s`" % result.root)
    lines.append("- 生成时间：%s" % _fmt_time(generated))
    lines.append("- 清单：%d 个（浏览 %d 个目录%s）"
                 % (len(result.files), result.dirs_scanned,
                    "，已截断" if result.truncated else ""))
    lines.append("- 依赖：%d 个 · 联网核对 %d 个%s"
                 % (len(deps), result.checked,
                    ("，跳过 %d 个" % result.skipped) if result.skipped else ""))
    lines.append("- 用时：%.1f 秒 · 网络：%s"
                 % (result.elapsed, "可用" if result.online else "不可用（仅本地解析）"))
    lines.append("")
    lines.append("> 结论：%d 个有漏洞 · %d 个大版本落后 · %d 个过期 · "
                 "%d 个正常 · %d 个未知"
                 % (stats.get("vulnerable", 0), stats.get("major", 0),
                    stats.get("outdated", 0), stats.get("ok", 0),
                    stats.get("unknown", 0)))
    lines.append("")

    ordered = sort_deps(deps)
    if stats.get("vulnerable"):
        lines.append("## 有漏洞（优先处理）")
        lines.append("")
        rows = []
        for dep in ordered:
            if dep.status != "vulnerable":
                continue
            ids = ", ".join(cve.get("id", "") for cve in dep.cves[:4])
            fixed = sorted({v for cve in dep.cves for v in cve.get("fixed") or []})
            rows.append([dep.name, dep.current or "-", dep.latest or "-",
                         ids or "-", ", ".join(fixed) or "-",
                         upgrade_command(dep) or "-",
                         _source(dep)])
        lines.extend(_md_table(
            ["名称", "当前", "最新", "漏洞", "修复版本", "升级命令", "来源"],
            rows))
        lines.append("")
        for dep in ordered:
            if dep.status != "vulnerable":
                continue
            for cve in dep.cves[:3]:
                detail = " · ".join(part for part in
                                    (cve.get("severity") or "",
                                     cve.get("summary") or "") if part)
                if detail:
                    lines.append("- `%s` %s：%s" % (dep.name, cve.get("id", ""),
                                                    detail))
        lines.append("")

    for status, title in (("major", "大版本落后"), ("outdated", "过期")):
        subset = [d for d in ordered if d.status == status]
        if not subset:
            continue
        lines.append("## %s（%d）" % (title, len(subset)))
        lines.append("")
        rows = []
        for dep in subset:
            rows.append([dep.name, dep.current or "-", dep.latest or "-",
                         versions.STATUS_LABEL.get(dep.status, dep.status),
                         _source(dep),
                         upgrade_command(dep) or "-"])
        lines.extend(_md_table(["名称", "当前", "最新", "状态", "来源", "升级命令"],
                               rows))
        lines.append("")

    lines.append("## 全部依赖（%d）" % len(deps))
    lines.append("")
    rows = []
    for dep in ordered:
        rows.append([
            parsers.ECO_LABEL.get(dep.ecosystem, dep.ecosystem),
            dep.name,
            dep.declared or "-",
            dep.resolved or "-",
            dep.latest or "-",
            versions.STATUS_LABEL.get(dep.status, dep.status),
            _SCOPE_LABEL.get(dep.scope, dep.scope)
            + ("/间接" if dep.transitive else ""),
            _source(dep),
        ])
    if rows:
        lines.extend(_md_table(
            ["生态", "名称", "声明", "锁定", "最新", "状态", "范围", "来源"],
            rows))
    else:
        lines.append("没有解析到依赖。")
    lines.append("")

    lines.append("## 清单索引（%d）" % len(result.files))
    lines.append("")
    file_rows = []
    for entry in result.files:
        state = {"parsed": "已解析", "empty": "无依赖",
                 "warn": "有备注", "error": "失败"}.get(entry.state, entry.state)
        file_rows.append([entry.path,
                           parsers.ECO_LABEL.get(entry.eco, entry.eco),
                           entry.kind, entry.count, state,
                           entry.note or ""])
    if file_rows:
        lines.extend(_md_table(["文件", "生态", "类型", "依赖数", "状态", "备注"],
                               file_rows))
    if result.errors:
        lines.append("")
        lines.append("## 提示")
        lines.append("")
        for item in result.errors:
            lines.append("- %s" % item)
    lines.append("")
    lines.append("---")
    lines.append("由 VBHelper 依赖医生生成：只读扫描，不修改任何项目文件。"
                 "版本数据来自 PyPI / npm / Maven Central / Go proxy，"
                 "漏洞来自 OSV（缓存 24h / 6h）。")
    return "\n".join(lines)

def build_json(result: ScanResult, *, generated: float | None = None) -> str:
    """生成机器可读的 JSON 报告。"""
    generated = generated or time.time()
    payload = {
        "tool": "VBHelper dependency doctor",
        "generated_at": generated,
        "root": result.root,
        "elapsed": round(result.elapsed, 3),
        "online": result.online,
        "checked": result.checked,
        "skipped": result.skipped,
        "truncated": result.truncated,
        "counts": counts(result.deps),
        "files": [
            {"path": f.path, "ecosystem": f.eco, "kind": f.kind,
             "count": f.count, "state": f.state, "note": f.note}
            for f in result.files
        ],
        "dependencies": [
            {"ecosystem": d.ecosystem, "name": d.name,
             "declared": d.declared, "resolved": d.resolved,
             "latest": d.latest, "status": d.status, "scope": d.scope,
             "transitive": d.transitive, "source": d.source_file,
             "line": d.line, "note": d.note,
             "upgrade": upgrade_command(d),
             "cves": d.cves}
            for d in result.deps
        ],
        "errors": result.errors,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

def build_prompt(result: ScanResult) -> str:
    """给 AI 的上下文：只放需要处理的依赖，控制长度。"""
    stats = counts(result.deps)
    todo = [d for d in sort_deps(result.deps)
            if d.status in ("vulnerable", "major", "outdated")]
    lines = [
        "项目：%s" % result.root,
        "扫描到 %d 个依赖：%d 有漏洞、%d 大版本落后、%d 过期。"
        % (len(result.deps), stats.get("vulnerable", 0),
           stats.get("major", 0), stats.get("outdated", 0)),
        "请给出安全、分步的升级建议（先修漏洞，再处理大版本，注意破坏性变更）：",
        "",
    ]
    for dep in todo[:60]:
        cves = ", ".join(c.get("id", "") for c in dep.cves[:3])
        lines.append("- [%s] %s %s -> %s（%s）%s"
                     % (versions.STATUS_LABEL.get(dep.status, dep.status),
                        dep.name, dep.current or "?", dep.latest or "?",
                        dep.source_file, ("漏洞: " + cves) if cves else ""))
    if len(todo) > 60:
        lines.append("……另有 %d 个待处理依赖省略。" % (len(todo) - 60))
    lines.append("")
    lines.append("要求：不要改动无关文件；每个大版本升级说明迁移风险；"
                 "给出可以逐条执行的命令。")
    return "\n".join(lines)

def default_filename(root: str, suffix: str) -> str:
    """导出时用的默认文件名。"""
    name = os.path.basename(os.path.normpath(root or "")) or "project"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return "dependency-report-%s.%s" % (safe, suffix)
