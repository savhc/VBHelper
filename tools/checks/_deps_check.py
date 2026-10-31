# -*- coding: utf-8 -*-
"""依赖医生自检：python _deps_check.py

覆盖：
  版本比较（pep440 子集 / semver / Maven / Go）、声明串清洗、状态判定；
  清单发现与解析（requirements / pyproject / package.json+lock / pom / gradle / go.mod）；
  Scanner 全流程（进度事件、状态回填、漏洞覆盖、忽略目录、离线降级、取消）；
  报告（Markdown/JSON/AI 提示词/升级命令）；
  窗口装配（索引树、结果表、详情面板、报告页、主题切换、取消）。

不联网：远端查询用 FakeRegistry 顶替；不写用户目录（路径全指临时目录）。
运行环境：D:\\python\\python.exe（与项目一致）。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from vbhelper.context import AppContext, AppPaths
from vbhelper.features.deps import parsers, report, versions
from vbhelper.features.deps.registry import _shrink
from vbhelper.features.deps.scanner import Scanner
from vbhelper.store.cache import HttpCache
from vbhelper.ui import scale, theme as ui_theme
from vbhelper.ui.async_runner import TaskRunner
from vbhelper.ui.http import HttpClient, HttpError
from vbhelper.ui.window import WindowManager

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

def check_versions():
    cases = [
        ("1.10.0", "1.9.3", 1),
        ("1.2", "1.2.0", 0),
        ("1.2.0rc1", "1.2.0", -1),
        ("1.2.0.dev1", "1.2.0a1", -1),
        ("1.2.3.post1", "1.2.3", 1),
        ("1.0.0.Final", "1.0.0", 0),
        ("1.0.0-SNAPSHOT", "1.0.0", -1),
        ("v1.2.3", "1.2.3", 0),
        ("1.2.3+incompatible", "1.2.3", 0),
        ("0.9.0", "1.0.0", -1),
    ]
    for left, right, want in cases:
        check("compare %s vs %s = %d" % (left, right, want),
              versions.compare(left, right) == want,
              versions.compare(left, right))
    check("compare 解析不了返回 None",
          versions.compare("not-a-version", "1.0") is None)
    check("major", versions.major("1.2.3") == 1)

    declared = [
        ("~=1.4.2", "pypi", "1.4.2"),
        (">=2.25,<3", "pypi", "2.25"),
        ("!=1.0,>=1.2", "pypi", "1.2"),
        ("==1.2.*", "pypi", "1.2"),
        ("*", "pypi", None),
        ("^1.2.3", "npm", "1.2.3"),
        ("~18.2.0", "npm", "18.2.0"),
        ("1.x", "npm", "1"),
        ("workspace:*", "npm", None),
        ("[1.0,2.0)", "maven", "1.0"),
        ("${spring.version}", "maven", None),
        ("v1.2.3", "go", "v1.2.3"),
    ]
    for raw, eco, want in declared:
        got = versions.clean_declared(raw, eco)
        check("clean_declared %r/%s = %r" % (raw, eco, want), got == want, got)

    checks = [
        ("1.9.3", "1.10.0", "outdated"),
        ("2.0.0", "3.0.0", "major"),
        ("1.0.0", "1.0.0", "ok"),
        ("1.0.0", None, "unknown"),
        ("1.0.0", "0.9.0", "ok"),
    ]
    for current, latest, want in checks:
        got = versions.status_for(current, latest)
        check("status %s -> %s = %s" % (current, latest, want), got == want, got)
    check("status 漏洞优先",
          versions.status_for("1.0.0", "1.0.1", vulnerable=True) == "vulnerable")


# ---------------------------------------------------------------------------
# 样例项目
# ---------------------------------------------------------------------------

REQUIREMENTS = """# 注释行
flask==2.0.1
requests[socks]>=2.25,<3   # 行内注释
django ; python_version >= "3.8"
urllib3==1.26.0
-e .
git+https://example.com/x.git
-r other.txt
"""

PYPROJECT = """[project]
name = "demo"
version = "0.1.0"
dependencies = ["httpx>=0.27", "rich==13.7.0"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.poetry.dependencies]
python = "^3.11"
fastapi = "^0.110.0"

