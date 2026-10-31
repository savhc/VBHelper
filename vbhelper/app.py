"""应用主类：按启动顺序初始化 DPI、主题、运行期服务与功能注册表。

目录树、悬浮球、扫描、标记仍是 Mixin 组装；新增能力走 features/ 下的
Feature，经 AppContext 取依赖，不再扩展本类。
"""
from . import core
from . import platform as plat
from .context import AppContext, AppPaths, ensure_feature_flags
from .core import *
from .features.registry import FeatureRegistry
from .floating import FloatingMixin
from .meta import MetaMixin
from .scan import ScanMixin, ScanPool
from .store.cache import HttpCache
from .tray import TrayIcon
from .tree import TreeMixin
from .ui import icons as ui_icons
from .ui import scale, theme as ui_theme, widgets
from .ui.async_runner import TaskRunner
from .ui.http import HttpClient
from .ui.mainthread import MainThreadInvoker
from .ui.window import WindowManager

class VBHelper(FloatingMixin, MetaMixin, ScanMixin, TreeMixin):
    def __init__(self):
        set_dpi_aware()
        self.cfg = load_cfg()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        # 窗口图标用 PNG（Tk 只认 PNG/GIF），托盘用多尺寸 ICO
        self.assets = ui_icons.asset_dir()
        self._icon_photo = None
        try:
            icon_png = self.assets / "vbhelper_256.png"
            if icon_png.is_file():
                self._icon_photo = tk.PhotoImage(file=str(icon_png))
                self.root.iconphoto(True, self._icon_photo)
                # FeatureWindow 是后建的 Toplevel，从 root 上取同一份图片
                self.root._vb_icon = self._icon_photo
        except Exception:
            pass
        core.FONT = pick_font(self.root)
        # DPI 统一缩放 + 主题管理：球和后续新功能窗口都从这取 token
        scale.init(self.root)
        self.cfg.setdefault("theme", {})
        self.theme = ui_theme.ThemeManager(self.root, self.cfg)
        # 启动即用已保存主题的令牌覆盖 C_* 旧常量，面板/按钮颜色与主题一致
        self._sync_colors(*self.theme.current())

        # 复用运行时服务集中到 AppContext：新功能从中取依赖，不再往主类加 Mixin
        self.paths = AppPaths.default()
        self.paths.ensure()
        self.cache = HttpCache(self.paths.db_path)
        self.http = HttpClient(cache=self.cache)
        self.tasks = TaskRunner(self.root)
        self.windows = WindowManager(self.root, self.theme)
        # 存储底座（DB/DPAPI/审计）失败不拖垮启动：相关功能降级为不可用
        self.db = None
        self.secrets = None
        self.audit = None
        try:
            from .store.audit import AuditLog
            from .store.db import Database
            from .store.secrets import SecretStore
            self.db = Database(self.paths.db_path)
            self.db.migrate()
            self.secrets = SecretStore(self.paths.secrets_file)
            self.audit = AuditLog(self.db)
        except Exception as exc:
            core.dbg_log("存储底座初始化失败：%r" % (exc,))
        self.invoker = MainThreadInvoker(self.root)
        self.ctx = AppContext(
            app=self, root=self.root, cfg=self.cfg, paths=self.paths,
            theme=self.theme, tasks=self.tasks, http=self.http,
            cache=self.cache, windows=self.windows, db=self.db,
            secrets=self.secrets, audit=self.audit, invoker=self.invoker)
        self.features = FeatureRegistry(self.ctx)
        self.ctx.features = self.features
        ensure_feature_flags(self.cfg)
        self.features.load()
        # 新控件跟随主题；feature 窗口在切换主题时重绘
        widgets.set_theme(self.theme.current()[1])
        self.theme.subscribe(lambda preset, tokens: widgets.set_theme(tokens))
        self.theme.subscribe(
            lambda preset, tokens: self.windows.on_theme_changed())

        self.work = work_area()
        self.root_dir = self.cfg.get("last_dir") or None
        self.topmost = bool(self.cfg.get("topmost", True))
        self.locked = bool(self.cfg.get("locked", False))
        self._pre_lock_topmost = self.topmost
        if self.locked:
            self.topmost = True
        self.panel = None
        self.tree = None
        self.snap_side = None
        self.parked = False
        self.armed_at = 0.0
        self._drag = None
        self._click_row = None
        self._pending = None
        self._budget = DRILL_BUDGET
        self._loaded = 0
        self._root_depth = 0
        self._slide_job = None
        self._quit_dialog = None
        self.rz_grip = None
        self._dragging = False
        self._flash_job = None
        self.set = dict(DEFAULT_SETTINGS)
        self.set.update(self.cfg.get("settings") or {})
        core.DEBUG_LOG_ON = bool(self.set.get("debug_log"))   # 日志开关启动即生效
        self._preheat_cache = OrderedDict()     # path -> rows 预扫描缓存（LRU）
        self._preheat_lock = threading.Lock()
        self._inflight = {}                     # 正在后台读取的路径 -> 等结果的人
        self._scan_failed = {}                  # 超时/读失败的路径 -> 时间戳（预热退避）
        self._dir_mtime = {}                    # path -> 扫描时的目录 mtime（新鲜度判断）
        self._no_watch = set()                  # stat 太慢的盘符，跳过主线程 mtime 检查
        self._results = queue.Queue()           # 后台线程 -> 主线程的结果通道
        self._scan_pool = ScanPool(self._results)   # 有界扫描线程池
        self._auto_pending = set()              # 「全部展开」等渲染完要接力的节点
        self._dir_state = {}                    # iid -> idle/loading/rendering/loaded
        self._dir_has_files = {}                # iid -> 这一层有没有文件
        self._dir_kids = {}                     # iid -> 子文件夹 iid 列表
        self._drilled = set()                   # 本轮已经自动下钻过的节点，防重复遍历
        self._want_open = set()                 # 带展开意图的节点：读取完成后要撑开
        self._iid_by_path = {}                  # path -> iid（迟到结果补渲染用）
        self._git_status = {}                   # 相对路径(小写) -> Git 状态字符
        self._watch_cursor = 0                  # 变更侦测在 _dir_state 里的轮询位置
        self._root_prefix = ""                  # 根目录前缀（小写、带分隔符）
        self._preheat_job = None

        self._init_style()
        self.ball = self._build_ball()
        self._place_ball()
        self.root.after(POLL_MS, self._hover_poll)
        self.root.after(60, self._pump_results)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        # 系统托盘：后台线程收消息，回调用 after 切回主线程（Tk 不允许子线程碰 UI）
        self._tray = TrayIcon(
            tooltip=APP_NAME,
            icon_path=str(self.assets / "vbhelper.ico"),
            on_show=lambda: self.root.after(0, self._tray_show),
            on_exit=lambda: self.root.after(0, self._tray_exit),
        )
        self._tray.start()
        # 上次退出时仪表盘处于开启状态则恢复
        if (self.cfg.get("hud") or {}).get("enabled"):
            self.root.after(600, self._open_hud)
        # 提权重启回来的话，自动把网络工具打开
        if self.cfg.pop("open_nettools_after_elevate", False):
            save_cfg(self.cfg)
            self.root.after(700, self._open_nettools)

    # ---- 托盘回调（已在主线程） ----

    def _tray_show(self):
        """托盘左键/「显示主面板」：展开目录树面板或悬浮球。

        root 是永久隐藏的壳（悬浮球形态没有主窗口），绝不能 deiconify——
        否则会弹出一个带标题栏的空白小窗。
        """
        try:
            if self.panel is not None and self.panel.winfo_exists() \
                    and self.panel.winfo_ismapped():
                self.panel.deiconify()
                self.panel.lift()
                return
        except tk.TclError:
            pass
        expand = getattr(self, "_expand", None)
        if callable(expand):
            expand()        # 没目录时与点悬浮球一致：先选文件夹
            return
        try:
            self.ball.deiconify()
            self.ball.lift()
        except Exception:
            pass

    def _tray_exit(self):
        """托盘「退出」：直接走正常退出流程（含确认框）"""
        self._quit()

    def _tray_cleanup(self):
        """退出前移除托盘图标"""
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:
            pass

