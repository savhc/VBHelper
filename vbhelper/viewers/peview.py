# -*- coding: utf-8 -*-
"""PE 结构查看器：DOS/NT 头、数据目录、节区、导入、导出。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import core
from .pe import PeError, parse


class PeView(tk.Frame):
    """只读 PE 结构树。

    ``on_goto(offset, label)``：双击节区时通知编辑器切到 Hex 并跳转。
    """

    IMPORT_CHILD_CAP = 400
    IMPORT_TOTAL_CAP = 4000
    EXPORT_CAP = 2000

    def __init__(self, parent, data: bytes, *, on_goto=None) -> None:
        super().__init__(parent, bg=core.C_BG)
        self.data = data or b""
        self.on_goto = on_goto
        self.pe = None
        self._targets: dict[str, tuple[int, str]] = {}
        try:
            self.pe = parse(self.data)
        except PeError as exc:
            self._show_error(str(exc))
            return
        except Exception as exc:      # 解析器自身的意外也要可读
            self._show_error("解析失败：%s" % exc)
            return
        self._build(self.pe)

    # ---------------- 错误态 ----------------

    def _show_error(self, message: str):
        box = tk.Frame(self, bg=core.C_BG)
        box.pack(fill="both", expand=True)
        tk.Label(box, text="不是可用的 PE 文件", bg=core.C_BG, fg=core.C_TEXT,
                 font=(core.FONT, 11, "bold")).pack(pady=(30, 6))
        tk.Label(box, text=message, bg=core.C_BG, fg=core.C_MUTED,
                 font=(core.FONT, 9), wraplength=700, justify="left").pack()

    # ---------------- 正常态 ----------------

    def _build(self, pe):
        head = tk.Frame(self, bg=core.C_BG)
        head.pack(fill="x", pady=(0, 6))
        bits = [
            ("架构", "%s (%s)" % (pe.machine_name, "64 位" if pe.is_64 else "32 位")),
            ("子系统", pe.subsystem_name),
            ("入口", "0x%X" % pe.entry_rva),
            ("镜像基址", "0x%X" % pe.image_base),
            ("大小", core.human_size(pe.size)),
        ]
        for label, value in bits:
            cell = tk.Frame(head, bg=core.C_BG)
            cell.pack(side="left", padx=(0, 18))
            tk.Label(cell, text=label, bg=core.C_BG, fg=core.C_MUTED,
                     font=(core.FONT, 8)).pack(anchor="w")
            tk.Label(cell, text=value, bg=core.C_BG, fg=core.C_TEXT,
                     font=(core.FONT, 9, "bold")).pack(anchor="w")

        wrap = tk.Frame(self, bg=core.C_BG)
        wrap.pack(fill="both", expand=True)
        try:
            st = ttk.Style()
            st.configure("PE.Treeview", background=core.C_PANEL,
                         foreground=core.C_TEXT, fieldbackground=core.C_PANEL,
                         borderwidth=0, rowheight=22,
                         font=(core.FONT, 9))
            st.map("PE.Treeview", background=[("selected", core.C_SELECT)],
                   foreground=[("selected", "#ffffff")])
        except tk.TclError:
            pass
        self.tree = ttk.Treeview(wrap, columns=("value",), show="tree headings",
                                 style="PE.Treeview")
        self.tree.heading("#0", text="项")
        self.tree.heading("value", text="值")
        self.tree.column("#0", width=330, stretch=True)
        self.tree.column("value", width=520, stretch=True)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview,
                            style="VB.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", self._on_double)

        self._fill(pe)
        if self.on_goto:
            tk.Label(self, text="双击节区可跳到 Hex 对应文件偏移", bg=core.C_BG,
                     fg=core.C_MUTED, font=(core.FONT, 8),
                     anchor="w").pack(fill="x", pady=(4, 0))

    def _add(self, parent, text, value="", *, open=False):
        return self.tree.insert(parent, "end", text=text, values=(value,),
                                open=open)

    def _fill(self, pe):
        root = ""
        summary = self._add(root, "概要", "", open=True)
        self._add(summary, "类型", "DLL" if pe.is_dll else "可执行文件")
        self._add(summary, "架构", "%s / %s" % (pe.machine_name,
                                                "PE32+" if pe.is_64 else "PE32"))
        self._add(summary, "子系统", pe.subsystem_name)
        self._add(summary, "时间戳", "%s (%d)" % (pe.timestamp_text, pe.timestamp))
        self._add(summary, "文件大小", "%s (%d 字节)" % (
            core.human_size(pe.size), pe.size))
        self._add(summary, "入口 RVA", "0x%X" % pe.entry_rva)
        self._add(summary, "镜像基址", "0x%X" % pe.image_base)
        self._add(summary, "节区 / 导入 / 导出",
                  "%d / %d / %d" % (len(pe.sections), pe.import_count,
                                    pe.export_count))

        dos = self._add(root, "DOS Header", "")
        self._add(dos, "e_magic", "MZ (0x5A4D)")
        self._add(dos, "e_lfanew", "0x%X" % pe.pe_offset)
        self._add(dos, "DOS Stub 大小", "%d 字节" % pe.dos_stub)

        nt = self._add(root, "NT Headers", "")
        self._add(nt, "Signature", "PE\\0\\0")
        self._add(nt, "Machine", "0x%04X (%s)" % (pe.machine, pe.machine_name))
        self._add(nt, "NumberOfSections", str(len(pe.sections)))
        self._add(nt, "TimeDateStamp", "%s (%d)" % (pe.timestamp_text,
                                                    pe.timestamp))
        self._add(nt, "Characteristics", "0x%04X" % pe.characteristics)
        self._add(nt, "Optional Magic", "0x%X (%s)" % (
            pe.optional_magic, "PE32+" if pe.is_64 else "PE32"))
        self._add(nt, "AddressOfEntryPoint", "0x%X" % pe.entry_rva)
        self._add(nt, "ImageBase", "0x%X" % pe.image_base)
        self._add(nt, "SectionAlignment", "0x%X" % pe.section_align)
        self._add(nt, "FileAlignment", "0x%X" % pe.file_align)
        self._add(nt, "SizeOfImage", "0x%X" % pe.size_of_image)
        self._add(nt, "SizeOfHeaders", "0x%X" % pe.size_of_headers)
        self._add(nt, "Subsystem", "0x%X (%s)" % (pe.subsystem,
                                                  pe.subsystem_name))
        self._add(nt, "DllCharacteristics", "0x%04X" % pe.dll_characteristics)
        self._add(nt, "CheckSum", "0x%X" % pe.checksum)

        dirs = self._add(root, "Data Directories",
                         "%d 项" % len(pe.data_dirs))
        for name, rva, size in pe.data_dirs:
            value = "RVA 0x%X · 大小 0x%X" % (rva, size) if (rva or size) else "—"
            self._add(dirs, name, value)

        secs = self._add(root, "Sections", "%d 个" % len(pe.sections))
        for sec in pe.sections:
            node = self._add(
                secs, sec.name or "（无名）",
                "VA 0x%X · 虚拟 0x%X · 原始 0x%X @ 0x%X" % (
                    sec.va, sec.vsize, sec.raw_size, sec.raw_ptr))
            self._add(node, "VirtualAddress", "0x%X" % sec.va)
            self._add(node, "VirtualSize", "0x%X" % sec.vsize)
            self._add(node, "PointerToRawData", "0x%X" % sec.raw_ptr)
            self._add(node, "SizeOfRawData", "0x%X" % sec.raw_size)
            self._add(node, "Characteristics", "0x%08X  %s" % (
                sec.characteristics, sec.flags))
            if sec.raw_ptr or sec.raw_size:
                self._targets[node] = (sec.raw_ptr, sec.name or "?")

        imps = self._add(root, "Imports (" + str(len(pe.imports)) + ")",
                         "%d 个函数" % pe.import_count)
        shown = 0
        for imp in pe.imports:
            if shown >= self.IMPORT_TOTAL_CAP:
                self._add(imps, "… 未显示", "导入项过多，仅列前 %d 条"
                          % self.IMPORT_TOTAL_CAP)
                break
            node = self._add(imps, imp.dll, "%d 个" % (imp.total or len(imp.funcs)))
            shown += 1
            for fn in imp.funcs[:self.IMPORT_CHILD_CAP]:
                self._add(node, fn, "")
                shown += 1
            extra = (imp.total or len(imp.funcs)) - len(imp.funcs[:self.IMPORT_CHILD_CAP])
            if extra > 0:
                self._add(node, "… 其余 %d 个未显示" % extra, "")
        if pe.import_truncated:
            self._add(imps, "（解析已截断）", "")

        exps = self._add(root, "Exports (" + str(len(pe.exports)) + ")", "")
        for exp in pe.exports[:self.EXPORT_CAP]:
            value = "序号 %d · RVA 0x%X" % (exp.ordinal, exp.rva)
            if exp.forwarder:
                value += " → " + exp.forwarder
            self._add(exps, exp.name, value)
        if pe.export_truncated or len(pe.exports) > self.EXPORT_CAP:
            self._add(exps, "（仅显示前 %d 条）" % self.EXPORT_CAP, "")

    # ---------------- 交互 ----------------

    def _on_double(self, event):
        if not self.on_goto:
            return "break"
        iid = self.tree.identify_row(event.y)
        target = self._targets.get(iid)
        if target:
            offset, label = target
            try:
                self.on_goto(offset, label)
            except Exception:
                pass
        return "break"
