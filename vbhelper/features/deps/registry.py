"""远端版本库查询。

只用标准库 + ``ui/http``：PyPI、npm、Maven Central、Go proxy 上取最新版，
OSV 批量查漏洞。所有响应进 SQLite http_cache（版本 24h、漏洞 6h），
离线/超时回退缓存，拿不到就返回错误让上层标 unknown。

"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET

VERSION_TTL = 24 * 3600
OSV_TTL = 6 * 3600
OSV_BATCH = 200                  # OSV 单次请求的 query 上限

_OSV_ECO = {"pypi": "PyPI", "npm": "npm", "maven": "Maven", "go": "Go"}

def _go_escape(path: str) -> str:
    """Go module proxy 要求大写字母转成 ``!x``。"""
    return "".join("!" + ch.lower() if ch.isupper() else ch
                   for ch in path)

def _shrink(vuln: dict) -> dict:
    """把 OSV 记录压成 UI/报告需要的几个字段。"""
    fixed: list[str] = []
    for affected in vuln.get("affected") or []:
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                value = event.get("fixed")
                if value and value not in fixed:
                    fixed.append(value)
    severity = ""
    db_specific = vuln.get("database_specific") or {}
    if isinstance(db_specific.get("severity"), str):
        severity = db_specific["severity"]
    if not severity:
        for item in vuln.get("severity") or []:
            if isinstance(item, dict) and item.get("score"):
                severity = str(item["score"])
                break
    summary = vuln.get("summary") or (vuln.get("details") or "")[:200]
    return {"id": vuln.get("id") or "",
            "aliases": list(vuln.get("aliases") or [])[:4],
            "summary": summary,
            "severity": severity.upper() if len(severity) <= 8 else severity,
            "fixed": fixed[:8]}

class RemoteRegistry:
    """最新版本 + 漏洞查询。实例无状态，可在后台线程并发调用。"""

    def __init__(self, http, cache=None, *, timeout: float = 8.0,
                 version_ttl: float = VERSION_TTL,
                 osv_ttl: float = OSV_TTL) -> None:
        self.http = http
        self.cache = cache if cache is not None else getattr(http, "_cache", None)
        self.timeout = timeout
        self.version_ttl = version_ttl
        self.osv_ttl = osv_ttl

    # ---------------- 最新版本 ----------------

    def latest(self, ecosystem: str, name: str) -> tuple[str | None, BaseException | None]:
        """返回 ``(最新版本, 错误)``；不支持的生态返回 (None, None)。"""
        try:
            if ecosystem == "pypi":
                return self._pypi(name), None
            if ecosystem == "npm":
                return self._npm(name), None
            if ecosystem == "maven":
                return self._maven(name), None
            if ecosystem == "go":
                return self._go(name), None
        except BaseException as exc:          # noqa: BLE001 - 交给上层分类
            return None, exc
        return None, None

    def _pypi(self, name: str) -> str | None:
        url = "https://pypi.org/pypi/%s/json" % urllib.parse.quote(name)
        data, _ = self.http.get_json(url, cache_ttl=self.version_ttl,
                                     timeout=self.timeout)
        return (data.get("info") or {}).get("version") or None

    def _npm(self, name: str) -> str | None:
        url = "https://registry.npmjs.org/%s" % urllib.parse.quote(name, safe="")
        data, _ = self.http.get_json(url, cache_ttl=self.version_ttl,
                                     timeout=self.timeout)
        return (data.get("dist-tags") or {}).get("latest") or None

    def _maven(self, name: str) -> str | None:
        group, _, artifact = name.partition(":")
        if not group or not artifact:
            return None
        path = group.replace(".", "/")
        url = ("https://repo1.maven.org/maven2/%s/%s/maven-metadata.xml"
               % (path, artifact))
        resp = self.http.get_text(url, cache_ttl=self.version_ttl,
                                  timeout=self.timeout)
        root = ET.fromstring(resp.text())
        for tag in ("release", "latest"):
            node = root.find(".//versioning/%s" % tag)
            if node is not None and (node.text or "").strip():
                return node.text.strip()
        versions = [v.text.strip() for v in root.findall(".//versions/version")
                    if (v.text or "").strip()]
        return versions[-1] if versions else None

    def _go(self, name: str) -> str | None:
        url = ("https://proxy.golang.org/%s/@latest"
               % _go_escape(name).strip("/"))
        data, _ = self.http.get_json(url, cache_ttl=self.version_ttl,
                                     timeout=self.timeout)
        return data.get("Version") or None

    # ---------------- 漏洞 ----------------

    def vulns(self, deps, *, limit: int = 500) -> dict[tuple, list[dict]]:
        """批量查询 OSV。

        入参是 ``parsers.Dependency`` 序列；返回
        ``{(生态, 小写包名, 当前版本): [cve...]}``。超过 limit 的条目跳过，
        避免大型 lockfile 把请求量打爆。
        """
        queries: list[dict] = []
        keys: list[tuple] = []
        for dep in deps:
            if len(queries) >= limit:
                break
            version = (dep.current or "").strip()
            ecosystem = _OSV_ECO.get(dep.ecosystem)
            if not ecosystem or not version:
                continue
            if dep.ecosystem == "go" and version[:1] == "v":
                version = version[1:]
            queries.append({"package": {"ecosystem": ecosystem,
                                        "name": dep.name},
                            "version": version})
            keys.append((dep.ecosystem, dep.name.lower(),
                         (dep.current or "").strip()))
        out: dict[tuple, list[dict]] = {}
        for start in range(0, len(queries), OSV_BATCH):
            chunk = queries[start:start + OSV_BATCH]
            chunk_keys = keys[start:start + OSV_BATCH]
            results = self._osv(chunk)
            if results is None:
                continue
            for key, item in zip(chunk_keys, results):
                vulns = (item or {}).get("vulns") or []
                if vulns:
                    out[key] = [_shrink(v) for v in vulns]
        return out

    def _osv(self, queries: list[dict]) -> list | None:
        payload = {"queries": queries}
        key = "osv:" + hashlib.sha1(
            json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            data, _ = self.http.post_json("https://api.osv.dev/v1/querybatch",
                                          payload, timeout=12.0)
        except BaseException:                 # noqa: BLE001 - 漏洞查询失败不致命
            return None
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, list):
            self._cache_put(key, results)
            return results
        return None

    def _cache_get(self, key: str):
        if self.cache is None:
            return None
        try:
            entry = self.cache.get(key)
        except Exception:
            return None
        if not entry:
            return None
        ttl = float(entry.get("ttl") or 0)
        if ttl > 0 and time.time() - float(entry.get("fetched_at") or 0) > ttl:
            return None
        try:
            return json.loads(bytes(entry.get("body") or b"").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _cache_put(self, key: str, results: list) -> None:
        if self.cache is None:
            return
        try:
            self.cache.put(key, body=json.dumps(results).encode("utf-8"),
                           ttl=self.osv_ttl)
        except Exception:
            pass
