# -*- coding: utf-8 -*-
"""窗口壳：FeatureWindow / WindowManager / DWM 外观（计划 01 §3.9）。

- 保留原生标题栏，用 DWM 上深色/圆角/标题栏颜色，全部静默回退；
- FeatureWindow 统一：内边距、头部（标题/副标题/右侧操作）、内容区、
  状态栏、ESC 关闭、几何记忆（写 ctx.cfg["win_geometry"][key]，不落盘）；
- WindowManager 保证同一 feature 单例。

ctx 是宿主对象（鸭子类型），至少提供 .root / .theme / .cfg。

用法示例::

    from vbhelper.ui import window
    wm = window.WindowManager(root, theme)
    def factory():
        return window.FeatureWindow(ctx, "net", "网络工具", (900, 640))
    win = wm.open("net", factory)
    win.set_status("就绪")
"""
import ctypes
import tkinter as tk
from typing import Any, Callable

from . import scale
from .theme import ThemeManager, ThemePreset, Tokens, build_preset

__all__ = ["FeatureWindow", "WindowManager", "apply_dark_mode",
           "enable_round_corners", "set_caption_colors", "round_window",
           "circle_window", "DWMWA_USE_IMMERSIVE_DARK_MODE",
           "DWMWA_WINDOW_CORNER_PREFERENCE", "DWMWA_CAPTION_COLOR",
           "DWMWA_TEXT_COLOR"]

# ---------------------------------------------------------------------------
# DWM（Windows 10/11，老系统全部静默失败）
# ---------------------------------------------------------------------------

DWMWA_USE_IMMERSIVE_DARK_MODE = 20       # 旧系统用 19
_DWMWA_DARK_MODE_FALLBACK = 19
DWMWA_WINDOW_CORNER_PREFERENCE = 33      # Win11 圆角
DWMWA_CAPTION_COLOR = 35                 # Win11 标题栏底色
DWMWA_TEXT_COLOR = 36                    # Win11 标题文字色
DWMWCP_ROUND = 2                         # 圆角


def _dwm_set_int(hwnd: int, attr: int, value: int) -> bool:
    """DwmSetWindowAttribute(int32)；失败返回 False，不抛错。"""
    try:
        v = ctypes.c_int(value)
        ok = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(hwnd), int(attr), ctypes.byref(v), ctypes.sizeof(v))
        return bool(ok == 0)
    except Exception:
        return False


def _dwm_set_uint(hwnd: int, attr: int, value: int) -> bool:
    """DwmSetWindowAttribute(uint32，COLORREF 等)。"""
    try:
        v = ctypes.c_uint(value)
        ok = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(hwnd), int(attr), ctypes.byref(v), ctypes.sizeof(v))
        return bool(ok == 0)
    except Exception:
        return False


def apply_dark_mode(hwnd: int, dark: bool = True) -> None:
    """开/关沉浸式深色标题栏；先试属性 20，失败回退 19。"""
    for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_DARK_MODE_FALLBACK):
        if _dwm_set_int(hwnd, attr, 1 if dark else 0):
            return


def enable_round_corners(hwnd: int) -> None:
    """Win11 圆角窗口；老系统静默忽略。"""
    _dwm_set_int(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)


def _colorref(hex_color: str) -> int:
    """'#RRGGBB' -> COLORREF (0x00BBGGRR)。"""
    h = hex_color.strip().lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def set_caption_colors(hwnd: int, caption: str, text: str) -> None:
    """设置标题栏底色与标题文字色（Win11）。"""
    _dwm_set_uint(hwnd, DWMWA_CAPTION_COLOR, _colorref(caption))
    _dwm_set_uint(hwnd, DWMWA_TEXT_COLOR, _colorref(text))


def round_window(win: tk.Misc, radius: int | None = None) -> None:
    """SetWindowRgn 圆角（core.round_region，radius*2 语义）；面板可用。"""
    try:
        from .. import core
        core.round_region(int(win.winfo_id()), win.winfo_width(),
                          win.winfo_height(),
                          radius if radius else scale.s(8))
    except Exception:
        pass


def circle_window(win: tk.Misc) -> None:
    """SetWindowRgn 正圆（core.circle_region）；悬浮球回退方案用。"""
    try:
        from .. import core
        core.circle_region(int(win.winfo_id()), win.winfo_width(),
                           win.winfo_height())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FeatureWindow
# ---------------------------------------------------------------------------

