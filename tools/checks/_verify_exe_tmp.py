# -*- coding: utf-8 -*-
"""一次性验证打包结果（跑完即删）。

踩过的坑：onefile 的父进程只是个解压器，真正的程序跑在它自己拉起来的**子进程**里，
所以按父进程 PID 找窗口永远找不到 —— 必须按进程名（VBHelper.exe）匹配。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXE = os.path.join(HERE, "dist", "VBHelper.exe")
u32 = ctypes.windll.user32


def pids_of(name="VBHelper.exe"):
    """当前所有同名的进程 PID（父进程 + 它拉起来的子进程）"""
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh", "/fi", "imagename eq " + name],
                         capture_output=True, text=True, encoding="gbk",
                         errors="replace", creationflags=0x08000000).stdout
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().endswith(".exe"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


before = pids_of()
print("启动前已有的 VBHelper 进程:", before or "无")

sandbox = tempfile.mkdtemp(prefix="vb_exe_")
env = dict(os.environ)
env["APPDATA"] = sandbox                 # 不碰用户真实配置

proc = subprocess.Popen([EXE], env=env)
print("启动 exe，引导进程 PID =", proc.pid)

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
rows = []


def cb(hwnd, _lp):
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value in mine:
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, 512)
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        rc = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rc))
        rows.append((pid.value, cls.value, buf.value, u32.IsWindowVisible(hwnd),
                     (rc.right - rc.left, rc.bottom - rc.top)))
    return True


tk_win, waited = [], 0.0
while waited < 45:
    time.sleep(0.5)
    waited += 0.5
    if proc.poll() is not None:
        print("引导进程已退出，退出码 =", proc.returncode)
        break
    mine = pids_of() - before            # 本轮属于本次启动的全部进程
    del rows[:]
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    tk_win = [r for r in rows if r[1] == "TkTopLevel"]
    if tk_win:
        break

alive = proc.poll() is None
print("等待 %.1f 秒 → 进程存活: %s" % (waited, alive))

# 刚建出来时窗口还没布局完，尺寸会偏小；等稳定了再量一次
time.sleep(3)
mine = pids_of() - before
del rows[:]
u32.EnumWindows(WNDENUMPROC(cb), 0)
print("\n稳定 3 秒后重新测量:")
for pid_, cls, title, vis, size in rows:
    if cls == "TkTopLevel" and vis:
        print("   悬浮球（可见的那个）尺寸 = %sx%s  ← 代码里 BALL_SIZE=54"
              % (size[0], size[1]))
print("本次启动的进程:", sorted(pids_of() - before))
print("属于它、且是 Tk 的窗口 %d 个:" % len(tk_win))
for pid_, cls, title, vis, size in rows:
    mark = "  <<< Tk 窗口" if cls == "TkTopLevel" else ""
    print("   pid=%-6s class=%-12s visible=%s size=%-10s title=%r%s"
          % (pid_, cls, vis, size, title, mark))

ok = alive and bool(tk_win)
print("\n结论:", "exe 正常启动，Tk 窗口已创建（说明 tkinter 和新增模块在打包环境下全部加载成功）"
      if ok else "异常！没建出 Tk 窗口 —— noconsole 下启动异常是被吞掉的")

# 只收掉本次启动的进程（含 onefile 的子进程），别误杀用户自己开的
for pid_ in pids_of() - before:
    subprocess.run(["taskkill", "/PID", str(pid_), "/F", "/T"],
                   capture_output=True, creationflags=0x08000000)
time.sleep(1)
print("已结束测试进程；剩余 VBHelper 进程:", pids_of() or "无")
sys.exit(0 if ok else 1)
