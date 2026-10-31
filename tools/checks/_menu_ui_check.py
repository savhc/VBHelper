# -*- coding: utf-8 -*-
"""功能面板自检：python _menu_ui_check.py（会短暂建一个隐藏 Tk 窗口）。

覆盖：分组渲染、搜索关键词/标题/描述、清空、上下移动、回车/点击打开。
"""
import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper import menu_ui
from vbhelper.ui import scale, widgets
from vbhelper.ui.theme import build_preset

fails, oks = [], []


def check(name, cond, detail=""):
    (oks if cond else fails).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + ((" | " + str(detail)) if detail and not cond else ""))


class _Theme:
    def current(self):
        preset = build_preset("midnight")
        return preset, preset.tokens


class _App:
    def __init__(self, root):
        self.root = root
        self.theme = _Theme()
        self.opened = []

    def _fit_work_area(self, w, h):
        return w, h

    def _settings_window(self):
        self.opened.append("settings")


def main():
    root = tk.Tk()
    root.withdraw()
    scale.init(root)
    preset, tokens = _Theme().current()
    widgets.set_theme(tokens)
    app = _App(root)
    win = tk.Toplevel(root)
    win.title("菜单 - VBHelper")
    calls = []
    items = [
        menu_ui.MenuItem("依赖医生", "扫描项目依赖与漏洞",
                         lambda: calls.append("deps"), group="开发工具",
                         icon="package", keywords="依赖 版本 漏洞"),
        menu_ui.MenuItem("网络工具", "DNS / DoH / hosts",
                         lambda: calls.append("net"), group="网络",
                         icon="globe", keywords="dns doh hosts 网络"),
        menu_ui.MenuItem("设置", "偏好与主题",
                         lambda: calls.append("settings"), group="桌面与系统",
                         icon="gear", keywords="偏好 pref"),
    ]
    menu_ui.build(app, win, items)
    palette = getattr(win, "_menu_palette", None)
    check("面板实例存在", palette is not None)
    check("初始渲染 3 项", palette is not None
          and len(palette._rows) == 3 and len(palette._visible) == 3,
          getattr(palette, "_visible", None))
    check("默认选中第一项", palette._selected == 0, palette._selected)

    palette._on_query("dns")
    check("关键词筛选 dns", [i.title for i in palette._visible] == ["网络工具"],
          [i.title for i in palette._visible])
    palette._on_query("依赖")
    check("标题/关键词筛选 依赖", [i.title for i in palette._visible] == ["依赖医生"],
          [i.title for i in palette._visible])
    palette._on_query("不存在")
    check("无结果", len(palette._visible) == 0 and palette._selected == -1,
          (palette._visible, palette._selected))
    palette._on_query("")
    check("清空恢复 3 项", [i.title for i in palette._visible]
          == ["依赖医生", "网络工具", "设置"],
          [i.title for i in palette._visible])

    palette._select(1)
    check("点击/选择第二项", palette._selected == 1, palette._selected)
    palette._move(1)
    check("向下移动", palette._selected == 2, palette._selected)
    palette._move(-1)
    check("向上移动", palette._selected == 1, palette._selected)

    # 键盘事件走真实控件：搜索框聚焦时按 ↓ 再 Enter
    # 窗口映射后鼠标可能正悬在某一行上，<Enter> 会把选择改掉；聚焦后重新定位
    try:
        entry = palette.search._entry
        entry.focus_force()
        root.update()
        palette._select(0)
        root.update()
        entry.event_generate("<Down>")
        root.update()
        check("键盘 ↓ 选中下一项", palette._selected == 1, palette._selected)
        entry.event_generate("<Return>")
        for _ in range(10):
            root.update()
            time.sleep(0.02)
        check("键盘 Enter 打开", calls == ["net"], calls)
        check("打开后窗口销毁", not win.winfo_exists())
    except Exception as exc:
        check("键盘事件", False, repr(exc))

    root.destroy()
    print("\n%s" % ("=" * 52))
    print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
