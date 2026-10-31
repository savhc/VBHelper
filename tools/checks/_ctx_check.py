# -*- coding: utf-8 -*-
"""AppContext / FeatureRegistry 接线自检：python _ctx_check.py

覆盖：
  AppPaths 目录派生/创建、feature 开关默认值；
  FeatureRegistry 按开关加载（启用/禁用）、单例窗口、逆序 shutdown；
  FeatureWindow 从 ctx 取 root/theme/cfg/runner，几何记忆；
  主题切换广播（widgets 令牌 + WindowManager 重绘）；
  AppContext.shutdown 幂等且能停掉 TaskRunner 线程。

不联网、不写用户目录（路径全部指到临时目录）。
"""
import os
import shutil
import sys
import tempfile
import tkinter as tk
from tkinter import ttk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from vbhelper.context import (AppContext, AppPaths, FEATURE_DEFAULTS,
                              ensure_feature_flags)
from vbhelper.features.base import Feature
from vbhelper.features.registry import FeatureRegistry
from vbhelper.store.cache import HttpCache
from vbhelper.ui import scale, theme as ui_theme, widgets
from vbhelper.ui.async_runner import TaskRunner
from vbhelper.ui.http import HttpClient
from vbhelper.ui.window import FeatureWindow, WindowManager

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


class EnabledFeature(Feature):
    id = "git_manager"
    title = "自检功能"
    group = "dev"
    order = 1
    mounted = 0
    opened = 0
    shutdowns = 0

    def mount(self):
        type(self).mounted += 1

    def open(self):
        type(self).opened += 1

    def shutdown(self):
        type(self).shutdowns += 1


class DisabledFeature(Feature):
    id = "context_pack"
    title = "未启用功能"
    order = 2
    mounted = 0

    def mount(self):
        type(self).mounted += 1

    def open(self):
        pass


def main():
    tmp = tempfile.mkdtemp(prefix="vbctx_")
    root = tk.Tk()
    root.withdraw()
    scale.init(root)
    ttk.Style(root).theme_use("clam")

    cfg = {"theme": {"preset": "midnight"}}
    tm = ui_theme.ThemeManager(root, cfg)
    paths = AppPaths(appdata=os.path.join(tmp, "roaming"),
                     localappdata=os.path.join(tmp, "local"))
    paths.ensure()
    cache = HttpCache(paths.db_path)
    http = HttpClient(cache=cache)
    tasks = TaskRunner(root)
    wm = WindowManager(root, tm)
    ctx = AppContext(app=root, root=root, cfg=cfg, paths=paths, theme=tm,
                     tasks=tasks, http=http, cache=cache, windows=wm)
    registry = FeatureRegistry(ctx)
    ctx.features = registry

    # ---- 路径与开关 ----
    check("AppPaths 配置目录派生",
          paths.config_file.endswith(os.path.join("VBHelper", "config.json")),
          paths.config_file)
    check("AppPaths 数据库在 LOCALAPPDATA",
          os.path.dirname(os.path.dirname(paths.db_path)).endswith("VBHelper"))
    check("AppPaths.ensure 建目录",
          os.path.isdir(paths.cache_dir) and os.path.isdir(paths.logs_dir))
    check("AppPaths.default 存在", AppPaths.default().config_dir.endswith("VBHelper"))
    check("默认开关：ui_foundation 开", FEATURE_DEFAULTS["ui_foundation"] is True)
    check("默认开关：git_manager 关", FEATURE_DEFAULTS["git_manager"] is False)
    ensure_feature_flags(cfg)
    check("ensure_feature_flags 补齐全部键",
          all(k in cfg["features"] for k in FEATURE_DEFAULTS))
    check("ctx.feature_enabled 默认值",
          ctx.feature_enabled("git_manager") is False)
    cfg["features"]["git_manager"] = True
    check("ctx.feature_enabled 用户值优先",
          ctx.feature_enabled("git_manager") is True)
    check("ctx.runner 与 tasks 同一对象", ctx.runner is tasks)

    # ---- 注册表加载 ----
    registry.register(DisabledFeature)
    check("未启用功能不加载", "context_pack" not in registry.load())
    check("未启用功能未 mount", DisabledFeature.mounted == 0)
    registry.register(EnabledFeature)
    loaded = registry.load()
    check("启用功能加载", "git_manager" in loaded)
    check("启用功能 mount 一次", EnabledFeature.mounted == 1)
    check("registry.get 拿到实例",
          isinstance(registry.get("git_manager"), EnabledFeature))
    described = {r["id"]: r["enabled"] for r in registry.describe()}
    check("describe 含启用/未启用项",
          {"git_manager", "context_pack"} <= set(described)
          and described.get("git_manager") is True
          and described.get("context_pack") is False, described)
    check("catalog 登记了依赖医生", "dependency_doctor" in described,
          sorted(described))
    check("open 已加载功能", registry.open("git_manager") is True
          and EnabledFeature.opened == 1)
    check("open 未知功能返回 False", registry.open("nope") is False)

    # ---- 窗口从 ctx 取依赖 ----
    win = wm.open("git_manager",
                  lambda: FeatureWindow(ctx, "git_manager", "接线自检", (620, 420)))
    win.withdraw()
    check("WindowManager 单例",
          wm.open("git_manager",
                  lambda: FeatureWindow(ctx, "git_manager", "不应新建")) is win)
    win.remember_geometry("git_manager")
    check("几何写入 ctx.cfg", "git_manager" in cfg.get("win_geometry", {}),
          cfg.get("win_geometry"))
    check("FeatureWindow 使用 ctx.theme", win._tokens is tm.current()[1])

    # ---- 主题广播 ----
    widgets.set_theme(tm.current()[1])
    theme_calls = []
    tm.subscribe(lambda preset, tokens: theme_calls.append(tokens))
    tm.subscribe(lambda preset, tokens: widgets.set_theme(tokens))
    tm.subscribe(lambda preset, tokens: wm.on_theme_changed())
    tm.set_accent("#ff8800")
    check("ThemeManager 通知订阅者", len(theme_calls) >= 1)
    check("widgets 全局令牌已更新",
          widgets.current_tokens() is tm.current()[1])
    check("WindowManager 重绘后令牌更新",
          win._tokens.accent.lower() == "#ff8800", win._tokens.accent)
    st = ttk.Style(root)
    tm.apply_ttk(st)
    check("apply_ttk 生效",
          str(st.lookup("TFrame", "background")).lower()
          == tm.current()[1].bg.lower(),
          st.lookup("TFrame", "background"))

    # ---- 退出 ----
    ctx.shutdown(wait=True)
    check("shutdown 调用功能 shutdown", EnabledFeature.shutdowns == 1)
    check("shutdown 清空注册表", registry.loaded() == [])
    check("shutdown 关闭窗口", wm.get("git_manager") is None)
    check("shutdown 后任务线程归零", tasks.alive_thread_count == 0,
          tasks.alive_thread_count)
    check("shutdown 幂等", (ctx.shutdown(wait=True) is None))

    root.destroy()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
