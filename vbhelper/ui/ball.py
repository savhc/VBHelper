# -*- coding: utf-8 -*-
"""悬浮球窗口：优先用 Win32 分层窗口做逐像素透明，失败回退颜色键 + 正圆裁剪。

对外保持 Tk 窗口的鸭子类型：宿主拿 ``.win`` 当普通 Toplevel 用，
拖动/吸附/收起等既有逻辑不感知渲染方式。

Linux/其它平台没有 ``UpdateLayeredWindow``，直接走 canvas 回退路径：
窗口整体透明由 WM 合成器决定，球体本身仍是绘制的圆形。
"""
import ctypes
import tkinter as tk

try:
    import ctypes.wintypes as wt
except ImportError:          # 非 Windows 平台没有 wintypes
    wt = None

from PIL import Image, ImageDraw

from . import icons, scale
from .. import core
from .. import platform as plat

# ---- Win32 常量 ----
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
BI_RGB = 0
DIB_RGB_COLORS = 0

class _Blend(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


if plat.IS_WINDOWS:
    _u32 = ctypes.windll.user32
    _g32 = ctypes.windll.gdi32

    # 64 位下句柄是 64 位，不显式声明 argtypes 会被 ctypes 截成 32 位 int
    _u32.GetDC.argtypes = [wt.HWND]
    _u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _u32.SetWindowLongPtrW.restype = ctypes.c_longlong
    _u32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_longlong]
    _u32.GetWindowLongPtrW.restype = ctypes.c_longlong
    _u32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    _u32.UpdateLayeredWindow.argtypes = [
        wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
        wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
        ctypes.POINTER(_Blend), wt.DWORD]
    _u32.UpdateLayeredWindow.restype = wt.BOOL
    _g32.CreateCompatibleDC.argtypes = [wt.HDC]
    _g32.CreateCompatibleDC.restype = wt.HDC
    _g32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
    _g32.SelectObject.restype = wt.HANDLE
    _g32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                      ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    _g32.CreateDIBSection.restype = wt.HANDLE
    _g32.DeleteObject.argtypes = [wt.HANDLE]
    _g32.DeleteDC.argtypes = [wt.HDC]
else:
    _u32 = None
    _g32 = None


def _set_layered_style(hwnd):
    """给窗口加 WS_EX_LAYERED|WS_EX_TOOLWINDOW，失败返回 False"""
    try:
        old = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        _u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                               old | WS_EX_LAYERED | WS_EX_TOOLWINDOW)
        return True
    except Exception:
        return False


