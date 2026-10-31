# -*- coding: utf-8 -*-
"""PIL 超采样渲染：圆/圆角/渐变/封面裁切/阴影（计划 01 §3.3）。

所有静态图形走这里，避免 Tk Canvas 锯齿；小图 4x、大图 2x 超采样，
LANCZOS 下采样。阴影用多层半透明叠加预渲染，不做实时高斯模糊。

界面文字仍然由上层 Tk create_text / Label 叠加；唯一的例外是图标——
`ui/icons.py` 用打包进来的 Feather 字体 + Pillow FreeType 栅格化，
再交给本模块的 `photo()` 缓存成 Tk 图片。打包时必须保留 PIL._imagingft。

用法示例::

    from vbhelper.ui import render
    img = render.circle(64, "#7C9BFF")
    ph = render.photo(img, key="ball:64:#7C9BFF")
    canvas.create_image(0, 0, image=ph, anchor="nw")
"""
import io
import zlib
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageTk

# photo() LRU 上限（Tk PhotoImage 驻留内存，超了会涨内存）
_LRU_MAX = 256

# (key -> PhotoImage) 最近使用在末尾
_photo_cache: "OrderedDict[object, ImageTk.PhotoImage]" = OrderedDict()


def _factor(w: int, h: int) -> int:
    """超采样倍率：小图 4x 保证边缘质量，大图 2x 控制 CPU。"""
    return 4 if max(w, h) < 64 else 2


def _new_canvas(w: int, h: int, f: int) -> Image.Image:
    """超采样透明画布。"""
    return Image.new("RGBA", (w * f, h * f), (0, 0, 0, 0))


def _shrink(img: Image.Image, w: int, h: int) -> Image.Image:
    """LANCZOS 下采样回目标尺寸。"""
    return img.resize((w, h), Image.LANCZOS)


