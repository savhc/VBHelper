"""Feature 协议。

约定：
- ``mount()`` 只注册命令/热键/菜单，不建窗口；
- ``open()`` 懒创建窗口，单例由 WindowManager 保证；
- ``shutdown()`` 取消任务/关端口/停线程；
- 功能之间不互相 import，通过 ``ctx`` 暴露的服务和事件通信。
"""

class Feature:
    """功能基类。子类至少覆盖 id/title 并实现 open()。"""

    id = ""                 # 稳定标识，和 config.features 的键一致
    title = ""
    icon = ""               # Feather 图标名（ui/icons.draw 的键）
    group = "dev"           # dev / agent / windows / system
    default_enabled = False
    order = 100             # 菜单排序，小的在前

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def is_available(self) -> bool:
        """当前环境是否可用（外部组件缺失时返回 False，入口不显示）。"""
        return True

    def mount(self) -> None:
        """注册入口，不创建窗口。"""

    def open(self) -> None:
        """打开主窗口（懒创建）。"""
        raise NotImplementedError

    def shutdown(self) -> None:
        """退出前清理：取消任务、关端口、停线程。"""

    def on_theme_changed(self) -> None:
        """主题变化；窗口还开着的话在这里重绘。"""