[tool.poetry.group.dev.dependencies]
ruff = "^0.4.0"
"""

PACKAGE_JSON = json.dumps({
    "name": "web",
    "dependencies": {"react": "^18.2.0"},
    "devDependencies": {"vite": "^5.0.0"},
}, indent=2)

PACKAGE_LOCK = json.dumps({
    "name": "web",
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "web"},
        "node_modules/react": {"version": "18.2.0"},
        "node_modules/loose-envify": {"version": "1.4.0", "dev": True},
    },
}, indent=2)

POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.demo</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
  <properties>
    <spring.version>6.1.0</spring.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
      <version>${spring.version}</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
"""

GRADLE = """plugins { id 'java' }

dependencies {
    implementation 'com.google.guava:guava:33.0.0-jre'
    testImplementation "org.mockito:mockito-core:5.11.0"
}
"""

GO_MOD = """module demo

go 1.22

require (
    github.com/gin-gonic/gin v1.9.1
    golang.org/x/text v0.14.0 // indirect
)
"""


def build_project(tmp):
    root = os.path.join(tmp, "demo-project")
    files = {
        "requirements.txt": REQUIREMENTS,
        "backend/pyproject.toml": PYPROJECT,
        "web/package.json": PACKAGE_JSON,
        "web/package-lock.json": PACKAGE_LOCK,
        "java/pom.xml": POM,
        "java/app/build.gradle": GRADLE,
        "go/go.mod": GO_MOD,
        # 依赖目录里的清单必须被忽略
        "web/node_modules/left-pad/package.json": '{"name":"left-pad"}',
    }
    for rel, text in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


class FakeRegistry:
    """离线替身：latest 用固定表，vulns 用固定表。"""

    LATEST = {
        ("pypi", "flask"): "3.0.0",
        ("pypi", "requests"): "2.32.0",
        ("pypi", "urllib3"): "1.26.1",
        ("npm", "react"): "19.0.0",
        ("maven", "org.springframework:spring-core"): "6.2.0",
        ("go", "github.com/gin-gonic/gin"): "v1.10.0",
    }
    VULNS = {
        ("pypi", "urllib3", "1.26.0"): [{
            "id": "CVE-2023-45803", "aliases": [], "severity": "HIGH",
            "summary": "urllib3 请求体泄漏", "fixed": ["1.26.18"],
        }],
    }

    def __init__(self, fail=None):
        self.fail = fail

    def latest(self, eco, name):
        if self.fail is not None:
            return None, self.fail
        return self.LATEST.get((eco, name.lower())), None

    def vulns(self, deps, limit=500):
        return dict(self.VULNS)


