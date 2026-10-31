# -*- coding: utf-8 -*-
"""本机反编译器自动识别（CFR / Vineflower / Recaf 内置）。

不联网、不下载：只扫描常见目录里的 jar，检查是否带可调用的反编译主类。
Recaf 3 的 jar 里带 CFR 0.152 和 Vineflower，可直接用 `java -cp recaf.jar
org.benf.cfr.reader.Main <class>` 取源码。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass

CREATE_NO_WINDOW = 0x08000000

JAR_HINTS = ("cfr", "recaf", "vineflower", "quiltflower", "fernflower",
             "procyon", "bytecode-viewer")

MAINS = (
    ("CFR", "org/benf/cfr/reader/Main", "stdout"),
    ("Vineflower",
     "org/jetbrains/java/decompiler/main/decompiler/ConsoleDecompiler",
     "outdir"),
    ("Procyon", "com/strobel/decompiler/DecompilerDriver", "stdout"),
)


@dataclass(frozen=True)
class DecompilerInfo:
    label: str
    command: str
    java: str
    jar: str
    kind: str


_CACHE: DecompilerInfo | None = None
_JAVA_CACHE: str | None = None
_RECAF_JAVA_CACHE: str | None = None


def _quote(path: str) -> str:
    return '"' + str(path).replace('"', '\\"') + '"'


def _java_major(path: str) -> int | None:
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        proc = subprocess.run([path, "-version"], capture_output=True,
                              timeout=6, **kwargs)
        text = (proc.stderr or b"") + (proc.stdout or b"")
        text = text.decode("utf-8", "replace")
        m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if not m or proc.returncode != 0:
            return None
        major = int(m.group(1))
        if major == 1:
            major = int(m.group(2) or 0)
        return major
    except Exception:
        return None


def _java_candidates(cfg: dict | None = None, *, recaf: bool = False):
    cands = []
    dec = cfg.get("decompiler") if isinstance(cfg, dict) else None
    if isinstance(dec, dict):
        key = "recaf_java" if recaf else "java"
        if dec.get(key):
            cands.append(str(dec[key]))
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        cands.append(os.path.join(java_home, "bin", "java.exe"))
    if recaf:
        cands += [
            r"D:\java\zulu26.32.203-ca-jdk26.0.2.1-win_x64\bin\java.exe",
            r"D:\java\Java25\bin\java.exe",
            r"D:\java\java24\bin\java.exe",
        ]
    cands += [
        r"D:\java\java21\bin\java.exe",
        r"D:\java\Java25\bin\java.exe",
        r"D:\java\java24\bin\java.exe",
        shutil.which("java"),
    ]
    return cands


def _recaf_min_java(recaf: str | None) -> int:
    try:
        with zipfile.ZipFile(recaf) as z:
            data = z.read("software/coley/recaf/Main.class")
        major = int.from_bytes(data[6:8], "big")
        return max(8, major - 44)
    except Exception:
        return 22


def find_java_for_recaf(cfg: dict | None = None, *,
                        min_major: int = 22) -> str | None:
    """按 Recaf Main 的 class 版本选择 Java（4.x 需要 22+）。"""
    global _RECAF_JAVA_CACHE
    recaf = find_recaf(cfg)
    if recaf:
        min_major = max(min_major, _recaf_min_java(recaf))
    if _RECAF_JAVA_CACHE and os.path.isfile(_RECAF_JAVA_CACHE):
        major = _java_major(_RECAF_JAVA_CACHE)
        if major is not None and major >= min_major:
            return _RECAF_JAVA_CACHE
    for cand in _java_candidates(cfg, recaf=True):
        if not cand or not os.path.isfile(cand):
            continue
        major = _java_major(cand)
        if major is not None and major >= min_major:
            _RECAF_JAVA_CACHE = cand
            return cand
    return None


def find_java(cfg: dict | None = None) -> str | None:
    """找一个能正常启动的 java.exe；PATH 里的 Git java 会验证失败。"""
    global _JAVA_CACHE
    if _JAVA_CACHE and os.path.isfile(_JAVA_CACHE):
        return _JAVA_CACHE
    for cand in _java_candidates(cfg):
        if not cand or not os.path.isfile(cand):
            continue
        major = _java_major(cand)
        if major is not None:
            _JAVA_CACHE = cand
            return cand
    return None


def _candidate_dirs(cfg: dict | None = None) -> list[str]:
    home = os.path.expanduser("~")
    dirs = [
        r"D:\桌面移动\Java", r"D:\桌面移动", r"D:\java", r"D:\CRKToolBox",
        r"D:\tools", r"D:\Tool", r"D:\software",
        os.path.join(home, "Downloads"), os.path.join(home, "Desktop"),
        os.path.join(home, "桌面"), r"C:\tools",
        os.path.join(home, "AppData", "Local", "Programs"),
    ]
    dec = cfg.get("decompiler") if isinstance(cfg, dict) else None
    if isinstance(dec, dict):
        extra = dec.get("search_dirs")
        if isinstance(extra, list):
            dirs.extend(str(p) for p in extra if p)
        if dec.get("jar"):
            dirs.append(os.path.dirname(str(dec["jar"])))
    out = []
    seen = set()
    for d in dirs:
        if not d:
            continue
        key = os.path.normcase(os.path.abspath(d))
        if key in seen or not os.path.isdir(d):
            continue
        seen.add(key)
        out.append(d)
    return out


def _rank(path: str) -> int:
    low = os.path.basename(path).lower()
    if "cfr" in low and "recaf" not in low:
        return 0
    if "recaf" in low:
        return 1
    if "vineflower" in low or "quiltflower" in low:
        return 2
    if "fernflower" in low:
        return 3
    if "bytecode-viewer" in low:
        return 4
    return 5


def _candidate_jars(cfg: dict | None = None) -> list[str]:
    paths = []
    dec = cfg.get("decompiler") if isinstance(cfg, dict) else None
    if isinstance(dec, dict) and dec.get("jar"):
        p = str(dec["jar"])
        if os.path.isfile(p):
            paths.append(p)
    for d in _candidate_dirs(cfg):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            low = name.lower()
            if not low.endswith(".jar"):
                continue
            if not any(h in low for h in JAR_HINTS):
                continue
            p = os.path.join(d, name)
            if os.path.isfile(p):
                paths.append(p)
    seen = set()
    unique = []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return sorted(unique, key=_rank)


def _probe(jar: str):
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
    except (OSError, zipfile.BadZipFile):
        return None
    for label, main, kind in MAINS:
        if main + ".class" in names:
            return label, main, kind
    return None


def detect_decompiler(cfg: dict | None = None, *,
                      refresh: bool = False) -> DecompilerInfo | None:
    """扫描本机；返回可直接使用的命令模板，失败返回 None。"""
    global _CACHE
    if not refresh and _CACHE is not None:
        return _CACHE
    java = find_java(cfg)
    if not java:
        return None
    for jar in _candidate_jars(cfg):
        probed = _probe(jar)
        if probed is None:
            continue
        label, main, kind = probed
        main_java = main.replace("/", ".")
        if kind == "outdir":
            command = ("%s -cp %s %s -dgs=1 {input} {outdir}"
                       % (_quote(java), _quote(jar), main_java))
        elif kind == "stdout":
            command = ("%s -cp %s %s {input}"
                       % (_quote(java), _quote(jar), main_java))
        else:
            continue
        info = DecompilerInfo(
            label="%s (%s)" % (label, os.path.basename(jar)),
            command=command, java=java, jar=jar, kind=kind)
        _CACHE = info
        return info
    return None


def find_recaf(cfg: dict | None = None) -> str | None:
    """找到本机 Recaf GUI jar（用于“用 Recaf 打开”兜底）。"""
    for jar in _candidate_jars(cfg):
        if "recaf" in os.path.basename(jar).lower():
            return jar
    return None


def reset_cache() -> None:
    global _CACHE, _JAVA_CACHE, _RECAF_JAVA_CACHE
    _CACHE = None
    _JAVA_CACHE = None
    _RECAF_JAVA_CACHE = None
