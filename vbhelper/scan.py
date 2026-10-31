"""目录扫描线程池：固定工人数、交互请求优先、卡死任务不阻塞队列。

扫描结果按目录缓存，供目录树展开时直接命中。
"""
from . import core
from .core import *

class ScanPool:
    """目录扫描线程池：固定工人、交互优先、卡死不拖垮整池。

    以前是「每个目录新开一条线程 + Semaphore(4)」：自动下钻一次能排上千条
    线程堵在信号量上，用户点开的目录排在它们后面，表现就是一直「正在读取…」；
    线程数无上限时还可能直接 new 失败，把请求永久挂在 inflight 里。
    现在改成有界队列：优先级小的先跑（交互=0，预热=1），工人卡在 I/O 上超过
    阈值就补一个，保证后面的请求还能继续。
    """

    def __init__(self, results, workers=SCAN_WORKERS,
                 max_workers=SCAN_MAX_WORKERS, stall=SCAN_STALL_S):
        self._results = results
        self._q = queue.PriorityQueue()
        self._lock = threading.Lock()
        self._busy = {}          # 工人编号 -> (路径, 开始时间)
        self._workers = 0
        self._max = max_workers
        self._stall = stall
        self._seq = 0
        self._closed = False
        for _ in range(max(1, workers)):
            self._spawn()

    def submit(self, path, priority=1):
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            seq = self._seq
        self._q.put((priority, seq, path))

    def _spawn(self):
        with self._lock:
            if self._closed or self._workers >= self._max:
                return
            idx = self._workers
            self._workers += 1
        threading.Thread(target=self._work, args=(idx,), daemon=True,
                         name="vbscan%d" % idx).start()

    def _work(self, idx):
        while True:
            item = self._q.get()
            if item is None:
                return
            path = item[2]
            with self._lock:
                self._busy[idx] = (path, time.monotonic())
            t0 = time.monotonic()
            try:
                rows = scan_dir(path)
                ok = rows is not None     # None=这次没读成（见 core.scan_dir）
            except Exception:
                rows, ok = None, False
            dbg_log("scan %6s rows %6.2fs  %s"
                    % ("%d" % len(rows) if rows is not None else "fail",
                       time.monotonic() - t0, path))
            with self._lock:
                self._busy.pop(idx, None)
            self._results.put((path, rows, ok))

    def poll_stalled(self):
        """卡住的扫描给界面一个超时结果，并补一个工人继续处理队列。"""
        now = time.monotonic()
        stalled = []
        with self._lock:
            for idx, (path, t0) in list(self._busy.items()):
                if now - t0 >= self._stall:
                    del self._busy[idx]
                    stalled.append(path)
        if not stalled:
            return
        self._spawn()
        for path in stalled:
            dbg_log("scan stalled %ss  %s" % (self._stall, path))
            self._results.put((path, None, False))

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            n = self._workers
        for _ in range(n):
            self._q.put(None)

