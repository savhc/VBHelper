"""清单发现与解析。

只读解析，**不修改任何项目文件**，也不执行 pip/npm/mvn。支持的清单：

- Python：``requirements*.txt``、``pyproject.toml``（PEP 621 / Poetry）、
  ``Pipfile``、``setup.cfg``
- Node：``package.json``、``package-lock.json``（v1/v2/v3）
- Java：``pom.xml``、``build.gradle[.kts]``、``gradle/libs.versions.toml``
- Go：``go.mod``

"""
from __future__ import annotations

import configparser
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import versions

try:                     # Python 3.11+
    import tomllib
except ImportError:      # pragma: no cover - 3.10 及以下
    tomllib = None

ECO_LABEL = {"pypi": "Python", "npm": "Node", "maven": "Java", "go": "Go"}
ECO_ORDER = ("pypi", "npm", "maven", "go")

# 这些目录里的清单不是项目自己的依赖（依赖树/构建产物/虚拟环境）
IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vs", ".vscode", ".gradle",
    "node_modules", "bower_components", "vendor",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", "venv", "env", ".env", "virtualenv",
    "dist", "build", "out", "target", "bin", "obj",
    ".next", ".nuxt", ".svelte-kit", "coverage", "htmlcov",
}

MAX_FILE_BYTES = 4 * 1024 * 1024      # 单个清单上限，超过跳过并记备注
MAX_ENTRIES = 60000                  # 目录项上限，防止误扫整个磁盘

@dataclass
class Dependency:
    """一条依赖记录（字段对齐计划 02 §1.3）。"""

    ecosystem: str
    name: str
    declared: str | None = None
    resolved: str | None = None
    latest: str | None = None
    latest_allowed: str | None = None
    scope: str = "runtime"          # runtime/dev/optional/peer/build/provided
    source_file: str = ""           # 相对项目根的路径
    line: int | None = None
    status: str = "unknown"
    cves: list[dict] = field(default_factory=list)
    homepage: str | None = None
    note: str = ""
    transitive: bool = False        # 来自锁定文件、非直接依赖

    @property
    def current(self) -> str:
        """用于比较的当前版本：锁定优先，否则取声明里能确定的版本。"""
        if self.resolved:
            return self.resolved.strip()
        return versions.clean_declared(self.declared, self.ecosystem) or ""

    @property
    def is_exact(self) -> bool:
        """当前版本是否精确到某一个版本（决定能不能拿去查 OSV）。"""
        if self.resolved:
            return True
        text = (self.declared or "").strip().lstrip("=").strip()
        if not text or not text[:1].isdigit():
            return False
        return not any(ch in text for ch in "<>^~*,")

    @property
    def key(self) -> tuple[str, str]:
        return (self.ecosystem, self.name.lower())

@dataclass
class Manifest:
    """一个待解析的清单文件。"""

    path: str          # 绝对路径
    rel: str           # 相对项目根（正斜杠）
    eco: str
    kind: str

    @property
    def label(self) -> str:
        return "%s · %s" % (ECO_LABEL.get(self.eco, self.eco), self.kind)

def classify(name: str) -> tuple[str, str] | None:
    """按文件名判断生态与类型；不是清单返回 None。"""
    low = name.lower()
    if low.startswith("requirements") and low.endswith(".txt"):
        return ("pypi", "requirements")
    if low == "pyproject.toml":
        return ("pypi", "pyproject")
    if low == "pipfile":
        return ("pypi", "Pipfile")
    if low == "setup.cfg":
        return ("pypi", "setup.cfg")
    if low == "package.json":
        return ("npm", "package.json")
    if low == "package-lock.json":
        return ("npm", "package-lock")
    if low == "pom.xml":
        return ("maven", "pom.xml")
    if low in ("build.gradle", "build.gradle.kts"):
        return ("maven", "gradle")
    if low == "libs.versions.toml":
        return ("maven", "version catalog")
    if low == "go.mod":
        return ("go", "go.mod")
    return None

