# -*- coding: utf-8 -*-
"""UI 异步运行时：TaskRunner 线程池 + 结果回主线程。

规则（架构文档 §5）：
- 只有主线程碰 Tk；后台线程只做纯计算/IO；
- 后台线程把结果放进 queue.Queue，用 root.after(30, poll) 切回主线程；
- 任务异常必须进 on_error，不许静默；
- 超时只标记结果（handle.error 置为 CancelledError），不强杀线程；
- 网络/磁盘/子进程分队列，防止单类慢任务饿死其他任务。

用法::

    import tkinter as tk
    from vbhelper.ui.async_runner import TaskRunner, CancelledError

    root = tk.Tk(); root.withdraw()
    runner = TaskRunner(root)

    def on_done(result): print("结果:", result)
    def on_error(err): print("出错:", err)

    runner.submit("scan", lambda: 1 + 1, group="doc",
                  on_done=on_done, on_error=on_error, timeout=10)
    # 主循环里结果会自动回调
    runner.shutdown()
"""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

POLL_MS = 30                 # 主线程轮询结果队列的间隔
DEFAULT_POOL_SIZE = 4        # 默认线程池大小

# 队列名 → 各自独立的线程池，互相不抢
QUEUE_KINDS = ("default", "net", "disk", "proc")


class CancelledError(Exception):
    """任务被取消/超时标记时挂到 handle.error 上的异常。"""


class TaskHandle:
    """一次提交的句柄：查询任务状态与结果。

    属性：cancelled（被取消或超时标记）、done（已结束）、
    result（正常结果，无则 None）、error（异常对象，无则 None）。
    只在主线程更新/读取。
    """

    __slots__ = ("key", "group", "cancelled", "done", "result", "error",
                 "created_at", "timeout")

    def __init__(self, key: str, group: str, timeout: float | None) -> None:
        self.key = key
        self.group = group
        self.cancelled = False
        self.done = False
        self.result: Any = None
        self.error: BaseException | None = None
        self.created_at = time.monotonic()
        self.timeout = timeout


