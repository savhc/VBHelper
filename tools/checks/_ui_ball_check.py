# -*- coding: utf-8 -*-
"""BallWindow 检查：layered 路线是否生效、回退是否可用、状态切换是否正常"""
import tkinter as tk

root = tk.Tk()
root.withdraw()

from vbhelper.ui import scale, theme
from vbhelper.ui.ball import BallWindow

scale.init(root)
tm = theme.ThemeManager(root, {})

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


bw = BallWindow(root, tm)
bw.win.update_idletasks()
check("layered 模式生效（本机支持逐像素透明）", bw._layered)

img = bw._render("idle")
check("渲染尺寸=物理像素 %d" % bw.px, img.size == (bw.px, bw.px))
check("idle 帧四角透明", all(img.getpixel(p)[3] == 0 for p in [(0, 0), (bw.px - 1, 0), (0, bw.px - 1), (bw.px - 1, bw.px - 1)]))
c = img.getpixel((bw.px // 2, bw.px // 2))
check("中心不透明", c[3] == 255)

bw.set_state("hover")
check("hover 帧独立缓存", ("hover", bw.px) in bw._imgs)
bw.set_label("folder")
check("带图标帧重新渲染", ("hover", bw.px) in bw._imgs)

bw.set_position(100, 100)
bw.show()
bw.win.update_idletasks()
check("show 后位置生效", bw.winfo_x() == 100 and bw.winfo_y() == 100)

# 回退路径：构造时就强制走颜色键路线
bw2 = BallWindow(root, tm, force_fallback=True)
check("回退模式未启用 layered", not bw2._layered)
check("回退渲染挂上 Canvas", bw2._fallback_item is not None)
bw2.destroy()

bw.destroy()
root.destroy()
print()
print("结果：" + ("全部通过" if not fails else "失败 %d 项" % len(fails)))
raise SystemExit(1 if fails else 0)
