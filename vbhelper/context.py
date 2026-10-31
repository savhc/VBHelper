"""应用依赖容器。

现有主类仍是 Mixin 组装；本容器只把「要复用给新功能」的运行期服务集中
起来：路径、配置、主题、后台任务、HTTP、缓存、窗口管理、功能注册表。
新功能（features/*）通过 AppContext 取依赖，不再往 app.py 里加 Mixin。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

APP_DIR_NAME = "VBHelper"

# 功能开关默认值：未完成/未验收的功能默认关闭，菜单与入口不显示。
FEATURE_DEFAULTS: dict[str, bool] = {
    "ui_foundation": True,
    "dependency_doctor": True,
    "context_pack": False,
    "api_workbench": True,
    "git_manager": False,
    "mcp_bus": True,
    "rag": True,
}

FEATURES_VERSION = 2      # 2: 首次引入 RAG，覆盖旧配置里的默认 false

def ensure_feature_flags(cfg: dict) -> dict:
    """在内存里补齐 config["features"]（不改写磁盘，用户已有值优先）。

    新功能首次上线时，旧配置里存的是「默认 false」，不能当成用户主动关闭；
    用 features_version 做一次性迁移，之后用户在面板/配置里关掉就尊重。
    """
    feats = cfg.get("features")
    if not isinstance(feats, dict):
        feats = {}
        cfg["features"] = feats
    for key, default in FEATURE_DEFAULTS.items():
        feats.setdefault(key, default)
    try:
        version = int(cfg.get("features_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < FEATURES_VERSION:
        # RAG 是首个新功能：旧配置的 rag=false 来自默认值，不是用户选择。
        feats["rag"] = bool(FEATURE_DEFAULTS.get("rag", True))
        cfg["features_version"] = FEATURES_VERSION
    return feats

@dataclass
class AppPaths:
    """配置（%APPDATA%）与缓存/数据库（%LOCALAPPDATA%）路径。"""

    appdata: str
    localappdata: str

    def __post_init__(self) -> None:
        self.config_dir = os.path.join(self.appdata, APP_DIR_NAME)
        self.config_file = os.path.join(self.config_dir, "config.json")
        self.local_dir = os.path.join(self.localappdata, APP_DIR_NAME)
        self.db_path = os.path.join(self.local_dir, "db", "vbhelper.db")
        self.cache_dir = os.path.join(self.local_dir, "cache")
        self.logs_dir = os.path.join(self.local_dir, "logs")
        self.captures_dir = os.path.join(self.local_dir, "captures")
        self.mcp_dir = os.path.join(self.local_dir, "mcp")
        self.models_dir = os.path.join(self.local_dir, "models")
        self.secrets_file = os.path.join(self.local_dir, "secrets.json")

    @classmethod
    def default(cls) -> "AppPaths":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        local = os.environ.get("LOCALAPPDATA") or appdata
        return cls(appdata=appdata, localappdata=local)

    def ensure(self) -> None:
        """创建需要的目录；失败不致命（缓存/日志写不进去时功能降级）。"""
        for d in (self.config_dir, os.path.dirname(self.db_path),
                  self.cache_dir, self.logs_dir, self.captures_dir,
                  self.mcp_dir, self.models_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass

@dataclass
class AppContext:
    """传给 Feature/FeatureWindow 的依赖容器。

    ``tasks`` 是 TaskRunner；``runner`` 是兼容 FeatureWindow.close 的别名。
    """

    app: Any
    root: Any
    cfg: dict
    paths: AppPaths
    theme: Any
    tasks: Any
    http: Any
    cache: Any
    windows: Any
    features: Any = None
    db: Any = None          # store/db.Database（迁移后的统一 SQLite）
    secrets: Any = None     # store/secrets.SecretStore（DPAPI）
    audit: Any = None       # store/audit.AuditLog
    invoker: Any = None     # ui/mainthread.MainThreadInvoker
    _closed: bool = field(default=False, init=False)

    @property
    def runner(self) -> Any:
        """FeatureWindow.close 里按这个名取任务管理器。"""
        return self.tasks

    def post(self, fn, timeout: float = 30.0) -> Any:
        """把回调排到 Tk 主线程（后台线程调用确认弹窗/Toast 用）。"""
        if self.invoker is not None:
            return self.invoker.call(fn, timeout)
        return fn()

    def toast(self, text: str, duration: int = 2500, *,
              topmost: bool = True) -> bool:
        """应用内轻提示；返回是否成功入队（不阻塞调用线程）。

        后台线程调这个时只投递到 Tk 主线程，不等待绘制完成——通知是
        锦上添花，不能拖住工具调用。主线程已关闭/异常时返回 False。
        """
        def _show():
            from .ui import widgets
            widgets.Toast(self.root, text, duration, topmost=topmost)
        try:
            if self.invoker is not None:
                if getattr(self.invoker, "closed", False):
                    return False
                self.invoker.post(_show)
            else:
                _show()
            return True
        except Exception as exc:
            try:
                from .core import dbg_log
                dbg_log("toast 失败：%r" % (exc,))
            except Exception:
                pass
            return False

    def feature_enabled(self, feature_id: str) -> bool:
        """功能开关：用户配置优先，缺省回落到内置默认值。"""
        feats = self.cfg.get("features")
        if isinstance(feats, dict) and feature_id in feats:
            return bool(feats.get(feature_id))
        return bool(FEATURE_DEFAULTS.get(feature_id, False))

    def save_cfg(self) -> None:
        """持久化配置（Feature 改 cfg 后调用）。"""
        from .core import save_cfg
        save_cfg(self.cfg)

    def shutdown(self, wait: bool = False) -> None:
        """退出顺序：功能 -> 窗口 -> 后台任务 -> HTTP/缓存。幂等。"""
        if self._closed:
            return
        self._closed = True
        steps = []
        if self.features is not None:
            steps.append(self.features.shutdown)
        steps.extend([
            self.windows.close_all,
            lambda: self.tasks.shutdown(wait=wait),
            lambda: self.invoker.stop() if self.invoker is not None else None,
            self.http.close,
            self.cache.close,
            lambda: self.db.close() if self.db is not None else None,
        ])
        for step in steps:
            try:
                step()
            except Exception as exc:          # 退出阶段不能让单个失败卡死
                try:
                    from .core import dbg_log
                    dbg_log("AppContext.shutdown %r: %r" % (step, exc))
                except Exception:
                    pass
