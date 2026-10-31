# -*- coding: utf-8 -*-
"""UI 主题渲染层自检：scale / theme / render / icons（vbhelper/ui 前四个模块）。

无 GUI 交互，root.withdraw() 隐藏窗口；全部逻辑断言，失败打印 FAIL 并以
非零码退出。不写任何配置文件。
"""
import copy
import io
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tkinter as tk
from tkinter import ttk

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("PASS  %s" % name)
    else:
        print("FAIL  %s  %s" % (name, detail))
        FAILS.append(name)


def main():
    root = tk.Tk()
    root.withdraw()

    from vbhelper.ui import icons, render, scale, theme
    from PIL import Image

    # ------------------------------------------------------------------
    # scale
    # ------------------------------------------------------------------
    scale.init(root)
    real_dpi = scale.dpi()
    check("scale.init/dpi", real_dpi > 0 and scale.scale() == real_dpi / 96.0,
          "dpi=%s" % real_dpi)

    # 模拟四档 DPI 验证 s() 取整（半数向上）
    expect = {96: 10, 120: 13, 144: 15, 192: 20}
    for dpi, want in expect.items():
        scale._dpi, scale._scale = dpi, dpi / 96.0
        got = scale.s(10)
        check("scale.s(10)@%ddpi" % dpi, got == want,
              "got=%s want=%s" % (got, want))
    scale._dpi, scale._scale = 96, 1.0
    check("scale.s(0.4)@96 最小1", scale.s(0.4) == 1, "got=%s" % scale.s(0.4))
    scale._dpi, scale._scale = 192, 2.0
    check("scale.s(1)@192", scale.s(1) == 2, "got=%s" % scale.s(1))
    scale.reset_cache()
    f = scale.font(13)
    check("scale.font 像素字号(负数)", int(f.cget("size")) == -26,
          "size=%s" % f.cget("size"))
    check("scale.font 加粗", f.cget("weight") == "normal" and
          scale.font(13, weight="bold").cget("weight") == "bold")
    scale.reset_cache()
    scale._dpi, scale._scale = real_dpi, real_dpi / 96.0

    check("scale.window_size", scale.window_size(900, 640) ==
          (scale.s(900), scale.s(640)))
    w, h = scale.clamp_to_work_area(root, 99999, 99999)
    check("scale.clamp_to_work_area", w > 0 and h > 0 and w <= 99999 and
          h <= 99999, "(%s,%s)" % (w, h))

    # ------------------------------------------------------------------
    # theme
    # ------------------------------------------------------------------
    cfg = {"theme": {"preset": "midnight", "accent": "#7C9BFF",
                     "animations": "full"}}
    snapshot = copy.deepcopy(cfg)
    tm = theme.ThemeManager(root, cfg)

    ids = [p.id for p in tm.presets()]
    check("theme 12 套预设齐全",
          ids == ["midnight", "graphite", "nord", "sakura", "amber", "forest",
                  "mocha", "rose", "tokyo", "evergreen", "terracotta", "kanagawa"],
          "ids=%s" % ids)
    accents = {"midnight": "#7C9BFF", "graphite": "#5BD1D7",
               "nord": "#88C0D0", "sakura": "#F08FB3",
               "amber": "#E8B15C", "forest": "#6FD3A6"}
    for p in tm.presets():
        check("theme 预设有名字/%s" % p.id, bool(p.name) and len(p.ball) == 3)

    for pid, acc in accents.items():
        tm.set_preset(pid)
        preset, tokens = tm.current()
        check("theme.set_preset/%s" % pid,
              preset.id == pid and tokens.accent == acc,
              "accent=%s want=%s" % (tokens.accent, acc))
        cr_bg = theme.contrast_ratio(tokens.text, tokens.bg)
        cr_sf = theme.contrast_ratio(tokens.text, tokens.surface)
        check("theme 正文对比度/%s" % pid,
              cr_bg >= 4.5 and cr_sf >= 4.5,
              "bg=%.2f surface=%.2f" % (cr_bg, cr_sf))
        cr_acc = theme.contrast_ratio(tokens.accent, tokens.bg)
        check("theme 主色对比度/%s" % pid, cr_acc >= 3.0,
              "accent=%.2f" % cr_acc)
        # 派生色一致性：hover 更亮、pressed 更暗、soft 是低比例混入
        check("theme 派生一致/%s" % pid,
              tokens.accent_hover != tokens.accent and
              tokens.accent_pressed != tokens.accent and
              tokens.accent_soft != tokens.accent and
              tokens.selected != tokens.surface and
              tokens.radius_sm > 0 and tokens.radius_md >= tokens.radius_sm)

    # set_accent 派生
    hits = []
    cancel = tm.subscribe(lambda p, t: hits.append((p.id, t.accent)))
    tm.set_accent("#FF5555")
    preset, tokens = tm.current()
    check("theme.set_accent 生效",
          tokens.accent == "#FF5555" and preset.id == "forest",
          "accent=%s" % tokens.accent)
    check("theme.subscribe 触发", hits and hits[-1] == ("forest", "#FF5555"),
          "hits=%r" % (hits,))
    lum = lambda c: theme._luminance(c)
    check("theme hover亮/pressed暗",
          lum(tokens.accent_hover) > lum(tokens.accent) >
          lum(tokens.accent_pressed),
          "%s > %s > %s" % (lum(tokens.accent_hover), lum(tokens.accent),
                            lum(tokens.accent_pressed)))
    # 取消订阅后不再触发
    cancel()
    n = len(hits)
    tm.set_accent("#66CCFF")
    check("theme.subscribe 取消", len(hits) == n, "hits=%s" % len(hits))

    # set_preset 带主色覆盖
    tm.set_preset("nord", "#7C9BFF")
    check("theme.set_preset(preset, accent)",
          tm.current()[1].accent == "#7C9BFF" and
          tm.current()[0].id == "nord")
    tm.set_preset("nord")
    check("theme.set_preset 回默认主色", tm.current()[1].accent == "#88C0D0",
          "accent=%s" % tm.current()[1].accent)

    # color / font / apply_ttk
    check("theme.color", tm.color("bg") == tm.current()[1].bg)
    try:
        tm.color("nope")
        check("theme.color 未知名抛错", False)
    except KeyError:
        check("theme.color 未知名抛错", True)
    tf = tm.font(12, weight="bold")
    check("theme.font", int(tf.cget("size")) == -scale.s(12) and
          tf.cget("weight") == "bold")
    try:
        tm.apply_ttk(ttk.Style(root))
        check("theme.apply_ttk", True)
    except Exception as e:
        check("theme.apply_ttk", False, repr(e))

    # cfg 只读
    check("theme cfg 只读不写", cfg == snapshot, "cfg=%s" % cfg)

    # 动画档位
    check("theme animations=off",
          theme.ThemeManager(root, {"theme": {"animations": "off"}})
          .animations == "off")
    check("theme animations=reduced",
          theme.ThemeManager(root, {"theme": {"animations": "reduced"}})
          .animations == "reduced")
    check("theme animations=full(或系统降级)",
          theme.ThemeManager(root, {"theme": {"animations": "full"}})
          .animations in ("full", "reduced"))
    check("theme 空 cfg 兜底",
          theme.ThemeManager(root, {}).current()[0].id == "midnight")

    # ------------------------------------------------------------------
    # render
    # ------------------------------------------------------------------
    img = render.circle(64, "#7C9BFF")
    check("render.circle 尺寸", img.size == (64, 64) and img.mode == "RGBA")
    px = img.load()
    check("render.circle 中心不透明", px[32, 32][3] == 255,
          "alpha=%s" % px[32, 32][3])
    check("render.circle 角落透明", px[0, 0][3] == 0, "alpha=%s" % px[0, 0][3])
    # pad 区（球下方）应只有柔和阴影：低 alpha 但非零
    shadow_px = px[32, 60][3]
    check("render.circle 阴影半透明", 0 < shadow_px <= 128,
          "alpha=%s" % shadow_px)
    rim = [px[x, y][3] for x in range(0, 64, 2) for y in range(0, 64, 2)]
    check("render.circle 有抗锯齿过渡带",
          any(1 <= a <= 254 for a in rim), "rim=%s" % rim[:8])

    img2 = render.circle(64, "#7C9BFF", border="#FFFFFF", shadow=False,
                         highlight=False)
    p2 = img2.load()
    rim2 = [p2[x, y][3] for x in range(0, 64, 2) for y in range(0, 64, 2)]
    check("render.circle 无阴影中心/边缘", p2[32, 32][3] == 255 and
          p2[0, 0][3] == 0 and any(1 <= a <= 254 for a in rim2),
          "edge=%s" % p2[63, 32][3])

    img3 = render.rounded((80, 60), 12, "#22242C")
    p3 = img3.load()
    check("render.rounded 中心/角落", p3[40, 30][3] == 255 and
          p3[0, 0][3] == 0 and img3.size == (80, 60))
    check("render.rounded 颜色", tuple(p3[40, 30][:3]) == (0x22, 0x24, 0x2C),
          "px=%s" % (p3[40, 30],))

    g = render.gradient((40, 60), "#AABBCC", "#112233", radius=8)
    pg = g.load()
    check("render.gradient 顶部色",
          abs(pg[20, 0][0] - 0xAA) <= 3 and abs(pg[20, 0][2] - 0xCC) <= 3,
          "px=%s" % (pg[20, 0],))
    check("render.gradient 底部色",
          abs(pg[20, 59][0] - 0x11) <= 3 and abs(pg[20, 59][2] - 0x33) <= 3,
          "px=%s" % (pg[20, 59],))
    check("render.gradient 圆角透明", pg[0, 0][3] == 0, "alpha=%s" % pg[0, 0][3])

    # cover：内存造一张 100x50 纯红图
    src = Image.new("RGB", (100, 50), (200, 30, 30))
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    cov = render.cover(buf.getvalue(), 64, 12)
    pc = cov.load()
    check("render.cover 尺寸", cov.size == (64, 64))
    check("render.cover 中心色/角落", pc[32, 32][3] == 255 and
          abs(pc[32, 32][0] - 200) <= 3 and pc[0, 0][3] == 0,
          "center=%s" % (pc[32, 32],))
    covs = render.cover(buf.getvalue(), 64, 12, shadow=True)
    pad = max(4, 64 // 12)
    check("render.cover 阴影外扩尺寸", covs.size == (64 + 2 * pad,) * 2,
          "size=%s pad=%s" % (covs.size, pad))
    pcs = covs.load()
    check("render.cover 阴影版中心/角落",
          pcs[pad + 32, pad + 32][3] == 255 and pcs[0, 0][3] == 0)

    # photo LRU
    base = render.rounded((32, 32), 8, "#7C9BFF")
    ph1 = render.photo(base, key="btn:32:#7C9BFF:hover")
    ph2 = render.photo(base, key="btn:32:#7C9BFF:hover")
    check("render.photo 缓存命中", ph1 is ph2)
    ph_auto1 = render.photo(base)
    ph_auto2 = render.photo(base)
    check("render.photo 自动签名缓存命中", ph_auto1 is ph_auto2)
    render.clear_cache()
    ph3 = render.photo(base, key="btn:32:#7C9BFF:hover")
    check("render.clear_cache 后重建", ph3 is not ph1)

    # icons：Feather 字体随包分发，Pillow FreeType 栅格化，不碰系统图标字体
    from PIL import features as pil_features
    check("PIL FreeType 可用", pil_features.check("freetype2"))
    check("Feather 字体已随包分发", icons.font_path().is_file(),
          "path=%s" % icons.font_path())
    check("Feather 图标数量", len(icons.GLYPHS) >= 250,
          "count=%d" % len(icons.GLYPHS))

    empty = [n for n in icons.GLYPHS if not icons.glyph(n)]
    pua_bad = [n for n in icons.GLYPHS
               if not (0xE000 <= ord(icons.glyph(n)) <= 0xF8FF)]
    check("icons.glyph 全部非空", not empty, "empty=%s" % empty)
    check("icons.glyph 均为 PUA 码位", not pua_bad, "bad=%s" % pua_bad)
    check("icons.glyph 别名", icons.glyph("cancel") == icons.glyph("close")
          and icons.glyph("delete") == icons.glyph("trash")
          and icons.glyph("find") == icons.glyph("search"))
    check("icons.glyph 未知名回退空串", icons.glyph("no_such_icon") == "")

    bad_draw = []
    for n in icons.icon_names():
        for size in (16, 24, 32):
            im = icons.draw(n, size, "#FFFFFF")
            if (im.size != (size, size) or im.mode != "RGBA"
                    or im.getbbox() is None):
                bad_draw.append((n, size))
    check("icons.draw 全部图标出图且非空", not bad_draw, "bad=%s" % bad_draw[:12])
    big = icons.draw("play", 96, "#7C9BFF")
    check("icons.draw 大尺寸(2x)", big.size == (96, 96) and
          big.getbbox() is not None)
    check("icons.draw 未知名兜底", icons.draw("???", 24, "#FFF").getbbox()
          is not None)

    # 全项目不得再依赖 Segoe 图标字体 / 系统 emoji 作为 UI 图标
    pkg_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "vbhelper")
    seg_hits = []
    for dirpath, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            code = open(p, encoding="utf-8", errors="replace").read()
            if "Segoe Fluent Icons" in code or "Segoe MDL2 Assets" in code:
                seg_hits.append(os.path.relpath(p, pkg_dir))
    check("无 Segoe 图标字体依赖", not seg_hits, "hits=%s" % seg_hits)

    root.destroy()

    print()
    if FAILS:
        print("结果：失败 %d 项 -> %s" % (len(FAILS), FAILS))
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
