"""平台能力探测与路径约定。

只做判断，不含业务逻辑。各模块按这里的常量分支：Windows 走原生实现，
其它平台走降级实现，避免把 Win32 调用散落到业务代码里。

新增平台相关能力时，先在这里加常量，再在调用点写 ``if IS_WINDOWS`` 分支，
不要在函数内部临时判断 ``sys.platform``。
"""
import os
import sys

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

# 能力开关：调用方据此决定走原生还是降级路径
HAS_LAYERED_WINDOW = IS_WINDOWS   # UpdateLayeredWindow 逐像素透明
HAS_DWM = IS_WINDOWS              # DWM 圆角与标题栏着色
HAS_TRAY = IS_WINDOWS             # Shell_NotifyIcon
HAS_DPAPI = IS_WINDOWS            # CryptProtectData
HAS_UAC = IS_WINDOWS              # ShellExecute runas
HAS_MCI = IS_WINDOWS              # winmm 音频播放
HAS_WINREG = IS_WINDOWS           # 注册表
HAS_X11 = IS_LINUX


def config_dir() -> str:
    """配置目录。

    Windows 取 ``%APPDATA%\\VBHelper``；其它平台按 XDG 规范取
    ``$XDG_CONFIG_HOME/VBHelper``，缺省 ``~/.config/VBHelper``。
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "VBHelper")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "VBHelper")


def data_dir() -> str:
    """数据目录（数据库、缓存、截图）。

    Windows 取 ``%LOCALAPPDATA%\\VBHelper``；其它平台按 XDG 规范取
    ``$XDG_DATA_HOME/VBHelper``，缺省 ``~/.local/share/VBHelper``。
    """
    if IS_WINDOWS:
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA")
                or os.path.expanduser("~"))
        return os.path.join(base, "VBHelper")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "VBHelper")


def autostart_file() -> str:
    """Linux 开机自启的 .desktop 文件路径；Windows 不使用。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "vbhelper.desktop")


def ui_font_candidates() -> tuple:
    """按平台给出界面字体候选，顺序即优先级。"""
    if IS_WINDOWS:
        return ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
    if IS_MAC:
        return ("PingFang SC", "Hiragino Sans GB", "Helvetica Neue")
    return ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
            "Noto Sans", "DejaVu Sans")


def python_launcher() -> str:
    """开发模式下的解释器名：Windows 用 pythonw，其它平台用 python3。"""
    return "pythonw.exe" if IS_WINDOWS else "python3"
