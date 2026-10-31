# -*- coding: utf-8 -*-
"""MCP 面板真实截图：python _mcp_shot.py

构建真实 VBHelper（隐藏主壳），打开 MCP 面板与服务（仅内存，不落配置），
PrintWindow 截面板图到 _shots/mcp_1_panel.png，并截主面板菜单入口。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from PIL import Image, ImageGrab

SHOTS = os.path.join(HERE, "_shots")
u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def grab_window(win):
    """PrintWindow 抓窗口内容（不受遮挡影响），失败回退屏幕区域。"""
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

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
                    ("biHeight", ctypes.c_int), ("biPlanes", ctypes.c_ushort),
                    ("biBitCount", ctypes.c_ushort),
                    ("biCompression", ctypes.c_uint),
                    ("biSizeImage", ctypes.c_uint),
                    ("biXPelsPerMeter", ctypes.c_int),
                    ("biYPelsPerMeter", ctypes.c_int),
                    ("biClrUsed", ctypes.c_uint),
                    ("biClrImportant", ctypes.c_uint)]

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
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
    print("     printwindow=%s" % ok)
    return img


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.root.update()
        time.sleep(0.02)


def shot(app, win, name):
    try:
        ctypes.windll.user32.SetCursorPos(0, 0)
    except Exception:
        pass
    pump(app, 0.5)
    try:
        win.lift()
    except Exception:
        pass
    pump(app, 0.3)
    img = grab_window(win) if win.winfo_exists() else None
    if img is None:
        print("shot 失败：%s" % name)
        return
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    img.save(path)
    print("shot -> %s (%dx%d)" % (path, img.width, img.height))


def main():
    from vbhelper.app import VBHelper
    app = VBHelper()
    app.root.update()
    feature = app.features.get("mcp_bus")
    if feature is None:
        print("mcp_bus 未加载；检查 features.catalog / config.features.mcp_bus")
        app.root.destroy()
        return 1
    win = feature.open()
    pump(app, 0.8)
    try:
        info = feature.runtime.start(persist=False)
        print("server on 127.0.0.1:%s" % info.get("port"))
    except Exception as exc:
        print("服务启动跳过：%r" % (exc,))
    pump(app, 0.8)
    shot(app, win, "mcp_1_panel.png")
    for idx, name in ((1, "mcp_2_perm.png"), (2, "mcp_3_conn.png"),
                      (3, "mcp_4_audit.png")):
        try:
            win._show_page(idx)
        except Exception:
            continue
        shot(app, win, name)

    # 主面板 ☰ 菜单里的入口
    holder = {}
    original = app._popup

    def capture(name):
        w = original(name)
        holder["w"] = w
        return w

    app._popup = capture
    try:
        app._menu_window()
        pump(app, 0.5)
        popup = holder.get("w")
        if popup is not None and popup.winfo_exists():
            shot(app, popup, "mcp_0_menu.png")
            try:
                popup.destroy()
            except Exception:
                pass
    finally:
        app._popup = original

    try:
        feature.runtime.stop(persist=False)
    except Exception:
        pass
    try:
        win.close()
    except Exception:
        pass
    pump(app, 0.2)
    app.root.destroy()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