def check_parsers(root):
    manifests, dirs, truncated = parsers.discover(root)
    rels = sorted(m.rel for m in manifests)
    check("发现 7 个清单", len(manifests) == 7, rels)
    check("忽略 node_modules",
          not any("node_modules" in rel for rel in rels))
    check("目录计数 > 0", dirs >= 5, dirs)

    found = {}
    for manifest in manifests:
        deps, note = parsers.parse_file(manifest)
        for dep in deps:
            found[(dep.source_file, dep.name)] = dep

    def get(rel, name):
        return found.get((rel, name))

    flask = get("requirements.txt", "flask")
    check("requirements: flask 声明版本", flask and flask.declared == "==2.0.1",
          flask)
    check("requirements: 行号", flask and flask.line == 2, flask)
    requests_dep = get("requirements.txt", "requests")
    check("requirements: extras 与范围",
          requests_dep and requests_dep.declared == ">=2.25,<3", requests_dep)
    django = get("requirements.txt", "django")
    check("requirements: 环境标记不当版本用",
          django and django.declared is None and "条件" in django.note, django)
    check("requirements: 跳过 -e / git+ / -r",
          all(key[1] not in ("git", "e") for key in found))

    rich = get("backend/pyproject.toml", "rich")
    check("pyproject: PEP 621", rich and rich.declared == "==13.7.0", rich)
    pytest_dep = get("backend/pyproject.toml", "pytest")
    check("pyproject: optional 范围", pytest_dep and pytest_dep.scope == "optional",
          pytest_dep)
    fastapi = get("backend/pyproject.toml", "fastapi")
    check("pyproject: Poetry", fastapi and fastapi.declared == "^0.110.0", fastapi)
    ruff = get("backend/pyproject.toml", "ruff")
    check("pyproject: Poetry group dev", ruff and ruff.scope == "dev", ruff)
    check("pyproject: 跳过 python 本身",
          get("backend/pyproject.toml", "python") is None)

    react = get("web/package.json", "react")
    check("package.json: 依赖范围", react and react.declared == "^18.2.0", react)
    vite = get("web/package.json", "vite")
    check("package.json: devDependencies", vite and vite.scope == "dev", vite)
    loose = get("web/package-lock.json", "loose-envify")
    check("package-lock: 锁定版本", loose and loose.resolved == "1.4.0", loose)
    check("package-lock: 标为间接依赖", loose and loose.transitive)
    check("package-lock: 不把根 package 当依赖",
          get("web/package-lock.json", "web") is None)

    spring = get("java/pom.xml", "org.springframework:spring-core")
    check("pom: 变量替换", spring and spring.declared == "6.1.0", spring)
    junit = get("java/pom.xml", "junit:junit")
    check("pom: test 作用域", junit and junit.scope == "dev", junit)
    guava = get("java/app/build.gradle", "com.google.guava:guava")
    check("gradle: 字符串写法", guava and guava.declared == "33.0.0-jre", guava)
    mockito = get("java/app/build.gradle", "org.mockito:mockito-core")
    check("gradle: test 作用域", mockito and mockito.scope == "dev", mockito)
    gin = get("go/go.mod", "github.com/gin-gonic/gin")
    check("go.mod: require 块", gin and gin.declared == "v1.9.1", gin)
    text = get("go/go.mod", "golang.org/x/text")
    check("go.mod: indirect", text and text.scope == "dev", text)


def check_scanner(root):
    events = []
    result = Scanner(root, registry=FakeRegistry()).run(
        progress=lambda event: events.append(event))
    check("扫描文件数 7", len(result.files) == 7, len(result.files))
    check("依赖总数 18", len(result.deps) == 18, len(result.deps))
    check("联网核对成功", result.online and result.checked >= 6, result.checked)

    reacts = [dep for dep in result.deps if dep.name.lower() == "react"]
    check("声明与锁定合并成一行",
          len(reacts) == 1 and bool(reacts[0].declared)
          and bool(reacts[0].resolved),
          [(dep.declared, dep.resolved) for dep in reacts])

    by_name = {dep.name.lower(): dep for dep in result.deps}
    check("flask 大版本落后", by_name["flask"].status == "major",
          by_name["flask"].status)
    check("requests 过期", by_name["requests"].status == "outdated",
          by_name["requests"].status)
    check("react 大版本落后", by_name["react"].status == "major",
          by_name["react"].status)
    check("spring-core 过期", by_name["org.springframework:spring-core"].status
          == "outdated", by_name["org.springframework:spring-core"].status)
    check("gin 过期", by_name["github.com/gin-gonic/gin"].status == "outdated")
    check("urllib3 命中漏洞", by_name["urllib3"].status == "vulnerable"
          and by_name["urllib3"].cves[0]["id"] == "CVE-2023-45803",
          by_name["urllib3"].status)
    check("间接依赖默认不核对",
          by_name["loose-envify"].status == "unknown"
          and by_name["loose-envify"].latest is None)
    check("django 未声明版本 -> 未知", by_name["django"].status == "unknown")

    kinds = {kind for kind, _payload in events}
    for kind in ("manifests", "file", "resolve_start", "resolve", "phase",
                 "done"):
        check("进度事件 %s" % kind, kind in kinds, sorted(kinds))
    check("进度事件里有文件条目",
          any(kind == "file" and payload.count > 0
              for kind, payload in events))

    # 离线降级
    offline = Scanner(root, registry=FakeRegistry(
        fail=HttpError("dns", "模拟离线"))).run()
    check("离线时标记 offline", offline.online is False)
    check("离线时全部未知",
          all(dep.status == "unknown" for dep in offline.deps))
    check("离线提示写进 errors",
          any("网络不可用" in item for item in offline.errors), offline.errors)

    # 取消
    cancelled = Scanner(root, registry=FakeRegistry(),
                        stop=lambda: True).run()
    check("停止位立即生效", cancelled.cancelled is True)

    # 不写项目文件
    before = sorted(os.listdir(root))
    Scanner(root, registry=FakeRegistry()).run()
    check("扫描不改动项目目录", sorted(os.listdir(root)) == before)