class TaskRunner:
    """后台任务调度器：submit/cancel/cancel_group/shutdown。

    用法::

        runner = TaskRunner(root)
        runner.submit("k1", fetch_fn, group="net",
                      on_done=cb, on_error=err_cb)
        ...
        runner.cancel_group("net")
        runner.shutdown()
    """

    def __init__(self, root, pool_size: int = DEFAULT_POOL_SIZE,
                 poll_ms: int = POLL_MS) -> None:
        self._root = root
        self._poll_ms = poll_ms
        self._lock = threading.Lock()
        self._closed = False
        self._after_id: str | None = None
        # kind → 各自的线程池，分队列防饿死
        workers = max(1, pool_size // len(QUEUE_KINDS) + 1)
        self._pools: dict[str, ThreadPoolExecutor] = {
            kind: ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="vb-task-" + kind)
            for kind in QUEUE_KINDS
        }
        self._queue: "queue.Queue[tuple[TaskHandle, Any, BaseException | None]]" = queue.Queue()
        self._handles: dict[str, TaskHandle] = {}
        self._callbacks: dict[str, tuple[Callable | None, Callable | None]] = {}
        self._start_poll()

    # ---------------- 内部 ----------------

    def _start_poll(self) -> None:
        """启动 root.after 轮询循环（只在主线程调用）。"""
        try:
            self._after_id = self._root.after(self._poll_ms, self._poll)
        except Exception:
            self._after_id = None

    def _poll(self) -> None:
        """主线程：分发结果队列 + 检查超时；继续下一轮轮询。"""
        if self._closed:
            return
        try:
            self._check_timeouts()
            while True:
                try:
                    handle, result, error = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._dispatch(handle, result, error)
        finally:
            if not self._closed:
                self._start_poll()

    def _dispatch(self, handle: TaskHandle, result: Any,
                  error: BaseException | None) -> None:
        """主线程里执行 on_done / on_error，并更新 handle 状态。"""
        with self._lock:
            cb = self._callbacks.get(handle.key)
        handle.done = True
        if handle.cancelled:
            return              # 已取消/超时：结果作废，不回调
        if error is not None:
            handle.error = error
            if cb and cb[1]:
                try:
                    cb[1](error)
                except Exception:
                    pass        # 回调内部异常不能再往外抛崩掉主线程
        else:
            handle.result = result
            if cb and cb[0]:
                try:
                    cb[0](result)
                except Exception:
                    pass

    def _check_timeouts(self) -> None:
        """主线程里标记超时任务：置 cancelled + error，不强杀线程。"""
        now = time.monotonic()
        with self._lock:
            handles = list(self._handles.values())
        for h in handles:
            if (h.timeout is not None and not h.done and not h.cancelled
                    and now - h.created_at > h.timeout):
                h.cancelled = True
                h.error = CancelledError(
                    "任务 %s 超时（%.1fs）" % (h.key, h.timeout))
                h.done = True

    def _wrap(self, handle: TaskHandle,
              fn: Callable[[], Any]) -> Callable[[], None]:
        """包装任务函数：执行、捕获异常、结果入队（在后台线程跑）。"""
        def run() -> None:
            result: Any = None
            error: BaseException | None = None
            try:
                result = fn()
            except BaseException as exc:      # 任务异常必须进 on_error
                error = exc
            finally:
                self._queue.put((handle, result, error))
        return run

    # ---------------- 公共 API ----------------

    def submit(self, key: str, fn: Callable[[], Any], *,
               group: str = "",
               on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None,
               timeout: float | None = None,
               kind: str = "default") -> TaskHandle:
        """提交后台任务，立即返回 TaskHandle。

        - key 全局唯一，重复 key 覆盖旧记录；group 用于批量取消；
        - kind 取 "default"/"net"/"disk"/"proc"，对应独立线程池；
        - timeout 只标记不强杀；超时后 on_done/on_error 不再触发。
        """
        if self._closed:
            raise RuntimeError("TaskRunner 已 shutdown")
        with self._lock:
            self._callbacks[key] = (on_done, on_error)
            handle = TaskHandle(key, group, timeout)
            self._handles[key] = handle
        pool = self._pools.get(kind) or self._pools["default"]
        pool.submit(self._wrap(handle, fn))
        return handle

    def cancel(self, key: str) -> None:
        """取消单个任务：标记 cancelled，回调不再触发。"""
        with self._lock:
            handle = self._handles.get(key)
        if handle is not None and not handle.done:
            handle.cancelled = True
            handle.error = CancelledError("任务 %s 已取消" % key)

    def cancel_group(self, group: str) -> None:
        """取消一个 group 内所有未完成任务（窗口关闭时调用）。"""
        with self._lock:
            handles = list(self._handles.values())
        for h in handles:
            if h.group == group and not h.done:
                h.cancelled = True
                h.error = CancelledError("任务组 %s 已取消" % group)

    def shutdown(self, wait: bool = True) -> None:
        """停止轮询、取消所有未完任务、关闭全部线程池。"""
        with self._lock:
            self._closed = True
        # 取消 after 轮询
        if self._after_id is not None:
            try:
                self._root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        # 标记所有未完成任务已取消
        with self._lock:
            handles = list(self._handles.values())
        for h in handles:
            if not h.done:
                h.cancelled = True
                h.done = True
        # 排空结果队列，避免残留结果触发已失效的回调
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        # 关闭线程池
        for pool in self._pools.values():
            pool.shutdown(wait=wait)
        # 清空记录
        with self._lock:
            self._handles.clear()
            self._callbacks.clear()

    @property
    def alive_thread_count(self) -> int:
        """诊断用：当前还活着的池线程数（按线程名前缀统计）。"""
        return sum(1 for t in threading.enumerate()
                   if t.name.startswith("vb-task-"))
