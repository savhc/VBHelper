"""全局常量、Win32 基础封装与配置读写。

包含 DPI 声明、工作区查询、圆角/圆形区域裁剪、剪贴板写入、
配置文件加载与迁移、开机启动注册表项。不依赖项目内其它模块。
"""

"""
VBHelper — 氛围编程目录树悬浮助手

用途：补足部分 AI 编程工具目录树弱的问题，用一个可常驻桌面的悬浮球，
按需展开成目录树面板，支持靠边吸附、拖动、右键菜单（查看属性/复制文字/完整路径）。

运行：python vbhelper.py
打包：pyinstaller --onefile --noconsole --name VBHelper vbhelper.py
"""

import ctypes

import ctypes.wintypes as wt

import json
import sys

import os

import queue

import subprocess

import threading

import time

from collections import OrderedDict, deque

from datetime import datetime

import tkinter as tk

from tkinter import filedialog, font as tkfont, messagebox, ttk

APP_NAME = "VBHelper"

BALL_SIZE = 54

PANEL_W, PANEL_H = 360, 540

HANDLE_W = 8           # 吸附后露在屏幕内的把手宽度

SNAP_MARGIN = 30       # 距屏幕边缘多少像素内触发吸附

SLIDE_STEP = 26        # 滑出动画每帧像素

POLL_MS = 120          # 悬停检测间隔

DRILL_DEPTH = 12       # 自动下钻最大层数

DRILL_BUDGET = 6000    # 单次自动下钻最多加载的节点数

EXPAND_BUDGET = 30000  # 「全部展开」的节点上限

POPULATE_LIMIT = 20000 # 单个目录一次性读入的条目上限

MAGIC = "#ff00fe"      # 透明色（球形窗口用）

MARK_COLORS = {
    "红": "#7a2020", "橙": "#7a4410", "黄": "#7a6a12",
    "绿": "#1d5c2c", "青": "#12595c", "蓝": "#1d3f7a",
    "紫": "#4b2080", "灰": "#3c3f46",
}

MARK_TAGS = {name: "mark%d" % i for i, name in enumerate(MARK_COLORS)}

# Git 状态 tag：只用前景色区分，不跟标记颜色抢背景
GIT_TAGS = {"M": "git_mod", "A": "git_add", "D": "git_del",
            "?": "git_new", "R": "git_ren"}
GIT_COLORS = {"git_mod": "#e5c07b", "git_add": "#98c379", "git_del": "#e06c75",
              "git_new": "#61afef", "git_ren": "#c678dd"}

META_NAME = ".vbhelper.json"  # 标记与注释跟着项目走，就存在项目根目录下

NOTE_ICON = "i"               # 右侧注释列显示的图标

DEFAULT_SETTINGS = {
    "drill": False,        # 无文件层自动下钻（关掉后一律手动展开）
    "root_expand": True,   # 选完目录后自动展开第一层
    "snap": True,          # 靠边吸附
    "hud_period": 2,       # 常驻仪表盘的刷新周期（秒）
    "debug_log": False,    # 诊断日志（排查目录树卡顿时打开）
    "auto_start": False,   # 开机自启动（写注册表 Run 键）
}

PREHEAT_TICK_MS = 1500    # 预热巡检间隔

PREHEAT_BATCH = 24        # 每轮最多入队的目录数

PREHEAT_CACHE_MAX = 600   # 缓存目录数上限，超出按最久未用淘汰

SCAN_WORKERS = 4          # 常驻扫描线程数

SCAN_MAX_WORKERS = 8      # 扫描线程最多补到几个（磁盘卡死时换人继续干活）

SCAN_STALL_S = 8.0        # 单次扫描超过该秒数判定为卡死，由 ScanPool 返回超时结果

SCAN_FAIL_BACKOFF = 60    # 超时的目录，预热在这么多秒内不再去碰

RENDER_FRAME_MS = 12      # 每帧最多花多少毫秒往树里塞行（after(1) 实际粒度 ~15ms，预算太小会空等）

DRILL_FANOUT = 8          # 自动下钻时单层最多跟进的子文件夹数（包名链通常 1~3 个）

PREHEAT_DEPTH = 1         # 预热往下探的层数（只够点开下一层就够）

PREHEAT_FANOUT = 4        # 每个纯文件夹层预热几个子目录

WATCH_BATCH = 24          # 每轮变更侦测抽查多少个已展开目录的 mtime

DEBUG_LOG_ON = False      # 运行时随设置里的 debug_log 开关改写

def debug_log_path():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
    return os.path.join(d, "debug.log")

