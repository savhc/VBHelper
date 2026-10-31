# -*- coding: utf-8 -*-
"""RAG 真实窗口截图 + 菜单/目录树入口验证：python _rag_shot.py

生成临时项目（默认在系统临时目录），打开真实 RagWindow 自动索引，
截进度/完成两页到 _shots/rag_*.png，并验证：
- 功能面板含「项目 RAG / 建立索引」；
- 目录树右键只有文件夹有 RAG 项，文件没有。
"""
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from PIL import Image, ImageGrab

SHOTS = os.path.join(HERE, "_shots")
u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def grab_window(win):
    win.update()
    hwnd = u32.GetAncestor(ctypes.c_void_p(win.winfo_id()), 2)
    if not hwnd:
        hwnd = win.winfo_id()
    hwnd = int(hwnd)
    rect = wt.RECT()
    u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 2 or h <= 2:
        return None
    hdc = ctypes.c_void_p(u32.GetDC(ctypes.c_void_p(hwnd)))
    memdc = ctypes.c_void_p(gdi32.CreateCompatibleDC(hdc))
    bmp = ctypes.c_void_p(gdi32.CreateCompatibleBitmap(hdc, w, h))
    old = ctypes.c_void_p(gdi32.SelectObject(memdc, bmp))
    ok = u32.PrintWindow(ctypes.c_void_p(hwnd), memdc, 2)

    class HDR(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
                    ("biHeight", ctypes.c_int), ("biPlanes", ctypes.c_ushort),
                    ("biBitCount", ctypes.c_ushort),
                    ("biCompression", ctypes.c_uint),
                    ("biSizeImage", ctypes.c_uint),
                    ("biXPelsPerMeter", ctypes.c_int),
                    ("biYPelsPerMeter", ctypes.c_int),
                    ("biClrUsed", ctypes.c_uint),
                    ("biClrImportant", ctypes.c_uint)]

    hdr = HDR()
    hdr.biSize = ctypes.sizeof(HDR)
    hdr.biWidth, hdr.biHeight, hdr.biPlanes = w, -h, 1
    hdr.biBitCount, hdr.biCompression = 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(hdr), 0)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    if sum(img.convert("L").getdata()) / max(1, w * h) < 6:
        img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                             all_screens=True)
    gdi32.SelectObject(memdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    u32.ReleaseDC(ctypes.c_void_p(hwnd), hdc)
    return img


def pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.root.update()
        time.sleep(0.02)


def shot(win, name):
    try:
        ctypes.windll.user32.SetCursorPos(0, 0)
    except Exception:
        pass
    img = grab_window(win) if win is not None and win.winfo_exists() else None
    if img is None:
        print("shot 失败：%s" % name)
        return
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    img.save(path)
    print("shot -> %s (%dx%d)" % (path, img.width, img.height))


def labels_under(widget):
    out = []
    stack = list(widget.winfo_children())
    while stack:
        w = stack.pop()
        if isinstance(w, tk.Label):
            try:
                out.append(str(w.cget("text")))
            except Exception:
                pass
        stack.extend(w.winfo_children())
    return out


def make_project(root):
    os.makedirs(os.path.join(root, "src", "core"), exist_ok=True)
    os.makedirs(os.path.join(root, "docs"), exist_ok=True)
    for i in range(160):
        text = ("def feature_%d(value):\n"
                "    \"\"\"module %d 的示例函数。\"\"\"\n"
                "    return value + %d\n\n" % (i, i, i)) * 2
        with open(os.path.join(root, "src", "core", "mod_%03d.py" % i),
                  "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(os.path.join(root, "src", "main.py"), "w",
              encoding="utf-8") as fh:
        fh.write("from core import feature_0\n\n"
                 "def entry_marker():\n"
                 "    return 'rag demo entry'\n")
    with open(os.path.join(root, "docs", "guide.md"), "w",
              encoding="utf-8") as fh:
        fh.write("# 项目 RAG 指南\n\n"
                 "这是中文索引测试。\n\n## 用法\n\n"
                 "在 VBHelper 里选择文件夹建立索引。\n")


def tree_menu_labels(app, iid):
    """用真实菜单对象调用 _tree_menu，返回菜单项文字。"""
    menus = []

    def mk():
        menu = tk.Menu(app.root, tearoff=0)
        menus.append(menu)
        return menu

    original = app._mk_menu
    identify = app.tree.identify_row
    app._mk_menu = mk
    app.tree.identify_row = lambda y: iid
    try:
        app._tree_menu(SimpleNamespace(x=0, y=0, x_root=0, y_root=0))
        menu = menus[0] if menus else None
        labels = []
        if menu is not None:
            try:
                for i in range(menu.index("end") + 1):
                    try:
                        labels.append(str(menu.entrycget(i, "label")))
                    except Exception:
                        pass
            finally:
                try:
                    menu.unpost()
                    menu.destroy()
                except Exception:
                    pass
        return labels
    finally:
        app._mk_menu = original
        app.tree.identify_row = identify


def main():
    base = tempfile.mkdtemp(prefix="vbragshot_")
    project = os.path.join(base, "demo-project")
    make_project(project)
    os.environ["APPDATA"] = os.path.join(base, "appdata")
    os.environ["LOCALAPPDATA"] = os.path.join(base, "local")
    os.makedirs(os.environ["APPDATA"], exist_ok=True)
    os.makedirs(os.environ["LOCALAPPDATA"], exist_ok=True)

    from vbhelper.app import VBHelper
    from vbhelper.services.rag import engine

    app = VBHelper()
    app.root.update()
    app._ensure_panel()
    app.panel.deiconify()

    # 先验证目录树右键（只对文件夹显示）
    app._load(project)
    pump(app, 6.0)
    def walk(parent=""):
        for iid in app.tree.get_children(parent):
            yield iid
            yield from walk(iid)

    folder_iid = file_iid = None
    for iid in walk():
        vals = app.tree.item(iid, "values")
        if not vals or len(vals) < 3:
            continue
        path, kind = vals[1], vals[2]
        if kind == "dir" and os.path.basename(str(path)) == "src":
            folder_iid = iid
    if folder_iid:
        app._open_item(folder_iid)
        pump(app, 0.6)
    for iid in walk():
        vals = app.tree.item(iid, "values")
        if vals and len(vals) >= 3 and vals[2] == "file":
            file_iid = iid
            break
    if folder_iid:
        labels = tree_menu_labels(app, folder_iid)
        print("folder menu has RAG:", any("RAG" in t for t in labels))
    if file_iid:
        labels = tree_menu_labels(app, file_iid)
        print("file menu has RAG:", any("RAG" in t for t in labels))
    print("tree iids:", bool(folder_iid), bool(file_iid))

    app.panel.withdraw()
    feature = app.features.get("rag")
    print("RAG feature:", feature is not None)
    win = feature.open(project)
    pump(app, 0.5)
    shot(win, "rag_1_progress.png")

    deadline = time.time() + 90
    while time.time() < deadline and getattr(win, "_running", False):
        app.root.update()
        time.sleep(0.05)
    pump(app, 0.4)
    shot(win, "rag_2_done.png")
    stats = engine.project_stats(app.db, project)
    print("stats:", {k: stats.get(k) for k in
                     ("file_count", "chunk_count", "status")}
          if stats else None)
    print("running after wait:", getattr(win, "_running", None))

    # 256 线程：必须先弹警告；取消则回退，允许才保存
    from vbhelper.ui import widgets as _w
    calls = []
    real_confirm = _w.confirm
    old_threads = win._rag_cfg().get("threads")
    _w.confirm = lambda *a, **k: (calls.append(a), False)[1]
    try:
        win._on_threads("256")
    finally:
        _w.confirm = real_confirm
    print("threads 256 warning:", bool(calls),
          "cfg", win._rag_cfg().get("threads"),
          "old", old_threads, "seg", win._thread_seg.get())
    _w.confirm = lambda *a, **k: True
    try:
        win._on_threads("256")
    finally:
        _w.confirm = real_confirm
    print("threads 256 accepted:", win._rag_cfg().get("threads"),
          "seg", win._thread_seg.get())
    win._on_threads("自动")

    # 功能面板入口
    original = app._popup
    holder = {}

    def capture(name):
        w = original(name)
        holder["w"] = w
        return w

    app._popup = capture
    try:
        app._menu_window()
        pump(app, 0.4)
        popup = holder.get("w")
        if popup is not None and popup.winfo_exists():
            labels = labels_under(popup)
            print("menu has RAG:", any("项目 RAG" in t for t in labels))
            shot(popup, "rag_0_menu.png")
            popup.destroy()
    finally:
        app._popup = original

    try:
        win.close()
    except Exception:
        pass
    pump(app, 0.2)
    try:
        app.ctx.shutdown()
    except Exception:
        pass
    try:
        app.root.destroy()
    except Exception:
        pass
    shutil.rmtree(base, ignore_errors=True)
    print("done")


if __name__ == "__main__":
    sys.exit(main() or 0)
