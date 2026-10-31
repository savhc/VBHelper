# -*- coding: utf-8 -*-
"""Feather 图标字体：一套字体接管全项目图标，Windows / Linux / macOS 通用。

设计取舍：

- 字体文件随程序打包（``vbhelper/assets/feather.ttf``），由 Pillow 的
  FreeType 在运行时栅格化成 Tk 图片，不依赖任何系统图标字体，也不会把
  Unicode 表情/符号丢给系统做彩色 fallback；
- 不注册系统字体、不依赖 ``AddFontResourceEx``，三个平台走同一条渲染路径；
- 所有图标都是单色轮廓，颜色/尺寸由调用方在运行时指定；
- ``draw(name, size, color)`` 返回 RGBA 的 PIL 图片，调用方用
  ``render.photo()`` 转成 Tk 图片并缓存。

图标名使用 Feather 的官方名字（``folder`` / ``play`` / ``x`` ...），同时保留
项目里原有的语义别名（``close`` / ``trash`` / ``next`` ...），旧调用点不用
一次性改名。

用法示例::

    from vbhelper.ui import icons, render
    img = icons.draw("folder", 18, "#7C9BFF")
    ph = render.photo(img, key="folder:18:#7C9BFF")
"""
from functools import lru_cache
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

from .feather_glyphs import GLYPHS

# 字体资源名：打包后位于 <_MEIPASS>/vbhelper/assets/
FONT_NAME = "feather.ttf"

# 语义别名 -> Feather 官方图标名。
# 只保留项目里真实用到的旧名字；没列出的名字会先原样查 GLYPHS，再把下划线
# 换成连字符查一次，所以 ``arrow_down`` 这种写法也能直接用。
_ALIASES = {
    # 通用动作
    "close": "x",
    "cancel": "x",
    "clear": "x-circle",
    "add": "plus",
    "remove": "trash-2",
    "delete": "trash-2",
    "trash": "trash-2",
    "confirm": "check",
    "done": "check",
    "more": "more-horizontal",
    "menu": "menu",
    "settings": "settings",
    "gear": "settings",
    "cog": "settings",
    "search": "search",
    "find": "search",
    "refresh": "refresh-cw",
    "reload": "refresh-cw",
    "sync": "refresh-ccw",
    "download": "download",
    "upload": "upload",
    "copy": "copy",
    "edit": "edit-2",
    "save": "save",
    "filter": "filter",
    "send": "send",
    "share": "share-2",
    "link": "link",
    "external": "external-link",
    # 媒体
    "play": "play",
    "pause": "pause",
    "stop": "square",
    "next": "skip-forward",
    "skip_next": "skip-forward",
    "prev": "skip-back",
    "skip_prev": "skip-back",
    "forward": "skip-forward",
    "back": "skip-back",
    "volume": "volume-2",
    "mute": "volume-x",
    "music": "music",
    "heart": "heart",
    "heart_fill": "heart",
    "fav": "heart",
    "favorite": "star",
    "star": "star",
    "star_fill": "star",
    "mic": "mic",
    "microphone": "mic",
    "camera": "camera",
    # 文件/目录
    "folder": "folder",
    "folder_open": "folder",
    "folder_add": "folder-plus",
    "file": "file",
    "doc": "file",
    "page": "file",
    "note": "file-text",
    "image": "image",
    # 窗口/状态
    "pin": "map-pin",
    "unpin": "navigation",
    "lock": "lock",
    "unlock": "unlock",
    "warning": "alert-triangle",
    "error": "alert-circle",
    "info": "info",
    "help": "help-circle",
    "minimize": "minus",
    "collapse": "minus",
    "expand": "maximize",
    "restore": "maximize",
    "style_normal": "disc",
    "style_soft": "circle",
    "style_clear": "minus-circle",
    # 导航
    "up": "chevron-up",
    "down": "chevron-down",
    "left": "chevron-left",
    "right": "chevron-right",
    "chevron": "chevron-right",
    "arrow_up": "arrow-up",
    "arrow_down": "arrow-down",
    "arrow_left": "arrow-left",
    "arrow_right": "arrow-right",
    "back_arrow": "arrow-left",
    "wifi": "wifi",
    "network": "globe",
    "globe": "globe",
    "monitor": "monitor",
    "sun": "sun",
    "moon": "moon",
    "clock": "clock",
    "recent": "clock",
    "list": "list",
    "grid": "grid",
    "chart": "bar-chart-2",
    "queue": "list",
    "downloads": "download",
    "radio": "radio",
}

