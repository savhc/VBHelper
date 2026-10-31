"""AI 自动化确认弹窗（服务线程 -> 主线程，由 McpRuntime.confirm 调用）。

这是唯一会打断用户的 MCP 界面：鼠标/键盘等 confirm 级工具在
``confirm.mode=ask`` 时每次都要经过它；``session`` 模式由服务端缓存授权。
超时/关闭窗口都按拒绝处理。
"""
from __future__ import annotations

import tkinter as tk

from ...ui import scale, widgets
from ...ui.theme import build_preset

def ask_confirm(ctx, title: str, summary: str, *, session: bool = False) -> bool:
    """弹出确认框并等待；返回是否允许。只应在 Tk 主线程调用。"""
    root = ctx.root
    timeout = 30
    try:
        timeout = int((ctx.cfg.get("mcp") or {}).get("confirm", {})
                      .get("timeout") or 30)
    except Exception:
        pass
    try:
        tokens = ctx.theme.current()[1]
    except Exception:
        tokens = build_preset("midnight").tokens

    d = tk.Toplevel(root)
    d.title("AI 请求操作权限")
    d.configure(bg=tokens.bg)
    d.resizable(False, False)
    try:
        d.attributes("-topmost", True)
    except Exception:
        pass
    pad = scale.s(20)
    width = scale.s(470)

    head = tk.Frame(d, bg=tokens.bg)
    head.pack(fill="x", padx=pad, pady=(pad, scale.s(6)))
    tk.Label(head, text=title or "AI 请求执行操作", bg=tokens.bg,
             fg=tokens.text, font=scale.font(14, weight="bold"),
             anchor="w").pack(fill="x")
    tk.Label(d, text=summary or "（无参数）", bg=tokens.bg, fg=tokens.muted,
             font=scale.font(11), anchor="w", justify="left",
             wraplength=width, height=2).pack(fill="x", padx=pad)
    tk.Label(d, text="这会真实控制你的鼠标/键盘。确认你看到了当前屏幕，"
                     "必要时按 Ctrl+Alt+Shift+Esc 急停。",
             bg=tokens.bg, fg=tokens.warning, font=scale.font(10),
             anchor="w", justify="left",
             wraplength=width).pack(fill="x", padx=pad, pady=(scale.s(10), 0))

    btns = tk.Frame(d, bg=tokens.bg)
    btns.pack(fill="x", padx=pad, pady=(scale.s(16), scale.s(6)))
    result = {"ok": False}
    cd = tk.Label(btns, text="", bg=tokens.bg, fg=tokens.dim,
                  font=scale.font(10), anchor="w")
    cd.pack(side="left")

    def _close(ok: bool) -> None:
        result["ok"] = bool(ok)
        try:
            d.grab_release()
        except Exception:
            pass
        try:
            d.destroy()
        except Exception:
            pass

    allow_text = "允许 5 分钟" if session else "允许一次"
    widgets.PillButton(btns, "拒绝", lambda: _close(False),
                       kind="secondary").pack(side="right",
                                              padx=(scale.s(6), 0))
    widgets.PillButton(btns, allow_text, lambda: _close(True),
                       kind="primary").pack(side="right")

    state = {"left": max(5, timeout), "job": None}

    def _tick() -> None:
        state["left"] -= 1
        if state["left"] <= 0:
            _close(False)
            return
        try:
            cd.configure(text="%d 秒后自动拒绝" % state["left"])
        except tk.TclError:
            return
        state["job"] = d.after(1000, _tick)

    cd.configure(text="%d 秒后自动拒绝" % state["left"])
    state["job"] = d.after(1000, _tick)
    d.bind("<Escape>", lambda _e: _close(False))
    d.protocol("WM_DELETE_WINDOW", lambda: _close(False))
    d.update_idletasks()
    try:
        sw = d.winfo_screenwidth()
        sh = d.winfo_screenheight()
        w = max(scale.s(470), d.winfo_width())
        h = d.winfo_height()
        d.geometry("+%d+%d" % (max(0, (sw - w) // 2),
                               max(0, (sh - h) // 3)))
        hwnd = int(d.winfo_id())
        from ...ui.window import (apply_dark_mode, enable_round_corners,
                                  set_caption_colors)
        apply_dark_mode(hwnd, True)
        enable_round_corners(hwnd)
        set_caption_colors(hwnd, tokens.bg, tokens.text)
    except Exception:
        pass
    try:
        d.grab_set()
    except Exception:
        pass
    try:
        d.lift()
        d.focus_force()
    except Exception:
        pass
    try:
        d.wait_window()
    finally:
        try:
            if state.get("job"):
                d.after_cancel(state["job"])
        except Exception:
            pass
    return result["ok"]
