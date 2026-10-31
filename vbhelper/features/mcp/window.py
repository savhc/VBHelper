# -*- coding: utf-8 -*-
"""MCP 工具总线的控制面板。

一页看清：服务状态、工具开关、屏幕/鼠标键盘总闸、确认策略、急停、
AI 客户端配置复制、最近调用审计。所有开关即时写 config.json 并生效。
"""
from __future__ import annotations

import json
import os
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox

from ...ui import scale, widgets
from ...ui.scroll import bind_canvas_wheel
from ...ui.tabs import TabBar
from ...ui.window import FeatureWindow
from .registry import tool_enabled

GROUP_ORDER = ("web", "screen", "input", "clipboard", "system")
GROUP_TITLES = {
    "web": "联网搜索",
    "screen": "屏幕",
    "input": "鼠标键盘",
    "clipboard": "剪贴板",
    "system": "系统",
}
_PERMISSION_TEXT = {"safe": "安全", "confirm": "需确认", "dangerous": "危险"}


def _attach_scrollbar(bar, widget, *, side: str = "right"):
    """把 ttk 滚动条接到 Text/Canvas，内容不溢出时隐藏。

    clam 下禁用态滚动条会露白条，用户对这个很敏感；这里只让它在
    「真的能滚」时出现。
    """
    state = {"shown": True}

    def on_scroll(first, last):
        try:
            bar.set(first, last)
            full = float(first) <= 0.0 and float(last) >= 1.0
            if full and state["shown"]:
                bar.pack_forget()
                state["shown"] = False
            elif not full and not state["shown"]:
                bar.pack(side=side, fill="y")
                state["shown"] = True
        except Exception:
            pass

    widget.configure(yscrollcommand=on_scroll)
    bar.configure(command=widget.yview)
    return on_scroll
_MODE_LABEL = {"ask": "每次确认", "session": "本次会话", "never": "直接允许"}
_LABEL_MODE = {v: k for k, v in _MODE_LABEL.items()}