def _single_instance():
    """单实例检测，返回需要持有到进程退出的句柄。

    Windows 用命名 Mutex；其它平台对 ``$TMPDIR/vbhelper.lock`` 加 flock，
    进程退出由内核释放，不会留下需要清理的锁文件状态。
    """
    if plat.IS_WINDOWS:
        import ctypes
        # 必须用 use_last_error 版本：ctypes.windll 读 GetLastError 会拿到
        # 上一次调用的陈旧错误码（实测恒为 183），导致误判「已经在运行」。
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # argtypes 必须写全：漏掉 c_wchar_p 时字符串会按 ANSI 传进 W 版本，
        # 返回 0，被后面误判成「已经在运行」。
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel32.CreateMutexW(None, 0, "VBHelper_SingleInstance")
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return mutex

    import fcntl
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "vbhelper.lock")
    handle = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle

def _set_app_id():
    """登记进程级 AppUserModelID。

    Windows 任务栏按 AppUserModelID 分组；不登记时 python.exe 启动的窗口会被
    归到「Python」分组，固定项和跳转列表都跟着走。
    """
    if not plat.IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "VBHelper.Desktop.1")
    except Exception:
        pass


def main():
    _set_app_id()
    mutex = _single_instance()
    if mutex is None:
        try:
            import tkinter.messagebox as mb
            mb.showinfo(APP_NAME, "VBHelper 已经在运行了。")
        except Exception:
            pass
        return
    app = VBHelper()
    app.root.mainloop()
