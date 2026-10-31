"""管理员权限检测与 UAC 提权重启"""
import ctypes
import os
import sys

def is_admin():
    """当前进程是否以管理员身份运行"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def relaunch_as_admin(args=()):
    """以管理员身份重新启动自己：打包后重启 exe，开发时重启 python + 脚本。

    返回 True 表示 UAC 通过并已拉起新进程（调用方应退出旧实例）。
    """
    try:
        exe = sys.executable
        if getattr(sys, "frozen", False):
            params = " ".join(args)
        else:
            # 开发模式：python.exe（或 pythonw.exe）+ 脚本路径
            params = '"%s"' % os.path.abspath(sys.argv[0])
            if args:
                params += " " + " ".join(args)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return int(rc) > 32          # >32 才是成功，其余是错误码
    except Exception:
        return False