def check_report(result):
    text = report.build_markdown(result)
    for needle in ("# 依赖体检报告", "## 有漏洞", "CVE-2023-45803",
                   "## 全部依赖", "## 清单索引", "pip install -U urllib3==1.26.1"):
        check("报告包含 %s" % needle, needle in text)
    data = json.loads(report.build_json(result))
    check("JSON 依赖条数", len(data["dependencies"]) == len(result.deps))
    check("JSON 统计", data["counts"]["vulnerable"] == 1, data["counts"])
    prompt = report.build_prompt(result)
    check("AI 提示词含漏洞与升级目标",
          "urllib3" in prompt and "有漏洞" in prompt)
    check("upgrade_command npm", report.upgrade_command(
        type("D", (), {"ecosystem": "npm", "name": "react", "latest": "19.0.0",
                       "scope": "runtime"})()) == "npm install react@19.0.0")
    check("upgrade_command maven", "pom.xml" in (report.upgrade_command(
        type("D", (), {"ecosystem": "maven", "name": "g:a", "latest": "2.0",
                       "scope": "runtime"})()) or ""))
    check("upgrade_command 无最新版返回 None", report.upgrade_command(
        type("D", (), {"ecosystem": "pypi", "name": "x", "latest": None,
                       "scope": "runtime"})()) is None)


def check_osv_shrink():
    raw = {
        "id": "GHSA-aaaa-bbbb",
        "aliases": ["CVE-2024-0001"],
        "summary": "示例漏洞",
        "database_specific": {"severity": "high"},
        "affected": [{"ranges": [{"events": [{"introduced": "0"},
                                             {"fixed": "1.2.3"}]}]}],
    }
    out = _shrink(raw)
    check("OSV 压缩：id/summary/severity",
          out["id"] == "GHSA-aaaa-bbbb" and out["summary"] == "示例漏洞"
          and out["severity"] == "HIGH", out)
    check("OSV 压缩：修复版本", out["fixed"] == ["1.2.3"], out["fixed"])


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

