# -*- coding: utf-8 -*-
"""主题令牌与主题管理器（计划 01 §3.2）。

6 套内置预设 + 自定义主色派生；低饱和、克制，不做霓虹渐变。
颜色 token 全部从这里取，禁止新代码继续散落 C_* 常量。

用法示例::

    from vbhelper.ui import theme
    tm = theme.ThemeManager(root, cfg)          # cfg 只读，不回写
    preset, tokens = tm.current()
    cancel = tm.subscribe(lambda p, t: repaint(t))
    tm.set_accent("#88C0D0")
    cancel()
"""
import colorsys
import ctypes
from dataclasses import dataclass, replace
from tkinter import font as tkfont
from tkinter import ttk

from . import scale


# ---------------------------------------------------------------------------
# 颜色工具（纯 Python，不引依赖）
# ---------------------------------------------------------------------------

def _parse(hex_color: str) -> tuple[int, int, int]:
    """'#RRGGBB' -> (r, g, b)，异常输入返回灰色。"""
    try:
        h = hex_color.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise ValueError(hex_color)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return 128, 128, 128


def _to_hex(r: int, g: int, b: int) -> str:
    """(r, g, b) -> '#RRGGBB'。"""
    return "#%02X%02X%02X" % (
        max(0, min(255, int(round(r)))),
        max(0, min(255, int(round(g)))),
        max(0, min(255, int(round(b)))))


def _adjust(hex_color: str, light: float = 0.0, sat: float = 0.0) -> str:
    """HSL 微调：light/sat 为 -1~1 的相对变化量（0.10 ≈ 提亮 10%）。"""
    r, g, b = (v / 255.0 for v in _parse(hex_color))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l + light))
    s = max(0.0, min(1.0, s * (1.0 + sat)))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return _to_hex(r * 255, g * 255, b * 255)


def _blend(fg: str, bg: str, ratio: float) -> str:
    """fg 按 ratio 混入 bg（0=纯 bg，1=纯 fg）。"""
    r1, g1, b1 = _parse(fg)
    r2, g2, b2 = _parse(bg)
    return _to_hex(r2 + (r1 - r2) * ratio,
                   g2 + (g1 - g2) * ratio,
                   b2 + (b1 - b2) * ratio)


