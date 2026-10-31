"""Feature 注册表：按 config.features 只加载启用的功能。

加载顺序由 Feature.order 决定；单个功能加载失败只记录，不拖垮启动。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Feature

if TYPE_CHECKING:
    from ..context import AppContext

def default_catalog() -> list[type[Feature]]:
    """内置功能清单；``features/__init__.py:catalog()`` 里登记。"""
    items: list[type[Feature]] = []
    try:
        from . import catalog
        for cls in catalog():
            if isinstance(cls, type) and issubclass(cls, Feature):
                items.append(cls)
    except Exception:
        pass
    items.sort(key=lambda c: getattr(c, "order", 100))
    return items

class FeatureRegistry:
    """功能生命周期管理：register -> load -> open -> shutdown。"""

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._catalog: list[type[Feature]] = []
        self._loaded: dict[str, Feature] = {}
        self._order: list[str] = []
        self.errors: list[tuple[str, BaseException]] = []

    # ---------------- 注册与加载 ----------------

    def register(self, cls: type[Feature]) -> None:
        """登记一个功能类（重复登记会忽略）。"""
        if isinstance(cls, type) and issubclass(cls, Feature) and cls not in self._catalog:
            self._catalog.append(cls)
            self._catalog.sort(key=lambda c: getattr(c, "order", 100))

    def load(self) -> list[str]:
        """按开关加载功能，返回成功加载的 id 列表。"""
        for cls in default_catalog():
            self.register(cls)
        for cls in self._catalog:
            fid = getattr(cls, "id", "") or ""
            if not fid or fid in self._loaded:
                continue
            if not self.ctx.feature_enabled(fid):
                continue
            try:
                inst = cls(self.ctx)
                if not inst.is_available():
                    continue
                inst.mount()
            except Exception as exc:       # 单个功能挂了不影响其他功能
                self.errors.append((fid, exc))
                self._log("加载功能 %s 失败：%r" % (fid, exc))
                continue
            self._loaded[fid] = inst
            self._order.append(fid)
        return list(self._order)

    # ---------------- 查询 ----------------

    def get(self, feature_id: str) -> Feature | None:
        return self._loaded.get(feature_id)

    def loaded(self) -> list[Feature]:
        return [self._loaded[fid] for fid in self._order]

    def describe(self) -> list[dict]:
        """功能清单（含未启用项），供命令面板/侧栏使用。"""
        rows = []
        for cls in self._catalog:
            fid = getattr(cls, "id", "")
            if not fid:
                continue
            rows.append({
                "id": fid,
                "title": getattr(cls, "title", fid),
                "icon": getattr(cls, "icon", ""),
                "group": getattr(cls, "group", "dev"),
                "enabled": fid in self._loaded,
            })
        return rows

    # ---------------- 生命周期 ----------------

    def open(self, feature_id: str) -> bool:
        """打开已加载的功能；未启用/不存在返回 False。"""
        inst = self._loaded.get(feature_id)
        if inst is None:
            return False
        try:
            inst.open()
            return True
        except Exception as exc:
            self.errors.append((feature_id, exc))
            self._log("打开功能 %s 失败：%r" % (feature_id, exc))
            return False

    def on_theme_changed(self) -> None:
        for inst in self.loaded():
            try:
                inst.on_theme_changed()
            except Exception:
                pass

    def shutdown(self) -> None:
        """按加载逆序关闭。"""
        for fid in reversed(self._order):
            inst = self._loaded.get(fid)
            if inst is None:
                continue
            try:
                inst.shutdown()
            except Exception as exc:
                self._log("关闭功能 %s 失败：%r" % (fid, exc))
        self._loaded.clear()
        self._order.clear()

    @staticmethod
    def _log(msg: str) -> None:
        try:
            from ..core import dbg_log
            dbg_log(msg)
        except Exception:
            pass
