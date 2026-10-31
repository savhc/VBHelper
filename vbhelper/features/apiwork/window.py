"""API 工作台窗口：API 站点管理器（cc-switch 形态）。

主列表（名称/Base URL/密钥打码/连通/余额/备注）+ 一键操作：
添加 · 编辑 · 测试连通 · 查询余额 · 应用到 Claude Code。
密钥 DPAPI 加密落盘、界面只显示打码态；保存密钥时弹风险提示。
"""
from __future__ import annotations

import json
import os
import time
import tkinter as tk
from tkinter import messagebox, ttk

from ... import core
from ...ui import scale, widgets
from ...ui.window import FeatureWindow
from . import store

_STATUS_TONE = {"ok": "#3fb27f", "bad": "#e06c6c", "unknown": ""}

class ApiWindow(FeatureWindow):
    """API 站点管理器主窗口。"""

    def __init__(self, ctx) -> None:
        self._data_path = os.path.join(ctx.paths.config_dir, "api_workbench.json")
        self._data = store.load(self._data_path)
        self._f = scale.font(11)
        self._f_small = scale.font(10)
        self._warned = False
        super().__init__(ctx, "api_workbench", "API 工作台",
                         (980, 620), min_size=(860, 520),
                         subtitle="API 站点管理：密钥 DPAPI 加密存储 · "
                                  "一键测试连通 · 余额查询")
        self._build_body()
        self.apply_theme()
        self._refresh_tree()
        self._risk_notice()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    def _build_body(self) -> None:
        body = self.content()
        self._body = body
        t = self._tokens
        bar = tk.Frame(body, bg=t.bg)
        bar.pack(fill="x")
        for text, cmd, kind in (
                ("＋添加", self._add_provider, "primary"),
                ("编辑", lambda: self._edit_provider(), "secondary"),
                ("测试", lambda: self._test_selected(), "secondary"),
                ("余额", lambda: self._balance_selected(), "secondary"),
                ("应用到 CC", self._apply_claude, "secondary"),
                ("删除", self._delete_selected, "ghost")):
            widgets.PillButton(bar, text, cmd, kind=kind).pack(
                side="left", padx=(0, scale.s(6)))

        self._tip = tk.Label(body, anchor="w", font=self._f_small,
                             justify="left", wraplength=scale.s(880), text=(
            "密钥以 Windows DPAPI 加密后存本机（列表里只显示打码态）。"
            "「应用到 CC」会改写 ~/.claude/settings.json（自动备份）。"
            "双击行 = 编辑。"))
        self._tip.pack(fill="x", pady=(scale.s(8), scale.s(2)))

        wrap = tk.Frame(body, bg=t.bg)
        wrap.pack(fill="both", expand=True, pady=(scale.s(4), 0))
        cols = ("name", "base", "key", "status", "balance", "note")
        heads = ("名称", "Base URL", "密钥", "连通", "余额", "备注")
        widths = (110, 200, 130, 56, 100, 140)
        self._tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                  selectmode="browse", style="Api.Treeview")
        for col, w, label in zip(cols, widths, heads):
            self._tree.heading(col, text=label)
            self._tree.column(col, width=scale.s(w), anchor="w",
                              stretch=(col in ("base", "note")))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self._tree.bind("<Double-1>", lambda _e: self._edit_provider())
        self._active_lbl = tk.Label(body, anchor="w", font=self._f_small)
        self._active_lbl.pack(fill="x", pady=(scale.s(6), 0))
        self.set_status("就绪")

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def _refresh_tree(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for i, p in enumerate(self._data.get("providers") or []):
            active = p.get("id") == self._data.get("active_id")
            status = p.get("status") or "unknown"
            text = {"ok": "✓", "bad": "✗"}.get(status, "—")
            self._tree.insert("", "end", iid=str(i), values=(
                ("★ " if active else "") + p.get("name", ""),
                p.get("base_url", ""),
                store.mask_key(store.key_of(p)) if p.get("key_enc") else "（未填）",
                text, p.get("balance", ""), p.get("note", "")),
                tags=("row", "st%s" % status))
        active = store.get_provider(self._data, self._data.get("active_id") or "")
        self._active_lbl.configure(text=(
            "当前站点：★ %s（%s）" % (active.get("name"), active.get("base_url"))
            if active else "当前站点：未设置（选中一行后点「编辑」里的设为当前）"))

    def _recolor(self, w, t) -> None:
        """递归给普通 tk 控件上主题色（_Themed 组件自己会跟）。"""
        try:
            cls = w.winfo_class()
            if cls in ("Frame", "TkFrame"):
                w.configure(bg=t.bg)
            elif cls in ("Label", "TLabel"):
                w.configure(bg=t.bg, fg=t.text)
        except Exception:
            pass
        for child in w.winfo_children():
            self._recolor(child, t)

    def apply_theme(self) -> None:
        super().apply_theme()
        if not hasattr(self, "_tree"):
            return                          # 外壳首次装配时业务控件还没建
        t = self._tokens
        style = ttk.Style(self)
        try:
            self.ctx.theme.apply_ttk(style)
        except Exception:
            pass
        try:
            style.configure("Api.Treeview", background=t.bg,
                            fieldbackground=t.bg, foreground=t.text,
                            borderwidth=0, relief="flat", rowheight=scale.s(24),
                            bordercolor=t.bg, lightcolor=t.bg, darkcolor=t.bg,
                            font=scale.font(11))
            style.configure("Api.Treeview.Heading", background=t.bg,
                            foreground=t.dim, relief="flat",
                            font=scale.font(10), padding=(scale.s(4), scale.s(2)))
            style.map("Api.Treeview", background=[("selected", t.selected)],
                      foreground=[("selected", t.text)])
        except Exception:
            pass
        self._recolor(self._body, t)
        try:
            # vista 基础主题下 Treeview 的行底色不吃 style.configure，
            # 只能用行级 tag 着色（选中高亮用 map 覆盖）
            self._tree.tag_configure("row", background=t.surface,
                                     foreground=t.text)
            self._tree.tag_configure("row", background=[("selected", t.selected)],
                                     foreground=[("selected", t.text)])
            self._tree.tag_configure("stok", foreground="#3fb27f")
            self._tree.tag_configure("stbad", foreground="#e06c6c")
            self._tree.tag_configure("stunknown", foreground=t.dim)
        except Exception:
            pass
        for lbl in (self._tip, self._active_lbl):
            try:
                lbl.configure(bg=t.bg, fg=t.muted)
            except Exception:
                pass

    def _selected(self) -> dict | None:
        sel = self._tree.selection()
        if not sel:
            self.set_status("先选中一行")
            return None
        return store.get_provider(self._data,
                                  (self._data.get("providers") or [{}])
                                  [int(sel[0])].get("id", ""))

    # ------------------------------------------------------------------
    # 添加 / 编辑 / 删除
    # ------------------------------------------------------------------

    def _add_provider(self) -> None:
        self._edit_dialog(None)

    def _edit_provider(self) -> None:
        self._edit_dialog(self._selected())

    def _edit_dialog(self, provider: dict | None) -> None:
        t = self._tokens
        win = tk.Toplevel(self)
        win.title("编辑站点" if provider else "添加站点")
        win.geometry("520x360")
        win.configure(bg=t.surface)
        win.transient(self)
        win.grab_set()
        rows = tk.Frame(win, bg=t.surface)
        rows.pack(fill="x", padx=16, pady=(14, 0))
        rows.columnconfigure(1, weight=1)

        def field(r, label, initial="", show=""):
            tk.Label(rows, text=label, font=self._f, anchor="e",
                     bg=t.surface, fg=t.text).grid(
                row=r, column=0, sticky="e", pady=4)
            ent = ttk.Entry(rows, font=self._f, show=show)
            ent.grid(row=r, column=1, sticky="ew", padx=(scale.s(8), 0))
            if initial:
                ent.insert(0, initial)
            return ent

        name_e = field(0, "名称", (provider or {}).get("name", ""))
        base_e = field(1, "Base URL", (provider or {}).get("base_url", ""))
        key_e = field(2, "API Key（粘贴，保存后加密显示）",
                      store.key_of(provider) if provider else "", show="•")
        model_e = field(3, "模型（可选，如 claude-sonnet-4-5）",
                        (provider or {}).get("model", ""))
        note_e = field(4, "备注", (provider or {}).get("note", ""))
        tk.Label(win, text="Base URL 填到域名即可（例：https://api.example.com），"
                           "不要带 /v1/chat/completions。",
                 font=self._f_small, anchor="w", justify="left",
                 bg=t.surface, fg=t.muted).pack(
            fill="x", padx=16, pady=(8, 0))

        def save():
            nonlocal provider     # 不写这句，下面的赋值会让 provider 变局部名，
            name = name_e.get().strip()   # 开头的 is None 判断直接 UnboundLocalError
            base = base_e.get().strip().rstrip("/")
            key = key_e.get().strip()
            if not name or not base:
                messagebox.showwarning("信息不全", "名称和 Base URL 必填。",
                                       parent=win)
                return
            if provider is None:
                provider = store.new_provider()
                self._data.setdefault("providers", []).append(provider)
            provider["name"] = name
            provider["base_url"] = base
            if key:
                try:
                    provider["key_enc"] = store.dpapi_encrypt(key)
                except OSError as e:
                    messagebox.showerror("加密失败", str(e), parent=win)
                    return
            provider["model"] = model_e.get().strip()
            provider["note"] = note_e.get().strip()
            if not self._data.get("active_id"):
                self._data["active_id"] = provider["id"]
            self._persist_refresh()
            win.destroy()
            self._risk_notice()

        widgets.PillButton(win, "保存", save, kind="primary").pack(
            side="right", padx=(0, 16), pady=12)
        widgets.PillButton(win, "取消", win.destroy,
                           kind="ghost").pack(side="right", pady=12)

    def _delete_selected(self) -> None:
        p = self._selected()
        if p is None:
            return
        if not messagebox.askyesno("删除站点",
                                   "删除「%s」？加密的密钥将一并删除。" % p.get("name"),
                                   parent=self):
            return
        self._data["providers"] = [q for q in self._data.get("providers") or []
                                   if q.get("id") != p["id"]]
        if self._data.get("active_id") == p.get("id"):
            self._data["active_id"] = ""
        self._persist_refresh()

    # ------------------------------------------------------------------
    # 测试连通 / 查余额
    # ------------------------------------------------------------------

    def _test_selected(self) -> None:
        p = self._selected()
        if p is None:
            return
        if not p.get("key_enc"):
            self.set_status("这个站点还没填 API Key")
            return
        built = store.build_probe(p)
        self.set_status("测试连通中：%s …" % built["url"])
        http = self.ctx.http
        self.ctx.tasks.submit(
            "apiwork.probe", lambda: http.request(
                built["url"], headers=built["headers"], timeout=15.0,
                allow_private=True, max_bytes=512 * 1024),
            group=self.feature_id, kind="net",
            on_done=lambda resp: self._on_probe(p, resp),
            on_error=lambda e: self._on_probe_err(p, e))

    def _on_probe(self, provider: dict, resp) -> None:
        status = resp.status
        provider["status"] = "ok" if status < 400 else "bad"
        detail = ""
        if status == 200:
            try:
                doc = json.loads(resp.text())
                models = doc.get("data") or []
                detail = "，可用模型 %d 个" % len(models) if models else ""
            except ValueError:
                pass
        elif status in (401, 403):
            provider["status"] = "bad"
            detail = "密钥无效或无权限"
        elif status == 404:
            detail = "/v1/models 不存在（服务在，但不是 OpenAI 兼容接口）"
        provider["status_text"] = "%d%s" % (status, detail)
        self._persist_refresh()
        self.set_status("测试完成：%d %s%s" % (status, _reason(status), detail))

    def _on_probe_err(self, provider: dict, exc: BaseException) -> None:
        provider["status"] = "bad"
        provider["status_text"] = str(exc)[:60]
        self._persist_refresh()
        self.set_status("连通失败：%s" % exc)

    def _balance_selected(self) -> None:
        p = self._selected()
        if p is None:
            return
        if not p.get("key_enc"):
            self.set_status("这个站点还没填 API Key")
            return
        self.set_status("查询余额中（one-api / new-api 协议）…")
        http = self.ctx.http
        queries = store.build_balance(p)
        results: list[str] = []

        def run(i: int):
            if i >= len(queries):
                self.after(0, lambda: self._on_balance_done(p, results))
                return
            q = queries[i]

            def attempt(idx: int = 0):
                if idx >= len(q["urls"]):
                    run(i + 1)
                    return
                try:
                    resp = http.request(q["urls"][idx], headers=q["headers"],
                                        timeout=12.0, allow_private=True,
                                        max_bytes=256 * 1024)
                except Exception as e:
                    attempt(idx + 1)
                    return
                if resp.status != 200:
                    attempt(idx + 1)
                    return
                try:
                    doc = json.loads(resp.text())
                except ValueError:
                    attempt(idx + 1)
                    return
                text = q["pick"](doc, resp.headers)
                if text:
                    results.append("%s：%s" % (q["label"], text))
                run(i + 1)

            attempt(0)

        run(0)

    def _on_balance_done(self, provider: dict, results: list[str]) -> None:
        text = "；".join(results) if results else "站点不支持余额查询（不是 one-api/new-api 系）"
        provider["balance"] = "；".join(results) if results else "—"
        self._persist_refresh()
        self.set_status("余额查询完成：%s" % text)

    # ------------------------------------------------------------------
    # 应用到 Claude Code
    # ------------------------------------------------------------------

    def _apply_claude(self) -> None:
        if not store.claude_settings_exists():
            messagebox.showinfo(
                "未找到 Claude Code",
                "没有找到 ~/.claude/settings.json（Claude Code 未安装或未初始化）。",
                parent=self)
            return
        p = self._selected()
        if p is None:
            return
        if not p.get("key_enc"):
            self.set_status("这个站点还没填 API Key")
            return
        masked = store.mask_key(store.key_of(p))
        if not messagebox.askyesno(
                "应用到 Claude Code",
                "把「%s」写入 ~/.claude/settings.json？\n\n"
                "  ANTHROPIC_BASE_URL = %s\n  ANTHROPIC_AUTH_TOKEN = %s\n\n"
                "原文件会自动备份。写入后重启 Claude Code 会话生效。"
                % (p.get("name"), p.get("base_url"), masked),
                parent=self):
            return
        ok, msg = store.apply_to_claude(p)
        if ok:
            self._data["active_id"] = p.get("id")
            self._persist_refresh()
            self.set_status(msg)
        else:
            messagebox.showerror("应用失败", msg, parent=self)

    # ------------------------------------------------------------------
    # 风险提示
    # ------------------------------------------------------------------

    def _risk_notice(self) -> None:
        if self._warned or not (self._data.get("providers")):
            return
        self._warned = True
        messagebox.showwarning(
            "密钥安全提示",
            "API 密钥已用 Windows DPAPI 加密后存盘，界面上也只显示打码态。\n\n"
            "但请注意：任何本机加密都无法对抗拥有你 Windows 账户权限的程序，"
            "密钥仍存在被读取的风险。\n建议：只在可信设备上使用，"
            "并定期到服务商后台更换密钥。", parent=self)

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------

    def _persist_refresh(self) -> None:
        try:
            store.save(self._data_path, self._data)
        except Exception as e:
            self.set_status("保存失败：%s" % e)
        self._refresh_tree()

    def on_theme_changed(self) -> None:
        self.apply_theme()

def _reason(status: int) -> str:
    return {200: "OK", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            429: "Too Many Requests", 500: "Server Error",
            502: "Bad Gateway", 503: "Unavailable"}.get(status, "")