def discover(root: str, *, on_dir=None) -> tuple[list[Manifest], int, bool]:
    """遍历项目目录找清单。

    返回 ``(manifests, 浏览过的目录数, 是否被 MAX_ENTRIES 截断)``；
    ``on_dir(count)`` 每 200 个目录回调一次（给进度条用）。
    """
    manifests: list[Manifest] = []
    dirs = 0
    entries = 0
    truncated = False
    root = os.path.abspath(root)
    for cur, dirnames, filenames in os.walk(root, topdown=True,
                                            followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORE_DIRS and not d.startswith("."))
        dirs += 1
        if callable(on_dir) and dirs % 200 == 0:
            try:
                on_dir(dirs)
            except Exception:
                pass
        for fname in sorted(filenames):
            entries += 1
            if entries > MAX_ENTRIES:
                truncated = True
                break
            hit = classify(fname)
            if not hit:
                continue
            path = os.path.join(cur, fname)
            rel = os.path.relpath(path, root).replace("\\", "/")
            manifests.append(Manifest(path=path, rel=rel, eco=hit[0],
                                      kind=hit[1]))
        if truncated:
            break
    manifests.sort(key=lambda m: (m.eco, m.rel))
    return manifests, dirs, truncated

# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def read_text(path: str) -> str | None:
    """按 utf-8(-sig) -> gbk -> 替换 的顺序读文本；过大/读不了返回 None。"""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES)
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")

def _dep(eco: str, name: str, declared, rel: str, line: int | None = None,
         *, resolved=None, scope: str = "runtime", note: str = "",
         transitive: bool = False) -> Dependency:
    return Dependency(ecosystem=eco, name=str(name).strip(),
                      declared=(str(declared).strip() if declared else None),
                      resolved=resolved, scope=scope, source_file=rel,
                      line=line, note=note, transitive=transitive)

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

_REQ_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)")

def _requirement(raw: str, rel: str, line: int | None) -> Dependency | None:
    """解析一行 PEP 508 需求；选项行/空行返回 None。"""
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("-"):
        return None                      # -r/-c/-e/--index-url 等
    low = text.lower()
    if low.startswith(("git+", "http://", "https://", "file:", "./", "../",
                       "/", "~/")):
        return None                      # 直接 URL / 本地路径，没有版本可查
    text = text.split(" #", 1)[0].strip()
    marker = ""
    if ";" in text:
        text, marker = text.split(";", 1)
        text = text.strip()
    if not text:
        return None
    note = ("条件: " + marker.strip()) if marker.strip() else ""
    if " @ " in text:                    # name @ url
        name = text.split(" @ ", 1)[0].strip()
        return _dep("pypi", name, None, rel, line,
                    note=note or "URL 依赖")
    match = _REQ_NAME.match(text)
    if not match:
        return None
    name = match.group(1)
    rest = text[match.end():].strip()
    if rest.startswith("["):             # extras
        close = rest.find("]")
        if close > 0:
            rest = rest[close + 1:].strip()
    return _dep("pypi", name, rest or None, rel, line, note=note)

def _requirements(text: str, rel: str) -> tuple[list[Dependency], str]:
    deps, notes = [], []
    for no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith(("-r ", "--requirement", "-c ", "--constraint")):
            notes.append("第 %d 行引用了其他清单，未展开" % no)
            continue
        dep = _requirement(raw, rel, no)
        if dep:
            deps.append(dep)
    return deps, "；".join(notes[:3])

