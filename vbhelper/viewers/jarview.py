# -*- coding: utf-8 -*-
"""JAR / class 查看器（Recaf 风格的只读结构视图）。

左：条目树（懒展开）+ 搜索；右：
- .class：类结构、javap 风格字节码、常量池、Hex；
- 文本资源：只读预览；
- 图片：预览；
- 其他二进制：Hex。
不反编译、不修改、不执行 JAR。
"""
from __future__ import annotations

import bz2
import gzip
import io
import lzma
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import webbrowser
import zlib
import tkinter.font as tkfont
import zipfile
from tkinter import messagebox, ttk

from .. import core
from ..ui import scale
from .decompiler import (detect_decompiler, find_java, find_java_for_recaf,
                         find_recaf)
from .hexview import HexView
from .kbox import decrypt_entry as kbox_decrypt, looks_kbox
from .javaclass import (ClassFormatError, class_listing, constant_pool_lines,
                        field_type, format_flags, method_signature, parse_class)

COLOR_OK = "#7fd1a0"
COLOR_WARN = "#e2c08d"
COLOR_BAD = "#e06c75"

TEXT_EXTS = {".java", ".kt", ".kts", ".xml", ".json", ".properties", ".mf",
             ".txt", ".md", ".yaml", ".yml", ".gradle", ".pro", ".cfg",
             ".conf", ".ini", ".html", ".htm", ".js", ".css", ".sql"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}
TEXT_ENTRY_CAP = 4 * 1024 * 1024
HEX_ENTRY_CAP = 16 * 1024 * 1024
IMAGE_ENTRY_CAP = 16 * 1024 * 1024
CLASS_ENTRY_CAP = 32 * 1024 * 1024
NESTED_EXTS = (".jar", ".war", ".ear", ".zip", ".jmod")
NESTED_MAX = 512 * 1024 * 1024


def _kind_label(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext == ".class":
        return "Class"
    if ext in IMAGE_EXTS:
        return "图片"
    if ext in TEXT_EXTS or name.upper().endswith("MANIFEST.MF"):
        return "文本"
    return ext[1:].upper() if ext else "资源"


def _ensure_styles() -> None:
    try:
        st = ttk.Style()
        st.configure("Jar.Treeview", background=core.C_PANEL,
                     foreground=core.C_TEXT, fieldbackground=core.C_PANEL,
                     borderwidth=0, rowheight=22, font=(core.FONT, 9))
        st.map("Jar.Treeview", background=[("selected", core.C_SELECT)],
               foreground=[("selected", "#ffffff")])
        st.configure("Jar.TNotebook", background=core.C_BG, borderwidth=0)
        st.configure("Jar.TNotebook.Tab", background=core.C_PANEL,
                     foreground=core.C_TEXT, padding=(10, 4))
        st.map("Jar.TNotebook.Tab",
               background=[("selected", core.C_BG)],
               foreground=[("selected", core.C_ACCENT)])
    except tk.TclError:
        pass


def _mono(widget, size: int = 9) -> tkfont.Font:
    return tkfont.Font(root=widget, family="Consolas", size=-scale.s(size))


def _text_with_scroll(parent, *, wrap="word"):
    wrap_frame = tk.Frame(parent, bg=core.C_BG)
    wrap_frame.pack(fill="both", expand=True)
    txt = tk.Text(wrap_frame, bg=core.C_BG, fg=core.C_TEXT, bd=0,
                  relief="flat", highlightthickness=0, wrap=wrap,
                  padx=scale.s(10), pady=scale.s(8),
                  insertbackground=core.C_TEXT, font=_mono(wrap_frame, 9))
    vsb = ttk.Scrollbar(wrap_frame, orient="vertical", command=txt.yview,
                        style="VB.Vertical.TScrollbar")
    txt.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)
    if wrap == "none":
        hsb = ttk.Scrollbar(wrap_frame, orient="horizontal",
                            command=txt.xview,
                            style="VB.Horizontal.TScrollbar")
        txt.configure(xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")
    return txt


def _tree_table(parent, columns, widths, height=6):
    """columns: [(key, title, anchor)] -> (frame, tree)"""
    frame = tk.Frame(parent, bg=core.C_BG)
    frame.pack(fill="both", expand=True)
    keys = [c[0] for c in columns]
    tree = ttk.Treeview(frame, columns=keys, show="headings",
                        height=height, style="Jar.Treeview")
    for key, title, anchor in columns:
        tree.heading(key, text=title)
        tree.column(key, width=widths.get(key, 120), anchor=anchor,
                    stretch=(key != "flags"))
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview,
                        style="VB.Vertical.TScrollbar")
    tree.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    return frame, tree


# ---------------------------------------------------------------------------
# 单个 class
# ---------------------------------------------------------------------------

def _try_auto_unpack(data):
    """尝试常见压缩容器 -> class；返回 (class_bytes, 标签) 或 None。"""
    try:
        size = len(data)
        if size > 64 * 1024 * 1024:
            return None
        raw = bytes(data)
    except Exception:
        return None
    head = raw[:8]
    candidates = []
    try:
        if head.startswith(bytes.fromhex("1f8b")):
            candidates.append(("gzip", gzip.decompress(raw)))
        elif head.startswith(b"BZh"):
            candidates.append(("bz2", bz2.decompress(raw)))
        elif head.startswith(b"\xfd7zXZ"):
            candidates.append(("xz", lzma.decompress(raw)))
        else:
            try:
                candidates.append(("zlib", zlib.decompress(raw)))
            except Exception:
                pass
            try:
                candidates.append(("deflate", zlib.decompress(raw, -15)))
            except Exception:
                pass
    except Exception:
        return None
    for label, out in candidates:
        if not out or len(out) > 64 * 1024 * 1024:
            continue
        off = _find_class_offset(out)
        if off < 0:
            continue
        try:
            parse_class(out[off:])
        except Exception:
            continue
        return out[off:], label
    return None


def _entropy(raw: bytes) -> float:
    if not raw:
        return 0.0
    counts = [0] * 256
    for b in raw:
        counts[b] += 1
    n = len(raw)
    return -sum((c / n) * math.log2(c / n) for c in counts if c)


def _find_class_offset(data) -> int:
    """在开头 64KB 内找 CAFEBABE：兼容混淆器/打包器加的前置头。"""
    try:
        head = bytes(data[:65536])
    except Exception:
        return -1
    return head.find(b"\xca\xfe\xba\xbe")


