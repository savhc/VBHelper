"""系统托盘入口。

Windows 用 ``Shell_NotifyIcon``（实现见 ``tray_win.py``）；其它平台暂时没有
托盘后端，返回一个只保持接口的空实现，调用方无需分支。
"""

from . import platform as plat

if plat.IS_WINDOWS:
    from .tray_win import TrayIcon
else:
    class TrayIcon:
        """无托盘后端的占位实现：接口一致，不做任何事。"""

        def __init__(self, tooltip="VBHelper", icon_path=None,
                     on_show=None, on_exit=None):
            self.tooltip = tooltip
            self.icon_path = icon_path
            self.on_show = on_show
            self.on_exit = on_exit

        def start(self) -> None:
            """没有托盘可注册，直接返回。"""

        def stop(self) -> None:
            """没有托盘可移除，直接返回。"""


__all__ = ["TrayIcon"]
