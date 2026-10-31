"""管理员权限检测与提权重启。

Windows 走 UAC（``ShellExecuteW`` + ``runas``）；Linux 走 ``pkexec``，
需要 polkit 并对用户弹出认证框。调用方只需判断返回值，不必感知平台差异。
"""
import ctypes
import os
import subprocess
import sys

from . import platform as plat


def is_admin():
    """当前进程是否具备管理员/root 权限。"""
    if plat.IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def relaunch_as_admin(args=()):
    """以管理员身份重新启动自己，返回是否已拉起新进程。

    成功时调用方应退出当前实例。Linux 下 ``pkexec`` 不存在或用户取消认证时返回 False。
    """
    if not plat.IS_WINDOWS:
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable] + list(args)
            else:
                cmd = [sys.executable, os.path.abspath(sys.argv[0])] + list(args)
            subprocess.Popen(["pkexec"] + cmd)
            return True
        except Exception:
            return False

    try:
        exe = sys.executable
        if getattr(sys, "frozen", False):
            params = " ".join(args)
        else:
            params = '"%s"' % os.path.abspath(sys.argv[0])
            if args:
                params += " " + " ".join(args)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return int(rc) > 32
    except Exception:
        return False
