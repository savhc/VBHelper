"""生成 VBHelper 程序图标。

设计：深色圆角底 + 主色悬浮球 + 球内目录树符号。全部程序化绘制，
不依赖外部素材，改配色只需调下面的常量。

输出：
    vbhelper/assets/vbhelper.ico        多尺寸（16–256），给 exe/托盘
    vbhelper/assets/vbhelper_256.png    给 Tk iconphoto
    _shots/icon_preview.png             多尺寸预览（自检用）

运行：python tools/make_icon.py
"""
import io as _io
import math
import os
import struct

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SS = 1  # 直接在 1024 上绘制，缩小时用 LANCZOS

BG_TOP = (40, 46, 66)
BG_BOTTOM = (13, 15, 21)
BALL_HI = (150, 174, 255)
BALL_LO = (84, 112, 224)
BALL_CENTER = (512, 496)
BALL_RADIUS = 272

TREE_COLOR = (247, 249, 255)
TREE_ALPHA = 246
ROOT = (356, 512)
SPLIT = (508, 512)
LEAF_UP = (652, 398)
LEAF_DOWN = (652, 626)
LINE_W = 48
NODE_R = 38

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "vbhelper", "assets")
SHOTS = os.path.join(HERE, "_shots")


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _vertical_gradient(size, top, bottom):
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        grad.putpixel((0, y), _lerp(top, bottom, y / (size - 1)))
    return grad.resize((size, size), Image.BILINEAR)


def _radial_ball(size, center, radius, inner, outer):
    """球体：中心偏左上亮，边缘暗；超出半径的像素用外圈色。"""
    img = Image.new("RGB", (size, size), outer)
    px = img.load()
    cx, cy = center
    # 高光中心比几何中心更靠左上
    hx, hy = cx - radius * 0.28, cy - radius * 0.34
    for y in range(max(0, int(cy - radius)), min(size, int(cy + radius) + 1)):
        dy = y - cy
        for x in range(max(0, int(cx - radius)), min(size, int(cx + radius) + 1)):
            d = math.hypot(x - cx, dy)
            if d >= radius:
                continue
            dn = d / radius
            hd = math.hypot(x - hx, y - hy) / (radius * 1.7)
            # 基础径向 + 左上高光
            t = min(1.0, dn * 1.05)
            base = _lerp(inner, outer, t)
            glow = max(0.0, 1.0 - hd) ** 2 * 0.30
            px[x, y] = _lerp(base, (255, 255, 255), glow)
    return img


