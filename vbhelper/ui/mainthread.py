# -*- coding: utf-8 -*-
"""主线程调度：后台线程把回调投递到 Tk 主线程执行。

MCP 的 HTTP 服务端跑在独立线程，但确认弹窗、Toast 等必须回主线程。
注意：Tk 的 after 只能在主线程调用，所以这里用队列 + 主线程轮询。

用法::

    inv = MainThreadInvoker(root)
    ok = inv.call(lambda: ask_user(), timeout=30)   # 任意线程，阻塞取结果
    inv.post(lambda: toast("done"))                 # 不关心结果
    inv.stop()                                      # 退出前调用
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable


class InvokerClosed(RuntimeError):
    """调度器已停止（窗口/应用正在退出）。"""


class MainThreadInvoker:
    """把任意线程的回调排到 Tk 主线程；``call`` 可等待返回值。"""

    def __init__(self, root: Any, interval_ms: int = 30) -> None:
        self.root = root
        self._interval = max(10, int(interval_ms))
        self._q: "queue.Queue[tuple[Callable[[], Any], Any]]" = queue.Queue()
        self._closed = False
        self._job = None
        self._lock = threading.Lock()
        self._schedule()

    # ---------------- 公共 API ----------------

    @property
    def closed(self) -> bool:
        return self._closed

    def call(self, fn: Callable[[], Any], timeout: float = 30.0) -> Any:
        """在主线程执行 fn 并返回结果；超时抛 TimeoutError。

        当前线程就是主线程时直接执行，避免自己等自己。
        """
        if self._closed:
            raise InvokerClosed("主线程调度器已停止")
        if threading.current_thread() is threading.main_thread():
            return fn()
        fut = _Future()
        self._q.put((fn, fut))
        if not fut.event.wait(max(0.01, float(timeout))):
            raise TimeoutError("等待主线程回调超时（%.1fs）" % timeout)
        if fut.error is not None:
            raise fut.error
        return fut.result

    def post(self, fn: Callable[[], Any]) -> None:
        """投递回调，不等待结果。"""
        if self._closed:
            return
        self._q.put((fn, None))

    def stop(self) -> None:
        """停止轮询并唤醒所有等待者（幂等）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self._job is not None and self.root is not None:
                self.root.after_cancel(self._job)
        except Exception:
            pass
        self._job = None
        while True:
            try:
                _fn, fut = self._q.get_nowait()
            except queue.Empty:
                break
            if fut is not None:
                fut.set_error(InvokerClosed("主线程调度器已停止"))
        # 轮询循环里已取走但还没执行的，由 _poll 自己检查 closed

    # ---------------- 主线程轮询 ----------------

    def _schedule(self) -> None:
        if self._closed or self.root is None:
            return
        try:
            self._job = self.root.after(self._interval, self._poll)
        except Exception:
            self._closed = True

    def _poll(self) -> None:
        self._job = None
        if self._closed:
            return
        deadline = time.monotonic() + 0.05      # 每轮最多占主线程 50ms
        for _ in range(64):
            try:
                fn, fut = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                result = fn()
            except BaseException as exc:        # 回调异常不能杀轮询
                if fut is not None:
                    fut.set_error(exc)
                else:
                    self._log("回调异常：%r" % (exc,))
            else:
                if fut is not None:
                    fut.set_result(result)
            if time.monotonic() >= deadline:
                break
        self._schedule()

    @staticmethod
    def _log(msg: str) -> None:
        try:
            from ..core import dbg_log
            dbg_log(msg)
        except Exception:
            try:
                import sys
                sys.stderr.write(msg + "\n")
            except Exception:
                pass


class _Future:
    """轻量一次性 future（不引入 concurrent.futures 的线程池语义）。"""

    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None

    def set_result(self, value: Any) -> None:
        self.result = value
        self.event.set()

    def set_error(self, exc: BaseException) -> None:
        self.error = exc
        self.event.set()