def dbg_log(msg):
    """诊断日志：只在设置里打开后才落盘，失败一律吞掉（别让日志拖垮主功能）"""
    if not DEBUG_LOG_ON:
        return
    try:
        with open(debug_log_path(), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], msg))
    except Exception:
        pass

def scan_dir(path):
    """读取并排序目录内容：文件夹在前，同类按名称不区分大小写。

    返回 None 表示「这次没读成」（目录本身打不开、或扫到一半出错）；
    返回 [] 才是真·空目录。两者绝不能混：失败被当成空结果缓存住（连 mtime
    都记上），之后每次展开都命中缓存秒出一个空目录，连「正在读取…」和重试
    都没有——这正是「点开一直是空的」的元凶。
    """
    rows = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    is_dir = e.is_dir(follow_symlinks=True)
                except OSError:
                    is_dir = False
                rows.append((not is_dir, e.name.lower(), e.name, e.path, is_dir))
        rows.sort(key=lambda r: (r[0], r[1]))
    except OSError:
        return None          # 失败必须返回 None：空列表会被当作真空目录写入缓存
    return rows

C_BG = "#1b1d23"

C_PANEL = "#22242c"

C_BORDER = "#33363f"

C_TEXT = "#d9dce2"

C_MUTED = "#8b909c"

C_ACCENT = "#3e7bfa"

C_ACCENT_H = "#5f95ff"

C_SELECT = "#2f5fd0"

C_DIR = "#9ecbff"

C_HOVER = "#2b2e37"

FONT = "TkDefaultFont"

def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

def work_area():
    """返回工作区 (left, top, right, bottom)，自动排除任务栏"""
    try:
        r = wt.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        if r.right > r.left and r.bottom > r.top:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080

def cursor_pos():
    p = wt.POINT()
    try:
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y
    except Exception:
        return -1, -1

def round_region(hwnd, w, h, radius):
    """把窗口裁成圆角矩形，radius 为圆角半径"""
    try:
        hrgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        ctypes.windll.user32.SetWindowRgn(hwnd, hrgn, True)
    except Exception:
        pass

def circle_region(hwnd, w, h):
    """把窗口裁成正圆"""
    try:
        hrgn = ctypes.windll.gdi32.CreateEllipticRgn(0, 0, w + 1, h + 1)
        ctypes.windll.user32.SetWindowRgn(hwnd, hrgn, True)
    except Exception:
        pass

def set_clipboard(text):
    """用 Win32 写剪贴板，程序退出后内容依然保留"""
    try:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        if not u32.OpenClipboard(None):
            raise OSError("open clipboard")
        try:
            u32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            h = k32.GlobalAlloc(0x0042, len(data))
            p = k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(13, h)
        finally:
            u32.CloseClipboard()
        return True
    except Exception:
        return False

def human_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%d %s" % (n, u) if u == "B" else "%.1f %s" % (n, u)
        n /= 1024
    return "%.1f PB" % n

def fmt_time(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"

def pick_font(root):
    try:
        fams = set(tkfont.families(root))
        for f in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            if f in fams:
                return f
    except Exception:
        pass
    return "TkDefaultFont"

def load_cfg():
    try:
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
        with open(os.path.join(d, "config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        # 配置损坏：改名留底并提示，避免用户以为设置/标记"消失了"
        try:
            src = os.path.join(d, "config.json")
            bad = src + ".bad"
            os.replace(src, bad)
            import tkinter.messagebox as mb
            mb.showwarning(APP_NAME,
                           "配置文件损坏，已重置。\n坏文件保留在：%s" % bad)
        except Exception:
            pass
        return {}

def save_cfg(cfg):
    try:
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def display_name(path):
    n = os.path.basename(path.rstrip("\\/")) or path
    return n

# ---------------- 开机自启动（注册表 Run 键） ----------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "VBHelper"

def _app_command():
    """返回启动命令：打包后用 exe 路径，开发时用 pythonw + 入口脚本"""
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    script = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "run.py")
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = "pythonw"
    return '"%s" "%s"' % (pyw, script)

def is_auto_start():
    """查注册表里有没有我们这一条"""
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                           winreg.KEY_READ)
        winreg.QueryValueEx(k, _RUN_NAME)
        winreg.CloseKey(k)
        return True
    except OSError:
        return False

def set_auto_start(enable):
    """写/删注册表 Run 键。返回 (成功, 错误信息或空)"""
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                           winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(k, _RUN_NAME, 0, winreg.REG_SZ, _app_command())
        else:
            try:
                winreg.DeleteValue(k, _RUN_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(k)
        return True, ""
    except OSError as e:
        return False, str(e)