# 老测试/外部代码里可能直接拿 _GLYPHS 查表；保留这个名字
_GLYPHS = GLYPHS

# 字体缺失时的兜底，不让 UI 因为资源打包漏了直接崩
_FALLBACK_GLYPH = "help-circle"


def asset_dir() -> Path:
    """字体所在目录：源码运行取包内 assets，PyInstaller 运行取 _MEIPASS。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "vbhelper" / "assets"
        if p.is_dir():
            return p
        return Path(base) / "assets"
    return Path(__file__).resolve().parents[1] / "assets"


def font_path() -> Path:
    """Feather 字体文件路径。"""
    return asset_dir() / FONT_NAME


def _resolve(name: str) -> str | None:
    """把项目里的语义名/下划线名落到 Feather 官方名。"""
    if not name:
        return None
    key = _ALIASES.get(name, name)
    if key in GLYPHS:
        return key
    key = name.replace("_", "-")
    if key in GLYPHS:
        return key
    return None


def glyph(name: str) -> str:
    """取图标字符（PUA 码位）；名字未收录时返回空串。

    图标默认走 :func:`draw` 的图片渲染；这个函数只用于调试或需要把字符
    交给已经加载了 Feather 字体的第三方控件。
    """
    key = _resolve(name)
    return chr(GLYPHS[key]) if key else ""


def icon_names() -> tuple[str, ...]:
    """全部 Feather 官方图标名（按字体里的码位顺序）。"""
    return tuple(GLYPHS)


@lru_cache(maxsize=64)
def _load_font(px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path()), px)


@lru_cache(maxsize=512)
def _render(name: str, size: int, color: str) -> Image.Image:
    """栅格化并缓存一张图标；返回的图片请当作只读对象。"""
    key = _resolve(name)
    if key is None:
        key = _FALLBACK_GLYPH
    ch = chr(GLYPHS[key])

    size = max(1, int(size))
    ss = 4 if size < 64 else 2          # 小图标多倍超采样，大图标控制 CPU
    px = size * ss

    try:
        font = _load_font(px)
    except Exception:
        # 字体文件缺失：画一个很小的圆点，保证 UI 不崩、不出现豆腐块
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        r = max(1, size // 5)
        ImageDraw.Draw(img).ellipse(
            (size // 2 - r, size // 2 - r, size // 2 + r, size // 2 + r),
            fill=color)
        return img

    bbox = font.getbbox(ch)
    w, h = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
    pad = max(2, px // 16)
    layer = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((pad - bbox[0], pad - bbox[1]), ch,
                               font=font, fill=color)

    # 去掉四周透明留白后按墨迹居中：不同图标的高度差很多（x / menu 很扁，
    # folder / lock 很满），只靠基线摆会看起来忽高忽低。
    ink = layer.getbbox()
    if ink:
        layer = layer.crop(ink)
    if layer.width > px or layer.height > px:
        k = min(px / layer.width, px / layer.height)
        layer = layer.resize((max(1, int(layer.width * k)),
                              max(1, int(layer.height * k))), Image.LANCZOS)

    canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    canvas.alpha_composite(layer, ((px - layer.width) // 2,
                                    (px - layer.height) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


def draw(name: str, size: int, color: str) -> Image.Image:
    """把图标名渲染成 RGBA 图片（透明底）。

    示例::

        img = icons.draw("folder", 24, tokens.accent)
        ph = render.photo(img, key="icon:folder:24:%s" % tokens.accent)
    """
    return _render(name, int(size), color)
