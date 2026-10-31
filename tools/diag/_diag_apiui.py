# -*- coding: utf-8 -*-
"""临时：起 API 工作台窗口并截图，诊断 UI。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tkinter as tk
from vbhelper import core
from vbhelper.ui import scale, widgets
from vbhelper.ui.theme import ThemeManager
from vbhelper.context import AppPaths
from vbhelper.ui.async_runner import TaskRunner
from vbhelper.ui.http import HttpClient
from vbhelper.ui.window import WindowManager
from vbhelper.features.apiwork.window import ApiWindow


class Ctx:
    pass


root = tk.Tk()
root.withdraw()
core.FONT = "Microsoft YaHei UI"
scale.init(root)
cfg = {"theme": {}}
theme = ThemeManager(root, cfg)
paths = AppPaths.default()
paths.ensure()
tasks = TaskRunner(root)
http = HttpClient()
from vbhelper.store.cache import HttpCache
cache = HttpCache()
windows = WindowManager(root, theme)
ctx = Ctx()
ctx.root = root
ctx.theme = theme
ctx.cfg = cfg
ctx.paths = paths
ctx.tasks = tasks
ctx.http = http
ctx.cache = cache
ctx.windows = windows
ctx.app = None
ctx.feature_id = "api_workbench"

# 造两条示例数据让列表有内容
from vbhelper.features.apiwork import store
ctx._data_path = os.path.join(paths.config_dir, "api_workbench.json")
data = store.load(ctx._data_path)
if not data.get("providers"):
    p1 = store.new_provider("中转站 A", "https://api.example.com",
                            store.dpapi_encrypt("sk-demo-abcdef123456"), "gpt-4o",
                            "主用")
    p1["status"] = "ok"
    p1["balance"] = "额度 $20.00；已用 $5.60"
    p2 = store.new_provider("本地 Ollama", "http://127.0.0.1:11434", "", "qwen2.5")
    p2["status"] = "bad"
    p2["status_text"] = "连接拒绝"
    data["providers"] = [p1, p2]
    data["active_id"] = p1["id"]
    store.save(ctx._data_path, data)

win = ApiWindow(ctx)
win.geometry("980x620+100+80")
root.update()
for _ in range(6):
    win.update()
    root.update()
    import time as _t
    _t.sleep(0.05)

from PIL import ImageGrab
import ctypes
u32 = ctypes.windll.user32
hwnd = u32.GetAncestor(ctypes.c_void_p(win.winfo_id()), 2)
u32.GetWindowRect.restype = ctypes.c_int
import ctypes.wintypes as wt
r = wt.RECT()
u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
os.makedirs("_shots", exist_ok=True)
out = os.path.join("_shots", "apiwork_ui.png")
img.save(out)
print("saved:", out, img.size)
win.destroy()
tasks.shutdown(wait=False)
root.destroy()
