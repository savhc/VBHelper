# -*- coding: utf-8 -*-
"""仪表盘自检：菜单入口 / 读数 / 三态透明 / 锁定拖动 / 刷新周期 / 关闭 / 退出收尾

跑完会把配置里的 hud 段落还原（中途出错也还原），不影响你自己的设置。
"""
import os
import sys
import time
import tkinter as tk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper import hud
from vbhelper.app import VBHelper
from vbhelper.core import save_cfg

try:
    import psutil
except Exception:
    psutil = None


def pump(app, n=40):
    for _ in range(n):
        app.root.update()
        time.sleep(0.01)


def find(win, cls, text=None):
    stack, out = [win], []
    while stack:
        x = stack.pop()
        try:
            if isinstance(x, cls) and (text is None or str(x.cget("text")) == text):
                out.append(x)
            stack.extend(x.winfo_children())
        except tk.TclError:
            pass
    return out


def top(app, prefix):
    for w in app.root.winfo_children():
        if isinstance(w, tk.Toplevel):
            try:
                if w.title().startswith(prefix):
                    return w
            except tk.TclError:
                pass
    return None


class Ev:
    def __init__(self, x, y):
        self.x_root, self.y_root = x, y


app = VBHelper()
app.ball.withdraw()
backup = dict(app.cfg.get("hud") or {})
pump(app, 90)                        # 启动时 600ms 的「自动恢复常驻仪表盘」得等它先跑完
if hud.is_open(app):
    hud.close_hud(app)
    pump(app, 20)
    print("0 启动时自动恢复了上次的仪表盘")

