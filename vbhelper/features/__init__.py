"""功能包：每个功能一个子包，由 FeatureRegistry 按开关加载。

新增功能时在 ``catalog()`` 里登记（未实现的功能不要登记，避免开关打开后
找不到模块）。登记必须写成静态 import 语句：打包工具靠静态分析收集模块，
``__import__`` 拼名字会让功能在 EXE 里静默消失。
"""


def _load_error(name: str, exc: BaseException) -> None:
    """记录单个功能加载失败；日志不可用时忽略。"""
    try:
        from ..core import dbg_log
        dbg_log("features.catalog 加载 %s 失败：%r" % (name, exc))
    except Exception:
        pass


def catalog() -> list:
    """内置功能清单，返回 Feature 子类列表。

    每项独立 try：某个功能缺依赖时只跳过它，不影响启动和其它功能。
    """
    items: list = []

    try:
        from .deps import DepsFeature
        items.append(DepsFeature)
    except Exception as exc:
        _load_error("DepsFeature", exc)

    try:
        from .apiwork import ApiWorkbenchFeature
        items.append(ApiWorkbenchFeature)
    except Exception as exc:
        _load_error("ApiWorkbenchFeature", exc)

    try:
        from .mcp import McpFeature
        items.append(McpFeature)
    except Exception as exc:
        _load_error("McpFeature", exc)

    try:
        from .rag import RagFeature
        items.append(RagFeature)
    except Exception as exc:
        _load_error("RagFeature", exc)

    return items