def check_window(tmp, result):
    root = tk.Tk()
    root.withdraw()
    scale.init(root)
    cfg = {"theme": {"preset": "nord"}}
    tm = ui_theme.ThemeManager(root, cfg)
    paths = AppPaths(appdata=os.path.join(tmp, "roaming"),
                     localappdata=os.path.join(tmp, "local"))
    paths.ensure()
    cache = HttpCache(paths.db_path)
    http = HttpClient(cache=cache)
    tasks = TaskRunner(root)
    wm = WindowManager(root, tm)
    ctx = AppContext(app=None, root=root, cfg=cfg, paths=paths, theme=tm,
                     tasks=tasks, http=http, cache=cache, windows=wm)

    from vbhelper.features.deps.window import DepsWindow
    win = DepsWindow(ctx, result.root)
    check("窗口创建成功", bool(win.winfo_exists()))

    # 用固定结果顶替真实扫描：验证进度事件 -> 索引树/表格/报告的整条链路
    def fake_work():
        win._queue.put(("walk", {"dirs": 5}))
        win._queue.put(("manifests", {"count": len(result.files)}))
        for entry in result.files:
            win._queue.put(("file", entry))
        win._queue.put(("resolve_start", {"total": 6}))
        win._queue.put(("resolve", {"done": 6, "total": 6, "name": "flask"}))
        win._queue.put(("phase", {"name": "vulns"}))
        return result

    win._work = fake_work
    win.start_scan()
    deadline = time.time() + 20
    while time.time() < deadline:
        root.update()
        if not win.scanning:
            break
        time.sleep(0.02)
    root.update()
    check("窗口内扫描走完", not win.scanning)
    check("索引树根节点存在", bool(win._tree.get_children(win._tree_root)))
    check("索引树清单数与结果一致", win._file_nodes == len(result.files),
          win._file_nodes)
    check("结果表载入", len(win._rows) == len(result.deps), len(win._rows))
    check("详情面板有内容", len(win._detail_inner.winfo_children()) > 0)
    check("报告页已渲染", len(win._report_holder.winfo_children()) > 0)

    win._show_page(1)
    check("页签切到检测结果",
          win._tabs.active() == 1
          and win._tab_result.winfo_manager() == "pack"
          and win._tab_index.winfo_manager() == "")
    win._on_filter_status("漏洞")
    check("按状态筛选", win._rows and all(d.status == "vulnerable"
                                     for d in win._rows), len(win._rows))
    win._on_filter_status("全部")
    win._on_filter_eco("Python")
    check("按生态筛选", win._rows and all(d.ecosystem == "pypi"
                                     for d in win._rows), len(win._rows))
    win._on_filter_eco("全部")
    win._on_search("spring")
    check("搜索命中", len(win._rows) == 1, len(win._rows))
    win._on_search("")
    check("清空搜索恢复全量", len(win._rows) == len(result.deps))

    tm.set_preset("sakura")
    root.update()
    check("主题切换后窗口仍可绘制", bool(win.winfo_exists()))
    win.apply_theme()
    check("重复 apply_theme 不炸", bool(win.winfo_exists()))

    win.close()
    root.update()

    # ---- feature.open 全链路：选择文件夹 -> 建窗 -> 自动扫描 -> 关闭 ----
    from tkinter import filedialog
    from vbhelper.features.deps import DepsFeature
    from vbhelper.features.deps import registry as registry_mod
    original_dialog = filedialog.askdirectory
    original_latest = registry_mod.RemoteRegistry.latest
    original_vulns = registry_mod.RemoteRegistry.vulns
    fake = FakeRegistry()
    ctx.save_cfg = lambda: None            # 自检不写用户配置
    filedialog.askdirectory = lambda **_kw: result.root
    registry_mod.RemoteRegistry.latest = (
        lambda self, eco, name: fake.latest(eco, name))
    registry_mod.RemoteRegistry.vulns = (
        lambda self, deps, limit=500: fake.vulns(deps, limit))
    try:
        feature = DepsFeature(ctx)
        opened = feature.open()
        check("feature.open 通过选目录建窗", isinstance(opened, DepsWindow))
        deadline = time.time() + 20
        while time.time() < deadline:
            root.update()
            if opened is None or not opened.scanning:
                break
            time.sleep(0.02)
        root.update()
        check("feature.open 自动完成扫描",
              opened is not None and opened._result is not None
              and len(opened._rows) == len(result.deps),
              len(opened._rows) if opened else -1)
        check("窗口记住上次目录",
              (cfg.get("deps") or {}).get("last_dir") == result.root,
              cfg.get("deps"))
        feature.shutdown()
        check("feature.shutdown 关窗", wm.get("dependency_doctor") is None)
    finally:
        filedialog.askdirectory = original_dialog
        registry_mod.RemoteRegistry.latest = original_latest
        registry_mod.RemoteRegistry.vulns = original_vulns

    tasks.shutdown()
    http.close()
    cache.close()
    root.destroy()


def main():
    check_versions()
    tmp = tempfile.mkdtemp(prefix="vbdeps_")
    try:
        project = build_project(tmp)
        check_parsers(project)
        events = []
        result = Scanner(project, registry=FakeRegistry()).run(
            progress=lambda event: events.append(event))
        check_scanner(project)
        check_report(result)
        check_osv_shrink()
        check_window(tmp, result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print("失败 %d 项：" % len(FAILS))
        for name in FAILS:
            print("  - " + name)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