try:
    # ---- 1 菜单入口 ----
    app._menu_window()
    pump(app)
    mw = top(app, "菜单")
    labels = [str(w.cget("text")) for w in find(mw, tk.Label)] if mw else []
    print("1 菜单里有仪表盘入口 :", any("常驻仪表盘" in t for t in labels),
          "|", [t for t in labels if "仪表盘" in t])
    if mw:
        mw.destroy()
    pump(app, 10)

    # ---- 2 打开与读数 ----
    app._toggle_hud()
    pump(app, 60)
    w = getattr(app, "_hud_win", None)
    assert w is not None, "仪表盘没打开"
    w.locked = False                 # 测试要从干净状态起步
    print("2 仪表盘已打开       :", bool(w.win.winfo_exists()), "|", w.win.geometry())
    print("2 采集线程在跑       :", bool(w.collector._thread and w.collector._thread.is_alive()))
    print("2 配置已记住常驻     :", bool((app.cfg.get("hud") or {}).get("enabled")))
    time.sleep(3.0)
    pump(app, 60)
    print("2 刚开 3 秒           :", "CPU %s / 内存 %s / 磁盘 %s / 网络 %s / GPU %s"
          % tuple(w.rows[i].val.cget("text") for i in range(5)),
          "| 温度区", [c.cget("text") for c in w.temp_box.winfo_children()
                       if isinstance(c, tk.Label)])
    w1 = w.win.winfo_width()
    t0 = time.time()
    while time.time() - t0 < 16:                    # 采集进程起得慢（本机 PowerShell 光启动就 2.6 秒）
        if w.collector.snapshot()["probed"] and w.collector.snapshot()["ready"]:
            break
        time.sleep(0.5)
        pump(app, 20)
    time.sleep(0.6)
    pump(app, 40)
    print("2 五路读数           :", "CPU %s / 内存 %s / 磁盘 %s / 网络 %s / GPU %s"
          % tuple(w.rows[i].val.cget("text") for i in range(5)))
    print("2 各行旁注           :", [w.rows[i].note.cget("text") for i in range(5)])
    print("2 温度区             :", [(c.cget("text") if isinstance(c, tk.Label)
                                    else [x.cget("text") for x in c.winfo_children()])
                                   for c in w.temp_box.winfo_children()])
    print("2 进度条已按比例画   :", w.row_cpu.canvas.coords(w.row_cpu.fill)[2] > 0)
    print("2 宽度不随数值变化   :", w.win.winfo_width() == w1, w1, "->", w.win.winfo_width())

    # ---- 3 三态透明 ----
    while w.style != 0:
        w.btn_style.event_generate("<Button-1>")
        pump(app, 30)
    for step in range(4):
        print("3 透明档位 %d：图标 %s 关闭按钮可见 %s 锁定按钮可见 %s 透明按钮可见 %s alpha=%s 键=%s 宽 %d"
              % (w.style, w.btn_style.cget("text"),
                 bool(w.btn_close.winfo_ismapped()), bool(w.btn_lock.winfo_ismapped()),
                 bool(w.btn_style.winfo_ismapped()), w.win.attributes("-alpha"),
                 w.win.attributes("-transparentcolor"), w.win.winfo_width()))
        if step < 3:
            w.btn_style.event_generate("<Button-1>")     # 走真实点击路径，不是直接调方法
            pump(app, 30)
    need = max(r.frame.winfo_reqwidth() for r in w.rows) + 4
    print("3 一路点回正常档     :", w.style == 0, "| 关闭按钮回来了", bool(w.btn_close.winfo_ismapped()),
          "| 宽度够放进度条", w.win.winfo_width() >= need, (w.win.winfo_width(), need))

    # ---- 4 锁定与拖动 ----
    pump(app, 20)
    x0, y0 = w.win.winfo_x(), w.win.winfo_y()
    w._press(Ev(x0 + 30, y0 + 10))
    w._motion(Ev(x0 + 120, y0 + 60))
    w._release(Ev(x0 + 120, y0 + 60))
    pump(app, 20)
    moved = (w.win.winfo_x(), w.win.winfo_y()) != (x0, y0)
    print("4 未锁定时能拖动     :", moved, (w.win.winfo_x(), w.win.winfo_y()), "原", (x0, y0))
    print("4 拖完位置已记住     :", (app.cfg.get("hud") or {}).get("pos"))
    w.btn_lock.event_generate("<Button-1>")
    pump(app, 20)
    x1, y1 = w.win.winfo_x(), w.win.winfo_y()
    w._press(Ev(x1 + 30, y1 + 10))
    w._motion(Ev(x1 + 200, y1 + 120))
    w._release(Ev(x1 + 200, y1 + 120))
    pump(app, 20)
    print("4 锁定后拖不动       :", (w.win.winfo_x(), w.win.winfo_y()) == (x1, y1),
          "| 状态", w.locked, "| 图标", w.btn_lock.cget("text"))
    w.btn_lock.event_generate("<Button-1>")
    pump(app, 20)
    print("4 解锁恢复           :", not w.locked)

    # ---- 5 设置里的刷新周期 ----
    app._settings_window()
    pump(app, 40)
    sw = top(app, "设置")
    spins = find(sw, tk.Spinbox) if sw else []
    print("5 设置窗有周期输入框 :", bool(spins), "当前值", spins[0].get() if spins else "-")
    if spins:
        spin = spins[0]
        spin.delete(0, "end")
        spin.insert(0, "5")
        spin.event_generate("<FocusOut>")
        pump(app, 30)
        print("5 改 5 秒立即生效    :", app.set.get("hud_period") == 5, "采集周期", w.collector._period)
        spin.focus_force()
        pump(app, 10)
        spin.delete(0, "end")
        spin.insert(0, "9")
        spin.event_generate("<Return>")
        pump(app, 30)
        print("5 回车同样生效       :", app.set.get("hud_period") == 9, "采集周期", w.collector._period)
        spin.delete(0, "end")
        spin.insert(0, "2")
        spin.event_generate("<FocusOut>")
        pump(app, 30)
        print("5 改回 2 秒          :", app.set.get("hud_period") == 2, "采集周期", w.collector._period)
    if sw:
        sw.destroy()
    pump(app, 10)

    # ---- 6 关闭按钮 ----
    pid = w.collector._proc.pid if w.collector._proc else None
    w.btn_close.event_generate("<Button-1>")
    pump(app, 40)
    gone = True
    if pid and psutil:
        gone = not psutil.pid_exists(pid)
        if not gone:
            time.sleep(0.8)
            gone = not psutil.pid_exists(pid)
    print("6 点关闭后窗口消失   :", not hud.is_open(app), "| 采集进程已回收", gone, "pid", pid)
    print("6 关掉后不再自启     :", not (app.cfg.get("hud") or {}).get("enabled"))

    # ---- 7 重新打开 + 退出收尾 ----
    app._toggle_hud()
    pump(app, 40)
    w2 = getattr(app, "_hud_win", None)
    print("7 菜单可再次打开     :", bool(w2 and w2.win.winfo_exists()))
finally:
    try:
        app._do_quit()
    except Exception:
        pass
    app2 = VBHelper()                       # 还原配置，别把测试状态留给用户
    app2.cfg["hud"] = backup or {}
    save_cfg(app2.cfg)
    app2.root.destroy()
    print("7 配置已还原         :", backup or "（原本就没有）")
    print("完成")
