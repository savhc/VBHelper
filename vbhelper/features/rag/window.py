# -*- coding: utf-8 -*-
"""项目 RAG 窗口：选目录、静默多线程索引、进度、统计与 AI 用法。

索引任务用 TaskRunner（group=rag）跑在后台；文件读取/分块在 engine 的
线程池里并行，数据库写入由索引线程批量提交。窗口只轮询进度队列，
不碰索引线程的数据。
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

from ...services.rag import engine
from ...ui import scale, widgets
from ...ui.window import FeatureWindow

class _Bar(tk.Canvas):
    """4px 自绘进度线（不用 ttk.Progressbar，避免高饱和色块）。"""

    def __init__(self, parent, tokens, *, height: int = 4) -> None:
        self._tokens = tokens
        self._ratio = 0.0
        self._h = scale.s(height)
        tk.Canvas.__init__(self, parent, height=self._h, bd=0,
                           highlightthickness=0, bg=tokens.surface)
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, ratio: float) -> None:
        try:
            self._ratio = max(0.0, min(1.0, float(ratio)))
        except (TypeError, ValueError):
            self._ratio = 0.0
        self._draw()

    def _draw(self) -> None:
        t = self._tokens
        self.delete("all")
        w = max(1, self.winfo_width())
        h = self._h
        self.create_rectangle(0, h // 2 - 1, w, h // 2 + 1,
                              fill=t.border, outline="")
        if self._ratio > 0:
            self.create_rectangle(0, h // 2 - 1, int(w * self._ratio),
                                  h // 2 + 1, fill=t.accent, outline="")


class RagWindow(FeatureWindow):
    """项目 RAG 主窗口（单例，可切换项目）。"""

    def __init__(self, ctx, path: str) -> None:
        self._path = engine.norm_root(path)
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._running = False
        self._indexed = False
        self._last_event: dict = {}
        self._poll_job = None
        self._task_key = "rag:index:%d" % id(self)
        self._body_ready = False
        self._f = scale.font(11)
        self._f_small = scale.font(10)
        self._f_mono = scale.font(10, family="Consolas")
        super().__init__(
            ctx, "rag", "项目 RAG", (900, 640), min_size=(760, 540),
            subtitle="本地多线程索引 · 让 AI 只读相关文件，不整盘扫")
        self.remember_geometry("rag")
        self._build_body()
        self._body_ready = True
        self._load_stats()
        self.set_status("索引只写本机 SQLite · 跳过 .git/依赖/二进制/密钥 · "
                        "不执行代码、不上传")
        self.after(220, self._maybe_auto_start)

    # ------------------------------------------------------------------
    # 生命周期/主题
    # ------------------------------------------------------------------

    def close(self) -> None:
        shutting_down = bool(getattr(self.ctx, "_closed", False))
        if self._running:
            if not shutting_down and not widgets.confirm(
                    self, "索引正在进行",
                    "关闭窗口会停止本次索引，已写入的部分会保留。\n确定关闭？",
                    danger=True):
                return
            self._stop.set()
            try:
                self.ctx.tasks.cancel(self._task_key)
            except Exception:
                pass
        self._cancel_poll()
        try:
            super().close()
        except Exception:
            pass

    def apply_theme(self) -> None:
        super().apply_theme()
        if getattr(self, "_body_ready", False):
            self._build_body()
            if self._last_event:
                self._apply_event(self._last_event)
            else:
                self._load_stats()
            if self._running:
                self._schedule_poll()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    def _build_body(self) -> None:
        self._cancel_poll()
        body = self.content()
        for child in list(body.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        t = self._tokens
        body.configure(bg=t.bg)

        card = widgets.Card(body, "项目")
        card.pack(fill="x")
        b = card.body
        self._path_lbl = tk.Label(
            b, text=self._path, bg=t.surface, fg=t.text, font=self._f,
            anchor="w", justify="left", wraplength=scale.s(780))
        self._path_lbl.pack(fill="x")
        row = tk.Frame(b, bg=t.surface)
        row.pack(fill="x", pady=(scale.s(10), 0))
        self._btn_choose = widgets.PillButton(
            row, "选择文件夹", self._choose_folder, kind="secondary")
        self._btn_choose.pack(side="left", padx=(0, scale.s(6)))
        self._btn_start = widgets.PillButton(
            row, "开始索引", self._start_from_button, kind="primary")
        self._btn_start.pack(side="left", padx=(0, scale.s(6)))
        self._btn_cancel = widgets.PillButton(
            row, "取消", self._cancel, kind="danger")
        self._btn_cancel.pack(side="left", padx=(0, scale.s(6)))
        self._btn_rebuild = widgets.PillButton(
            row, "重建", self._rebuild, kind="ghost")
        self._btn_rebuild.pack(side="left")
        self._btn_cancel.set_enabled(False)
        widgets.PillButton(row, "复制项目路径", self._copy_path,
                           kind="ghost").pack(side="right")

        prog = widgets.Card(body, "索引进度")
        prog.pack(fill="x", pady=(scale.s(10), 0))
        pb = prog.body
        prow = tk.Frame(pb, bg=t.surface)
        prow.pack(fill="x")
        self._phase_lbl = tk.Label(
            prow, text="尚未建立索引", bg=t.surface, fg=t.text,
            font=scale.font(13, weight="bold"), anchor="w")
        self._phase_lbl.pack(side="left")
        self._pct_lbl = tk.Label(
            prow, text="0%", bg=t.surface, fg=t.muted,
            font=scale.font(11, weight="bold"), anchor="e")
        self._pct_lbl.pack(side="right")
        self._bar = _Bar(pb, t, height=6)
        self._bar.pack(fill="x", pady=(scale.s(6), scale.s(4)))
        self._stats_lbl = tk.Label(
            pb, text="", bg=t.surface, fg=t.muted, font=self._f_small,
            anchor="w", justify="left", wraplength=scale.s(800))
        self._stats_lbl.pack(fill="x")
        self._file_lbl = tk.Label(
            pb, text="", bg=t.surface, fg=t.dim, font=self._f_small,
            anchor="w", justify="left", wraplength=scale.s(800))
        self._file_lbl.pack(fill="x", pady=(scale.s(2), 0))

        setrow = tk.Frame(pb, bg=t.surface)
        setrow.pack(fill="x", pady=(scale.s(8), 0))
        tk.Label(setrow, text="线程", bg=t.surface, fg=t.muted,
                 font=self._f_small).pack(side="left")
        threads = int(self._rag_cfg().get("threads") or 0)
        self._thread_seg = widgets.SegmentedControl(
            setrow, ["自动", "2", "4", "8", "16", "32", "64", "128", "256"],
            self._thread_label(threads), self._on_threads)
        self._thread_seg.pack(side="left", padx=(scale.s(6), scale.s(14)))
        tk.Label(setrow, text="MAX 上限", bg=t.surface, fg=t.muted,
                 font=self._f_small).pack(side="left")
        self._max_sw = widgets.ToggleSwitch(
            setrow, bool(self._rag_cfg().get("max_mode")),
            self._on_max_mode)
        self._max_sw.pack(side="left", padx=(scale.s(6), 0))
        self._max_hint = tk.Label(
            pb, text=self._max_hint_text(), bg=t.surface, fg=t.dim,
            font=self._f_small, anchor="w", justify="left",
            wraplength=scale.s(820))
        self._max_hint.pack(fill="x", pady=(scale.s(3), 0))

        info = widgets.Card(body, "索引完成后怎么给 AI 用")
        info.pack(fill="both", expand=True, pady=(scale.s(10), 0))
        ib = info.body
        self._usage = tk.Text(
            ib, height=5, wrap="word", bg=t.bg, fg=t.text, relief="flat",
            highlightthickness=1, highlightbackground=t.border,
            highlightcolor=t.border, font=self._f_mono,
            padx=scale.s(8), pady=scale.s(6))
        self._usage.pack(fill="both", expand=True, pady=(scale.s(8), 0))
        self._usage.configure(state="disabled")
        brow = tk.Frame(ib, bg=t.surface)
        brow.pack(fill="x", pady=(scale.s(8), 0))
        self._btn_copy_usage = widgets.PillButton(
            brow, "复制 AI 用法", self._copy_usage, kind="primary")
        self._btn_copy_usage.pack(side="left")
        tk.Label(brow, text="把这段发给 AI：它会先 rag.search，只读返回的文件/行。",
                 bg=t.surface, fg=t.dim, font=self._f_small,
                 anchor="w").pack(side="left", padx=(scale.s(10), 0))
        self._set_running(self._running)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _fmt_size(self, n: int) -> str:
        n = max(0, int(n or 0))
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return ("%d %s" % (n, unit)) if unit == "B" else (
                    "%.1f %s" % (n, unit))
            n /= 1024.0
        return "0 B"

    def _fmt_time(self, ts: float) -> str:
        if not ts:
            return "—"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    def _stats_text(self, files: int, chunks: int, skipped: int = 0,
                    errors: int = 0, elapsed: float | None = None,
                    extra: str = "") -> str:
        parts = ["文件 %s" % format(int(files), ","),
                 "块 %s" % format(int(chunks), ",")]
        if skipped:
            parts.append("跳过 %s" % format(int(skipped), ","))
        if errors:
            parts.append("错误 %s" % format(int(errors), ","))
        if elapsed is not None:
            parts.append("用时 %.1fs" % float(elapsed))
        if extra:
            parts.append(extra)
        return " · ".join(parts)

    def _rag_cfg(self) -> dict:
        cfg = self.ctx.cfg
        rag = cfg.get("rag")
        if not isinstance(rag, dict):
            rag = {}
            cfg["rag"] = rag
        rag.setdefault("threads", 0)
        rag.setdefault("max_mode", False)
        return rag

    def _save_rag_cfg(self) -> None:
        try:
            self.ctx.save_cfg()
        except Exception:
            pass

    @staticmethod
    def _thread_label(value: int) -> str:
        return "自动" if not value else str(int(value))

    def _on_threads(self, label: str) -> None:
        value = 0 if label == "自动" else int(label)
        old = int(self._rag_cfg().get("threads") or 0)
        if value >= 128 and value != old:
            ok = widgets.confirm(
                self, "高线程数性能警告",
                "你选择了 %d 个索引线程。\n\n"
                "RAG 的瓶颈通常是磁盘 I/O、Python GIL 和 SQLite 单写者；"
                "128/256 线程会争抢这些资源，可能让索引更慢，甚至拖慢整机。\n\n"
                "建议先用 8/16 或「自动」；确实要试就点确定（下次索引生效）。"
                % value, danger=True)
            if not ok:
                try:
                    self._thread_seg.set(self._thread_label(old), fire=False)
                except Exception:
                    pass
                return
        self._rag_cfg()["threads"] = value
        self._save_rag_cfg()
        try:
            self._thread_seg.set(self._thread_label(value), fire=False)
        except Exception:
            pass
        widgets.Toast(self.ctx.root, "线程数已保存，下次索引生效")

    def _on_max_mode(self, on: bool) -> None:
        self._rag_cfg()["max_mode"] = bool(on)
        self._save_rag_cfg()
        try:
            self._max_hint.configure(text=self._max_hint_text())
        except Exception:
            pass
        widgets.Toast(self.ctx.root, "MAX 上限已%s，下次索引生效"
                      % ("开启" if on else "关闭"))

    def _max_hint_text(self) -> str:
        if self._rag_cfg().get("max_mode"):
            return ("MAX 上限：单文件 ≤ 512KB · 最多 10,000 个文件；"
                    "大项目更快，可能跳过较大的源码/资源文件。")
        return "默认：单文件 ≤ 1MB · 最多 50,000 个文件。"

    def _set_pct(self, ratio: float) -> None:
        try:
            self._bar.set(ratio)
            self._pct_lbl.configure(text="%d%%" % int(
                max(0.0, min(1.0, float(ratio))) * 100))
        except Exception:
            pass

    def _usage_text(self, files: int, chunks: int, indexed_at: float) -> str:
        return (
            "项目：%s\n"
            "索引：%s 文件 / %s 块（%s）\n"
            "AI：rag.search(project_path=\"%s\", query=\"你的问题\")\n"
            "只读返回的 path:line 片段，不要整盘扫描。"
            % (self._path, format(files, ","), format(chunks, ","),
               self._fmt_time(indexed_at), self._path))

    def _set_usage(self, text: str) -> None:
        try:
            self._usage.configure(state="normal")
            self._usage.delete("1.0", "end")
            self._usage.insert("1.0", text)
            self._usage.configure(state="disabled")
        except Exception:
            pass

    def _load_stats(self) -> None:
        """从数据库读当前项目统计，更新静态状态。"""
        row = None
        if self.ctx.db is not None:
            try:
                row = engine.project_stats(self.ctx.db, self._path)
            except Exception:
                row = None
        if row is None:
            self._indexed = False
            self._phase_lbl.configure(text="尚未建立索引")
            self._stats_lbl.configure(text="点「开始索引」后会在后台静默处理。")
            self._file_lbl.configure(text="")
            self._set_pct(0.0)
            self._set_usage(self._usage_text(0, 0, 0))
            self._btn_start.set_text("开始索引")
            self._btn_start.set_enabled(not self._running)
            return
        files = int(row.get("file_count") or 0)
        chunks = int(row.get("chunk_count") or 0)
        status = str(row.get("status") or "")
        self._indexed = bool(row.get("indexed_at")) and status in (
            "ready", "partial")
        phase = "已建立索引"
        if status == "partial":
            phase = "部分索引（上次取消/失败，可重新索引）"
        elif status == "indexing":
            phase = "上次索引未完成"
        self._phase_lbl.configure(text=phase)
        self._stats_lbl.configure(text=(
            "%s · %s · 上次 %s"
            % (self._stats_text(files, chunks),
               self._fmt_size(int(row.get("total_bytes") or 0)),
               self._fmt_time(float(row.get("indexed_at") or 0)))))
        self._file_lbl.configure(text="")
        self._set_pct(1.0 if status == "ready" else 0.0)
        self._set_usage(self._usage_text(files, chunks,
                                         float(row.get("indexed_at") or 0)))
        self._btn_start.set_text("重新索引" if files or chunks else "开始索引")
        self._btn_start.set_enabled(not self._running)

    def _set_running(self, running: bool) -> None:
        self._running = bool(running)
        try:
            self._btn_start.set_enabled(not running)
            self._btn_choose.set_enabled(not running)
            self._btn_rebuild.set_enabled(not running)
            self._btn_cancel.set_enabled(running)
            self._btn_copy_usage.set_enabled(True)
            if running:
                self._btn_start.set_text("索引中…")
            elif self._indexed:
                self._btn_start.set_text("重新索引")
            else:
                self._btn_start.set_text("开始索引")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 启动/取消
    # ------------------------------------------------------------------

    def _maybe_auto_start(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if not self._running and not self._indexed:
            self.start_index()

    def _start_from_button(self) -> None:
        self.start_index()

    def _rebuild(self) -> None:
        if self._running:
            return
        if not widgets.confirm(
                self, "重建索引",
                "会删除当前项目的索引并从头扫描（适合索引损坏或文件时间戳"
                "没变但内容改过的情况）。确定？", danger=True):
            return
        self.start_index(full=True)

    def start_index(self, *, full: bool = False) -> None:
        if self._running:
            return
        if self.ctx.db is None:
            messagebox.showerror("项目 RAG", "数据库不可用，无法建立索引。",
                                 parent=self)
            return
        if not os.path.isdir(self._path):
            messagebox.showerror("项目 RAG", "文件夹不存在：\n%s" % self._path,
                                 parent=self)
            return
        self._stop.clear()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._last_event = {}
        self._indexed = False
        self._set_running(True)
        self._phase_lbl.configure(text="扫描项目文件…")
        self._stats_lbl.configure(text="正在遍历目录（跳过无关/二进制/超大文件）")
        self._file_lbl.configure(text="")
        self._set_pct(0.02)
        try:
            rag = self._rag_cfg()
            threads = int(rag.get("threads") or 0) or 0
            max_mode = bool(rag.get("max_mode"))
            max_file_bytes = 512 * 1024 if max_mode else 1024 * 1024
            max_files = 10000 if max_mode else 50000
            self.ctx.tasks.submit(
                self._task_key,
                lambda: engine.index_project(
                    self.ctx.db, self._path, full=full, threads=threads,
                    max_file_bytes=max_file_bytes, max_files=max_files,
                    progress=self._queue.put, stop=self._stop),
                group="rag", on_done=self._on_task_done,
                on_error=self._on_task_error)
        except Exception as exc:
            self._set_running(False)
            self._phase_lbl.configure(text="索引启动失败")
            self._stats_lbl.configure(text=str(exc))
            return
        self._schedule_poll()

    def _cancel(self) -> None:
        if not self._running:
            return
        self._stop.set()
        self._phase_lbl.configure(text="正在取消…")
        try:
            self.ctx.tasks.cancel(self._task_key)
        except Exception:
            pass
        self.after(5000, self._cancel_timeout)

    def _cancel_timeout(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._running:
            self._running = False
            self._set_running(False)
            self._phase_lbl.configure(text="已取消")
            self._load_stats()

    def _on_task_done(self, stats) -> None:
        if not self.winfo_exists():
            return
        self._running = False
        self._set_running(False)
        if isinstance(stats, dict) and stats.get("cancelled"):
            self._phase_lbl.configure(text="已取消（部分索引保留）")
        else:
            self._phase_lbl.configure(text="索引完成")
            self._set_pct(1.0)
        self._load_stats()
        self._refresh_usage_from_db()
        self._cancel_poll()

    def _on_task_error(self, exc) -> None:
        if not self.winfo_exists():
            return
        self._running = False
        self._set_running(False)
        self._phase_lbl.configure(text="索引失败")
        self._stats_lbl.configure(text=str(exc or "未知错误"))
        self._cancel_poll()

    # ------------------------------------------------------------------
    # 进度轮询
    # ------------------------------------------------------------------

    def _schedule_poll(self) -> None:
        self._cancel_poll()
        try:
            self._poll_job = self.after(100, self._poll)
        except Exception:
            self._poll_job = None

    def _cancel_poll(self) -> None:
        job, self._poll_job = self._poll_job, None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass

    def _poll(self) -> None:
        self._poll_job = None
        try:
            while True:
                try:
                    ev = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._apply_event(ev)
        except Exception:
            pass
        if self._running:
            try:
                self._poll_job = self.after(100, self._poll)
            except Exception:
                self._poll_job = None

    def _apply_event(self, ev: dict) -> None:
        if not isinstance(ev, dict):
            return
        self._last_event = dict(ev)
        phase = str(ev.get("phase") or "")
        t = self._tokens
        if phase in ("scan_start", "scan"):
            self._phase_lbl.configure(text="扫描项目文件…")
            self._stats_lbl.configure(
                text=self._stats_text(ev.get("files", 0), 0,
                                      ev.get("skipped", 0)))
            return
        if phase == "scan_done":
            self._phase_lbl.configure(text="准备索引…")
            self._stats_lbl.configure(
                text="共 %s 个文件 · 跳过 %s"
                % (format(int(ev.get("total") or 0), ","),
                   format(int(ev.get("skipped") or 0), ",")))
            self._set_pct(0.08)
            return
        if phase == "parse_start":
            self._phase_lbl.configure(text="多线程读取并分块…")
            self._stats_lbl.configure(
                text="待处理 %s · 保持不变 %s · 删除 %s"
                % (format(int(ev.get("total") or 0), ","),
                   format(int(ev.get("kept") or 0), ","),
                   format(int(ev.get("deleted") or 0), ",")))
            self._set_pct(0.1)
            return
        if phase == "parse":
            done = int(ev.get("done") or 0)
            total = max(1, int(ev.get("total") or 0))
            self._phase_lbl.configure(
                text="读取并分块 %d/%d（%d%%）"
                % (done, total, int(100 * done / total)))
            self._stats_lbl.configure(
                text=self._stats_text(done, int(ev.get("chunks") or 0),
                                      errors=int(ev.get("errors") or 0)))
            self._file_lbl.configure(
                text=("当前：%s" % ev.get("file")) if ev.get("file") else "")
            self._set_pct(0.1 + 0.75 * done / total)
            return
        if phase == "db":
            done = int(ev.get("done") or 0)
            total = max(1, int(ev.get("total") or 0))
            self._phase_lbl.configure(
                text="写入数据库 %d/%d（%d%%）"
                % (done, total, int(100 * done / total)))
            self._stats_lbl.configure(
                text=self._stats_text(done, int(ev.get("chunks") or 0),
                                      errors=int(ev.get("errors") or 0)))
            self._set_pct(0.85 + 0.14 * done / total)
            return
        if phase in ("done", "cancelled"):
            self._running = False
            self._set_running(False)
            if phase == "done":
                self._phase_lbl.configure(text="索引完成")
                self._set_pct(1.0)
                self._stats_lbl.configure(
                    text=self._stats_text(
                        ev.get("files", 0), ev.get("chunks", 0),
                        ev.get("skipped", 0), ev.get("errors", 0),
                        ev.get("elapsed")))
                self._file_lbl.configure(text="")
                self._refresh_usage_from_db()
                try:
                    widgets.Toast(self.ctx.root, "RAG 索引完成：%s 文件 / %s 块"
                                  % (format(int(ev.get("files") or 0), ","),
                                     format(int(ev.get("chunks") or 0), ",")))
                except Exception:
                    pass
            else:
                self._phase_lbl.configure(text="已取消（部分索引保留）")
                self._stats_lbl.configure(
                    text=self._stats_text(ev.get("files", 0),
                                          ev.get("chunks", 0),
                                          errors=ev.get("errors", 0)))
                self._load_stats()
            self._cancel_poll()
            return

    def _refresh_usage_from_db(self) -> None:
        try:
            row = engine.project_stats(self.ctx.db, self._path)
        except Exception:
            row = None
        if row:
            self._set_usage(self._usage_text(
                int(row.get("file_count") or 0),
                int(row.get("chunk_count") or 0),
                float(row.get("indexed_at") or 0)))

    # ------------------------------------------------------------------
    # 目录/复制
    # ------------------------------------------------------------------

    def _choose_folder(self) -> None:
        if self._running:
            widgets.Toast(self.ctx.root, "先取消当前索引再切换项目")
            return
        ball = getattr(getattr(self.ctx, "app", None), "ball", None)
        try:
            ball.withdraw()
        except Exception:
            pass
        try:
            path = filedialog.askdirectory(
                parent=self.ctx.root, title="项目 RAG - 选择项目文件夹",
                initialdir=self._path or os.path.expanduser("~"))
        finally:
            try:
                ball.deiconify()
                ball.lift()
            except Exception:
                pass
        if path:
            self.switch_project(path)

    def switch_project(self, path: str) -> None:
        path = engine.norm_root(path)
        if path == self._path:
            self.show()
            return
        if self._running:
            widgets.Toast(self.ctx.root, "先取消当前索引再切换项目")
            return
        self._path = path
        self._last_event = {}
        self._indexed = False
        try:
            self.ctx.cfg.setdefault("rag", {})["last_dir"] = path
            self.ctx.save_cfg()
        except Exception:
            pass
        self._build_body()
        self._load_stats()
        self.after(180, self._maybe_auto_start)

    def _copy_path(self) -> None:
        self._copy(self._path, "项目路径")

    def _copy_usage(self) -> None:
        try:
            text = self._usage.get("1.0", "end").strip()
        except Exception:
            text = ""
        self._copy(text or self._usage_text(0, 0, 0), "AI 用法")

    def _copy(self, text: str, label: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            widgets.Toast(self.ctx.root, "已复制%s" % label)
        except Exception as exc:
            messagebox.showerror("复制失败", str(exc), parent=self)
