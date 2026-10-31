"""MCP 工具注册表：工具规格、调用上下文、结果封装与频率限制。

本模块只依赖标准库，不 import Tk、不 import 任何服务实现，可独立导入测试。
服务层对象通过 ``ToolCall.services`` 以鸭子类型传入，handler 内部自行取用。
"""

from __future__ import annotations

import base64
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "ToolSpec",
    "ToolResult",
    "ToolError",
    "ToolCall",
    "ToolRegistry",
    "tool_enabled",
    "RateLimiter",
    "parse_rate_limit",
]

# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class ToolSpec:
    """一个 MCP 工具的定义（不可变，注册后不允许改权限）。"""

    name: str
    title: str
    description: str
    schema: dict
    handler: Callable[["ToolCall"], "ToolResult"]
    group: str = "system"
    permission: str = "safe"          # safe | confirm
    default_enabled: bool = True
    timeout: float = 30.0
    rate_limit: str | None = None     # "60/min"

@dataclass
class ToolResult:
    """工具执行结果：MCP content 列表 + 错误标记 + 可选结构化数据。"""

    content: list[dict]
    is_error: bool = False
    structured: dict | None = None

    @classmethod
    def text(cls, text: str, *, structured: dict | None = None) -> "ToolResult":
        """纯文本结果（text 会强制转成 str）。"""
        return cls(
            content=[{"type": "text", "text": str(text)}],
            structured=structured,
        )

    @classmethod
    def image(cls, png: bytes, *, text: str = "",
              mime: str = "image/png") -> "ToolResult":
        """图片结果：先 image 后 text（text 为空则只有 image）。"""
        data = base64.b64encode(bytes(png or b"")).decode("ascii")
        content: list[dict] = [
            {"type": "image", "data": data, "mimeType": str(mime or "image/png")}
        ]
        if text:
            content.append({"type": "text", "text": str(text)})
        return cls(content=content)

class ToolError(Exception):
    """工具层可预期的失败：server 统一转成 ``isError: true``。"""

    def __init__(self, message: str, *, code: str = "tool_error",
                 detail: str = "") -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = str(code or "tool_error")
        self.detail = str(detail or "")

    def __str__(self) -> str:
        return self.message

@dataclass
class ToolCall:
    """一次工具调用上下文（由 server 组装，handler 只读）。"""

    ctx: Any
    services: Any
    args: dict
    session: str = ""
    confirm: Callable[[str, str], bool] | None = None
    audit: Any = None
    cfg: dict | None = None
    notify: Callable[[str, str], None] | None = None

class ToolRegistry:
    """按名字保存 ToolSpec；重复注册直接报错，避免静默覆盖。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._lock = threading.RLock()

    def register(self, spec: ToolSpec) -> None:
        if not isinstance(spec, ToolSpec):
            raise TypeError("只能注册 ToolSpec")
        if not spec.name:
            raise ValueError("工具名不能为空")
        with self._lock:
            if spec.name in self._specs:
                raise ValueError("工具已注册：%s" % spec.name)
            self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        with self._lock:
            return self._specs.get(name)

    def all(self) -> list[ToolSpec]:
        with self._lock:
            return list(self._specs.values())

    def enabled_specs(self, cfg: dict) -> list[ToolSpec]:
        """按配置过滤：用户开关 + 分组总闸 + 截屏总闸。"""
        with self._lock:
            specs = list(self._specs.values())
        return [spec for spec in specs if tool_enabled(spec, cfg)]

# ---------------------------------------------------------------- 开关规则

def _as_dict(value: Any) -> dict:
    """配置段必须是 dict，畸形值回退空 dict（避免 AttributeError）。"""
    return value if isinstance(value, dict) else {}

def tool_enabled(spec: ToolSpec, cfg: dict) -> bool:
    """计算工具是否启用。

    规则（计划文档 §4.1）：
    1. 单项覆盖 ``cfg["tools"][name]["enabled"]`` 优先于 ``default_enabled``；
    2. ``group == "input"`` 的整组工具还要求 ``cfg["input"]["enabled"]``；
    3. ``screen.capture`` 还要求 ``cfg["screen"]["allow"]``；
       ``screen.info`` / ``screen.list_windows`` 不受该总闸影响。
    配置缺失或畸形时使用安全默认：输入关、截屏开。
    """
    cfg = _as_dict(cfg)

    enabled = bool(spec.default_enabled)
    override = _as_dict(_as_dict(cfg.get("tools")).get(spec.name))
    if "enabled" in override:
        enabled = bool(override.get("enabled"))
    if not enabled:
        return False

    if spec.group == "input":
        if not bool(_as_dict(cfg.get("input")).get("enabled", False)):
            return False

    if spec.name == "screen.capture":
        screen_cfg = _as_dict(cfg.get("screen"))
        if "allow" in screen_cfg and not bool(screen_cfg.get("allow")):
            return False

    return True

# ---------------------------------------------------------------- 频率限制

_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d*)\s*([a-zA-Z]+)\s*$")

_UNIT_SECONDS: dict[str, float] = {
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
}

def parse_rate_limit(limit: str | None) -> tuple[int, float] | None:
    """解析 ``"60/min"`` → (次数, 窗口秒数)；无法解析返回 None（表示不限流）。

    支持 ``"2/min"``、``"100/10s"``、``"30/hour"``；次数 <= 0 视为不限流。
    """
    if not isinstance(limit, str) or not limit.strip():
        return None
    match = _RATE_RE.match(limit)
    if not match:
        return None
    count = int(match.group(1))
    amount = int(match.group(2) or "1")
    unit = match.group(3).lower()
    seconds = _UNIT_SECONDS.get(unit)
    if seconds is None or count <= 0 or amount <= 0:
        return None
    return count, float(amount) * seconds

class RateLimiter:
    """线程安全的滑动窗口限流器。

    以 key（一般用工具名）为粒度记录最近窗口内的调用时间戳；
    超限时返回 False 且不记录本次调用（避免持续请求把窗口越推越长）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: str | None) -> bool:
        parsed = parse_rate_limit(limit)
        if parsed is None:
            return True
        count, window = parsed
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= count:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
