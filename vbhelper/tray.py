"""系统托盘图标：纯 ctypes 调 Win32 API，不引第三方依赖。

后台线程跑消息循环，主线程只管回调。右键菜单弹出后按返回值分发动作，
左键单击显示/隐藏主面板，双击同左键。
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading

# ---- Win32 常量 ----
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

WM_USER = 0x0400          # ctypes.wintypes 里没有这个常量，得自己给
WM_TRAYICON = WM_USER + 1
UINT_PTR = ctypes.c_size_t    # wintypes 也没导出 UINT_PTR，按指针宽度给
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_NULL = 0x0000

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_DEFAULTSIZE = 0x00000040

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
CW_USEDEFAULT = 0x80000000

# ---- 菜单命令 ID ----
CMD_SHOW = 1001
CMD_EXIT = 1002

class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uTimeout", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wt.HICON),
    ]

class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]

class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]

# ---- 缓存函数签名 ----
_shell32 = ctypes.windll.shell32
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
_shell32.Shell_NotifyIconW.restype = wt.BOOL
_user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]  # IDI_* 是整数资源号，不能用 LPCWSTR
_user32.LoadIconW.restype = wt.HICON

_user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wt.HWND, wt.HMENU,
                                     wt.HINSTANCE, wt.LPVOID]
_user32.CreateWindowExW.restype = wt.HWND
_user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.DefWindowProcW.restype = wt.LPARAM
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
_user32.RegisterClassW.restype = wt.ATOM
_user32.CreatePopupMenu.restype = wt.HMENU
_user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, UINT_PTR, wt.LPCWSTR]
_user32.AppendMenuW.restype = wt.BOOL
_user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wt.HWND, ctypes.c_void_p]
_user32.TrackPopupMenu.restype = wt.UINT
_user32.DestroyMenu.argtypes = [wt.HMENU]
_user32.DestroyMenu.restype = wt.BOOL
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.PostMessageW.restype = wt.BOOL
_user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.PostThreadMessageW.restype = wt.BOOL
_user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.TranslateMessage.argtypes = [ctypes.c_void_p]
_user32.TranslateMessage.restype = wt.BOOL
_user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
_user32.DispatchMessageW.restype = wt.LPARAM
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.SetForegroundWindow.restype = wt.BOOL
_user32.DestroyWindow.argtypes = [wt.HWND]
_user32.DestroyWindow.restype = wt.BOOL
_user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                                ctypes.c_int, wt.UINT]
_user32.LoadImageW.restype = wt.HICON
_shell32.ExtractIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, ctypes.c_int]
_shell32.ExtractIconW.restype = wt.HICON

class TrayIcon:
    """系统托盘图标。on_show / on_exit 是主线程回调，内部自动切回主线程执行。"""

    def __init__(self, tooltip="VBHelper", icon_path=None,
                 on_show=None, on_exit=None):
        self.tooltip = tooltip
        self.icon_path = icon_path
        self.on_show = on_show
        self.on_exit = on_exit
        self._thread = None
        self._nid = None
        self._hwnd = None
        self._ready = threading.Event()
        self._alive = False

    def start(self):
        """启动后台消息循环线程"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # 等托盘就绪（最多 3 秒），避免启动时立刻退出导致图标残留
        self._ready.wait(timeout=3.0)

    def stop(self):
        """移除托盘图标并退出消息循环（发 WM_CLOSE 让窗口自己走销毁流程）"""
        self._alive = False
        hwnd = self._hwnd
        if hwnd:
            try:
                _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    # ---- 后台线程 ----

    def _run(self):
        try:
            self._msg_loop()
        except Exception:
            pass

    def _msg_loop(self):
        cls_name = "VBHelperTrayClass"
        wc = WNDCLASS()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = ctypes.cast(
            self._make_wndproc(), ctypes.c_void_p).value
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.lpszClassName = cls_name
        _user32.RegisterClassW(ctypes.byref(wc))

        # 创建隐藏窗口（只用来收托盘消息）
        hwnd = _user32.CreateWindowExW(
            0, cls_name, "VBHelperTray", 0,
            0, 0, 0, 0,
            0, 0, wc.hInstance, None)
        if not hwnd:
            return
        self._hwnd = hwnd

        icon = self._load_icon()

        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = icon
        nid.szTip = self.tooltip
        try:
            if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                return
            self._nid = nid
            self._alive = True
        finally:
            self._ready.set()   # 注册失败也释放等待，避免 start() 空等 3 秒

        # 消息循环：必须 DispatchMessageW，否则窗口过程永远收不到托盘回调，
        # 图标能出现但左右键都没反应（之前就栽在这）。
        msg = ctypes.create_string_buffer(64)   # MSG 结构体（x64 下 48 字节）
        while self._alive:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:                        # 0 = WM_QUIT，-1 = 出错
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        _user32.DestroyWindow(hwnd)

    def _make_wndproc(self):
        """创建窗口过程回调（必须保持引用防止 GC）"""
        @ctypes.WINFUNCTYPE(wt.LPARAM, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAYICON:
                if lparam == WM_LBUTTONUP or lparam == WM_LBUTTONDBLCLK:
                    self._safe(self.on_show)
                elif lparam == WM_RBUTTONUP:
                    self._popup_menu(hwnd)
                return 0
            if msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = wndproc   # 防 GC
        return wndproc

    def _popup_menu(self, hwnd):
        """右键弹出菜单，返回值分发动作"""
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        _user32.AppendMenuW(menu, MF_STRING, CMD_SHOW, "显示主面板")
        _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "退出")
        pt = POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        # 弹菜单前必须把窗口提到前台，否则菜单不会失焦、点别处也不消失
        _user32.SetForegroundWindow(hwnd)
        cmd = _user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
            pt.x, pt.y, 0, hwnd, None)
        _user32.DestroyMenu(menu)
        _user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        if cmd == CMD_SHOW:
            self._safe(self.on_show)
        elif cmd == CMD_EXIT:
            self._safe(self.on_exit)

    def _load_icon(self):
        """优先用 .ico 文件，其次当前 exe 的图标，最后退回系统默认图标。

        取 exe 图标必须用 ExtractIconW：LoadImageW 不认带 .exe 的路径（它会当成资源名）。
        """
        if self.icon_path and os.path.exists(self.icon_path):
            try:
                h = _user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                       LR_DEFAULTSIZE | 0x00000010)  # LR_LOADFROMFILE
                if h:
                    return h
            except Exception:
                pass
        try:
            exe = os.path.abspath(__import__("sys").executable)
            if os.path.exists(exe):
                h = _shell32.ExtractIconW(None, exe, 0)
                if h:
                    return h
        except Exception:
            pass
        try:
            return _user32.LoadIconW(None, IDI_APPLICATION)
        except Exception:
            return 0

    @staticmethod
    def _safe(fn):
        """安全调用回调（忽略异常）"""
        if fn is None:
            return
        try:
            fn()
        except Exception:
            pass
