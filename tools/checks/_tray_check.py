# -*- coding: utf-8 -*-
"""托盘自检：python _tray_check.py

验三件事：图标能加进托盘、窗口过程真能收到点击（消息派发生效）、stop 能干净退出。
不会弹右键菜单（那个是模态的，得人手点）。
"""
import ctypes
import os
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper import tray as T

fails, oks = [], []


def check(name, cond, detail=""):
    (oks if cond else fails).append(name)
    print(("  ok   " if cond else "  FAIL ") + "%-30s %s" % (name, detail))


def main():
    hits = {"show": 0, "exit": 0}
    t = T.TrayIcon(tooltip="VBHelper 自检",
                   on_show=lambda: hits.__setitem__("show", hits["show"] + 1),
                   on_exit=lambda: hits.__setitem__("exit", hits["exit"] + 1))
    t.start()
    check("托盘图标已添加", bool(t._alive and t._hwnd), "hwnd=%s" % t._hwnd)
    if not t._hwnd:
        print("\n图标没加上，后面的用例跳过")
        return 1

    # 直接往隐藏窗口投递托盘消息：能走到回调就说明 DispatchMessage 生效了
    u = ctypes.windll.user32
    u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_size_t, ctypes.c_ssize_t]
    u.PostMessageW(ctypes.c_void_p(t._hwnd), T.WM_TRAYICON, 0, T.WM_LBUTTONUP)
    time.sleep(0.4)
    check("左键点击触发 on_show", hits["show"] == 1, "hits=%d" % hits["show"])

    u.PostMessageW(ctypes.c_void_p(t._hwnd), T.WM_TRAYICON, 0, T.WM_LBUTTONDBLCLK)
    time.sleep(0.3)
    check("双击也触发 on_show", hits["show"] == 2, "hits=%d" % hits["show"])

    # 右键菜单：把 TrackPopupMenu 换成直接返回某个命令 ID 的假函数，
    # 这样能同时验菜单项文案和「点了退出之后回调有没有真被调」。
    labels = []

    def run_menu(return_cmd):
        labels.clear()
        orig_append = T._user32.AppendMenuW
        orig_track = T._user32.TrackPopupMenu
        orig_fg = T._user32.SetForegroundWindow
        orig_post = T._user32.PostMessageW
        T._user32.AppendMenuW = lambda menu, flags, cid, text: (
            labels.append((cid, text)), True)[1]
        T._user32.TrackPopupMenu = lambda *a, **k: (labels.append(("track", return_cmd)),
                                                    return_cmd)[1]
        T._user32.SetForegroundWindow = lambda hwnd: True
        T._user32.PostMessageW = lambda *a, **k: True
        try:
            t._popup_menu(t._hwnd)
        finally:
            T._user32.AppendMenuW = orig_append
            T._user32.TrackPopupMenu = orig_track
            T._user32.SetForegroundWindow = orig_fg
            T._user32.PostMessageW = orig_post

    run_menu(0)
    texts = [x[1] for x in labels if x[0] != "track"]
    check("右键菜单有「显示主面板」「退出」", "退出" in texts and
          "显示主面板" in texts, str(texts))
    check("退出项用的是 CMD_EXIT", any(cid == T.CMD_EXIT and txt == "退出"
                                       for cid, txt in labels if cid != "track"),
          str(labels))
    hits["exit"] = 0
    run_menu(T.CMD_EXIT)
    check("菜单点退出会触发 on_exit", hits["exit"] == 1, "hits=%d" % hits["exit"])
    hits["show"] = 0
    run_menu(T.CMD_SHOW)
    check("菜单点显示会触发 on_show", hits["show"] == 1, "hits=%d" % hits["show"])

    thread = t._thread
    t.stop()
    thread.join(timeout=3.0)
    check("stop 后消息循环退出", not thread.is_alive())

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
    for f in fails:
        print("  - " + f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
