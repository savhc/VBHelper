# -*- coding: utf-8 -*-
"""依赖医生真实窗口截图（临时脚本）：python _deps_shot.py

用 _deps_check 的样例项目 + 固定版本表（不联网），扫完截四页图到 _shots/。
"""
import ctypes
import os
import shutil
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from PIL import Image, ImageGrab

import _deps_check as fixture
from vbhelper.features.deps import registry as registry_mod

SHOTS = os.path.join(HERE, "_shots")
u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def grab_window(win):
    """PrintWindow 直接抓窗口内容，不受遮挡影响。"""
    win.update()
    hwnd = u32.GetAncestor(ctypes.c_void_p(win.winfo_id()), 2)
    if not hwnd:
        hwnd = win.winfo_id()
    hwnd = int(hwnd)
    import ctypes.wintypes as wt
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

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
                    ("biHeight", ctypes.c_int), ("biPlanes", ctypes.c_ushort),
                    ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
                    ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_int),
                    ("biYPelsPerMeter", ctypes.c_int), ("biClrUsed", ctypes.c_uint),
                    ("biClrImportant", ctypes.c_uint)]

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight, hdr.biPlanes = w, -h, 1
    hdr.biBitCount, hdr.biCompression = 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(hdr), 0)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    if sum(img.convert("L").getdata()) / max(1, w * h) < 6:
        try:
            img = ImageGrab.grab(
                bbox=(rect.left, rect.top, rect.right, rect.bottom),
                all_screens=True)
        except Exception:
            pass
    gdi32.SelectObject(memdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    u32.ReleaseDC(ctypes.c_void_p(hwnd), hdc)
    print("     printwindow=%s" % ok)
    return img


def patch_registry() -> None:
    fake = fixture.FakeRegistry()
    registry_mod.RemoteRegistry.latest = (
        lambda self, eco, name: fake.latest(eco, name))
    registry_mod.RemoteRegistry.vulns = (
        lambda self, deps, limit=500: fake.vulns(deps, limit))


def pump(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.root.update()
        time.sleep(0.02)


def shot(app, win, name: str) -> None:
    # 截图要确定性：光标移开（避免 hover）、表格滚回顶部
    try:
        import ctypes
        ctypes.windll.user32.SetCursorPos(0, 0)
    except Exception:
        pass
    table = getattr(win, "_table", None)
    if table is not None and getattr(table, "_rows", None):
        table._scroll_to(0.0)
    pump(app, 0.6)
    win.lift()
    pump(app, 0.4)
    try:
        img = grab_window(win) if win.winfo_exists() else None
    except Exception as exc:
        print("shot 跳过 %s：%r" % (name, exc))
        return
    if img is None:
        print("shot 失败：%s" % name)
        return
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    img.save(path)
    print("shot -> %s (%dx%d)" % (path, img.width, img.height))


def main() -> int:
    patch_registry()
    tmp = tempfile.mkdtemp(prefix="depsshot_")
    project = fixture.build_project(tmp)

    from vbhelper.app import VBHelper
    from vbhelper.features.deps.window import DepsWindow
    app = VBHelper()
    app.root.update()
    win = DepsWindow(app.ctx, project)
    win.deiconify()
    win.lift()
    win.start_scan()
    deadline = time.time() + 60
    while time.time() < deadline:
        app.root.update()
        if not win.scanning:
            break
        time.sleep(0.05)
    print("scanning=%s rows=%d deps=%d" % (
        win.scanning, len(win._rows),
        len(win._result.deps) if win._result else -1))
    shot(app, win, "deps_1_index.png")
    win._show_page(1)
    shot(app, win, "deps_2_result.png")
    if win._rows:
        win._table._select(0)
        win._on_row_select(win._table.selection())
        shot(app, win, "deps_3_detail.png")
    win._show_page(2)
    shot(app, win, "deps_4_report.png")
    win.close()
    pump(app, 0.2)

    # 主面板 ☰ 菜单里的入口
    holder = {}
    original_popup = app._popup

    def capture(name):
        popup = original_popup(name)
        holder["w"] = popup
        return popup

    app._popup = capture
    try:
        app._menu_window()
        pump(app, 0.4)
        popup = holder.get("w")
        if popup is not None and popup.winfo_exists():
            shot(app, popup, "deps_0_menu.png")
            try:
                popup.destroy()
            except Exception:
                pass
    finally:
        app._popup = original_popup
    app.root.destroy()
    shutil.rmtree(tmp, ignore_errors=True)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