def _draw_shadow(canvas: Image.Image, box: tuple[int, int, int, int],
                 color: str, f: int) -> None:
    """多层半透明圆角矩形叠加成柔和阴影（预渲染，无模糊滤镜）。"""
    r, g, b = _rgb(color)
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    rw = (x1 - x0) // 2
    rh = (y1 - y0) // 2
    layers = 8
    spread = max(2 * f, (rw + rh) // 12)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for i in range(layers):
        k = (i + 1) / layers
        grow = int(spread * k * 0.8)
        alpha = int(70 * (1.0 - k) + 6)
        d.ellipse((cx - rw - grow, cy - rh - grow,
                   cx + rw + grow, cy + rh + int(grow * 2.2)),
                  fill=(r, g, b, alpha))
    canvas.alpha_composite(overlay)


def _rgb(color: str) -> tuple[int, int, int]:
    """'#RRGGBB' -> (r, g, b)。"""
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def circle(size: int, fill: str, *,
           border: str | None = None, border_width: int = 1,
           shadow: bool = True, highlight: bool = True) -> Image.Image:
    """渲染圆形（悬浮球/头像/状态点）。

    shadow=True 时圆形内缩给阴影留边（pad=max(3, size//10)），
    输出尺寸恒等于 size x size。

    示例::

        img = render.circle(64, tokens.accent, shadow=True)
    """
    f = _factor(size, size)
    pad = max(3, size // 10) if shadow else 0
    d = size - 2 * pad
    if d < 2:
        d = size
        pad = 0
    canvas = _new_canvas(size, size, f)
    x0, y0 = pad * f, pad * f
    x1, y1 = (pad + d) * f, (pad + d) * f

    if shadow:
        _draw_shadow(canvas, (x0, y0, x1, y1), "#000000", f)

    d = ImageDraw.Draw(canvas)
    d.ellipse((x0, y0, x1, y1), fill=_rgb(fill) + (255,))
    if border:
        bw = max(1, border_width) * f
        d.ellipse((x0 + bw // 2, y0 + bw // 2, x1 - bw // 2, y1 - bw // 2),
                  outline=_rgb(border) + (255,), width=bw)
    if highlight:
        # 顶部两层半透明白椭圆近似弧形高光；
        # ImageDraw 是覆盖写入不是混合，必须画在独立层再 alpha_composite
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        hd = ImageDraw.Draw(overlay)
        hw = int((x1 - x0) * 0.56)
        hh = int((y1 - y0) * 0.34)
        hcx = (x0 + x1) // 2
        hcy = y0 + int((y1 - y0) * 0.30)
        hd.ellipse((hcx - hw // 2, hcy - hh // 2,
                    hcx + hw // 2, hcy + hh // 2), fill=(255, 255, 255, 40))
        hw //= 2
        hh //= 2
        hd.ellipse((hcx - hw // 2, hcy - hh // 2,
                    hcx + hw // 2, hcy + hh // 2), fill=(255, 255, 255, 34))
        canvas.alpha_composite(overlay)
    return _shrink(canvas, size, size)


def rounded(size: tuple[int, int], radius: int, fill: str, *,
            border: str | None = None, border_width: int = 1,
            shadow: bool = False) -> Image.Image:
    """圆角矩形（卡片/面板/按钮底）。

    shadow=False 时贴满画布，四角透明；shadow=True 内缩留阴影边。

    示例::

        img = render.rounded((320, 200), 12, tokens.surface)
    """
    w, h = size
    f = _factor(w, h)
    pad = max(4, min(w, h) // 10) if shadow else 0
    canvas = _new_canvas(w, h, f)
    r = max(0, int(radius)) * f
    x0, y0 = pad * f, pad * f
    x1, y1 = (w - pad) * f, (h - pad) * f
    if shadow:
        _draw_shadow(canvas, (x0, y0, x1, y1), "#000000", f)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((x0, y0, x1, y1), radius=r,
                        fill=_rgb(fill) + (255,))
    if border:
        bw = max(1, border_width) * f
        d.rounded_rectangle((x0 + bw // 2, y0 + bw // 2,
                             x1 - bw // 2, y1 - bw // 2),
                            radius=max(0, r - bw // 2),
                            outline=_rgb(border) + (255,), width=bw)
    return _shrink(canvas, w, h)


def gradient(size: tuple[int, int], top: str, bottom: str,
             radius: int = 0) -> Image.Image:
    """垂直线性渐变，radius>0 时裁圆角。

    示例::

        img = render.gradient((64, 64), preset.ball[0], preset.ball[1])
    """
    w, h = size
    c1, c2 = _rgb(top), _rgb(bottom)
    # 1 x h 的渐变条再放大，避免逐像素填大图
    strip = Image.new("RGBA", (1, h))
    px = strip.load()
    for y in range(h):
        k = y / max(1, h - 1)
        px[0, y] = (int(c1[0] + (c2[0] - c1[0]) * k),
                    int(c1[1] + (c2[1] - c1[1]) * k),
                    int(c1[2] + (c2[2] - c1[2]) * k), 255)
    img = strip.resize((w, h), Image.BILINEAR).convert("RGBA")

    if radius and radius > 0:
        f = _factor(w, h)
        mask = Image.new("L", (w * f, h * f), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, w * f - 1, h * f - 1), radius=int(radius) * f, fill=255)
        mask = _shrink(mask, w, h)
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out
    return img


def cover(source: bytes | str, size: int, radius: int,
          shadow: bool = False) -> Image.Image:
    """封面图：居中方形裁切 + 圆角；source 为图片字节或路径。

    shadow=True 时输出 (size+2p) x (size+2p)，p=max(4,size//12)。

    示例::

        img = render.cover(cover_bytes, 96, 12)
    """
    if isinstance(source, bytes):
        src = Image.open(io.BytesIO(source))
    else:
        src = Image.open(source)
    with src:
        img = src.convert("RGBA")
        w, h = img.size
        if w != h:
            m = min(w, h)
            img = img.crop(((w - m) // 2, (h - m) // 2,
                            (w - m) // 2 + m, (h - m) // 2 + m))
        img = img.resize((size, size), Image.LANCZOS)

    if radius and radius > 0:
        f = _factor(size, size)
        mask = Image.new("L", (size * f, size * f), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, size * f - 1, size * f - 1), radius=int(radius) * f,
            fill=255)
        mask = _shrink(mask, size, size)
        cut = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        cut.paste(img, (0, 0), mask)
        img = cut

    if not shadow:
        return img
    pad = max(4, size // 12)
    total = size + 2 * pad
    out = Image.new("RGBA", (total, total), (0, 0, 0, 0))
    # 阴影单独在 2x 画布上预渲染再缩小，避免硬边
    sf = 2
    shadow_layer = Image.new("RGBA", (total * sf, total * sf),
                             (0, 0, 0, 0))
    _draw_shadow(shadow_layer,
                 (pad * sf, pad * sf,
                  (pad + size) * sf, (pad + size) * sf),
                 "#000000", sf)
    out.alpha_composite(_shrink(shadow_layer, total, total))
    out.alpha_composite(img, (pad, pad))
    return out


def photo(image: Image.Image, key: str | None = None) -> ImageTk.PhotoImage:
    """Image -> ImageTk.PhotoImage，带 LRU 缓存。

    key 建议包含尺寸/颜色/状态（如 "ball:64:#7C9BFF:hover"）；
    key=None 时按图像内容/对象自动生成签名。窗口关闭时调用
    clear_cache() 或换 key 前缀释放。

    示例::

        ph1 = render.photo(img, key="cover:42")
        ph2 = render.photo(img, key="cover:42")   # 命中缓存，同一对象
    """
    if key is None:
        if max(image.size) <= 256:
            sig = zlib.crc32(image.tobytes())
        else:
            sig = id(image)
        key = ("auto", image.size, image.mode, sig)

    cached = _photo_cache.get(key)
    if cached is not None:
        _photo_cache.move_to_end(key)
        return cached

    ph = ImageTk.PhotoImage(image)
    _photo_cache[key] = ph
    _photo_cache.move_to_end(key)
    while len(_photo_cache) > _LRU_MAX:
        _photo_cache.popitem(last=False)
    return ph


def clear_cache() -> None:
    """清空 photo 缓存（窗口销毁/主题切换释放旧帧）。"""
    _photo_cache.clear()