class ClassView(tk.Frame):
    def __init__(self, parent, data: bytes, name: str, *,
                 cfg=None, save_cfg=None, on_open_nested=None,
                 decode_note="") -> None:
        super().__init__(parent, bg=core.C_BG)
        _ensure_styles()
        self.data = data
        self.entry_name = name
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.save_cfg = save_cfg or (lambda: None)
        self.on_open_nested = on_open_nested
        self.class_offset = 0
        self.decode_note = decode_note
        self._dec_running = False
        self._dec_queue: queue.Queue = queue.Queue()
        self._decode_running = False
        self._decode_queue: queue.Queue = queue.Queue()
        self.info = None
        err = ""
        try:
            self.info = parse_class(data)
        except ClassFormatError as exc:
            err = str(exc)
        except Exception as exc:
            err = "解析失败：%s" % exc
        if self.info is None:
            alt = _find_class_offset(data)
            if alt > 0:
                try:
                    self.info = parse_class(data[alt:])
                    self.class_offset = alt
                except Exception:
                    self.info = None
            if self.info is None:
                unpacked = _try_auto_unpack(data)
                if unpacked is not None:
                    decoded, label = unpacked
                    try:
                        self.info = parse_class(decoded)
                        self.data = decoded
                        self.class_offset = 0
                        self.decode_note = "已自动解压（%s）" % label
                    except Exception:
                        self.info = None
            if self.info is None:
                self._build_fallback(err or "无法解析")
                return
        try:
            self._build()
        except Exception as exc:
            for child in self.winfo_children():
                child.destroy()
            self._build_fallback("界面构建失败：%s" % exc)

    def _refresh_members(self):
        if not hasattr(self, "ftree") or not hasattr(self, "mtree"):
            return
        hide = bool(self.hide_synth.get())
        self.ftree.delete(*self.ftree.get_children(""))
        self.mtree.delete(*self.mtree.get_children(""))
        for f in self.info.fields[:2000]:
            if hide and (f.access & 0x1000):
                continue
            self.ftree.insert("", "end", values=(
                format_flags(f.access, "field"), f.name,
                field_type(f.descriptor)))
        for m in self.info.methods[:3000]:
            if hide and (m.access & (0x1000 | 0x0040)):
                continue
            self.mtree.insert("", "end", values=(
                format_flags(m.access, "method"), m.name,
                method_signature(m.descriptor)))

    def _on_tab_changed(self, _e=None):
        try:
            frame = self.nb.nametowidget(self.nb.select())
        except (tk.TclError, KeyError):
            return
        builder = self._lazy_tabs.pop(frame, None)
        if builder is None:
            return
        try:
            builder(frame)
        except Exception as exc:
            tk.Label(frame, text="加载失败：%s" % exc, bg=core.C_BG,
                     fg=core.C_MUTED, font=(core.FONT, 9), wraplength=700,
                     justify="left").pack(pady=20)

    def _fill_bytecode(self, frame):
        txt = _text_with_scroll(frame, wrap="none")
        lines = class_listing(self.info)
        total = len(lines)
        if not lines:
            lines = ["（没有可显示的字节码）"]
        elif total > 20000:
            lines = lines[:20000] + ["… 字节码过多，已截断（共 %d 行）" % total]
        self._insert_code(txt, lines)
        txt.configure(state="disabled")

    def _fill_cp(self, frame):
        txt = _text_with_scroll(frame, wrap="none")
        lines = constant_pool_lines(self.info)
        if len(lines) > 20000:
            lines = lines[:20000] + ["… 常量池过多，已截断"]
        self._insert_cp(txt, lines)
        txt.configure(state="disabled")

    def _fill_hex(self, frame):
        HexView(frame, self.data, font=_mono(frame, 9)).pack(
            fill="both", expand=True)

    def _build_decompile(self, parent):
        self._dec_frame = parent
        bar = tk.Frame(parent, bg=core.C_BG)
        bar.pack(fill="x", pady=(4, 4))
        self.dec_btn = tk.Button(
            bar, text="运行反编译", command=self._run_decompile,
            bg=core.C_ACCENT, fg="white", activebackground=core.C_ACCENT_H,
            activeforeground="white", relief="flat", bd=0, padx=12, pady=4,
            font=(core.FONT, 9), cursor="hand2")
        self.dec_btn.pack(side="left")
        tk.Button(bar, text="配置 / 下载…", command=self._open_decompiler_config,
                  bg=core.C_PANEL, fg=core.C_TEXT,
                  activebackground=core.C_HOVER, activeforeground=core.C_TEXT,
                  relief="flat", bd=0, padx=12, pady=4,
                  font=(core.FONT, 9), cursor="hand2").pack(
            side="left", padx=(8, 0))
        self.dec_status = tk.Label(bar, text="", bg=core.C_BG,
                                   fg=core.C_MUTED, font=(core.FONT, 8),
                                   anchor="w")
        self.dec_status.pack(side="left", padx=(10, 0))
        self.dec_text = _text_with_scroll(parent, wrap="word")
        self.dec_text.configure(state="disabled")
        self._dec_update_hint()

    def _dec_command(self) -> str:
        dec = self.cfg.get("decompiler")
        if not isinstance(dec, dict):
            return ""
        return str(dec.get("command") or "").strip()

    def _dec_update_hint(self):
        try:
            self.dec_text.configure(state="normal")
            self.dec_text.delete("1.0", "end")
            cmd = self._dec_command()
            auto_label = ""
            if not cmd:
                try:
                    info = detect_decompiler(self.cfg)
                except Exception:
                    info = None
                if info is not None:
                    dec = self.cfg.get("decompiler")
                    if not isinstance(dec, dict):
                        dec = {}
                        self.cfg["decompiler"] = dec
                    dec["command"] = info.command
                    dec["auto"] = info.label
                    try:
                        self.save_cfg()
                    except Exception:
                        pass
                    cmd = info.command
                    auto_label = info.label
            if cmd:
                self.dec_text.insert(
                    "1.0",
                    "反编译命令：\n  " + cmd + "\n\n"
                    "点「运行反编译」会把当前 class 写到临时文件，"
                    "替换 {input}/{outdir} 后执行命令，并在这里显示输出。\n"
                    "命令由本机配置/自动识别，不会上传任何内容。\n")
                self.dec_status.configure(
                    text=("已自动识别：" + auto_label) if auto_label
                    else "已配置", fg=COLOR_OK)
            else:
                self.dec_text.insert(
                    "1.0",
                    "还没有配置反编译器。\n\n"
                    "点「配置 / 下载…」可以填写命令，例如：\n"
                    "  java -jar C:\\tools\\cfr.jar {input}\n\n"
                    "也提供 CFR / Vineflower / Procyon 的官方下载页按钮。\n")
                self.dec_status.configure(text="未配置", fg=COLOR_WARN)
            self.dec_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _auto_decompile_maybe(self):
        dec = self.cfg.get("decompiler")
        if isinstance(dec, dict) and dec.get("auto_run") is False:
            return
        if self._dec_running or not self._dec_command():
            return
        self._dec_auto_select = True
        self._run_decompile(auto=True)

    def _run_decompile(self, auto=False):
        self._dec_auto_select = bool(auto)
        if self._dec_running:
            return
        cmd_tpl = self._dec_command()
        if not cmd_tpl:
            self._open_decompiler_config()
            return
        if "{input}" not in cmd_tpl:
            self.dec_status.configure(text="命令缺少 {input} 占位符",
                                      fg=COLOR_WARN)
            return
        fd, tmp = tempfile.mkstemp(prefix="vbdec_", suffix=".class")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.data[self.class_offset:])
        except Exception as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self.dec_status.configure(text="写临时文件失败：%s" % exc,
                                      fg=COLOR_BAD)
            return
        cmd = cmd_tpl.replace("{input}", '"%s"' % tmp)
        outdir = None
        if "{outdir}" in cmd_tpl:
            outdir = tempfile.mkdtemp(prefix="vbdec_out_")
            cmd = cmd.replace("{outdir}", '"%s"' % outdir)
        self._dec_running = True
        try:
            self.dec_btn.configure(state="disabled")
        except tk.TclError:
            pass
        self.dec_status.configure(text="正在反编译…", fg=core.C_MUTED)
        try:
            self.dec_text.configure(state="normal")
            self.dec_text.delete("1.0", "end")
            self.dec_text.insert("1.0", "命令：\n  " + cmd + "\n\n等待输出…\n")
            self.dec_text.configure(state="disabled")
        except tk.TclError:
            pass

        def work():
            out = err = ""
            code = -1
            try:
                kwargs = {}
                if os.name == "nt":
                    kwargs["creationflags"] = 0x08000000
                proc = subprocess.run(cmd, shell=True, capture_output=True,
                                      timeout=120, **kwargs)
                code = proc.returncode
                out = (proc.stdout or b"").decode("utf-8", "replace")
                err = (proc.stderr or b"").decode("utf-8", "replace")
                if outdir and os.path.isdir(outdir):
                    parts = []
                    for root, _dirs, files in os.walk(outdir):
                        for name in sorted(files):
                            if not name.endswith(".java"):
                                continue
                            try:
                                with open(os.path.join(root, name),
                                          encoding="utf-8",
                                          errors="replace") as f:
                                    parts.append(f.read())
                            except OSError:
                                pass
                    if parts:
                        generated = (chr(10) * 2).join(parts)
                        out = generated + ((chr(10) * 2 + out) if out else "")
            except subprocess.TimeoutExpired:
                err = "反编译超时（120s）"
            except Exception as exc:
                err = "启动失败：%s" % exc
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                if outdir:
                    shutil.rmtree(outdir, ignore_errors=True)
            self._dec_queue.put((code, out, err))

        while True:
            try:
                self._dec_queue.get_nowait()
            except queue.Empty:
                break
        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._dec_poll)

    def _dec_poll(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            code, out, err = self._dec_queue.get_nowait()
        except queue.Empty:
            self.after(50, self._dec_poll)
            return
        self._dec_done(code, out, err)

    def _dec_done(self, code, out, err):
        self._dec_running = False
        try:
            self.dec_btn.configure(state="normal")
        except tk.TclError:
            return
        body = out
        if err:
            body += ("\n\n--- stderr ---\n" if body else "") + err
        if not body:
            body = "(命令没有输出)"
        try:
            self.dec_text.configure(state="normal")
            self.dec_text.delete("1.0", "end")
            self.dec_text.insert("1.0", body)
            self.dec_text.configure(state="disabled")
            self.dec_text.yview_moveto(0)
        except tk.TclError:
            return
        if code == 0:
            self.dec_status.configure(text="完成", fg=COLOR_OK)
            if getattr(self, "_dec_auto_select", False):
                try:
                    self.nb.select(self._dec_frame)
                except tk.TclError:
                    pass
        else:
            self.dec_status.configure(text="失败（退出码 %s）" % code,
                                      fg=COLOR_BAD)

    def _open_decompiler_config(self):
        win = tk.Toplevel(self.winfo_toplevel())
        win.title("配置反编译器")
        win.configure(bg=core.C_BG)
        win.geometry("780x340")
        win.resizable(True, True)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(win, text="外接反编译器", bg=core.C_BG, fg=core.C_TEXT,
                 font=(core.FONT, 11, "bold"), anchor="w").pack(
            fill="x", padx=18, pady=(14, 2))
        tk.Label(win, text="命令模板；{input} 会替换成当前 class 的临时文件路径。\n"
                           "示例：java -jar C:\\tools\\cfr.jar {input}\n"
                           "Vineflower 这类输出到目录的可用 {outdir} 占位符。",
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 8),
                 anchor="w", justify="left").pack(fill="x", padx=18)
        var = tk.StringVar(value=self._dec_command())
        entry = tk.Entry(win, textvariable=var, bg=core.C_PANEL,
                         fg=core.C_TEXT, insertbackground=core.C_TEXT,
                         relief="flat", bd=0, font=("Consolas", 9))
        entry.pack(fill="x", padx=18, pady=(10, 0), ipady=5)

        links = tk.Frame(win, bg=core.C_BG)
        links.pack(fill="x", padx=18, pady=(12, 0))
        tk.Label(links, text="下载反编译器：", bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 8)).pack(side="left")
        for name, url in (
                ("CFR", "https://github.com/leibnitz27/cfr/releases"),
                ("Vineflower", "https://github.com/Vineflower/vineflower/releases"),
                ("Procyon", "https://github.com/mstrobel/procyon/releases")):
            tk.Button(links, text=name,
                      command=lambda u=url: webbrowser.open(u),
                      bg=core.C_PANEL, fg=core.C_TEXT,
                      activebackground=core.C_HOVER,
                      activeforeground=core.C_TEXT, relief="flat", bd=0,
                      padx=10, pady=3, font=(core.FONT, 8),
                      cursor="hand2").pack(side="left", padx=(6, 0))
        auto_lab = tk.Label(win, text="", bg=core.C_BG, fg=core.C_MUTED,
                            font=(core.FONT, 8), anchor="w",
                            justify="left", wraplength=730)
        auto_lab.pack(fill="x", padx=18, pady=(6, 0))

        def auto_detect():
            try:
                info = detect_decompiler(self.cfg, refresh=True)
            except Exception:
                info = None
            if info is None:
                auto_lab.configure(
                    text="未找到可用反编译器（需要 java + CFR/Vineflower/Recaf）",
                    fg=COLOR_WARN)
                return
            var.set(info.command)
            auto_lab.configure(text="找到：" + info.label, fg=COLOR_OK)

        tk.Button(links, text="自动识别本机", command=auto_detect,
                  bg=core.C_ACCENT, fg="white",
                  activebackground=core.C_ACCENT_H, activeforeground="white",
                  relief="flat", bd=0, padx=10, pady=3,
                  font=(core.FONT, 8), cursor="hand2").pack(
            side="left", padx=(14, 0))
        tk.Label(win, text="下载后把实际路径填进命令；命令只在本机运行，"
                           "默认不会上传 class。",
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 8),
                 anchor="w").pack(fill="x", padx=18, pady=(4, 0))

        btns = tk.Frame(win, bg=core.C_BG)
        btns.pack(fill="x", padx=18, pady=(14, 12))

        def save():
            dec = self.cfg.get("decompiler")
            if not isinstance(dec, dict):
                dec = {}
                self.cfg["decompiler"] = dec
            dec["command"] = var.get().strip()
            try:
                self.save_cfg()
            except Exception:
                pass
            self._dec_update_hint()
            win.destroy()

        tk.Button(btns, text="保存", command=save, bg=core.C_ACCENT,
                  fg="white", activebackground=core.C_ACCENT_H,
                  activeforeground="white", relief="flat", bd=0,
                  padx=16, pady=4, font=(core.FONT, 9),
                  cursor="hand2").pack(side="right")
        tk.Button(btns, text="取消", command=win.destroy, bg=core.C_PANEL,
                  fg=core.C_TEXT, activebackground=core.C_HOVER,
                  activeforeground=core.C_TEXT, relief="flat", bd=0,
                  padx=16, pady=4, font=(core.FONT, 9),
                  cursor="hand2").pack(side="right", padx=(0, 8))

    def _code_tags(self, txt):
        txt.tag_configure("off", foreground=core.C_MUTED)
        txt.tag_configure("op", foreground="#c586c0")
        txt.tag_configure("ref", foreground="#9cdcfe")
        txt.tag_configure("num", foreground="#b5cea8")
        txt.tag_configure("str", foreground="#ce9178")
        txt.tag_configure("com", foreground="#6a9955")
        txt.tag_configure("hdr", foreground="#dcdcaa",
                          font=tkfont.Font(root=txt, family=core.FONT,
                                           size=-scale.s(9), weight="bold"))

    def _insert_operand(self, txt, text):
        for token in re.split(r"(\s+)", text):
            if not token:
                continue
            if token.isspace():
                txt.insert("end", token)
            elif token.startswith('"'):
                txt.insert("end", token, ("str",))
            elif re.fullmatch(r"-?\d+", token):
                txt.insert("end", token, ("num",))
            elif "/" in token or ":" in token or "." in token:
                txt.insert("end", token, ("ref",))
            else:
                txt.insert("end", token)

    def _insert_code(self, txt, lines):
        self._code_tags(txt)
        for line in lines:
            m = re.match(r"^(\s*)(\d+):(\s+)(\S+)(\s*)(.*)$", line)
            if m:
                indent, pc, sp1, op, sp2, rest = m.groups()
                txt.insert("end", indent)
                txt.insert("end", pc + ":", "off")
                txt.insert("end", sp1)
                txt.insert("end", op, "op")
                txt.insert("end", sp2)
                self._insert_operand(txt, rest)
                txt.insert("end", "\n")
                continue
            stripped = line.strip()
            if not stripped:
                txt.insert("end", "\n")
            elif stripped.startswith(("Compiled from", "minor version",
                                      "major version", "flags:",
                                      "Exception table:", "stack=")):
                txt.insert("end", line + "\n", ("com",))
            elif (re.match(r"^\s{2}[^:]*\(.*\)", line)
                  or stripped.startswith(("public class", "final class",
                                          "abstract class", "interface "))):
                txt.insert("end", line + "\n", ("hdr",))
            else:
                txt.insert("end", line + "\n")

    def _insert_cp(self, txt, lines):
        self._code_tags(txt)
        for line in lines:
            m = re.match(r"^(#\d+)(\s*=\s*)(\S+)(.*)$", line)
            if not m:
                txt.insert("end", line + "\n")
                continue
            txt.insert("end", m.group(1), "off")
            txt.insert("end", m.group(2))
            txt.insert("end", m.group(3), "op")
            rest = m.group(4)
            if "//" in rest:
                left, comment = rest.split("//", 1)
                txt.insert("end", left)
                txt.insert("end", "//" + comment, "com")
            else:
                txt.insert("end", rest)
            txt.insert("end", "\n")

    def _resize_wrap(self, event):
        width = max(240, event.width - 24)
        for lab in (getattr(self, "_title_lab", None),
                    getattr(self, "_sub_lab", None)):
            if lab is not None:
                try:
                    lab.configure(wraplength=width)
                except tk.TclError:
                    pass

    def _build_fallback(self, message: str):
        """非标准/损坏的 .class 也给可读视图：Hex + 文本 + 诊断。"""
        try:
            data = self.data
            size = len(data)
            head = bytes(data[:64]) if size else b""
            magic = head[:4]
            if head.startswith(b"PK"):
                kind = "ZIP/JAR 容器（扩展名是 .class）"
            elif head.startswith(b"MZ"):
                kind = "PE 可执行文件"
            elif head.startswith(b"\xca\xfe\xba\xbe"):
                kind = "JVM class（结构解析失败）"
            else:
                sample = head[:2048]
                printable = sum(1 for b in sample
                                if 32 <= b < 127 or b in (9, 10, 13))
                kind = ("文本/日志" if sample and printable / len(sample) > 0.85
                        else "未知二进制")
            version = ""
            if magic == b"\xca\xfe\xba\xbe" and len(data) >= 8:
                minor = int.from_bytes(data[4:6], "big")
                major = int.from_bytes(data[6:8], "big")
                version = " · class %d.%d" % (major, minor)

            tk.Label(self, text=self.entry_name or "(class)",
                     bg=core.C_BG, fg=core.C_TEXT,
                     font=(core.FONT, 12, "bold"), anchor="w",
                     justify="left", wraplength=760).pack(fill="x")
            ent = _entropy(head)
            tk.Label(self, text="%s · %s · 首字节 %s%s · 熵 %.2f"
                     % (core.human_size(size), kind,
                        magic.hex(" ").upper() if magic else "—", version,
                        ent),
                     bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 8),
                     anchor="w", justify="left", wraplength=760).pack(
                fill="x", pady=(2, 6))
            tk.Label(self, text=message, bg=core.C_BG, fg=COLOR_WARN,
                     font=(core.FONT, 8), anchor="w", justify="left",
                     wraplength=760).pack(fill="x", pady=(0, 4))
            if head.startswith(b"PK") and callable(self.on_open_nested):
                tk.Button(self, text="作为内嵌归档打开",
                          command=self._open_as_nested,
                          bg=core.C_ACCENT, fg="white",
                          activebackground=core.C_ACCENT_H,
                          activeforeground="white", relief="flat", bd=0,
                          padx=14, pady=4, font=(core.FONT, 9),
                          cursor="hand2").pack(anchor="w", pady=(0, 6))

            nb = ttk.Notebook(self, style="Jar.TNotebook")
            self.nb = nb
            nb.pack(fill="both", expand=True, pady=(4, 0))
            hx = tk.Frame(nb, bg=core.C_BG)
            nb.add(hx, text="Hex")
            HexView(hx, data, font=_mono(hx, 9)).pack(fill="both",
                                                      expand=True)
            tx = tk.Frame(nb, bg=core.C_BG)
            nb.add(tx, text="文本")
            text = self._fallback_text(data)
            if text is None:
                tk.Label(tx, text="（不是可解码的文本）", bg=core.C_BG,
                         fg=core.C_MUTED, font=(core.FONT, 9)).pack(pady=20)
            else:
                t = _text_with_scroll(tx, wrap="word")
                t.insert("1.0", text)
                t.configure(state="disabled")
            info = tk.Frame(nb, bg=core.C_BG)
            nb.add(info, text="说明")
            lines = [
                "无法按标准 JVM class 解析：",
                "  " + message,
                "",
                "这通常是混淆器/打包器处理过、加密、或扩展名伪装成 .class 的条目。",
                "仍可直接查看原始字节；若首字节是 PK，说明它其实是 ZIP/JAR，",
                "可以点上面的“作为内嵌归档打开”，或切换到 JAR/Hex 模式。",
                "",
                "首 64 字节：",
                "  " + (head.hex(" ").upper() if head else "（空）"),
                "熵：%.2f（接近 8 通常意味着加密/压缩/自定义壳）" % ent,
                "前 64KB 内 CAFEBABE：%s" % (
                    ("偏移 0x%X" % off if (off := _find_class_offset(data)) >= 0
                     else "未找到"),
                ),
            ]
            it = _text_with_scroll(info, wrap="word")
            it.insert("1.0", "\n".join(lines))
            it.configure(state="disabled")
            dtab = tk.Frame(nb, bg=core.C_BG)
            nb.add(dtab, text="解码")
            self._build_decode_tab(dtab)
        except Exception as exc:
            tk.Label(self, text="无法显示该条目：%s" % exc, bg=core.C_BG,
                     fg=COLOR_BAD, font=(core.FONT, 9),
                     wraplength=700, justify="left").pack(pady=20)

    def _open_as_nested(self):
        if callable(self.on_open_nested):
            try:
                self.on_open_nested()
            except Exception:
                pass

    @staticmethod
    def _fallback_text(data) -> str | None:
        try:
            raw = bytes(data[:1024 * 1024])
        except Exception:
            return None
        if not raw:
            return ""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return None

    def _decoder_command(self) -> str:
        dec = self.cfg.get("decoder")
        if not isinstance(dec, dict):
            return ""
        return str(dec.get("command") or "").strip()

    def _update_decode_hint(self):
        try:
            cmd = self._decoder_command()
            self.decode_cmd_lab.configure(
                text=("命令：" + cmd) if cmd
                else "未配置解码器命令（输入原始条目，输出解密后的 class）")
            self.decode_status.configure(text="", fg=core.C_MUTED)
        except tk.TclError:
            pass

    def _build_decode_tab(self, parent):
        tk.Label(parent, text="非标准 class：可先自动解压；加密/自定义壳需要你配置解码器。",
                 bg=core.C_BG, fg=core.C_TEXT, font=(core.FONT, 9),
                 anchor="w", justify="left", wraplength=700).pack(
            fill="x", padx=8, pady=(10, 4))
        tk.Label(parent, text="解码器命令用 {input}（原始条目临时文件）和 {output}（输出 class 路径）。",
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 8),
                 anchor="w", justify="left", wraplength=700).pack(
            fill="x", padx=8)
        bar = tk.Frame(parent, bg=core.C_BG)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        tk.Button(bar, text="自动解压", command=self._auto_decode,
                  bg=core.C_PANEL, fg=core.C_TEXT,
                  activebackground=core.C_HOVER,
                  activeforeground=core.C_TEXT, relief="flat", bd=0,
                  padx=12, pady=4, font=(core.FONT, 9),
                  cursor="hand2").pack(side="left")
        self.decode_btn = tk.Button(
            bar, text="运行解码器", command=self._run_decoder,
            bg=core.C_ACCENT, fg="white",
            activebackground=core.C_ACCENT_H, activeforeground="white",
            relief="flat", bd=0, padx=12, pady=4,
            font=(core.FONT, 9), cursor="hand2")
        self.decode_btn.pack(side="left", padx=(8, 0))
        tk.Button(bar, text="配置解码器…", command=self._open_decoder_config,
                  bg=core.C_PANEL, fg=core.C_TEXT,
                  activebackground=core.C_HOVER,
                  activeforeground=core.C_TEXT, relief="flat", bd=0,
                  padx=12, pady=4, font=(core.FONT, 9),
                  cursor="hand2").pack(side="left", padx=(8, 0))
        self.decode_cmd_lab = tk.Label(parent, text="", bg=core.C_BG,
                                       fg=core.C_MUTED, font=(core.FONT, 8),
                                       anchor="w", justify="left",
                                       wraplength=700)
        self.decode_cmd_lab.pack(fill="x", padx=8, pady=(4, 0))
        self.decode_status = tk.Label(parent, text="", bg=core.C_BG,
                                      fg=core.C_MUTED, font=(core.FONT, 8),
                                      anchor="w", justify="left",
                                      wraplength=700)
        self.decode_status.pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(parent, text="示例：python C:/tools/decrypt.py {input} {output}",
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 8),
                 anchor="w").pack(fill="x", padx=8, pady=(8, 0))
        self._update_decode_hint()

    def _auto_decode(self):
        res = _try_auto_unpack(self.data)
        if res is None:
            try:
                self.decode_status.configure(
                    text="未识别到 gzip/zlib/bz2/xz 压缩；可能是加密或自定义壳。",
                    fg=COLOR_WARN)
            except tk.TclError:
                pass
            return
        decoded, label = res
        self._show_decoded(decoded, "自动解压（%s）" % label)

    def _show_decoded(self, decoded, note):
        try:
            info = parse_class(decoded)
        except Exception as exc:
            try:
                self.decode_status.configure(
                    text="解码结果仍不是 class：%s" % exc, fg=COLOR_BAD)
            except tk.TclError:
                pass
            return
        for child in self.winfo_children():
            child.destroy()
        self.data = decoded
        self.class_offset = 0
        self.info = info
        self.decode_note = note
        try:
            self._build()
        except Exception as exc:
            for child in self.winfo_children():
                child.destroy()
            self._build_fallback("解码后界面构建失败：%s" % exc)

    def _open_decoder_config(self):
        win = tk.Toplevel(self.winfo_toplevel())
        win.title("配置 class 解码器")
        win.configure(bg=core.C_BG)
        win.geometry("760x250")
        win.resizable(True, True)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(win, text="自定义解码器", bg=core.C_BG, fg=core.C_TEXT,
                 font=(core.FONT, 11, "bold"), anchor="w").pack(
            fill="x", padx=18, pady=(14, 2))
        tk.Label(win, text="{input} = 原始条目临时文件；{output} = 解码后的 class 输出路径。",
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 8),
                 anchor="w").pack(fill="x", padx=18)
        tk.Label(win, text="示例：python C:/tools/decrypt.py {input} {output}",
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 8),
                 anchor="w").pack(fill="x", padx=18)
        var = tk.StringVar(value=self._decoder_command())
        tk.Entry(win, textvariable=var, bg=core.C_PANEL, fg=core.C_TEXT,
                 insertbackground=core.C_TEXT, relief="flat", bd=0,
                 font=("Consolas", 9)).pack(fill="x", padx=18,
                                            pady=(10, 0), ipady=5)
        btns = tk.Frame(win, bg=core.C_BG)
        btns.pack(fill="x", padx=18, pady=(14, 12))

        def save():
            dec = self.cfg.get("decoder")
            if not isinstance(dec, dict):
                dec = {}
                self.cfg["decoder"] = dec
            dec["command"] = var.get().strip()
            try:
                self.save_cfg()
            except Exception:
                pass
            self._update_decode_hint()
            win.destroy()

        tk.Button(btns, text="保存", command=save, bg=core.C_ACCENT,
                  fg="white", activebackground=core.C_ACCENT_H,
                  activeforeground="white", relief="flat", bd=0,
                  padx=16, pady=4, font=(core.FONT, 9),
                  cursor="hand2").pack(side="right")
        tk.Button(btns, text="取消", command=win.destroy, bg=core.C_PANEL,
                  fg=core.C_TEXT, activebackground=core.C_HOVER,
                  activeforeground=core.C_TEXT, relief="flat", bd=0,
                  padx=16, pady=4, font=(core.FONT, 9),
                  cursor="hand2").pack(side="right", padx=(0, 8))

    def _run_decoder(self):
        if self._decode_running:
            return
        cmd_tpl = self._decoder_command()
        if not cmd_tpl:
            self._open_decoder_config()
            return
        if "{input}" not in cmd_tpl or "{output}" not in cmd_tpl:
            self.decode_status.configure(
                text="解码器命令需要 {input} 和 {output} 两个占位符",
                fg=COLOR_WARN)
            return
        fd, inp = tempfile.mkstemp(prefix="vbdecode_in_", suffix=".bin")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.data)
        except Exception as exc:
            try:
                os.unlink(inp)
            except OSError:
                pass
            self.decode_status.configure(text="写临时文件失败：%s" % exc,
                                         fg=COLOR_BAD)
            return
        fd2, outp = tempfile.mkstemp(prefix="vbdecode_out_", suffix=".class")
        os.close(fd2)
        try:
            os.unlink(outp)
        except OSError:
            pass
        cmd = (cmd_tpl.replace("{input}", '"%s"' % inp)
               .replace("{output}", '"%s"' % outp))
        self._decode_running = True
        try:
            self.decode_btn.configure(state="disabled")
            self.decode_status.configure(text="正在运行解码器…",
                                         fg=core.C_MUTED)
        except tk.TclError:
            pass

        def work():
            code = -1
            out_text = err_text = ""
            data_out = None
            try:
                kwargs = {}
                if os.name == "nt":
                    kwargs["creationflags"] = 0x08000000
                proc = subprocess.run(cmd, shell=True, capture_output=True,
                                      timeout=120, **kwargs)
                code = proc.returncode
                out_text = (proc.stdout or b"").decode("utf-8", "replace")
                err_text = (proc.stderr or b"").decode("utf-8", "replace")
                if os.path.isfile(outp):
                    with open(outp, "rb") as f:
                        data_out = f.read()
            except subprocess.TimeoutExpired:
                err_text = "解码超时（120s）"
            except Exception as exc:
                err_text = "启动失败：%s" % exc
            finally:
                for p in (inp, outp):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            self._decode_queue.put((code, out_text, err_text, data_out))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._decode_poll)

    def _decode_poll(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            code, out_text, err_text, data_out = \
                self._decode_queue.get_nowait()
        except queue.Empty:
            self.after(50, self._decode_poll)
            return
        self._decode_done(code, out_text, err_text, data_out)

    def _decode_done(self, code, out_text, err_text, data_out):
        self._decode_running = False
        try:
            self.decode_btn.configure(state="normal")
        except tk.TclError:
            return
        if data_out is None:
            msg = err_text or out_text or "解码器没有生成 {output} 文件"
            self.decode_status.configure(text=msg[-500:], fg=COLOR_BAD)
            return
        off = _find_class_offset(data_out)
        if off < 0:
            unpacked = _try_auto_unpack(data_out)
            if unpacked is not None:
                self._show_decoded(
                    unpacked[0], "解码器输出自动解压（%s）" % unpacked[1])
                return
            head = data_out[:8].hex(" ").upper()
            self.decode_status.configure(
                text="输出不是 class（首字节 %s）；检查解码器是否正确。" % head,
                fg=COLOR_BAD)
            return
        self._show_decoded(data_out[off:], "自定义解码器")

    def _build(self):
        info = self.info
        title = info.this_name.replace("/", ".") or "(default package)"
        self._title_lab = tk.Label(
            self, text=title, bg=core.C_BG, fg=core.C_TEXT,
            font=(core.FONT, 13, "bold"), anchor="w",
            justify="left", wraplength=420)
        self._title_lab.pack(fill="x")
        sub = "%s · class %d.%d" % (info.flags or "(package-private)",
                                    info.major, info.minor)
        if info.super_name:
            sub += " · extends " + info.super_name.replace("/", ".")
        if info.interfaces:
            sub += " · implements " + ", ".join(
                n.replace("/", ".") for n in info.interfaces)
        self._sub_lab = tk.Label(
            self, text=sub, bg=core.C_BG, fg=core.C_MUTED,
            font=(core.FONT, 8), anchor="w", justify="left", wraplength=420)
        self._sub_lab.pack(fill="x", pady=(2, 6))
        if info.source_file:
            tk.Label(self, text="SourceFile: " + info.source_file,
                     bg=core.C_BG, fg=core.C_MUTED,
                     font=(core.FONT, 8), anchor="w").pack(fill="x")
        if getattr(self, "class_offset", 0):
            tk.Label(self, text="检测到 class 头在偏移 0x%X，已跳过前置头"
                     % self.class_offset, bg=core.C_BG, fg=core.C_MUTED,
                     font=(core.FONT, 8), anchor="w").pack(fill="x")
        if getattr(self, "decode_note", ""):
            tk.Label(self, text=self.decode_note, bg=core.C_BG,
                     fg=core.C_MUTED, font=(core.FONT, 8),
                     anchor="w").pack(fill="x")
        self.bind("<Configure>", self._resize_wrap, add="+")

        nb = ttk.Notebook(self, style="Jar.TNotebook")
        self.nb = nb
        nb.pack(fill="both", expand=True, pady=(6, 0))

        struct = tk.Frame(nb, bg=core.C_BG)
        nb.add(struct, text="结构")
        self.hide_synth = tk.BooleanVar(value=False)
        tk.Checkbutton(
            struct, text="去噪：隐藏 synthetic / bridge 成员",
            variable=self.hide_synth, command=self._refresh_members,
            bg=core.C_BG, fg=core.C_TEXT, selectcolor=core.C_PANEL,
            activebackground=core.C_BG, activeforeground=core.C_TEXT,
            font=(core.FONT, 8), anchor="w").pack(
            fill="x", padx=4, pady=(4, 2))
        tk.Label(struct, text="字段 (%d)" % len(info.fields), bg=core.C_BG,
                 fg=core.C_MUTED, font=(core.FONT, 8), anchor="w").pack(
            fill="x", pady=(4, 2))
        _, ftree = _tree_table(
            struct, [("flags", "标志", "w"), ("name", "名称", "w"),
                     ("type", "类型", "w")],
            {"flags": 110, "name": 100, "type": 200}, height=4)
        self.ftree = ftree
        fields_shown = info.fields[:2000]
        for f in fields_shown:
            ftree.insert("", "end", values=(format_flags(f.access, "field"),
                                            f.name, field_type(f.descriptor)))
        if len(info.fields) > 2000:
            tk.Label(struct, text="… 其余 %d 个字段未显示"
                     % (len(info.fields) - 2000), bg=core.C_BG,
                     fg=core.C_MUTED, font=(core.FONT, 8), anchor="w").pack(
                fill="x", padx=4)
        if not info.fields:
            tk.Label(struct, text="（这个 class 没有字段）", bg=core.C_BG,
                     fg=core.C_MUTED, font=(core.FONT, 8), anchor="w").pack(
                fill="x", padx=4)
        tk.Label(struct, text="方法 (%d)" % len(info.methods), bg=core.C_BG,
                 fg=core.C_MUTED, font=(core.FONT, 8), anchor="w").pack(
            fill="x", pady=(6, 2))
        _, mtree = _tree_table(
            struct, [("flags", "标志", "w"), ("name", "名称", "w"),
                     ("sig", "描述符", "w")],
            {"flags": 110, "name": 100, "sig": 210}, height=8)
        self.mtree = mtree
        methods_shown = info.methods[:3000]
        for m in methods_shown:
            mtree.insert("", "end", values=(
                format_flags(m.access, "method"), m.name,
                method_signature(m.descriptor)))
        if len(info.methods) > 3000:
            tk.Label(struct, text="… 其余 %d 个方法未显示"
                     % (len(info.methods) - 3000), bg=core.C_BG,
                     fg=core.C_MUTED, font=(core.FONT, 8), anchor="w").pack(
                fill="x", padx=4)
        if not info.methods:
            tk.Label(struct, text="（这个 class 没有方法）", bg=core.C_BG,
                     fg=core.C_MUTED, font=(core.FONT, 8), anchor="w").pack(
                fill="x", padx=4)

        # 重页签按需构建：大 class 不再一打开就反汇编全部方法/塞满 Text
        self._lazy_tabs = {}
        bc = tk.Frame(nb, bg=core.C_BG)
        nb.add(bc, text="字节码")
        self._lazy_tabs[bc] = self._fill_bytecode
        cp = tk.Frame(nb, bg=core.C_BG)
        nb.add(cp, text="常量池")
        self._lazy_tabs[cp] = self._fill_cp
        hx = tk.Frame(nb, bg=core.C_BG)
        nb.add(hx, text="Hex")
        self._lazy_tabs[hx] = self._fill_hex
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # 反编译（外接，用户自定义命令）
        dec = tk.Frame(nb, bg=core.C_BG)
        nb.add(dec, text="反编译")
        self._build_decompile(dec)
        try:
            self.after(160, self._auto_decompile_maybe)
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# JAR
# ---------------------------------------------------------------------------

class JarView(tk.Frame):
    def __init__(self, parent, path: str, *, cfg=None, save_cfg=None) -> None:
        super().__init__(parent, bg=core.C_BG)
        _ensure_styles()
        self.path = path
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.save_cfg = save_cfg or (lambda: None)
        self.zf: zipfile.ZipFile | None = None
        self._node_info: dict[str, tuple] = {}
        self._populated: set[str] = set()
        self._search_job = None
        self._img_ref = None
        try:
            self.zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            self._error("不是可读取的 JAR/ZIP：%s" % exc)
            return
        self._entries = [i for i in self.zf.infolist() if not i.is_dir()]
        self._tree_data = self._build_tree(self.zf.namelist())
        self._stack: list[dict] = []
        self._current_label = os.path.basename(path)
        self._current_size = os.path.getsize(path)
        self._current_temp = None
        self._nested_busy = False
        self._nested_queue: queue.Queue = queue.Queue()
        self._nested_cancel = threading.Event()
        self._kbox_cache: dict = {}
        self._open_target = ""
        self._build()

    def destroy(self):
        self._nested_cancel.set()
        while True:
            try:
                _n, tmp, zf2, _e, _s = self._nested_queue.get_nowait()
            except queue.Empty:
                break
            if zf2 is not None:
                try:
                    zf2.close()
                except Exception:
                    pass
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        self._close_archive()
        for state in self._stack:
            try:
                state["zf"].close()
            except Exception:
                pass
            temp = state.get("temp")
            if temp:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
        self._stack.clear()
        super().destroy()

    def _open_in_recaf(self):
        recaf = find_recaf(self.cfg)
        if not recaf:
            messagebox.showerror("Recaf", "未找到 recaf.jar，请在配置里指定路径。",
                                 parent=self.winfo_toplevel())
            return
        java = find_java_for_recaf(self.cfg)
        if not java:
            messagebox.showerror(
                "Recaf",
                "Recaf 4 的 Main 需要 Java 22+，本机找到的 Java 版本过低。" + chr(10) +
                "可安装 Java 22/24/25，或在 config.json 的 "
                "decompiler.recaf_java 指定 java.exe。",
                parent=self.winfo_toplevel())
            return
        path = self._current_temp or self.path
        try:
            proc = subprocess.Popen(
                [java, "-jar", recaf, "-i", path],
                cwd=os.path.dirname(path) or None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as exc:
            messagebox.showerror("Recaf", "启动失败：%s" % exc,
                                 parent=self.winfo_toplevel())
            return
        self.hint.configure(text="已启动 Recaf（Java 22+）")

        def watch():
            try:
                out, err = proc.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                return
            if proc.returncode != 0:
                text = (err or out or b"").decode("utf-8", "replace")[-1600:]
                try:
                    self.after(0, lambda: messagebox.showerror(
                        "Recaf 启动失败", text or "进程已退出",
                        parent=self.winfo_toplevel()))
                except tk.TclError:
                    pass

        threading.Thread(target=watch, daemon=True).start()

    def _close_archive(self):
        if self.zf is not None:
            try:
                self.zf.close()
            except Exception:
                pass
        self.zf = None
        if self._current_temp:
            try:
                os.unlink(self._current_temp)
            except OSError:
                pass
        self._current_temp = None

    def _error(self, message: str):
        tk.Label(self, text="无法打开归档", bg=core.C_BG, fg=core.C_TEXT,
                 font=(core.FONT, 11, "bold")).pack(pady=(24, 6))
        tk.Label(self, text=message, bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 9), wraplength=700,
                 justify="left").pack()

    # ---------------- 树 ----------------

    @staticmethod
    def _build_tree(names):
        root = {}
        for name in names:
            if not name or name.endswith("/"):
                continue
            parts = [p for p in name.split("/") if p]
            if not parts:
                continue
            node = root
            for p in parts[:-1]:
                if p in node and node[p] is None:
                    node[p] = {}
                node = node.setdefault(p, {})
            leaf = parts[-1]
            if leaf not in node:
                node[leaf] = None
        return root

    def _summary_text(self) -> str:
        manifest = self._manifest()
        classes = sum(1 for e in self._entries
                      if e.filename.lower().endswith(".class"))
        sub = "%s · %d 项 · %d 个 class" % (
            core.human_size(self._current_size or 0), len(self._entries),
            classes)
        if manifest.get("main-class"):
            sub += " · Main-Class: " + manifest["main-class"]
        version = (manifest.get("implementation-version")
                   or manifest.get("bundle-version")
                   or manifest.get("specification-version"))
        if version:
            sub += " · v" + version
        return sub

    def _update_header(self):
        labels = [state["label"] for state in self._stack]
        labels.append(self._current_label)
        try:
            self.summary_lab.configure(text=self._summary_text())
            self.crumb_lab.configure(text=" › ".join(labels))
            self.back_btn.configure(state="normal" if self._stack
                                    else "disabled")
        except tk.TclError:
            pass

    def _build(self):
        head = tk.Frame(self, bg=core.C_BG)
        head.pack(fill="x", pady=(0, 6))
        self.summary_lab = tk.Label(
            head, text=self._summary_text(), bg=core.C_BG, fg=core.C_MUTED,
            font=(core.FONT, 8), anchor="w", justify="left", wraplength=820)
        self.summary_lab.pack(fill="x")
        recaf = find_recaf(self.cfg)
        if recaf:
            tk.Button(head, text="用 Recaf 打开", command=self._open_in_recaf,
                      bg=core.C_PANEL, fg=core.C_TEXT,
                      activebackground=core.C_HOVER,
                      activeforeground=core.C_TEXT, relief="flat", bd=0,
                      padx=10, pady=2, font=(core.FONT, 8),
                      cursor="hand2").pack(side="right", pady=(2, 0))

        body = tk.Frame(self, bg=core.C_BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=core.C_BG, width=scale.s(310))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = tk.Frame(body, bg=core.C_BG)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        brow = tk.Frame(left, bg=core.C_BG)
        brow.pack(fill="x", pady=(0, 4))
        self.back_btn = tk.Button(
            brow, text="← 上级", command=self._back, bg=core.C_PANEL,
            fg=core.C_TEXT, activebackground=core.C_HOVER,
            activeforeground=core.C_TEXT, relief="flat", bd=0, padx=8,
            pady=2, font=(core.FONT, 8), cursor="hand2")
        self.back_btn.pack(side="left")
        self.crumb_lab = tk.Label(
            brow, text="", bg=core.C_BG, fg=core.C_MUTED,
            font=(core.FONT, 8), anchor="w", justify="left",
            wraplength=scale.s(190))
        self.crumb_lab.pack(side="left", fill="x", expand=True, padx=(6, 0))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._schedule_filter())
        srow = tk.Frame(left, bg=core.C_BG)
        srow.pack(fill="x", pady=(0, 4))
        tk.Label(srow, text="搜索", bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 8)).pack(side="left")
        tk.Entry(srow, textvariable=self.search_var, bg=core.C_PANEL,
                 fg=core.C_TEXT, insertbackground=core.C_TEXT, bd=0,
                 relief="flat", font=(core.FONT, 9)).pack(
            side="left", fill="x", expand=True, padx=(5, 0), ipady=2)

        self.hint = tk.Label(left, text="", bg=core.C_BG, fg=core.C_MUTED,
                             font=(core.FONT, 8), anchor="w")
        self.hint.pack(fill="x", pady=(0, 4))

        twrap = tk.Frame(left, bg=core.C_BG)
        twrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            twrap, columns=("size", "kind"), show="tree headings",
            style="Jar.Treeview")
        self.tree.heading("#0", text="条目")
        self.tree.heading("size", text="大小")
        self.tree.heading("kind", text="类型")
        self.tree.column("#0", width=220, stretch=True)
        self.tree.column("size", width=70, anchor="e", stretch=False)
        self.tree.column("kind", width=64, anchor="w", stretch=False)
        vsb = ttk.Scrollbar(twrap, orient="vertical", command=self.tree.yview,
                            style="VB.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.preview = tk.Frame(right, bg=core.C_BG)
        self.preview.pack(fill="both", expand=True)
        self._empty_preview()
        self._update_header()
        self._build_root()

    def _empty_preview(self):
        for child in self.preview.winfo_children():
            child.destroy()
        tk.Label(self.preview, text="选择左侧的 class / 资源查看",
                 bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 10)).pack(pady=40)

    def _manifest(self) -> dict:
        try:
            raw = self.zf.read("META-INF/MANIFEST.MF")
        except (KeyError, AttributeError):
            return {}
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:
            return {}
        out = {}
        for line in text.splitlines():
            if not line or line.startswith(" "):
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                out[key.strip().lower()] = value.strip()
        return out

    def _build_root(self):
        try:
            self.tree.delete(*self.tree.get_children(""))
        except tk.TclError:
            return
        self._node_info.clear()
        self._populated.clear()
        self._populate("", self._tree_data, "")
        self.hint.configure(text="%d 项" % len(self._entries))

    def _enter_archive(self, label, zf, size, temp=None):
        if self.zf is not None:
            self._stack.append({
                "label": self._current_label, "zf": self.zf,
                "entries": self._entries, "tree": self._tree_data,
                "size": self._current_size, "temp": self._current_temp,
            })
        self.zf = zf
        self._current_label = label
        self._current_size = size
        self._current_temp = temp
        self._entries = [i for i in zf.infolist() if not i.is_dir()]
        self._tree_data = self._build_tree(zf.namelist())
        self._kbox_cache.clear()
        try:
            self.search_var.set("")
        except tk.TclError:
            pass
        self._update_header()
        self._build_root()
        self._empty_preview()

    def _back(self):
        if not self._stack:
            return
        self._close_archive()
        state = self._stack.pop()
        self.zf = state["zf"]
        self._current_label = state["label"]
        self._current_size = state["size"]
        self._current_temp = state["temp"]
        self._entries = state["entries"]
        self._tree_data = state["tree"]
        self._kbox_cache.clear()
        try:
            self.search_var.set("")
        except tk.TclError:
            pass
        self._update_header()
        self._build_root()
        self._empty_preview()

    def _show_nested(self, entry):
        info = entry
        name = info.filename
        for child in self.preview.winfo_children():
            child.destroy()
        self._img_ref = None
        head = tk.Frame(self.preview, bg=core.C_BG)
        head.pack(fill="x")
        tk.Label(head, text=name, bg=core.C_BG, fg=core.C_TEXT,
                 font=(core.FONT, 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(head, text="%s · 内嵌归档" % core.human_size(info.file_size),
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 8),
                 anchor="w").pack(fill="x")
        tk.Label(self.preview,
                 text="这个 JAR 里又套了一个归档。可以打开它继续浏览 class/资源；\n"
                      "会先解压到系统临时目录，关闭查看器后删除，不修改原包。",
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 9),
                 justify="left").pack(anchor="w", pady=(16, 8))
        tk.Button(self.preview, text="打开内嵌归档",
                  command=lambda: self._open_nested(entry, info.file_size),
                  bg=core.C_ACCENT, fg="white",
                  activebackground=core.C_ACCENT_H, activeforeground="white",
                  relief="flat", bd=0, padx=16, pady=5,
                  font=(core.FONT, 9), cursor="hand2").pack(anchor="w")

    def _open_nested(self, entry, size):
        name = entry.filename if not isinstance(entry, str) else entry
        if self._nested_busy:
            return
        if size > NESTED_MAX:
            self._message(name, "内嵌归档超过 %s，暂不支持解压查看。"
                          % core.human_size(NESTED_MAX))
            return
        self._nested_busy = True
        self._nested_cancel.clear()
        while True:                     # 清掉上一次残留的结果
            try:
                self._nested_queue.get_nowait()
            except queue.Empty:
                break
        for child in self.preview.winfo_children():
            child.destroy()
        tk.Label(self.preview, text="正在解压内嵌归档…\n" + name,
                 bg=core.C_BG, fg=core.C_MUTED, font=(core.FONT, 10),
                 justify="left").pack(pady=30)
        source = self.zf

        def work():
            tmp = None
            zf2 = None
            err = None
            try:
                fd, tmp = tempfile.mkstemp(prefix="vbjar_nested_",
                                           suffix=".zip")
                total = 0
                with os.fdopen(fd, "wb") as out, source.open(entry) as src:
                    while True:
                        if self._nested_cancel.is_set():
                            raise RuntimeError("已取消")
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > NESTED_MAX:
                            raise ValueError("内嵌归档超过 %s"
                                             % core.human_size(NESTED_MAX))
                        out.write(chunk)
                if self._nested_cancel.is_set():
                    raise RuntimeError("已取消")
                zf2 = zipfile.ZipFile(tmp)
            except Exception as exc:
                err = exc
                if zf2 is not None:
                    try:
                        zf2.close()
                    except Exception:
                        pass
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                tmp = zf2 = None
            if self._nested_cancel.is_set():
                if zf2 is not None:
                    try:
                        zf2.close()
                    except Exception:
                        pass
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                return
            self._nested_queue.put((name, tmp, zf2, err, size))

        threading.Thread(target=work, daemon=True).start()
        self.after(40, self._poll_nested)

    def _poll_nested(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            name, tmp, zf2, err, size = self._nested_queue.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_nested)
            return
        self._nested_done(name, tmp, zf2, err, size)

    def _nested_done(self, name, tmp, zf2, err, size):
        self._nested_busy = False
        try:
            alive = bool(self.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:
            if zf2 is not None:
                try:
                    zf2.close()
                except Exception:
                    pass
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return
        if err is not None:
            self._message(name, "解压失败：%s" % err)
            return
        if zf2 is None:
            self._message(name, "解压失败：没有生成临时归档")
            return
        self._enter_archive(name, zf2, size, tmp)

    def _populate(self, parent, node, prefix):
        keys = sorted(node, key=lambda k: (not isinstance(node[k], dict),
                                           k.lower()))
        for key in keys:
            value = node[key]
            full = prefix + key
            if isinstance(value, dict):
                iid = self.tree.insert(parent, "end", text=key,
                                       values=("", "目录"))
                self._node_info[iid] = ("dir", value, full + "/")
                if value:
                    self.tree.insert(iid, "end", text="")
            else:
                try:
                    info = self.zf.getinfo(full)
                except KeyError:
                    continue
                iid = self.tree.insert(
                    parent, "end", text=key,
                    values=(core.human_size(info.file_size), _kind_label(full)))
                self._node_info[iid] = ("file", info)

    def _on_click(self, event):
        # <<TreeviewOpen>> 不一定把 focus 放在刚展开的目录上，记录点击项
        row = self.tree.identify_row(event.y)
        if row:
            self._open_target = row

    def _on_open(self, _e=None):
        iid = self._open_target or self.tree.focus()
        self._open_target = ""
        info = self._node_info.get(iid)
        if not info or info[0] != "dir" or iid in self._populated:
            return
        self.tree.delete(*self.tree.get_children(iid))
        self._populate(iid, info[1], info[2])
        self._populated.add(iid)

    # ---------------- 搜索 ----------------

    def _schedule_filter(self):
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except tk.TclError:
                pass
        self._search_job = self.after(260, self._apply_filter)

    def _apply_filter(self):
        self._search_job = None
        query = self.search_var.get().strip().lower()
        if not query:
            self._build_root()
            return
        hits = [e for e in self._entries
                if query in e.filename.lower()]
        cap = 1000
        self.tree.delete(*self.tree.get_children(""))
        self._node_info.clear()
        self._populated.clear()
        for info in hits[:cap]:
            name = info.filename
            iid = self.tree.insert(
                "", "end", text=name,
                values=(core.human_size(info.file_size), _kind_label(name)))
            self._node_info[iid] = ("file", info)
        more = "" if len(hits) <= cap else "（前 %d / %d）" % (cap, len(hits))
        self.hint.configure(text="%d 个匹配%s" % (len(hits), more))

    # ---------------- 预览 ----------------

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        info = self._node_info.get(sel[0])
        if not info or info[0] != "file":
            return
        self._show_entry(info[1])

    def _read_entry(self, entry, cap: int) -> tuple[bytes, bool]:
        if isinstance(entry, str):
            entry = self.zf.getinfo(entry)
        with self.zf.open(entry) as f:
            data = f.read(cap + 1)
        return data[:cap], len(data) > cap

    def _show_entry(self, entry):
        for child in self.preview.winfo_children():
            child.destroy()
        self._img_ref = None
        if isinstance(entry, str):
            try:
                entry = self.zf.getinfo(entry)
            except KeyError:
                return
        info = entry
        name = info.filename
        low = name.lower()
        if low.endswith(".class"):
            if info.file_size > CLASS_ENTRY_CAP:
                self._message(name, "class 文件超过 %s，暂不解析。"
                              % core.human_size(CLASS_ENTRY_CAP))
                return
            try:
                data = self.zf.read(info)
            except Exception as exc:
                self._message(name, "读取失败：%s" % exc)
                return
            kbox_note = ""
            if looks_kbox(data):
                key = (id(self.zf), info.header_offset, info.filename)
                decoded = self._kbox_cache.get(key)
                if decoded is None:
                    try:
                        decoded = kbox_decrypt(self.zf, data)
                    except Exception:
                        decoded = None
                    if decoded is not None:
                        self._kbox_cache[key] = decoded
                if decoded is not None:
                    data = decoded
                    kbox_note = "KBox 解密（class-seed.bin）"
            try:
                view = ClassView(
                    self.preview, data, name, cfg=self.cfg,
                    save_cfg=self.save_cfg,
                    on_open_nested=lambda: self._open_nested(
                        info, info.file_size),
                    decode_note=kbox_note)
            except Exception as exc:
                self._message(name, "Class 视图构建失败：%s" % exc)
                return
            view.pack(fill="both", expand=True)
            return
        if low.endswith(NESTED_EXTS):
            self._show_nested(info)
            return
        ext = os.path.splitext(low)[1]
        if ext in TEXT_EXTS or low.endswith("manifest.mf"):
            try:
                raw, _trunc = self._read_entry(info, TEXT_ENTRY_CAP)
            except Exception as exc:
                self._message(name, "读取条目失败：%s" % exc)
                return
            text = self._decode_text(raw)
            self._show_text(name, info, text, raw, len(raw) >= TEXT_ENTRY_CAP)
            return
        if ext in IMAGE_EXTS:
            if info.file_size > IMAGE_ENTRY_CAP:
                self._message(name, "图片超过 %s，暂不预览。"
                              % core.human_size(IMAGE_ENTRY_CAP))
                return
            try:
                data = self.zf.read(info)
                from PIL import Image, ImageTk
                img = Image.open(io.BytesIO(data))
                img.thumbnail((820, 560))
                self._img_ref = ImageTk.PhotoImage(img)
                head = tk.Frame(self.preview, bg=core.C_BG)
                head.pack(fill="x")
                tk.Label(head, text=name, bg=core.C_BG, fg=core.C_TEXT,
                         font=(core.FONT, 10, "bold"), anchor="w").pack(
                    fill="x")
                tk.Label(head, text="%s · %s · %dx%d"
                         % (core.human_size(info.file_size), img.format or "",
                            img.width, img.height),
                         bg=core.C_BG, fg=core.C_MUTED,
                         font=(core.FONT, 8), anchor="w").pack(fill="x")
                tk.Label(self.preview, image=self._img_ref, bg=core.C_BG).pack(
                    pady=10)
                return
            except Exception:
                pass
        try:
            data, truncated = self._read_entry(info, HEX_ENTRY_CAP)
        except Exception as exc:
            self._message(name, "读取条目失败：%s" % exc)
            return
        head = tk.Frame(self.preview, bg=core.C_BG)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text=name, bg=core.C_BG, fg=core.C_TEXT,
                 font=(core.FONT, 10, "bold"), anchor="w").pack(fill="x")
        note = "%s · %s" % (core.human_size(info.file_size),
                            _kind_label(name))
        if truncated:
            note += " · 仅显示前 %s" % core.human_size(HEX_ENTRY_CAP)
        tk.Label(head, text=note, bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 8), anchor="w").pack(fill="x")
        HexView(self.preview, data, font=_mono(self.preview, 9)).pack(
            fill="both", expand=True)

    def _decode_text(self, raw: bytes) -> str:
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    def _show_text(self, name, info, text, raw, truncated):
        head = tk.Frame(self.preview, bg=core.C_BG)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text=name, bg=core.C_BG, fg=core.C_TEXT,
                 font=(core.FONT, 10, "bold"), anchor="w").pack(fill="x")
        note = "%s · %s" % (core.human_size(info.file_size),
                            _kind_label(name))
        if truncated:
            note += " · 仅显示前 %s" % core.human_size(TEXT_ENTRY_CAP)
        tk.Label(head, text=note, bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 8), anchor="w").pack(fill="x")
        txt = _text_with_scroll(self.preview, wrap="word")
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        txt.yview_moveto(0)

    def _message(self, name, message):
        for child in self.preview.winfo_children():
            child.destroy()
        tk.Label(self.preview, text=name, bg=core.C_BG, fg=core.C_TEXT,
                 font=(core.FONT, 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(self.preview, text=message, bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 9), wraplength=700,
                 justify="left").pack(pady=20)