def _pyproject(text: str, rel: str) -> tuple[list[Dependency], str]:
    if tomllib is None:
        return [], "缺少 TOML 解析器（需要 Python 3.11+）"
    data = tomllib.loads(text)
    deps: list[Dependency] = []
    notes: list[str] = []
    project = data.get("project") or {}
    for req in project.get("dependencies") or []:
        dep = _requirement(str(req), rel, None)
        if dep:
            deps.append(dep)
    for group, items in (project.get("optional-dependencies") or {}).items():
        for req in items or []:
            dep = _requirement(str(req), rel, None)
            if dep:
                dep.scope = "optional"
                dep.note = "extra: %s" % group
                deps.append(dep)

    poetry = (data.get("tool") or {}).get("poetry") or {}

    def from_map(mapping, scope: str, note: str = "") -> None:
        for name, spec in (mapping or {}).items():
            if str(name).lower() == "python":
                continue
            declared = None
            extra_note = note
            if isinstance(spec, str):
                declared = spec
            elif isinstance(spec, dict):
                declared = spec.get("version")
                local_scope = scope
                if spec.get("optional"):
                    local_scope = "optional"
                if not declared:
                    for kind in ("git", "path", "url"):
                        if spec.get(kind):
                            extra_note = extra_note or ("%s 依赖" % kind)
                            break
                deps.append(_dep("pypi", name, declared, rel,
                                 scope=local_scope, note=extra_note))
                continue
            deps.append(_dep("pypi", name, declared, rel, scope=scope,
                             note=extra_note))

    from_map(poetry.get("dependencies"), "runtime")
    from_map(poetry.get("dev-dependencies"), "dev")
    for group, body in (poetry.get("group") or {}).items():
        scope = "dev" if group in ("dev", "test") else "optional"
        from_map((body or {}).get("dependencies"), scope, "group: %s" % group)
    if (data.get("tool") or {}).get("poetry") and not deps:
        notes.append("Poetry 配置里没有找到依赖")
    return deps, "；".join(notes)

def _pipfile(text: str, rel: str) -> tuple[list[Dependency], str]:
    if tomllib is None:
        return [], "缺少 TOML 解析器（需要 Python 3.11+）"
    data = tomllib.loads(text)
    deps: list[Dependency] = []
    for section, scope in (("packages", "runtime"), ("dev-packages", "dev")):
        for name, spec in (data.get(section) or {}).items():
            version = None
            note = ""
            if isinstance(spec, str):
                version = spec
            elif isinstance(spec, dict):
                version = spec.get("version")
                if not version and (spec.get("git") or spec.get("path")):
                    note = "git/path 依赖"
            deps.append(_dep("pypi", name, version, rel, scope=scope,
                             note=note))
    return deps, ""