def _draw_symbol(size):
    """目录树：根在左，向右分叉到上下两个子节点。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    fill = TREE_COLOR + (TREE_ALPHA,)

    for a, b in ((ROOT, SPLIT), (SPLIT, LEAF_UP), (SPLIT, LEAF_DOWN)):
        d.line([a, b], fill=fill, width=LINE_W)
        d.ellipse([a[0] - LINE_W // 2, a[1] - LINE_W // 2,
                   a[0] + LINE_W // 2, a[1] + LINE_W // 2], fill=fill)
        d.ellipse([b[0] - LINE_W // 2, b[1] - LINE_W // 2,
                   b[0] + LINE_W // 2, b[1] + LINE_W // 2], fill=fill)

    for cx, cy in (ROOT, LEAF_UP, LEAF_DOWN):
        d.ellipse([cx - NODE_R, cy - NODE_R, cx + NODE_R, cy + NODE_R], fill=fill)
    return layer


def build(size=SIZE):
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 底色
    bg = _vertical_gradient(size, BG_TOP, BG_BOTTOM).convert("RGBA")
    canvas.paste(bg, (0, 0), _rounded_mask(size, int(size * 0.225)))

    # 内侧描边：1.5px 浅色，增加边缘分离度
    edge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        [1, 1, size - 2, size - 2], radius=int(size * 0.225),
        outline=(255, 255, 255, 26), width=max(2, size // 340))
    canvas = Image.alpha_composite(canvas, edge)

    # 球体外发光
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [BALL_CENTER[0] - BALL_RADIUS - size * 0.03,
         BALL_CENTER[1] - BALL_RADIUS - size * 0.03,
         BALL_CENTER[0] + BALL_RADIUS + size * 0.03,
         BALL_CENTER[1] + BALL_RADIUS + size * 0.03],
        fill=(92, 124, 230, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.045))
    canvas = Image.alpha_composite(canvas, glow)

    # 球体
    ball = _radial_ball(size, BALL_CENTER, BALL_RADIUS, BALL_HI, BALL_LO).convert("RGBA")
    ball_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(ball_mask).ellipse(
        [BALL_CENTER[0] - BALL_RADIUS, BALL_CENTER[1] - BALL_RADIUS,
         BALL_CENTER[0] + BALL_RADIUS, BALL_CENTER[1] + BALL_RADIUS], fill=255)
    canvas.paste(ball, (0, 0), ball_mask)

    # 球面顶部高光
    spec = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(spec).ellipse(
        [BALL_CENTER[0] - BALL_RADIUS * 0.62, BALL_CENTER[1] - BALL_RADIUS * 0.86,
         BALL_CENTER[0] + BALL_RADIUS * 0.10, BALL_CENTER[1] - BALL_RADIUS * 0.34],
        fill=(255, 255, 255, 26))
    spec = spec.filter(ImageFilter.GaussianBlur(size * 0.022))
    canvas = Image.alpha_composite(canvas, spec)

    # 目录树符号（裁剪在球内）
    symbol = _draw_symbol(size)
    symbol.putalpha(Image.composite(symbol.getchannel("A"),
                                    Image.new("L", (size, size), 0), ball_mask))
    canvas = Image.alpha_composite(canvas, symbol)

    return canvas


def compact(base, size):
    """小尺寸专用：裁出球的圆形区域，去掉深色底，保证 16px 下仍可辨认。"""
    r = BALL_RADIUS + 8
    cx, cy = BALL_CENTER
    box = (cx - r, cy - r, cx + r, cy + r)
    return base.crop(box).resize((size, size), Image.LANCZOS)


def save_ico(items, path):
    """写多尺寸 ICO，每个尺寸用各自的图像（PNG 压缩条目，Vista+ 支持）。"""
    blobs = []
    for _, im in items:
        buf = _io.BytesIO()
        im.save(buf, format="PNG")
        blobs.append(buf.getvalue())

    count = len(items)
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    offset = 6 + 16 * count
    for (size, _), blob in zip(items, blobs):
        entries += struct.pack("<BBBBHHII",
                               size if size < 256 else 0,
                               size if size < 256 else 0,
                               0, 0, 1, 32, len(blob), offset)
        offset += len(blob)

    with open(path, "wb") as fp:
        fp.write(header + entries + b"".join(blobs))


def main():
    os.makedirs(ASSETS, exist_ok=True)
    os.makedirs(SHOTS, exist_ok=True)
    base = build()

    ico = os.path.join(ASSETS, "vbhelper.ico")
    items = []
    for size in (256, 128, 96, 64, 48):
        items.append((size, base.resize((size, size), Image.LANCZOS)))
    for size in (32, 24, 16):
        items.append((size, compact(base, size)))
    save_ico(items, ico)

    png = os.path.join(ASSETS, "vbhelper_256.png")
    base.resize((256, 256), Image.LANCZOS).save(png, format="PNG")
    base.resize((512, 512), Image.LANCZOS).save(
        os.path.join(ASSETS, "vbhelper_512.png"), format="PNG")

    # 预览：各尺寸并排，深灰底
    sizes = [256, 128, 64, 48, 32, 24, 16]
    pad = 16
    width = sum(sizes) + pad * (len(sizes) + 1)
    height = 288
    preview = Image.new("RGBA", (width, height), (32, 34, 40, 255))
    x = pad
    for s in sizes:
        thumb = compact(base, s) if s <= 32 else base.resize((s, s), Image.LANCZOS)
        preview.paste(thumb, (x, (height - s) // 2), thumb)
        x += s + pad
    preview_path = os.path.join(SHOTS, "icon_preview.png")
    preview.save(preview_path, format="PNG")

    print("ico      ->", ico)
    print("png 256  ->", png)
    print("preview  ->", preview_path)


if __name__ == "__main__":
    main()