class ScanMixin:
    def _submit_scan(self, path, on_done=None, priority=1):
        """把一次目录读取排进后台队列。

        同一个目录可能被两拨人同时要（用户点展开 + 预热巡检），所以这里登记
        等结果：谁要都排进 waiter 队列，扫完一起通知。网络路径直接跳过，免得
        整个队列被一个断掉的共享盘挂死。
        """
        if path.startswith("\\\\") or path.startswith("//"):
            if on_done:
                self.root.after(0, on_done, [], False)
            return False
        with self._preheat_lock:
            waiters = self._inflight.get(path)
            if waiters is not None:
                if on_done:
                    waiters.append(on_done)
                return True                  # 已经在读了，结果出来会一并通知
            self._inflight[path] = [on_done] if on_done else []
        self._scan_pool.submit(path, priority)
        return True

    def _pump_results(self):
        """主线程定时取后台结果，这是唯一回到 UI 的通道。

        一轮只处理很短的时间片：渲染是分帧的，但如果一次灌 64 条结果，光每条
        的首帧叠加起来也能把界面按住半秒。
        """
        try:
            deadline = time.perf_counter() + 0.004
            n = 0
            while n < 8 and (n == 0 or time.perf_counter() < deadline):
                try:
                    path, rows, ok = self._results.get_nowait()
                except queue.Empty:
                    break
                n += 1
                try:
                    self._scan_done(path, rows, ok)
                except Exception:
                    pass
            try:
                self._scan_pool.poll_stalled()
            except Exception:
                pass
        finally:
            # 必须放 finally：这行一旦没执行，后台结果就再也没人取，整棵树都会僵死
            try:
                self.root.after(30, self._pump_results)
            except tk.TclError:
                pass

    def _scan_done(self, path, rows, ok):
        """后台读取完成，回到 UI 线程：先落缓存，再通知所有等这个目录的人"""
        with self._preheat_lock:
            waiters = self._inflight.pop(path, [])
            # 扫描结果全部保留：预热与按需扫描共用缓存，再次展开直接命中
            # 超时不缓存，留给下次展开重试。
            if ok and rows is not None:
                self._scan_failed.pop(path, None)
                self._preheat_cache[path] = rows
                self._preheat_cache.move_to_end(path)
                while len(self._preheat_cache) > PREHEAT_CACHE_MAX:
                    old, _ = self._preheat_cache.popitem(last=False)   # 只淘汰最久未用的
                    self._dir_mtime.pop(old, None)
                try:                 # 记下扫描时的 mtime，之后靠它判断缓存是否过期
                    self._dir_mtime[path] = os.stat(path).st_mtime
                except OSError:
                    self._dir_mtime.pop(path, None)
            elif not ok:
                self._scan_failed[path] = time.monotonic()    # 失败的目录不重复预热，避免反复超时
        for cb in waiters:
            if not cb:
                continue
            try:
                cb(rows, ok)
            except Exception:
                pass
        if not waiters and ok and rows is not None:
            self._late_render(path, rows)

    def _late_render(self, path, rows):
        """没有 waiter 的结果：多半是超时后迟到的扫描。

        超时当时占位行停在「读取超时」，工人其实还在扫；结果回来若没人认领，
        用户就只能重展开一次才看得到。这里按 path 找回节点直接补渲染——节点还
        开着就立刻见效，被用户折叠了也不强开，下次展开秒出。
        """
        iid = self._iid_by_path.get(path)
        if not iid:
            return
        try:
            if not self.tree.exists(iid) or self._dir_state.get(iid) != "idle":
                return
        except tk.TclError:
            return
        dbg_log("late result rendered  %s" % path)
        self._render_rows(iid, rows)

    def _dir_fresh(self, path):
        """目录自上次扫描后 mtime 没变过，缓存才算新鲜。

        Windows 上目录 mtime 只在直接子项增删/改名时变化，一次 stat 是微秒级。
        个别盘（断连的网络映射盘）stat 可能卡住主线程，第一次超过 50ms 就把该
        盘符拉黑，之后不再等它。
        """
        m = self._dir_mtime.get(path)
        if m is None:
            return False
        drive = os.path.splitdrive(path)[0].lower()
        if drive in self._no_watch:
            return True
        try:
            t0 = time.perf_counter()
            fresh = os.stat(path).st_mtime == m
            if time.perf_counter() - t0 > 0.05:
                self._no_watch.add(drive)
                dbg_log("slow stat, watch disabled on %s" % (drive or path))
            return fresh
        except OSError:
            return False

    def _preheat_task(self, path, depth=0):
        """预扫描：结果只进缓存，供下次点开秒用"""
        def done(rows, ok):
            if not ok or not rows or depth >= PREHEAT_DEPTH:
                return
            if all(r[4] for r in rows):     # 纯文件夹层继续往下探，顺着包名预热
                for r in rows[:PREHEAT_FANOUT]:
                    self._enqueue_preheat(r[3], depth + 1)

        self._submit_scan(path, done, priority=1)

    def _enqueue_preheat(self, path, depth=0):
        with self._preheat_lock:
            if path in self._preheat_cache or path in self._inflight:
                return
            t = self._scan_failed.get(path)
            if t is not None and time.monotonic() - t < SCAN_FAIL_BACKOFF:
                return
        self._preheat_task(path, depth)

    def _preheat_tick(self):
        """定期巡检：预热没读过的目录 + 抽查已展开目录有没有变更。

        以前这里按 tag 判类型，实际上每个节点都当目录处理；下钻一开就有几百个
        open 节点，预热反而成了主要 I/O 来源。现在只认状态表：读过的一律跳过。
        遍历仍然限量，否则大树会把主线程卡住。
        """
        try:
            if self.panel is not None and self.panel.winfo_ismapped():
                todo, stack, seen = [], list(self.tree.get_children("")), 0
                while stack and len(todo) < PREHEAT_BATCH and seen < 300:
                    iid = stack.pop()
                    seen += 1
                    if self._dir_state.get(iid) == "idle" and self.tree.item(iid, "open"):
                        vals = self._vals(iid)
                        if vals and len(vals) > 1 and vals[1] == "dir":
                            todo.append(vals[0])
                    stack.extend(self.tree.get_children(iid))
                for p in todo:
                    self._enqueue_preheat(p)
                self._watch_changes()
        except tk.TclError:
            pass
        self._preheat_job = self.root.after(PREHEAT_TICK_MS, self._preheat_tick)

    def _watch_changes(self):
        """变更侦测：轮询已展开目录的 mtime，磁盘变了就自动重扫。

        新文件/新目录不用等用户刷新——AI 工具刚生成的文件，最迟一个轮询周期
        就会自己出现在展开着的目录里。每轮只抽查 WATCH_BATCH 个，摊薄开销。
        """
        iids = list(self._dir_state)
        n = len(iids)
        if not n:
            return
        checked, k = 0, 0
        for k in range(n):
            iid = iids[(self._watch_cursor + k) % n]
            if self._dir_state.get(iid) != "loaded":
                continue
            try:
                if not self.tree.exists(iid) or not self.tree.item(iid, "open"):
                    continue
            except tk.TclError:
                continue
            vals = self._vals(iid)
            if not vals or vals[1] != "dir":
                continue
            if not self._dir_fresh(vals[0]):
                dbg_log("changed on disk, rescan  %s" % vals[0])
                self._ensure_loaded(iid)   # 内部会作废缓存并重新扫描渲染
            checked += 1
            if checked >= WATCH_BATCH:
                break
        self._watch_cursor = (self._watch_cursor + k + 1) % n

    def _scan_cached(self, path):
        """取预扫描结果：命中直接复用（缓存保留，刷新/重开同一目录才能秒开）。

        缓存有数量上限（PREHEAT_CACHE_MAX），超了按最久未用淘汰，不会整表清空。
        """
        with self._preheat_lock:
            rows = self._preheat_cache.get(path)
            if rows is not None:
                self._preheat_cache.move_to_end(path)
            return rows