def _setup_cfg(text: str, rel: str) -> tuple[list[Dependency], str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        return [], "INI 解析失败：%s" % exc
    deps: list[Dependency] = []
    body = parser.get("options", "install_requires", fallback="")
    for raw in body.splitlines():
        dep = _requirement(raw, rel, None)
        if dep:
            deps.append(dep)
    if parser.has_section("options.extras_require"):
        for group, value in parser.items("options.extras_require"):
            for raw in str(value).splitlines():
                dep = _requirement(raw, rel, None)
                if dep:
                    dep.scope = "optional"
                    dep.note = "extra: %s" % group
                    deps.append(dep)
    return deps, ""

# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

_NPM_SECTIONS = (
    ("dependencies", "runtime"),
    ("devDependencies", "dev"),
    ("peerDependencies", "peer"),
    ("optionalDependencies", "optional"),
)

def _package_json(text: str, rel: str) -> tuple[list[Dependency], str]:
    data = json.loads(text)
    deps: list[Dependency] = []
    for section, scope in _NPM_SECTIONS:
        for name, spec in (data.get(section) or {}).items():
            deps.append(_dep("npm", name, spec, rel, scope=scope))
    return deps, ""

def _package_lock(text: str, rel: str) -> tuple[list[Dependency], str]:
    data = json.loads(text)
    deps: list[Dependency] = []
    packages = data.get("packages")
    if isinstance(packages, dict):                    # lockfileVersion 2/3
        for key, meta in packages.items():
            if not key or "node_modules/" not in key:
                continue                              # 根/工作区包
            if not isinstance(meta, dict):
                continue
            name = key.rsplit("node_modules/", 1)[-1]
            scope = ("dev" if meta.get("dev")
                     else "optional" if meta.get("optional") else "runtime")
            deps.append(_dep("npm", name, None, rel,
                             resolved=str(meta.get("version") or ""),
                             scope=scope, transitive=True))
        return deps, ""

    legacy = data.get("dependencies")                 # lockfileVersion 1
    if isinstance(legacy, dict):
        def walk(node) -> None:
            for name, meta in (node or {}).items():
                if not isinstance(meta, dict):
                    continue
                deps.append(_dep(
                    "npm", name, None, rel,
                    resolved=str(meta.get("version") or ""),
                    scope="dev" if meta.get("dev") else "runtime",
                    transitive=True))
                walk(meta.get("dependencies"))
        walk(legacy)
    return deps, ""

# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

_XML_VAR = re.compile(r"\$\{([^}]+)\}")

def _pom(text: str, rel: str) -> tuple[list[Dependency], str]:
    root = ET.fromstring(text)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    # <properties> 里的自定义版本变量 + project.* 内置变量
    props: dict[str, str] = {}
    for element in root.iter():
        if local(element.tag) == "properties":
            for child in list(element):
                props[local(child.tag)] = (child.text or "").strip()
    for child in list(root):
        tag = local(child.tag)
        if tag in ("groupId", "artifactId", "version"):
            props["project." + tag] = (child.text or "").strip()

    managed: dict[tuple[str, str], str] = {}
    plain: list[dict[str, str]] = []

    def walk(element, in_management: bool) -> None:
        for child in list(element):
            tag = local(child.tag)
            if tag == "dependencyManagement":
                walk(child, True)
            elif tag == "dependency":
                fields = {local(c.tag): (c.text or "").strip()
                          for c in list(child)}
                if in_management:
                    key = (fields.get("groupId", ""),
                           fields.get("artifactId", ""))
                    if fields.get("version"):
                        managed[key] = fields["version"]
                else:
                    plain.append(fields)
            else:
                walk(child, in_management)

    walk(root, False)

    def substitute(value: str) -> str | None:
        for _ in range(6):
            match = _XML_VAR.search(value)
            if not match:
                return value if "${" not in value else None
            replacement = props.get(match.group(1))
            if replacement is None:
                return None
            value = value.replace(match.group(0), replacement)
        return None

    deps: list[Dependency] = []
    missing = 0
    for fields in plain:
        group = fields.get("groupId", "")
        artifact = fields.get("artifactId", "")
        if not group or not artifact:
            continue
        name = "%s:%s" % (group, artifact)
        version = fields.get("version", "")
        unresolved = False
        if version:
            resolved_value = substitute(version)
            if resolved_value is None:
                unresolved = True
                version = ""
            else:
                version = resolved_value
        if not version:
            version = managed.get((group, artifact), "")
            if version:
                version = substitute(version) or ""
        scope_raw = fields.get("scope", "compile")
        scope = {"test": "dev", "provided": "provided",
                 "runtime": "runtime", "system": "provided"}.get(
                     scope_raw, "runtime")
        if fields.get("optional", "").lower() == "true":
            scope = "optional"
        note = ""
        if unresolved:
            note = "版本是未解析的变量"
            missing += 1
        elif not version:
            note = "版本由父 POM 管理"
            missing += 1
        deps.append(_dep("maven", name, version or None, rel, scope=scope,
                         note=note))
    note = ("%d 个依赖缺少可解析版本" % missing) if missing else ""
    return deps, note

_GRADLE_PATTERNS = (
    re.compile(
        r"^\s*(?P<cfg>[A-Za-z][A-Za-z0-9]*)\s*\(?\s*['\"]"
        r"(?P<g>[\w.\-]+):(?P<a>[\w.\-]+)(?::(?P<v>[^'\"]+))?['\"]",
        re.MULTILINE),
    re.compile(
        r"^\s*(?P<cfg>[A-Za-z][A-Za-z0-9]*)\s*\(?\s*"
        r"group\s*:\s*['\"](?P<g>[^'\"]+)['\"]\s*,\s*"
        r"name\s*:\s*['\"](?P<a>[^'\"]+)['\"]"
        r"(?:\s*,\s*version\s*:\s*['\"](?P<v>[^'\"]+)['\"])?",
        re.MULTILINE),
)

def _gradle_scope(config: str) -> str:
    low = config.lower()
    if low.startswith(("test", "androidtest")):
        return "dev"
    if low in ("kapt", "ksp", "annotationprocessor", "compileonly",
               "classpath"):
        return "build"
    return "runtime"

def _gradle(text: str, rel: str) -> tuple[list[Dependency], str]:
    deps: list[Dependency] = []
    seen: set[tuple[str, str, str | None]] = set()
    for pattern in _GRADLE_PATTERNS:
        for match in pattern.finditer(text):
            group = match.group("g")
            artifact = match.group("a")
            version = match.group("v")
            if version and ("$" in version or version.startswith("[")):
                version = None           # 变量/区间晚点再说，先不猜
            line = text.count("\n", 0, match.start()) + 1
            key = (group, artifact, version)
            if key in seen:
                continue
            seen.add(key)
            deps.append(_dep(
                "maven", "%s:%s" % (group, artifact), version, rel, line,
                scope=_gradle_scope(match.group("cfg")),
                note="" if version else "版本在别处定义"))
    return deps, ""

def _version_catalog(text: str, rel: str) -> tuple[list[Dependency], str]:
    if tomllib is None:
        return [], "缺少 TOML 解析器（需要 Python 3.11+）"
    data = tomllib.loads(text)
    versions = {k: v for k, v in (data.get("versions") or {}).items()
                if isinstance(v, str)}
    deps: list[Dependency] = []
    for alias, spec in (data.get("libraries") or {}).items():
        if not isinstance(spec, dict):
            continue
        module = spec.get("module")
        if not module and spec.get("group") and spec.get("name"):
            module = "%s:%s" % (spec["group"], spec["name"])
        if not module:
            continue
        version = spec.get("version")
        note = ""
        if isinstance(version, dict):
            ref = version.get("ref")
            if ref:
                version = versions.get(ref)
            else:
                version = (version.get("strictly") or version.get("require")
                           or version.get("prefer"))
        elif not version and spec.get("version.ref"):
            version = versions.get(spec["version.ref"])
        if not version:
            note = "版本未在 catalog 中定义"
        deps.append(_dep("maven", module, version, rel, scope="runtime",
                         note=note))
    return deps, ""

# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

def _go_mod(text: str, rel: str) -> tuple[list[Dependency], str]:
    deps: list[Dependency] = []
    in_block = False
    for no, raw in enumerate(text.splitlines(), 1):
        body = raw.split("//", 1)[0].strip()
        indirect = "// indirect" in raw
        if body.startswith("require"):
            rest = body[len("require"):].strip()
            if rest.startswith("("):
                in_block = True
                continue
            if rest:
                parts = rest.split()
                if len(parts) == 2:
                    deps.append(_dep("go", parts[0], parts[1], rel, no,
                                     scope="dev" if indirect else "runtime"))
            continue
        if in_block:
            if body == ")":
                in_block = False
                continue
            parts = body.split()
            if len(parts) == 2 and parts[1].startswith("v"):
                deps.append(_dep("go", parts[0], parts[1], rel, no,
                                 scope="dev" if indirect else "runtime"))
    return deps, ""

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

_PARSERS = {
    ("pypi", "requirements"): _requirements,
    ("pypi", "pyproject"): _pyproject,
    ("pypi", "Pipfile"): _pipfile,
    ("pypi", "setup.cfg"): _setup_cfg,
    ("npm", "package.json"): _package_json,
    ("npm", "package-lock"): _package_lock,
    ("maven", "pom.xml"): _pom,
    ("maven", "gradle"): _gradle,
    ("maven", "version catalog"): _version_catalog,
    ("go", "go.mod"): _go_mod,
}

def parse_text(manifest: Manifest, text: str) -> tuple[list[Dependency], str]:
    """解析清单文本，返回 ``(依赖列表, 备注)``；解析失败不抛异常。"""
    parser = _PARSERS.get((manifest.eco, manifest.kind))
    if parser is None:
        return [], "暂不支持该清单类型"
    try:
        deps, note = parser(text, manifest.rel)
    except Exception as exc:              # 坏文件只影响自己，不中断整次扫描
        return [], "解析失败：%s" % exc
    for dep in deps:
        if not dep.ecosystem:
            dep.ecosystem = manifest.eco
    return deps, note

def parse_file(manifest: Manifest) -> tuple[list[Dependency], str]:
    """读文件并解析；读不了时返回空列表 + 备注。"""
    text = read_text(manifest.path)
    if text is None:
        return [], "文件过大或无法读取（上限 4MB）"
    return parse_text(manifest, text)