class McpWindow(FeatureWindow):
    """MCP 控制面板。"""

    def __init__(self, ctx, runtime) -> None:
        self.runtime = runtime
        self._f = scale.font(11)
        self._f_small = scale.font(10)
        self._f_mono = scale.font(10, family="Consolas")
        self._job = None
        self._body_ready = False
        self._tool_switches = {}
        self._status = {}
        super().__init__(
            ctx, "mcp_bus", "MCP 工具总线", (1080, 720),
            min_size=(940, 600),
            subtitle="给 AI 客户端开放本机能力：截屏返回 · 鼠标键盘 · 联网搜索")
        self.remember_geometry("mcp_bus")
        self._build_body()
        self._body_ready = True
        self._tick()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._cancel_tick()
        try:
            super().close()
        except Exception:
            pass

    def apply_theme(self) -> None:
        super().apply_theme()
        if getattr(self, "_body_ready", False):
            self._build_body()

    def _cancel_tick(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self) -> None:
        self._job = None
        try:
            self._refresh_status()
            self._refresh_recent()
        except Exception:
            pass
        try:
            self._job = self.after(1500, self._tick)
        except Exception:
            self._job = None

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    def _build_body(self) -> None:
        self._cancel_tick()
        body = self.content()
        for child in list(body.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        self._tool_switches = {}
        self._masters = {}
        self._perm_switches = {}
        self._auto_switch = None
        self._active_tab = int(getattr(self, "_active_tab", 0) or 0)
        self._pages = []
        t = self._tokens
        body.configure(bg=t.bg)

        # 顶部状态卡
        status_card = widgets.Card(body, "服务状态")
        status_card.pack(fill="x")
        sbody = status_card.body
        row = tk.Frame(sbody, bg=t.surface)
        row.pack(fill="x")
        self._dot = tk.Canvas(row, width=scale.s(10), height=scale.s(10),
                              bg=t.surface, highlightthickness=0, bd=0)
        self._dot.pack(side="left", padx=(0, scale.s(8)))
        self._dot_id = self._dot.create_oval(0, 0, scale.s(9), scale.s(9),
                                             fill=t.dim, outline="")
        left = tk.Frame(row, bg=t.surface)
        left.pack(side="left", fill="x", expand=True)
        self._status_lbl = tk.Label(
            left, text="服务未启动", anchor="w", bg=t.surface, fg=t.text,
            font=scale.font(13, weight="bold"))
        self._status_lbl.pack(fill="x")
        self._status_sub = tk.Label(
            left, text="", anchor="w", bg=t.surface, fg=t.muted,
            font=self._f_small, justify="left")
        self._status_sub.pack(fill="x", pady=(scale.s(2), 0))
        btns = tk.Frame(row, bg=t.surface)
        btns.pack(side="right")
        self._btn_start = widgets.PillButton(btns, "启动", self._start,
                                             kind="primary")
        self._btn_start.pack(side="left", padx=(0, scale.s(6)))
        self._btn_stop = widgets.PillButton(btns, "停止", self._stop,
                                            kind="secondary")
        self._btn_stop.pack(side="left", padx=(0, scale.s(6)))
        widgets.PillButton(btns, "重启", self._restart,
                           kind="secondary").pack(side="left")

        # 页签：工具 / 权限 / 连接 / 审计（自绘，不用 ttk.Notebook）
        self._tabs = TabBar(body, ("工具", "权限", "连接", "审计"), t,
                            command=self._show_page)
        self._tabs.pack(fill="x", pady=(scale.s(10), 0))
        holder = tk.Frame(body, bg=t.bg)
        holder.pack(fill="both", expand=True, pady=(scale.s(6), 0))

        page_tools = tk.Frame(holder, bg=t.bg)
        self._build_tools(page_tools)
        page_perm = tk.Frame(holder, bg=t.bg)
        self._build_safety(page_perm)
        page_conn = tk.Frame(holder, bg=t.bg)
        self._build_connection(page_conn)
        page_audit = tk.Frame(holder, bg=t.bg)
        self._build_recent(page_audit)
        self._pages = [page_tools, page_perm, page_conn, page_audit]
        self._show_page(self._active_tab, fire=False)

        self.set_status("MCP 只监听 127.0.0.1 · 令牌由 Windows DPAPI 保存 · "
                        "急停热键 Ctrl+Alt+Shift+Esc")
        # 主题切换/取消开关会重建面板；重建后要把状态轮询接回来
        if getattr(self, "_body_ready", False) and self._job is None:
            self._tick()

    def _show_page(self, index: int, fire: bool = True) -> None:
        """切换页签；主题重绘后保留当前页。"""
        pages = getattr(self, "_pages", None)
        if not pages:
            return
        index = max(0, min(int(index), len(pages) - 1))
        self._active_tab = index
        for page in pages:
            try:
                page.pack_forget()
            except Exception:
                pass
        pages[index].pack(fill="both", expand=True)
        tabs = getattr(self, "_tabs", None)
        if tabs is not None and tabs.active() != index:
            tabs.select(index, fire=False)

    # -- 工具列表 ------------------------------------------------------

    def _build_tools(self, parent) -> None:
        t = self._tokens
        card = widgets.Card(parent, "工具", "关闭后 AI 的 tools/list 就不再返回它")
        card.pack(fill="both", expand=True)
        wrap = tk.Frame(card.body, bg=t.surface)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=t.surface, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=t.surface)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            win, width=max(1, e.width)))
        _attach_scrollbar(vsb, canvas)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        bind_canvas_wheel(self, canvas, area=wrap)

        try:
            specs = self.runtime.registry().all()
            err = ""
        except Exception as exc:
            specs = []
            err = "工具加载失败：%s" % exc
        if err:
            tk.Label(inner, text=err, bg=t.surface, fg=t.danger,
                     font=self._f_small, wraplength=scale.s(500),
                     justify="left", anchor="w").pack(fill="x", pady=scale.s(6))
            return
        groups = {}
        for spec in specs:
            groups.setdefault(spec.group, []).append(spec)
        for group in GROUP_ORDER:
            items = groups.pop(group, [])
            if not items:
                continue
            self._tool_group(inner, group, items)
        for group, items in groups.items():
            self._tool_group(inner, group, items)

    def _tool_group(self, parent, group: str, specs: list) -> None:
        t = self._tokens
        cfg = self.runtime.cfg
        head = tk.Frame(parent, bg=t.surface)
        head.pack(fill="x", pady=(scale.s(10), scale.s(2)))
        tk.Label(head, text=GROUP_TITLES.get(group, group), bg=t.surface,
                 fg=t.text, font=scale.font(12, weight="bold"),
                 anchor="w").pack(side="left")
        if group == "screen":
            sw = widgets.ToggleSwitch(
                head, bool((cfg.get("screen") or {}).get("allow")),
                self._on_screen_allow)
            sw.pack(side="right")
            self._masters["screen"] = sw
            tk.Label(head, text="允许截屏", bg=t.surface, fg=t.muted,
                     font=self._f_small).pack(side="right",
                                              padx=(0, scale.s(6)))
        elif group == "input":
            sw = widgets.ToggleSwitch(
                head, bool((cfg.get("input") or {}).get("enabled")),
                self._on_input_allow)
            sw.pack(side="right")
            self._masters["input"] = sw
            tk.Label(head, text="总开关", bg=t.surface, fg=t.muted,
                     font=self._f_small).pack(side="right",
                                              padx=(0, scale.s(6)))
        tk.Frame(parent, bg=t.border, height=1).pack(fill="x",
                                                     pady=(scale.s(2), 0))
        for spec in sorted(specs, key=lambda s: s.name):
            self._tool_row(parent, spec)

    def _tool_row(self, parent, spec) -> None:
        t = self._tokens
        cfg = self.runtime.cfg
        row = tk.Frame(parent, bg=t.surface)
        row.pack(fill="x", pady=scale.s(3))
        left = tk.Frame(row, bg=t.surface)
        left.pack(side="left", fill="x", expand=True)
        line = tk.Frame(left, bg=t.surface)
        line.pack(fill="x")
        tk.Label(line, text=spec.title, bg=t.surface, fg=t.text,
                 font=self._f, anchor="w").pack(side="left")
        tk.Label(line, text=spec.name, bg=t.surface, fg=t.dim,
                 font=self._f_small, anchor="w").pack(side="left",
                                                     padx=(scale.s(8), 0))
        hint = (spec.description or "").replace("\n", " ")
        if len(hint) > 180:
            hint = hint[:180] + "…"
        tk.Label(left, text=hint, bg=t.surface, fg=t.muted,
                 font=self._f_small, anchor="w", justify="left",
                 wraplength=scale.s(760)).pack(
            fill="x", pady=(scale.s(1), 0))
        sw = widgets.ToggleSwitch(
            row, tool_enabled(spec, cfg),
            lambda on, s=spec, w=None: self._on_tool_toggle(s, on))
        sw.pack(side="right", padx=(scale.s(6), 0))
        tone = "warning" if spec.permission == "confirm" else "neutral"
        widgets.Badge(row, _PERMISSION_TEXT.get(spec.permission, spec.permission),
                      tone=tone).pack(side="right", pady=scale.s(4))
        self._tool_switches[spec.name] = (sw, spec)

    # -- 连接配置 ------------------------------------------------------

    def _build_connection(self, parent) -> None:
        t = self._tokens
        card = widgets.Card(parent, "连接 AI 客户端",
                            "stdio 桥会连接或拉起 VBHelper（需在面板启用）")
        card.pack(fill="both", expand=True, pady=(scale.s(10), 0))
        b = card.body
        self._auto_switch = self._switch_row(
            b, "允许 AI 客户端自动启动",
            "关闭后只能手动点「启动」；stdio 桥不会拉起服务",
            bool((self.runtime.cfg.get("server") or {}).get("auto_start",
                                                            True)),
            self._on_auto_start)
        btns = tk.Frame(b, bg=t.surface)
        btns.pack(fill="x", pady=(scale.s(8), 0))
        widgets.PillButton(btns, "复制 stdio 配置", self._copy_stdio,
                           kind="primary").pack(side="left",
                                                padx=(0, scale.s(6)))
        widgets.PillButton(btns, "复制 HTTP 配置", self._copy_http,
                           kind="secondary").pack(side="left",
                                                  padx=(0, scale.s(6)))
        widgets.PillButton(btns, "重置令牌", self._rotate_token,
                           kind="ghost").pack(side="left")
        tk.Label(b, text="1. 把下面的 JSON 加进客户端的 mcpServers；\n"
                         "2. 客户端启动这个桥后，会自动拉起 VBHelper 并连接本机服务；\n"
                         "3. 服务未启动时，先在顶部点「启动」，或打开上面的自动启动。",
                 bg=t.surface, fg=t.muted, font=self._f_small, anchor="w",
                 justify="left", wraplength=scale.s(860)).pack(
            fill="x", pady=(scale.s(8), 0))
        box = tk.Text(b, height=5, wrap="none", bg=t.bg, fg=t.text,
                      relief="flat", highlightthickness=1,
                      highlightbackground=t.border, highlightcolor=t.border,
                      font=self._f_mono, padx=scale.s(8), pady=scale.s(6))
        box.pack(fill="both", expand=True, pady=(scale.s(8), 0))
        box.insert("1.0", self._stdio_json())
        box.configure(state="disabled")
        self._json_box = box
        tk.Label(b, text="HTTP 配置只在服务运行时可用；令牌不会明文显示，"
                         "复制按钮会写入完整配置。",
                 bg=t.surface, fg=t.dim, font=self._f_small,
                 anchor="w").pack(fill="x", pady=(scale.s(4), 0))

    # -- 权限与安全 ----------------------------------------------------

    def _build_safety(self, parent) -> None:
        t = self._tokens
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=3)
        parent.grid_columnconfigure(1, weight=2)
        card = widgets.Card(parent, "权限与安全", "危险能力逐项授权，随时可急停")
        card.grid(row=0, column=0, sticky="nsew", padx=(0, scale.s(10)))
        b = card.body
        cfg = self.runtime.cfg

        self._perm_switches["screen"] = self._switch_row(
            b, "允许 AI 读取屏幕",
            "关闭后 screen.capture 不再返回图像（info/list 仍可用）",
            bool((cfg.get("screen") or {}).get("allow")),
            self._on_screen_allow)
        self._perm_switches["input"] = self._switch_row(
            b, "允许 AI 控制鼠标键盘",
            "总闸关闭时，单个 input 工具开关不会生效",
            bool((cfg.get("input") or {}).get("enabled")),
            self._on_input_allow)

        tk.Label(b, text="操作确认方式", bg=t.surface, fg=t.text,
                 font=self._f, anchor="w").pack(fill="x",
                                                 pady=(scale.s(10), 2))
        self._seg = widgets.SegmentedControl(
            b, ["每次确认", "本次会话", "直接允许"],
            _MODE_LABEL.get(str((cfg.get("confirm") or {}).get("mode")), "每次确认"),
            self._on_confirm_mode)
        self._seg.pack(anchor="w")
        tk.Label(b, text="让 AI 连续操作选「本次会话」：只弹一次 5 分钟授权；"
                         "「直接允许」不弹窗，风险最高。",
                 bg=t.surface, fg=t.dim, font=self._f_small, anchor="w",
                 justify="left", wraplength=scale.s(560)).pack(
            fill="x", pady=(scale.s(4), 0))

        row = tk.Frame(b, bg=t.surface)
        row.pack(fill="x", pady=(scale.s(12), 0))
        widgets.PillButton(row, "急停 60 秒", self._panic,
                           kind="danger").pack(side="left")
        widgets.PillButton(row, "输入自测", self._self_test_input,
                           kind="secondary").pack(side="left",
                                                  padx=(scale.s(6), 0))
        self._panic_lbl = tk.Label(row, text="", bg=t.surface, fg=t.danger,
                                   font=self._f_small, anchor="w")
        self._panic_lbl.pack(side="left", padx=(scale.s(8), 0))
        tk.Label(b, text="急停热键 Ctrl+Alt+Shift+Esc（不可关闭）。"
                         "所有工具调用写入本机审计。",
                 bg=t.surface, fg=t.dim, font=self._f_small, anchor="w",
                 justify="left", wraplength=scale.s(760)).pack(
            fill="x", pady=(scale.s(8), 0))

        notes = widgets.Card(parent, "自动化说明",
                             "默认关闭；开启后每次调用仍有确认与审计")
        notes.grid(row=0, column=1, sticky="nsew")
        nb = notes.body
        for text in (
                "截屏是安全工具：总闸开启后 AI 可随时读取屏幕；图片与临时文件只落本机。",
                "鼠标键盘是确认工具：按上方确认方式执行；关闭总闸立即失效。",
                "急停 Ctrl+Alt+Shift+Esc 会暂停输入 60 秒，并拒绝仍在等待的确认。",
                "每次调用（工具名、脱敏参数、耗时、成功/失败）写入 SQLite 审计，可查可清空。",
                "服务仅绑定 127.0.0.1，所有请求需要 bearer token；令牌由 Windows DPAPI 保存。"):
            tk.Label(nb, text="· " + text, bg=t.surface, fg=t.muted,
                     font=self._f_small, anchor="w", justify="left",
                     wraplength=scale.s(380)).pack(fill="x", pady=scale.s(4))

    def _switch_row(self, parent, title: str, hint: str, value: bool,
                    command) -> widgets.ToggleSwitch:
        t = self._tokens
        row = tk.Frame(parent, bg=t.surface)
        row.pack(fill="x", pady=scale.s(3))
        left = tk.Frame(row, bg=t.surface)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=title, bg=t.surface, fg=t.text,
                 font=self._f, anchor="w").pack(fill="x")
        tk.Label(left, text=hint, bg=t.surface, fg=t.muted,
                 font=self._f_small, anchor="w", justify="left",
                 wraplength=scale.s(760)).pack(fill="x")
        sw = widgets.ToggleSwitch(row, value, command)
        sw.pack(side="right")
        return sw

    # -- 最近调用 ------------------------------------------------------

    def _build_recent(self, parent) -> None:
        t = self._tokens
        card = widgets.Card(parent, "最近调用", "审计只保存在本机 SQLite")
        card.pack(fill="both", expand=True, pady=(scale.s(10), 0))
        b = card.body
        btns = tk.Frame(b, bg=t.surface)
        btns.pack(side="bottom", fill="x", pady=(scale.s(6), 0))
        widgets.PillButton(btns, "查看全部", self._open_audit,
                           kind="secondary").pack(side="left")
        widgets.PillButton(btns, "刷新", lambda: self._refresh_recent(),
                           kind="ghost").pack(side="left",
                                              padx=(scale.s(6), 0))
        wrap = tk.Frame(b, bg=t.surface)
        wrap.pack(fill="both", expand=True)
        box = tk.Text(wrap, height=8, wrap="word", bg=t.bg, fg=t.text,
                      relief="flat", highlightthickness=1,
                      highlightbackground=t.border, highlightcolor=t.border,
                      font=self._f_mono, padx=scale.s(8), pady=scale.s(6))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=box.yview)
        _attach_scrollbar(vsb, box)
        box.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        box.tag_configure("time", foreground=t.dim)
        box.tag_configure("ok", foreground=t.success)
        box.tag_configure("fail", foreground=t.danger)
        box.tag_configure("name", foreground=t.text)
        box.tag_configure("empty", foreground=t.muted)
        box.configure(state="disabled")
        self._recent_box = box
        self._refresh_recent()

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------

    def _refresh_status(self) -> None:
        st = self.runtime.status()
        self._status = st
        t = self._tokens
        running = bool(st.get("running"))
        try:
            self._dot.itemconfigure(self._dot_id,
                                    fill=t.success if running else t.dim)
        except Exception:
            pass
        if running:
            self._status_lbl.configure(
                text="服务运行中 · 127.0.0.1:%d" % st.get("port", 0),
                fg=t.success)
            up = max(0, int(time.time() - float(st.get("started_at") or 0)))
            on, total = self._tool_stats()
            sub = ("工具 %d/%d 启用 · 会话 %d · 调用 %d · 已运行 %s · 令牌 %s"
                   % (on, total, st.get("sessions", 0),
                      st.get("calls", 0), _fmt_uptime(up),
                      st.get("token_masked") or "未配置"))
        else:
            self._status_lbl.configure(text="服务未启动", fg=t.text)
            if (self.runtime.cfg.get("server") or {}).get("auto_start", True):
                sub = ("首次点「启动」后 AI 客户端才能连接；之后客户端连接时"
                       "可按需自动启动本服务（「停止」只停本轮）。")
            else:
                sub = ("自动启动已关闭：只能手动点「启动」。要允许 AI 客户端"
                       "自己拉起，请到「连接」页打开自动启动。")
        if st.get("last_error"):
            sub += "  " + str(st["last_error"])
        self._status_sub.configure(text=sub)
        try:
            self._btn_start.set_enabled(not running)
            self._btn_stop.set_enabled(running)
        except Exception:
            pass
        try:
            self._panic_lbl.configure(
                text="输入已急停" if st.get("panicked") else "")
        except Exception:
            pass
        self._sync_tool_switches()

    def _tool_stats(self) -> tuple[int, int]:
        try:
            reg = self.runtime.registry()
            return (len(reg.enabled_specs(self.runtime.cfg)),
                    len(reg.all()))
        except Exception:
            return (0, 0)

    def _sync_tool_switches(self) -> None:
        cfg = self.runtime.cfg
        masters = {
            "screen": bool((cfg.get("screen") or {}).get("allow")),
            "input": bool((cfg.get("input") or {}).get("enabled")),
        }
        for key, sw in list(getattr(self, "_masters", {}).items()):
            try:
                sw.set(masters.get(key, False), fire=False)
            except Exception:
                pass
        for key, sw in list(getattr(self, "_perm_switches", {}).items()):
            try:
                sw.set(masters.get(key, False), fire=False)
            except Exception:
                pass
        auto_sw = getattr(self, "_auto_switch", None)
        if auto_sw is not None:
            try:
                auto_sw.set(bool((cfg.get("server") or {}).get(
                    "auto_start", True)), fire=False)
            except Exception:
                pass
        for _name, (sw, spec) in list(self._tool_switches.items()):
            try:
                sw.set(tool_enabled(spec, cfg), fire=False)
            except Exception:
                pass

    def _refresh_recent(self) -> None:
        box = getattr(self, "_recent_box", None)
        if box is None:
            return
        rows = self.runtime.recent_audit(16)
        t = self._tokens
        box.configure(state="normal")
        box.delete("1.0", "end")
        if not rows:
            box.insert("end", "暂无调用记录。\n", "empty")
        else:
            for row in rows:
                ts = time.strftime("%H:%M:%S", time.localtime(row.get("ts") or 0))
                ok = bool(row.get("ok"))
                box.insert("end", ts + "  ", "time")
                box.insert("end", str(row.get("action") or "?"), "name")
                box.insert("end", "  ")
                box.insert("end", "OK" if ok else "FAIL", "ok" if ok else "fail")
                box.insert("end", "  %s ms\n" % row.get("elapsed_ms", 0), "time")
        box.configure(state="disabled")
        try:
            box.see("end")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def _start(self) -> None:
        try:
            info = self.runtime.start()
        except Exception as exc:
            messagebox.showerror("MCP 启动失败", str(exc), parent=self)
            return
        widgets.Toast(self.ctx.root, "MCP 服务已启动：127.0.0.1:%d"
                      % info.get("port", 0))
        self._refresh_status()

    def _stop(self) -> None:
        self.runtime.stop()
        widgets.Toast(self.ctx.root, "MCP 服务已停止")
        self._refresh_status()

    def _restart(self) -> None:
        try:
            self.runtime.restart()
        except Exception as exc:
            messagebox.showerror("MCP 重启失败", str(exc), parent=self)
            return
        widgets.Toast(self.ctx.root, "MCP 服务已重启")
        self._refresh_status()

    def _on_tool_toggle(self, spec, on: bool) -> None:
        cfg = self.runtime.cfg
        if on and spec.group == "input" and not (cfg.get("input") or {}).get("enabled"):
            widgets.Toast(self.ctx.root, "先打开「允许 AI 控制鼠标键盘」总开关")
            self._sync_tool_switches()
            return
        if on and spec.name == "screen.capture" \
                and not (cfg.get("screen") or {}).get("allow"):
            widgets.Toast(self.ctx.root, "先打开「允许 AI 读取屏幕」")
            self._sync_tool_switches()
            return
        self.runtime.set_tool_enabled(spec.name, on)
        self._sync_tool_switches()

    def _on_auto_start(self, on: bool) -> None:
        self.runtime.set_auto_start(on)
        widgets.Toast(self.ctx.root,
                      "已允许 AI 客户端自动启动服务" if on
                      else "已关闭自动启动：只能在面板手动启动")
        self._sync_tool_switches()

    def _on_screen_allow(self, on: bool) -> None:
        self.runtime.set_screen_allow(on)
        if on:
            widgets.Toast(self.ctx.root, "AI 可以截屏了")
        self._sync_tool_switches()

    def _on_input_allow(self, on: bool) -> None:
        if on:
            mode = _MODE_LABEL.get(
                str((self.runtime.cfg.get("confirm") or {}).get("mode")),
                "每次确认")
            if not widgets.confirm(
                    self, "允许 AI 控制鼠标键盘？",
                    "开启后 AI 可以真实移动鼠标、点击和输入文字。\n\n"
                    "当前确认方式：%s。\n急停热键：Ctrl+Alt+Shift+Esc。"
                    % mode, danger=True):
                self.after(0, self._build_body)   # 回调内不销毁自身，避免 Tk 回调链中断
                return
        self.runtime.set_input_enabled(on)
        widgets.Toast(self.ctx.root,
                      "AI 鼠标键盘已启用" if on else "AI 鼠标键盘已关闭")
        self._sync_tool_switches()

    def _on_confirm_mode(self, label: str) -> None:
        mode = _LABEL_MODE.get(label, "ask")
        if mode == "never":
            if not widgets.confirm(
                    self, "关闭逐次确认",
                    "之后 AI 调鼠标键盘不会再弹确认框，只在面板显示审计。\n"
                    "确定？", danger=True):
                self._rebuild_safety_state()
                return
        self.runtime.set_confirm_mode(mode)
        widgets.Toast(self.ctx.root, "确认方式：%s" % _MODE_LABEL[mode])

    def _rebuild_safety_state(self) -> None:
        mode = str((self.runtime.cfg.get("confirm") or {}).get("mode") or "ask")
        try:
            self._seg.set(_MODE_LABEL.get(mode, "每次确认"), fire=False)
        except Exception:
            pass

    def _panic(self) -> None:
        self.runtime.panic()
        self._refresh_status()

    def _self_test_input(self) -> None:
        """真实 SendInput 自测：打开小窗，2 秒后自动输入一行文字。"""
        cfg = self.runtime.cfg
        if not (cfg.get("input") or {}).get("enabled"):
            widgets.Toast(self.ctx.root, "先打开「允许 AI 控制鼠标键盘」总开关")
            return
        try:
            svc = self.runtime._ensure_services().input
        except Exception as exc:
            messagebox.showerror("输入自测不可用", str(exc), parent=self)
            return
        t = self._tokens
        d = tk.Toplevel(self.ctx.root)
        d.title("输入自测")
        d.configure(bg=t.bg)
        d.resizable(False, False)
        try:
            d.attributes("-topmost", True)
        except Exception:
            pass
        pad = scale.s(18)
        tk.Label(d, text="2 秒后自动点击输入框并键入（不要切换窗口）", bg=t.bg,
                 fg=t.text, font=scale.font(12, weight="bold"),
                 anchor="w").pack(fill="x", padx=pad, pady=(pad, scale.s(4)))
        tk.Label(d, text="会先真实点击下面这个输入框再输入文字；如果没输入或乱码，"
                         "说明 SendInput 被拦截。", bg=t.bg, fg=t.muted,
                 font=self._f_small, anchor="w", justify="left",
                 wraplength=scale.s(420)).pack(fill="x", padx=pad)
        var = tk.StringVar()
        entry = tk.Entry(d, textvariable=var, font=scale.font(12), bg=t.bg,
                         fg=t.text, insertbackground=t.text, relief="flat",
                         highlightthickness=1, highlightbackground=t.border,
                         highlightcolor=t.accent, width=42)
        entry.pack(fill="x", padx=pad, pady=(scale.s(10), scale.s(4)))
        tip = tk.Label(d, text="等待中…", bg=t.bg, fg=t.dim,
                       font=self._f_small, anchor="w")
        tip.pack(fill="x", padx=pad)
        btns = tk.Frame(d, bg=t.bg)
        btns.pack(fill="x", padx=pad, pady=(scale.s(8), pad))
        widgets.PillButton(btns, "关闭", d.destroy,
                           kind="secondary").pack(side="right")
        d.update_idletasks()
        try:
            x = self.winfo_rootx() + (self.winfo_width() - d.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - d.winfo_height()) // 3
            d.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass
        d.lift()
        d.focus_force()
        entry.focus_force()

        text = "VBHelper 输入自测 OK 123"

        def _click_then_type() -> None:
            try:
                x = entry.winfo_rootx() + entry.winfo_width() // 2
                y = entry.winfo_rooty() + entry.winfo_height() // 2
                svc.mouse_click(x=x, y=y)      # 先真实点击，强制前台焦点
            except Exception as exc:
                tip.configure(text="点击失败：%s" % exc, fg=t.danger)
                return
            d.after(350, _type)

        def _type() -> None:
            try:
                svc.type_text(text, method="unicode")
            except Exception as exc:
                tip.configure(text="输入失败：%s" % exc, fg=t.danger)
                return
            got = var.get()
            if got == text:
                tip.configure(text="成功：输入框收到完整文字。", fg=t.success)
            else:
                tip.configure(text="已发送，但输入框内容=%r。若焦点被抢走，"
                                   "文字可能到了别的窗口。" % got, fg=t.warning)

        d.after(2000, _click_then_type)

    def _rotate_token(self) -> None:
        if not widgets.confirm(
                self, "重置 MCP 令牌",
                "所有已配置 HTTP 直连的客户端都会失效，需要重新复制配置。\n"
                "stdio 客户端不受影响。确定？", danger=True):
            return
        try:
            self.runtime.rotate_token()
        except Exception as exc:
            messagebox.showerror("重置失败", str(exc), parent=self)
            return
        widgets.Toast(self.ctx.root, "令牌已重置")

    # -- 复制配置 ------------------------------------------------------

    def _stdio_json(self) -> str:
        if getattr(sys, "frozen", False):
            command = sys.executable
            args = ["--mcp-stdio"]
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            repo = os.path.dirname(os.path.dirname(os.path.dirname(here)))
            command = sys.executable
            args = [os.path.join(repo, "run.py"), "--mcp-stdio"]
        doc = {"mcpServers": {"vbhelper": {
            "command": command, "args": args}}}
        return json.dumps(doc, ensure_ascii=False, indent=2)

    def _http_json(self) -> str | None:
        st = self._status or self.runtime.status()
        if not st.get("running"):
            return None
        token = self.runtime.endpoint_info().get("token") or ""
        doc = {"mcpServers": {"vbhelper": {
            "type": "http",
            "url": st.get("url"),
            "headers": {"Authorization": "Bearer " + token}}}}
        return json.dumps(doc, ensure_ascii=False, indent=2)

    def _copy_stdio(self) -> None:
        self._copy(self._stdio_json(), "stdio 客户端配置")

    def _copy_http(self) -> None:
        text = self._http_json()
        if text is None:
            messagebox.showinfo("MCP 未启动", "先点「启动」，再用 HTTP 直连。",
                                parent=self)
            return
        self._copy(text, "HTTP 客户端配置")

    def _copy(self, text: str, label: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            widgets.Toast(self.ctx.root, "已复制%s" % label)
        except Exception as exc:
            messagebox.showerror("复制失败", str(exc), parent=self)

    # -- 审计窗口 ------------------------------------------------------

    def _open_audit(self) -> None:
        try:
            AuditWindow(self.ctx, self.runtime)
        except Exception as exc:
            messagebox.showerror("审计打开失败", str(exc), parent=self)


class AuditWindow(tk.Toplevel):
    """完整 MCP 审计（最近 500 条）。"""

    def __init__(self, ctx, runtime) -> None:
        tk.Toplevel.__init__(self, ctx.root)
        self.ctx = ctx
        self.runtime = runtime
        self.title("MCP 审计日志")
        preset, t = ctx.theme.current()
        self._t = t
        self.configure(bg=t.bg)
        self.geometry("%dx%d" % (scale.s(820), scale.s(560)))
        pad = scale.s(16)
        top = tk.Frame(self, bg=t.bg)
        top.pack(fill="x", padx=pad, pady=(pad, scale.s(6)))
        tk.Label(top, text="MCP 工具调用审计", bg=t.bg, fg=t.text,
                 font=scale.font(14, weight="bold"),
                 anchor="w").pack(side="left")
        widgets.PillButton(top, "关闭", self.destroy,
                           kind="secondary").pack(side="right")
        widgets.PillButton(top, "刷新", self._refresh,
                           kind="ghost").pack(side="right",
                                              padx=(0, scale.s(6)))
        widgets.PillButton(top, "清空", self._clear,
                           kind="ghost").pack(side="right",
                                              padx=(0, scale.s(6)))
        wrap = tk.Frame(self, bg=t.bg)
        wrap.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self._box = tk.Text(wrap, wrap="word", bg=t.bg, fg=t.text,
                            relief="flat", highlightthickness=1,
                            highlightbackground=t.border,
                            font=scale.font(10, family="Consolas"),
                            padx=scale.s(8), pady=scale.s(6))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._box.yview)
        _attach_scrollbar(vsb, self._box)
        self._box.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._box.tag_configure("time", foreground=t.dim)
        self._box.tag_configure("ok", foreground=t.success)
        self._box.tag_configure("fail", foreground=t.danger)
        self._box.tag_configure("name", foreground=t.accent)
        self._box.tag_configure("detail", foreground=t.muted)
        self._refresh()
        self.bind("<Escape>", lambda _e: self.destroy())
        try:
            from ...ui.window import (apply_dark_mode, enable_round_corners,
                                      set_caption_colors)
            hwnd = int(self.winfo_id())
            apply_dark_mode(hwnd, True)
            enable_round_corners(hwnd)
            set_caption_colors(hwnd, t.bg, t.text)
        except Exception:
            pass

    def _refresh(self) -> None:
        rows = self.runtime.ctx.audit.recent(500, scope="mcp") \
            if self.runtime.ctx.audit is not None else []
        t = self._t
        self._box.configure(state="normal")
        self._box.delete("1.0", "end")
        if not rows:
            self._box.insert("end", "暂无记录。\n")
        for row in rows:
            ts = time.strftime("%m-%d %H:%M:%S",
                               time.localtime(row.get("ts") or 0))
            ok = bool(row.get("ok"))
            self._box.insert("end", ts + "  ", "time")
            self._box.insert("end", str(row.get("action") or "?"), "name")
            self._box.insert("end", "  %s  %sms\n" % (
                "OK" if ok else "FAIL", row.get("elapsed_ms", 0)),
                "ok" if ok else "fail")
            detail = str(row.get("detail") or "").strip()
            if detail:
                if len(detail) > 600:
                    detail = detail[:600] + "…"
                self._box.insert("end", detail + "\n", "detail")
        self._box.configure(state="disabled")
        self._box.see("end")

    def _clear(self) -> None:
        if not widgets.confirm(self, "清空审计",
                               "只清空 MCP 调用记录，不影响其它数据。",
                               danger=True):
            return
        try:
            self.runtime.ctx.audit.clear(scope="mcp")
        except Exception:
            pass
        self._refresh()


def _fmt_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)
