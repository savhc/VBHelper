# -*- coding: utf-8 -*-
"""Canvas 滚动页的鼠标滚轮接线。

Tk 的 Treeview/Text/Listbox 自带 ``<MouseWheel>`` 类绑定，Canvas 没有。
Canvas 做的滚动页如果只 bind 在 canvas 自身，指针落在 inner 子控件上时
事件不会冒泡到 canvas（Tk 只给命中的控件发事件），表现就是"有滚动条但
滚轮没反应"。

这里把 ``<MouseWheel>`` 绑到 Toplevel：子控件的 bindtags 都会经过所属
Toplevel，所以指针在页面任何位置都能收到。命中的控件自己会滚
（Treeview/Text/Listbox）时跳过，避免页面和控件一起滚。
"""
import tkinter as tk

# 自带 <MouseWheel> 类绑定的控件；Canvas 不算（音乐里的按钮/滑杆也是 Canvas，
# 那些应该让页面滚，而不是被跳过）。
SELF_SCROLLING = frozenset({"Text", "Treeview", "Listbox"})


def _widget_class(widget) -> str:
    try:
        return widget.winfo_class()
    except tk.TclError:
        return ""


def _inside(widget, area) -> bool:
    """widget 是 area 本身或其后代（按 Tk 窗口路径判断）。"""
    try:
        name, base = str(widget), str(area)
    except Exception:
        return False
    return name == base or name.startswith(base + ".")


def bind_canvas_wheel(win, canvas, *, area=None, units: int = 3,
                      skip=SELF_SCROLLING):
    """让 canvas 滚动页在指针位于任意内容控件上时也能滚。

    - ``win``：页面所在 Toplevel（事件会经过它的 bindtag）；
    - ``area``：判定"指针在页面内"的容器，默认 canvas；同一窗口有多个
      滚动页时用它隔离；
    - ``units``：每格滚轮滚多少个 canvas 单位（有 yscrollincrement 时
      按它算，否则 Tk 默认约窗口高度的 1/10）。

    返回 bind id（可直接 ``win.unbind("<MouseWheel>", bid)`` 解除）。
    """
    area = area if area is not None else canvas

    def _wheel(event):
        widget = getattr(event, "widget", None)
        if widget is None or not _inside(widget, area):
            return None
        if widget is not canvas and _widget_class(widget) in skip:
            return None                    # 交给控件自己的类绑定
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return None
        step = -int(delta / 120)
        if step == 0:                      # 触摸板/驱动的小增量
            step = -1 if delta > 0 else 1
        try:
            canvas.yview_scroll(step * max(1, int(units)), "units")
        except tk.TclError:
            return None
        return "break"

    return win.bind("<MouseWheel>", _wheel, add="+")