def _luminance(hex_color: str) -> float:
    """WCAG 相对亮度。"""
    def chan(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _parse(hex_color)
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """两色对比度（WCAG 公式），正文要求 >= 4.5。"""
    l1, l2 = _luminance(fg), _luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def normalize_hex(hex_color: str) -> str:
    """规范成大写 '#RRGGBB'。"""
    r, g, b = _parse(hex_color)
    return _to_hex(r, g, b)


# ---------------------------------------------------------------------------
# 令牌与预设
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tokens:
    """一套主题的完整颜色/圆角令牌。"""
    bg: str
    surface: str
    elevated: str
    hover: str
    selected: str
    border: str
    text: str
    muted: str
    dim: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    success: str
    warning: str
    danger: str
    shadow: str
    radius_sm: int
    radius_md: int
    radius_lg: int


@dataclass(frozen=True)
class ThemePreset:
    """主题预设：id/名称/默认令牌/悬浮球渐变三色。"""
    id: str
    name: str
    tokens: Tokens
    ball: tuple[str, str, str]   # 渐变起、止、高光
    dark: bool = True


# 状态色：低饱和，全局共用
_STATUS = {"success": "#5BB98A", "warning": "#D9A85C", "danger": "#D96A6A"}

# 6 套预设的中性色底子（bg/surface/elevated/hover/border/text/muted/dim）
_BASES: dict[str, dict[str, str]] = {
    "midnight": {
        "name": "午夜",
        "bg": "#15171E", "surface": "#1C1F28", "elevated": "#222631",
        "hover": "#262B38", "border": "#2E3442",
        "text": "#D7DBE6", "muted": "#9BA2B2", "dim": "#6B7386",
    },
    "graphite": {
        "name": "石墨",
        "bg": "#161719", "surface": "#1D1E21", "elevated": "#232529",
        "hover": "#27292E", "border": "#2F3237",
        "text": "#D8DADC", "muted": "#9CA0A6", "dim": "#6D7176",
    },
    "nord": {
        "name": "北境",
        "bg": "#171B22", "surface": "#1E232D", "elevated": "#252B37",
        "hover": "#293041", "border": "#313A49",
        "text": "#D9DFEA", "muted": "#9FA8B8", "dim": "#737C8E",
    },
    "sakura": {
        "name": "樱雾",
        "bg": "#1A1418", "surface": "#221B20", "elevated": "#291F26",
        "hover": "#2E232B", "border": "#3A2C34",
        "text": "#EBDEE5", "muted": "#AD98A3", "dim": "#7D6B75",
    },
    "amber": {
        "name": "琥珀",
        "bg": "#171410", "surface": "#1E1A15", "elevated": "#252019",
        "hover": "#2A241C", "border": "#352E23",
        "text": "#E5DCCD", "muted": "#A99E8B", "dim": "#786E5E",
    },
    "forest": {
        "name": "苔原",
        "bg": "#121713", "surface": "#191F1B", "elevated": "#202823",
        "hover": "#242E27", "border": "#2C382F",
        "text": "#DAE4DB", "muted": "#99A9A0", "dim": "#6C7A71",
    },
    # 以下 6 套取自公开流行配色的官方色值，按本应用层级微调
    "mocha": {                                    # Catppuccin Mocha
        "name": "摩卡",
        "bg": "#11111B", "surface": "#181825", "elevated": "#1E1E2E",
        "hover": "#262637", "border": "#313244",
        "text": "#CDD6F4", "muted": "#A6ADC8", "dim": "#7F849C",
    },
    "rose": {                                     # Rosé Pine
        "name": "玫瑰",
        "bg": "#191724", "surface": "#1F1D2E", "elevated": "#26233A",
        "hover": "#2A2842", "border": "#403D52",
        "text": "#E0DEF4", "muted": "#908CAA", "dim": "#6E6A86",
    },
    "tokyo": {                                    # Tokyo Night
        "name": "东京夜",
        "bg": "#16161E", "surface": "#1A1B26", "elevated": "#1F2335",
        "hover": "#292E42", "border": "#3B4261",
        "text": "#C0CAF5", "muted": "#A9B1D6", "dim": "#737AA2",
    },
    "evergreen": {                                # Everforest Dark
        "name": "常青",
        "bg": "#2D353B", "surface": "#343F44", "elevated": "#3A464C",
        "hover": "#3D484D", "border": "#475258",
        "text": "#D3C6AA", "muted": "#9DA9A0", "dim": "#7A8478",
    },
    "terracotta": {                               # Gruvbox Dark
        "name": "陶土",
        "bg": "#1D2021", "surface": "#282828", "elevated": "#32302F",
        "hover": "#3C3836", "border": "#504945",
        "text": "#EBDBB2", "muted": "#A89984", "dim": "#7C6F64",
    },
    "kanagawa": {                                 # Kanagawa
        "name": "鎌仓",
        "bg": "#16161D", "surface": "#1F1F28", "elevated": "#2A2A37",
        "hover": "#2C2C3B", "border": "#3B3B4A",
        "text": "#DCD7BA", "muted": "#9E9B90", "dim": "#727169",
    },
}

# 各预设默认主色
_ACCENTS = {
    "midnight": "#7C9BFF",
    "graphite": "#5BD1D7",
    "nord": "#88C0D0",
    "sakura": "#F08FB3",
    "amber": "#E8B15C",
    "forest": "#6FD3A6",
    "mocha": "#CBA6F7",       # Catppuccin mauve
    "rose": "#EBBCBA",        # Rosé Pine rose
    "tokyo": "#7AA2F7",       # Tokyo Night blue
    "evergreen": "#7FBBB3",   # Everforest aqua
    "terracotta": "#83A598",  # Gruvbox blue
    "kanagawa": "#7FB4CA",    # Kanagawa springBlue
}

DEFAULT_PRESET = "midnight"


def derive_tokens(base: dict[str, str], accent: str) -> Tokens:
    """由中性色底子 + 主色派生完整 Tokens。"""
    surface = base["surface"]
    return Tokens(
        bg=base["bg"],
        surface=surface,
        elevated=base["elevated"],
        hover=base["hover"],
        selected=_blend(accent, surface, 0.22),
        border=base["border"],
        text=base["text"],
        muted=base["muted"],
        dim=base["dim"],
        accent=normalize_hex(accent),
        accent_hover=_adjust(accent, light=0.10, sat=0.04),
        accent_pressed=_adjust(accent, light=-0.12),
        accent_soft=_blend(accent, surface, 0.16),
        success=_STATUS["success"],
        warning=_STATUS["warning"],
        danger=_STATUS["danger"],
        shadow="#000000",
        radius_sm=scale.s(4),
        radius_md=scale.s(8),
        radius_lg=scale.s(14),
    )


def derive_ball(accent: str) -> tuple[str, str, str]:
    """悬浮球渐变三色：起（微亮）、止（微暗）、高光（明显亮）。"""
    return (_adjust(accent, light=0.06),
            _adjust(accent, light=-0.16),
            _adjust(accent, light=0.32))


def build_preset(pid: str, accent: str | None = None) -> ThemePreset:
    """构造一个预设（可带自定义主色）。"""
    base = _BASES[pid]
    acc = normalize_hex(accent) if accent else _ACCENTS[pid]
    return ThemePreset(
        id=pid,
        name=base["name"],
        tokens=derive_tokens(base, acc),
        ball=derive_ball(acc),
    )


def build_presets() -> dict[str, ThemePreset]:
    """全部内置预设（默认主色）。"""
    return {pid: build_preset(pid) for pid in _BASES}


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------

def _system_reduce_animations() -> bool:
    """读系统「关闭客户端区域动画」设置，命中则降级为 reduced。"""
    try:
        val = ctypes.c_int(0)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x1002, 0, ctypes.byref(val), 0)   # SPI_GETCLIENTAREAANIMATION
        return bool(ok) and val.value == 0
    except Exception:
        return False