def _premultiplied_bgra(img):
    """RGBA -> 预乘 BGRA 字节串（UpdateLayeredWindow 要求预乘 alpha）"""
    buf = bytearray(img.convert("RGBA").tobytes())
    for i in range(0, len(buf), 4):
        a = buf[i + 3]
        if a != 255:
            buf[i] = buf[i] * a // 255
            buf[i + 1] = buf[i + 1] * a // 255
            buf[i + 2] = buf[i + 2] * a // 255
        buf[i], buf[i + 2] = buf[i + 2], buf[i]      # RGBA -> BGRA
    return bytes(buf)


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def _update_layered(hwnd, img):
    """把 PIL RGBA 图送上 layered window 表面，成功返回 True"""
    try:
        w, h = img.size
        data = _premultiplied_bgra(img)
        hdc_win = _u32.GetDC(None)
        memdc = _g32.CreateCompatibleDC(hdc_win)
        bmi = _BitmapInfoHeader(40, w, -h, 1, 32, BI_RGB, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbmp = _g32.CreateDIBSection(hdc_win, ctypes.byref(bmi), DIB_RGB_COLORS,
                                     ctypes.byref(bits), None, 0)
        if not hbmp or not bits.value:
            return False
        _g32.SelectObject(memdc, hbmp)
        ctypes.memmove(bits.value, data, len(data))
        size = wt.SIZE(w, h)
        pt = wt.POINT(0, 0)
        blend = _Blend(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        ok = _u32.UpdateLayeredWindow(hwnd, None, None, ctypes.byref(size),
                                      memdc, ctypes.byref(pt), 0,
                                      ctypes.byref(blend), ULW_ALPHA)
        _g32.DeleteObject(hbmp)
        _g32.DeleteDC(memdc)
        _u32.ReleaseDC(None, hdc_win)
        return bool(ok)
    except Exception:
        return False


# ---- 球体绘制（PIL 超采样，不依赖字体插件） ----

def _supercircle(size, fill_top, fill_bottom, highlight, border, state):
    """4x 超采样画渐变圆 + 顶部高光 + 内描边，state 影响 hover 亮度和 press 缩放"""
    f = 4
    S = size * f
    if state == "hover":
        fill_top = _lighten(fill_top, 0.06)
        fill_bottom = _lighten(fill_bottom, 0.06)
    shrink = int(S * 0.02) if state == "press" else 0
    box = (shrink, shrink, S - shrink, S - shrink)

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 垂直渐变：逐行插值填充圆内区域
    c1, c2 = _rgb(fill_top), _rgb(fill_bottom)
    for y in range(box[1], box[3]):
        t = (y - box[1]) / max(1, box[3] - box[1])
        col = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)) + (255,)
        d.line([(box[0], y), (box[2], y)], fill=col)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    img.putalpha(mask)

    ov = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    # 顶部高光：上半圆的柔亮椭圆
    hl = _rgb(highlight)
    hx0, hx1 = box[0] + S // 8, box[2] - S // 8
    od.ellipse((hx0, box[1] + S // 12, hx1, box[1] + S // 3),
               fill=hl + (46,))
    od.ellipse((box[0] + 2, box[1] + 2, box[2] - 2, box[3] - 2),
               outline=_rgb(border) + (120,), width=f)
    img = Image.alpha_composite(img, ov)
    return img.resize((size, size), Image.LANCZOS)


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lighten(hex_color, ratio):
    r, g, b = _rgb(hex_color)
    return "#%02x%02x%02x" % (min(255, int(r + (255 - r) * ratio)),
                              min(255, int(g + (255 - g) * ratio)),
                              min(255, int(b + (255 - b) * ratio)))


class BallWindow:
    """悬浮球：layered 优先、颜色键回退，宿主直接拿 .win 当 Toplevel 用。

    用法：
        ball = BallWindow(root, theme, size=54)
        ball.bind_events(press=..., motion=..., release=..., context=...)
        ball.show(); ball.set_state("hover"); ball.destroy()
    """

    def __init__(self, master, theme, *, size=None, magic=None,
                 force_fallback=False):
        self.theme = theme
        self.logical = int(size or core.BALL_SIZE)
        self.magic = magic or core.MAGIC
        self.state = "idle"
        self.label = None            # glyph 名或 None（显示几何图标）
        self._imgs = {}              # (state, size) -> PIL Image

        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.withdraw()
        self.px = scale.s(self.logical)
        self.win.geometry("%dx%d+20000+20000" % (self.px, self.px))
        try:
            self.win.wm_attributes("-topmost", True)
        except Exception:
            pass

        self._layered = False
        self.canvas = None
        self._tk_img = None
        self._fallback_item = None
        self.win.update_idletasks()
        if not force_fallback:
            self._try_layered()

        if not self._layered:
            # 回退：颜色键透明 + 正圆裁剪（硬边但形状正确）
            try:
                self.win.wm_attributes("-transparentcolor", self.magic)
            except Exception:
                pass
            self.canvas = tk.Canvas(self.win, width=self.px, height=self.px,
                                    bg=self.magic, highlightthickness=0, bd=0,
                                    cursor="hand2")
            self.canvas.pack()
            self._fallback_item = None
            core.circle_region(self.win.winfo_id(), self.px, self.px)
            self._apply()

    # ---- 事件 ----

    def bind_events(self, press=None, motion=None, release=None, context=None,
                    enter=None, leave=None):
        """把 Tk 事件绑定到球窗口上（回退模式下绑在 Canvas，layered 下绑在 Toplevel）"""
        target = self.canvas if self.canvas is not None else self.win
        pairs = [("<Button-1>", press), ("<B1-Motion>", motion),
                 ("<ButtonRelease-1>", release), ("<Button-3>", context),
                 ("<Enter>", enter), ("<Leave>", leave)]
        for seq, cb in pairs:
            if cb is not None:
                target.bind(seq, cb)
        if enter is not None:
            self.win.bind("<Enter>", enter)
        if leave is not None:
            self.win.bind("<Leave>", leave)

    # ---- 渲染 ----

    def _render(self, state):
        key = (state, self.px)
        if key in self._imgs:
            return self._imgs[key]
        preset, _ = self.theme.current()
        top, bottom, hl = preset.ball
        img = _supercircle(self.px, top, bottom, hl,
                           preset.tokens.accent, state)
        if self.label:
            ic = icons.draw(self.label, self.px // 3, "#FFFFFF").resize(
                (self.px // 3, self.px // 3))
            base = img.copy()
            base.alpha_composite(ic, ((self.px - ic.width) // 2,
                                      (self.px - ic.height) // 2))
            img = base
        self._imgs[key] = img
        return img

    def _apply(self):
        img = self._render(self.state)
        if self._layered:
            if not _update_layered(self.win.winfo_id(), img):
                self._layered = False
            else:
                return
        if self.canvas is not None:
            from PIL import ImageTk
            flat = Image.new("RGBA", img.size, _rgb(self.magic) + (255,))
            flat.alpha_composite(img)
            self._tk_img = ImageTk.PhotoImage(flat.convert("RGB"))
            if self._fallback_item is None:
                self._fallback_item = self.canvas.create_image(
                    self.px // 2, self.px // 2, image=self._tk_img)
            else:
                self.canvas.itemconfig(self._fallback_item, image=self._tk_img)

    def _try_layered(self):
        if not plat.HAS_LAYERED_WINDOW:
            return
        hwnd = self.win.winfo_id()
        if not _set_layered_style(hwnd):
            return
        # UpdateLayeredWindow 需要窗口已映射，先在屏外临时显示再试
        self.win.deiconify()
        self.win.update_idletasks()
        try:
            if _update_layered(hwnd, self._render("idle")):
                self._layered = True
        finally:
            if not self._layered:
                self.win.withdraw()

    # ---- 对外 API ----

    def set_theme(self, preset=None):
        self._imgs.clear()
        self._apply()

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        self._apply()

    def set_label(self, glyph_name=None):
        self.label = glyph_name
        self._imgs.clear()
        self._apply()

    def set_position(self, x, y):
        self.win.geometry("+%d+%d" % (int(x), int(y)))

    def show(self):
        self.win.deiconify()
        self.win.lift()
        if self._layered:
            # deiconify 会让系统重发绘制，layered 表面要重新送一次
            self._apply()

    def hide(self):
        self.win.withdraw()

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass

    # ---- Tk 窗口鸭子类型转发（宿主把它当 Toplevel 用） ----

    def geometry(self, *a):
        return self.win.geometry(*a)

    def winfo_x(self):
        return self.win.winfo_x()

    def winfo_y(self):
        return self.win.winfo_y()

    def winfo_width(self):
        return self.win.winfo_width()

    def winfo_height(self):
        return self.win.winfo_height()

    def winfo_id(self):
        return self.win.winfo_id()

    def winfo_exists(self):
        return self.win.winfo_exists()

    def withdraw(self):
        self.win.withdraw()

    def deiconify(self):
        self.show()

    def lift(self):
        self.win.lift()

    def wm_attributes(self, *a):
        return self.win.wm_attributes(*a)
