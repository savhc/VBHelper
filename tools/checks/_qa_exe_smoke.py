# -*- coding: utf-8 -*-
"""VBHelper EXE 冒烟测试（兼容 PyInstaller onefile 父子进程）。"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

import psutil

EXE = sys.argv[1] if len(sys.argv) > 1 else r"E:\VBHELPER\dist\VBHelper.exe"
EXE_NAME = os.path.basename(EXE)
APP = "VBHelper"
R = []


def ok(n, p, d=""):
    R.append((n, p, d))
    print("[%s] %s%s" % ("PASS" if p else "FAIL", n, ("  -> " + d) if d else ""))


def enum_windows():
    out = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(h, _):
        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        n = ctypes.windll.user32.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(h, b, n + 1)
        c = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(h, c, 256)
        r = wt.RECT()
        ctypes.windll.user32.GetWindowRect(h, ctypes.byref(r))
        out.append(dict(hwnd=h, pid=pid.value, title=b.value, cls=c.value,
                        visible=bool(ctypes.windll.user32.IsWindowVisible(h)),
                        rect=(r.left, r.top, r.right, r.bottom)))
        return True

    fn = proto(cb)
    ctypes.windll.user32.EnumWindows(fn, 0)
    return out


def pids():
    s = set()
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() == EXE_NAME.lower():
                s.add(p.pid)
        except Exception:
            pass
    return s


def wins_of(ps):
    return [w for w in enum_windows() if w["pid"] in ps]


def is_ball(w):
    """悬浮球：可见、接近正方形、尺寸小于面板（DIP 缩放下 54 逻辑像素约 36 物理像素）。"""
    ww = w["rect"][2] - w["rect"][0]
    hh = w["rect"][3] - w["rect"][1]
    return (w["visible"] and 20 <= ww <= 120 and 20 <= hh <= 120
            and abs(ww - hh) <= 8)


def wait_for(fn, t, step=0.4):
    t0 = time.time()
    while time.time() - t0 < t:
        if fn():
            return True, time.time() - t0
        time.sleep(step)
    return False, time.time() - t0


def main():
    print("=" * 66)
    print("目标: %s" % EXE)
    if not os.path.exists(EXE):
        ok("产物存在", False, "文件不存在")
        return summary()
    size = os.path.getsize(EXE)
    ok("产物存在", True, "%.2f MB, %s" % (
        size / 1048576.0,
        time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(EXE)))))

    subprocess.run(["taskkill", "/IM", EXE_NAME, "/F", "/T"], capture_output=True)
    time.sleep(1.2)
    ok("测试前环境干净", not pids(), "残留: %s" % (pids() or "无"))

    cfg = os.path.join(os.environ.get("APPDATA", ""), APP, "config.json")

    # 冷启动
    t0 = time.time()
    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
    boot = proc.pid
    hit, cost = wait_for(lambda: bool(pids() - {boot}), 10)
    allp = pids()
    ok("onefile 子进程拉起", hit, "父 %d 子 %s, %.1fs" % (
        boot, sorted(allp - {boot}) or "无", cost))

    # 注意：Tk 初始化早期会先出现 TkChild(1x1) 内部窗口，不能拿它当就绪信号，
    # 必须等真正的悬浮球（TkTopLevel + 可见 + 约 54px）出现。
    hit, cost = wait_for(lambda: any(is_ball(w) for w in wins_of(pids())), 25)
    ws = wins_of(pids())
    tk = [w for w in ws if w["cls"].startswith("Tk")]
    ok("创建 Tk 窗口", bool(tk), "%d 个, 等待 %.1fs: %s" % (len(tk), cost, [
        "%s|%s|%dx%d" % (w["cls"], "vis" if w["visible"] else "hid",
                         w["rect"][2] - w["rect"][0], w["rect"][3] - w["rect"][1])
        for w in tk[:3]]))

    balls = [w for w in ws if is_ball(w)]
    ok("悬浮球已渲染", bool(balls), "可见 54x54 窗口 %d 个, 位置 %s" % (
        len(balls), [(w["rect"][0], w["rect"][1]) for w in balls]))

    if balls:
        b = balls[0]["rect"]
        pad = 20
        try:
            from PIL import ImageGrab
            p = os.path.join(os.path.dirname(EXE), "_qa_ball.png")
            ImageGrab.grab(bbox=(max(0, b[0] - pad), max(0, b[1] - pad),
                                 b[2] + pad, b[3] + pad)).save(p)
            ok("悬浮球截图留证", True, p)
        except Exception as e:
            ok("悬浮球截图留证", False, str(e))

    time.sleep(2.5)
    ok("启动后仍存活", bool(pids()), "运行 %.1fs" % (time.time() - t0))

    # 单实例
    base = pids()
    base_balls = [w["hwnd"] for w in wins_of(base) if is_ball(w)]
    p2 = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
    time.sleep(3.0)
    now = pids()
    new_balls = [w for w in wins_of(now) if is_ball(w) and w["hwnd"] not in base_balls]
    ok("单实例：未产生第二个悬浮球", not new_balls, "新增球 %d 个" % len(new_balls))
    # messagebox 是 Win32 #32770 对话框，不是 Tk 类，只按标题 + 可见 + 非球判断
    dlg = [w for w in wins_of(now)
           if w["visible"] and w["title"] == APP and not is_ball(w)]
    ok("单实例：提示框出现", bool(dlg), "标题 %s" % ([w["title"] for w in dlg] or "无"))
    subprocess.run(["taskkill", "/IM", EXE_NAME, "/F", "/T"], capture_output=True)
    time.sleep(1.5)

    # 配置
    ok("配置落盘", os.path.exists(cfg), cfg)
    try:
        with open(cfg, encoding="utf-8") as f:
            c = json.load(f)
        ok("配置可解析", True, "键: %s" % ", ".join(list(c)[:8]))
    except Exception as e:
        ok("配置可解析", False, str(e))

    ok("进程已清理", not pids(), "残留: %s" % (pids() or "无"))
    return summary()


def summary():
    print("=" * 66)
    bad = [r for r in R if not r[1]]
    print("结果: %d/%d 通过" % (len(R) - len(bad), len(R)))
    for n, _, d in bad:
        print("  FAIL  %s  %s" % (n, d))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
