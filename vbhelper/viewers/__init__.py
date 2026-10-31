# -*- coding: utf-8 -*-
"""快捷修改的特殊查看器：Hex / PE / Markdown。

编辑器只负责窗口与模式切换；具体内容由这里的控件渲染。
所有解析/渲染都是纯本地、无网络，二进制查看只读。
"""
from .hexview import HexView, format_hex_row, parse_hex_pattern
from .jarview import ClassView, JarView
from .javaclass import ClassFormatError, ClassInfo, parse_class
from .pe import (PeError, PeExport, PeFile, PeImport, PeSection, looks_pe,
                 parse_pe)
from .peview import PeView

__all__ = [
    "HexView", "format_hex_row", "parse_hex_pattern",
    "PeView", "PeError", "PeExport", "PeFile", "PeImport", "PeSection",
    "looks_pe", "parse_pe",
    "JarView", "ClassView", "ClassFormatError", "ClassInfo", "parse_class",
]