class FeatureWindow(tk.Toplevel):
    """功能窗口壳。

    ctx（宿主对象）至少有::

        ctx.root   # tk 主窗口
        ctx.theme  # vbhelper.ui.theme.ThemeManager
        ctx.cfg    # 应用配置 dict（几何记忆写 cfg["win_geometry"][key]）

    示例::

        win = FeatureWindow(ctx, "net", "网络工具", (900, 640))
        tk.Label(win.content(), text="hi").pack()
        win.show()
    """

    def __init__(self, ctx: Any, feature_id: str, title: str,
                 size: tuple[int, int] = (900, 640),
                 min_size: tuple[int, int] = (720, 480),
                 resizable: bool = True,
                 subtitle: str | None = None) -> None:
        self.ctx = ctx
        self.feature_id = feature_id
        self._geo_key: str | None = None
        tk.Toplevel.__init__(self, ctx.root)
        self.title(title)
        icon = getattr(ctx.root, "_vb_icon", None)
        if icon is not None:
            try:
                self.iconphoto(False, icon)
            except Exception:
                pass
        preset, tokens = self._theme_tokens()
        self._preset = preset
        self._tokens = tokens
        self._pad = scale.s(16)

        # 几何：优先恢复记忆值，否则按逻辑尺寸缩放并钳制到工作区
        stored = self._stored_geometry(feature_id)
        if stored:
            try:
                self.geometry(stored)
            except Exception:
                stored = None
        if not stored:
            w, h = scale.window_size(*size)
            try:
                w, h = scale.clamp_to_work_area(self, w, h)
            except Exception:
                pass
            self.geometry("%dx%d" % (w, h))
        try:
            self.minsize(scale.s(min_size[0]), scale.s(min_size[1]))
        except Exception:
            pass
        self.resizable(resizable, resizable)

        self._build(title, subtitle)
        self.bind("<Escape>", lambda _e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.apply_theme()

    # -- 主题 ----------------------------------------------------------

    def _theme_tokens(self) -> tuple[ThemePreset, Tokens]:
        tm = getattr(self.ctx, "theme", None)
        if isinstance(tm, ThemeManager):
            try:
                return tm.current()
            except Exception:
                pass
        preset = build_preset("midnight")
        return preset, preset.tokens

    # -- 布局 ----------------------------------------------------------

    def _build(self, title: str, subtitle: str | None) -> None:
        self._header = tk.Frame(self)
        self._header.pack(fill="x", padx=self._pad,
                          pady=(self._pad, scale.s(4)))
        left = tk.Frame(self._header)
        left.pack(side="left", fill="x", expand=True)
        self._title_lbl = tk.Label(left, text=title, anchor="w",
                                   font=scale.font(15, weight="bold"))
        self._title_lbl.pack(fill="x")
        self._subtitle_lbl = tk.Label(left, text=subtitle or "", anchor="w",
                                      font=scale.font(11))
        if subtitle:
            self._subtitle_lbl.pack(fill="x")
        self._actions = tk.Frame(self._header)
        self._actions.pack(side="right")

        self._sep_top = tk.Frame(self, height=1)
        self._sep_top.pack(fill="x", padx=self._pad)

        self._content = tk.Frame(self)
        self._content.pack(fill="both", expand=True,
                           padx=self._pad, pady=self._pad)

        self._sep_bottom = tk.Frame(self, height=1)
        self._sep_bottom.pack(fill="x", padx=self._pad)

        self._statusbar = tk.Frame(self)
        self._statusbar.pack(fill="x", padx=self._pad,
                             pady=(scale.s(6), scale.s(10)))
        self._status_lbl = tk.Label(self._statusbar, text="", anchor="w",
                                    font=scale.font(11))
        self._status_lbl.pack(fill="x")

    # -- 公共 ----------------------------------------------------------

    def content(self) -> tk.Frame:
        """内容区容器（业务控件装这里）。"""
        return self._content

    def actions(self) -> tk.Frame:
        """头部右侧操作区容器。"""
        return self._actions

    def set_subtitle(self, text: str) -> None:
        """设置/更新副标题（空串隐藏）。"""
        if text:
            self._subtitle_lbl.configure(text=text)
            self._subtitle_lbl.pack(fill="x")
        else:
            self._subtitle_lbl.pack_forget()

    def set_status(self, text: str) -> None:
        self._status_lbl.configure(text=text)

    def show(self) -> None:
        """显示并前置。"""
        self.deiconify()
        self.lift()
        try:
            self.focus_set()
        except Exception:
            pass

    def close(self) -> None:
        """关闭：记忆几何、取消该 feature 的后台任务、销毁。"""
        if self._geo_key:
            try:
                self.remember_geometry(self._geo_key)
            except Exception:
                pass
        runner = getattr(self.ctx, "runner", None)
        if runner is not None:
            try:
                runner.cancel_group(self.feature_id)
            except Exception:
                pass
        self.destroy()

    def remember_geometry(self, key: str) -> str:
        """记忆当前几何到 ctx.cfg["win_geometry"][key]；close 时自动再写。

        key 通常用 feature_id，重开时按 feature_id 恢复。
        只写 cfg dict，持久化由宿主负责。
        """
        self._geo_key = key
        geo = self.geometry()
        try:
            cfg = self.ctx.cfg
            wg = cfg.setdefault("win_geometry", {})
            wg[key] = geo
        except Exception:
            pass
        return geo

    def _stored_geometry(self, key: str) -> str | None:
        try:
            wg = self.ctx.cfg.get("win_geometry")
            if isinstance(wg, dict):
                geo = wg.get(key)
                if isinstance(geo, str) and "x" in geo:
                    return geo
        except Exception:
            pass
        return None

    def apply_theme(self) -> None:
        """按 ctx.theme 当前令牌重绘外壳（含 DWM 外观）。"""
        preset, t = self._theme_tokens()
        self._preset = preset
        self._tokens = t
        self.configure(bg=t.bg)
        self._header.configure(bg=t.bg)
        for child in self._header.winfo_children():
            try:
                child.configure(bg=t.bg)
            except Exception:
                pass
        self._title_lbl.configure(bg=t.bg, fg=t.text)
        self._subtitle_lbl.configure(bg=t.bg, fg=t.muted)
        self._content.configure(bg=t.bg)
        self._sep_top.configure(bg=t.border)
        self._sep_bottom.configure(bg=t.border)
        self._statusbar.configure(bg=t.bg)
        self._status_lbl.configure(bg=t.bg, fg=t.muted)
        self._propagate(self._content, t)
        # DWM 外观：深色、圆角、标题栏颜色（老系统静默失败）
        try:
            hwnd = int(self.winfo_id())
            apply_dark_mode(hwnd, getattr(preset, "dark", True))
            enable_round_corners(hwnd)
            set_caption_colors(hwnd, t.bg, t.text)
        except Exception:
            pass

    def _propagate(self, widget: tk.Misc, tokens: Tokens) -> None:
        """内容区里有 apply_theme 的控件跟随刷新（只调显式提供的控件）。"""
        for child in widget.winfo_children():
            fn = getattr(child, "apply_theme", None)
            if callable(fn) and not isinstance(child, (tk.Toplevel,)):
                try:
                    fn(tokens)
                except Exception:
                    pass
            self._propagate(child, tokens)


# ---------------------------------------------------------------------------
# WindowManager
# ---------------------------------------------------------------------------

class WindowManager:
    """feature 窗口单例管理。

    示例::

        wm = WindowManager(root, theme)
        win = wm.open("net", lambda: FeatureWindow(ctx, "net", "网络"))
        theme.subscribe(lambda p, t: wm.on_theme_changed())
    """

    def __init__(self, root: tk.Misc, theme: ThemeManager) -> None:
        self.root = root
        self.theme = theme
        self._wins: dict[str, FeatureWindow] = {}

    def open(self, feature_id: str,
             factory: Callable[[], FeatureWindow]) -> FeatureWindow:
        """打开（或前置已开的）feature 窗口；factory 返回新窗口。"""
        win = self._wins.get(feature_id)
        if win is not None and win.winfo_exists():
            win.show()
            return win
        win = factory()
        self._wins[feature_id] = win
        win.show()
        return win

    def close(self, feature_id: str) -> None:
        """关闭指定 feature 窗口。"""
        win = self._wins.pop(feature_id, None)
        if win is not None and win.winfo_exists():
            win.close()

    def close_all(self) -> None:
        for fid in list(self._wins):
            self.close(fid)

    def on_theme_changed(self) -> None:
        """主题变化回调：通知所有打开的窗口重绘。"""
        for win in list(self._wins.values()):
            if win.winfo_exists():
                try:
                    win.apply_theme()
                except Exception:
                    pass

    def get(self, feature_id: str) -> FeatureWindow | None:
        """当前打开的窗口（无则 None）。"""
        win = self._wins.get(feature_id)
        if win is not None and win.winfo_exists():
            return win
        return None