class ThemeManager:
    """主题管理器：预设/主色切换、令牌派生、ttk 应用、订阅通知。

    cfg 为应用配置 dict，只读不写（持久化由集成阶段负责）::

        tm = ThemeManager(root, cfg)
        tm.set_preset("nord")
        tm.set_accent("#88C0D0")
    """

    def __init__(self, root, cfg: dict) -> None:
        self.root = root
        self.cfg = cfg
        t = cfg.get("theme") or {}
        if not isinstance(t, dict):
            t = {}
        pid = t.get("preset") if t.get("preset") in _BASES else DEFAULT_PRESET
        accent = t.get("accent") or None
        anim = t.get("animations")
        if anim not in ("full", "reduced", "off"):
            anim = "full"
        if anim == "full" and _system_reduce_animations():
            anim = "reduced"

        self._presets = build_presets()
        self._preset_id = pid
        self._custom_accent = accent
        base = self._presets[pid]
        acc = normalize_hex(accent) if accent else base.tokens.accent
        self._tokens = derive_tokens(_BASES[pid], acc)
        self._preset = replace(base, tokens=self._tokens,
                               ball=derive_ball(acc))
        self._subs: list = []
        self._animations = anim

    # -- 查询 ------------------------------------------------------------

    def current(self) -> tuple[ThemePreset, Tokens]:
        """当前生效的 (ThemePreset, Tokens)，preset 已带自定义主色后的令牌。"""
        return self._preset, self._tokens

    def presets(self) -> list[ThemePreset]:
        """内置预设列表（按定义顺序，默认主色）。"""
        return list(self._presets.values())

    @property
    def animations(self) -> str:
        """动画档位：full | reduced | off。"""
        return self._animations

    def color(self, name: str) -> str:
        """按令牌名取颜色，未知名字抛 KeyError。"""
        try:
            return getattr(self._tokens, name)
        except AttributeError:
            raise KeyError("unknown token: %r" % name) from None

    def font(self, size: int, weight: str = "normal") -> tkfont.Font:
        """主题代理的像素字号 Font（委托 scale.font）。"""
        return scale.font(size, weight=weight)

    # -- 切换 ------------------------------------------------------------

    def set_preset(self, preset_id: str, accent: str | None = None) -> None:
        """切换预设。

        accent 给定时覆盖主色（记为自定义主色）；不给则回到该预设
        默认主色（想保留自定义主色的调用方需显式传入）。
        """
        if preset_id not in self._presets:
            raise KeyError("unknown preset: %r" % preset_id)
        base = self._presets[preset_id]
        self._custom_accent = normalize_hex(accent) if accent else None
        acc = self._custom_accent if self._custom_accent else base.tokens.accent
        self._preset_id = preset_id
        self._tokens = derive_tokens(_BASES[preset_id], acc)
        self._preset = replace(base, tokens=self._tokens,
                               ball=derive_ball(acc))
        self._notify()

    def set_accent(self, hex_color: str) -> None:
        """只改主色：在当前预设上派生 hover/pressed/soft 与球渐变。"""
        acc = normalize_hex(hex_color)
        self._custom_accent = acc
        self._tokens = derive_tokens(_BASES[self._preset_id], acc)
        self._preset = replace(self._preset, tokens=self._tokens,
                               ball=derive_ball(acc))
        self._notify()

    # -- 应用 ------------------------------------------------------------

    def apply_ttk(self, style: ttk.Style) -> None:
        """把当前令牌应用到 ttk.Style 的常用控件。

        先切 clam 基础主题：vista/win32 下 Treeview/Heading 的底色不吃
        style.configure（元素引擎固定渲染），clam 才能让深色真正生效。
        """
        t = self._tokens
        try:
            if style.theme_use() not in ("clam",):
                style.theme_use("clam")
        except Exception:
            pass
        try:
            style.configure(".", background=t.bg, foreground=t.text,
                            bordercolor=t.border,
                            lightcolor=t.surface, darkcolor=t.border,
                            troughcolor=t.surface, fieldbackground=t.surface)
            style.configure("TFrame", background=t.bg)
            style.configure("TPanedwindow", background=t.bg)
            style.configure("TLabel", background=t.bg, foreground=t.text)
            style.configure("TButton", background=t.surface,
                            foreground=t.text, borderwidth=0,
                            focusthickness=1, relief="flat",
                            focuscolor=t.accent)
            style.map("TButton",
                      background=[("pressed", t.accent_pressed),
                                  ("active", t.hover)],
                      foreground=[("disabled", t.dim)])
            style.configure("TEntry", fieldbackground=t.surface,
                            foreground=t.text, insertcolor=t.text,
                            bordercolor=t.border, lightcolor=t.border)
            style.configure("TCombobox", fieldbackground=t.surface,
                            background=t.surface, foreground=t.text,
                            arrowcolor=t.muted, bordercolor=t.border)
            style.configure("TCheckbutton", background=t.bg,
                            foreground=t.text, focuscolor=t.accent)
            style.configure("TRadiobutton", background=t.bg,
                            foreground=t.text)
            style.configure("TScale", background=t.bg,
                            troughcolor=t.surface)
            style.configure("Horizontal.TScrollbar", background=t.surface,
                            troughcolor=t.bg, borderwidth=0,
                            arrowsize=scale.s(12))
            style.configure("Vertical.TScrollbar", background=t.surface,
                            troughcolor=t.bg, borderwidth=0,
                            arrowsize=scale.s(12))
            style.configure("Treeview", background=t.surface,
                            foreground=t.text, fieldbackground=t.surface,
                            borderwidth=0, rowheight=scale.s(28))
            style.configure("Treeview.Heading", background=t.elevated,
                            foreground=t.muted, relief="flat")
            style.map("Treeview",
                      background=[("selected", t.selected)],
                      foreground=[("selected", t.text)])
            style.configure("TProgressbar", background=t.accent,
                            troughcolor=t.surface, borderwidth=0)
        except Exception:
            pass   # ttk 样式名随版本有差异，失败不致命

    # -- 订阅 ------------------------------------------------------------

    def subscribe(self, cb) -> "callable":
        """订阅主题变化：cb(preset, tokens)；返回取消函数。

        示例::

            cancel = tm.subscribe(lambda p, t: repaint(t))
            ...
            cancel()
        """
        self._subs.append(cb)

        def _cancel() -> None:
            try:
                self._subs.remove(cb)
            except ValueError:
                pass

        return _cancel

    def _notify(self) -> None:
        for cb in list(self._subs):
            try:
                cb(self._preset, self._tokens)
            except Exception:
                pass   # 单个订阅者出错不拖垮其他订阅者
