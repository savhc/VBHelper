# -*- coding: utf-8 -*-
"""验证音乐播放器能否从源码正常打开（含窗口截图）。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper.app import VBHelper
from vbhelper import music

SHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_qa_music.png")
app = VBHelper()
win = None
try:
    app.root.update()
    t0 = time.time()
    win = music.open_window(app)
    for _ in range(40):
        app.root.update()
        time.sleep(0.1)
    print("窗口对象:", type(win).__name__)
    print("win 存活:", win.win.winfo_exists())
    print("标题:", win.win.title())
    print("几何:", win.win.winfo_geometry())
    print("可见:", win.win.winfo_viewable())
    print("打开耗时: %.2fs" % (time.time() - t0))
    print("曲库文件夹:", getattr(win, "folders", None))
    try:
        from PIL import ImageGrab
        x, y = win.win.winfo_rootx(), win.win.winfo_rooty()
        w, h = win.win.winfo_width(), win.win.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(SHOT)
        print("截图:", SHOT)
    except Exception as e:
        print("截图失败:", e)
finally:
    try:
        music.shutdown(app)
    except Exception as e:
        print("shutdown err:", e)
    try:
        app._tray_cleanup()
    except Exception:
        pass
    try:
        app.root.destroy()
    except Exception:
        pass
print("DONE")
