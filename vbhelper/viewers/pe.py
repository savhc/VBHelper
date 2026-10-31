# -*- coding: utf-8 -*-
"""PE/COFF 解析（纯 struct，只读，不依赖 pefile）。

覆盖查看器需要的部分：DOS/NT 头、数据目录、节区、导入表、导出表。
解析失败抛 PeError，由查看器展示可读错误。
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field


class PeError(Exception):
    """不是有效 PE 或结构越界。"""


MACHINES = {
    0x014C: "x86",
    0x0166: "MIPS",
    0x01C0: "ARM",
    0x01C4: "ARMv7",
    0x01F0: "PowerPC",
    0x0200: "IA64",
    0x5032: "RISC-V 32",
    0x5064: "RISC-V 64",
    0x8664: "x64",
    0xAA64: "ARM64",
}

SUBSYSTEMS = {
    0: "未知",
    1: "Native",
    2: "Windows GUI",
    3: "Windows CUI",
    5: "OS/2 CUI",
    7: "POSIX CUI",
    9: "Windows CE",
    10: "EFI 应用",
    11: "EFI 引导",
    12: "EFI 运行时",
    13: "EFI ROM",
    14: "Xbox",
    16: "Windows 引导",
}

DATA_DIR_NAMES = (
    "导出表", "导入表", "资源表", "异常表", "安全表", "重定位表", "调试",
    "架构", "全局指针", "TLS", "加载配置", "绑定导入", "IAT", "延迟导入",
    "CLR 运行时", "保留",
)


def _need(data: bytes, off: int, size: int):
    if off < 0 or size < 0 or off + size > len(data):
        raise PeError("读取越界：0x%X + %d > 0x%X" % (off, size, len(data)))


def _u16(data: bytes, off: int) -> int:
    _need(data, off, 2)
    return struct.unpack_from("<H", data, off)[0]


def _u32(data: bytes, off: int) -> int:
    _need(data, off, 4)
    return struct.unpack_from("<I", data, off)[0]


def _u64(data: bytes, off: int) -> int:
    _need(data, off, 8)
    return struct.unpack_from("<Q", data, off)[0]


def _cstr(data: bytes, off: int, limit: int = 512) -> str:
    if off < 0 or off >= len(data):
        return ""
    end = data.find(b"\0", off, min(len(data), off + limit))
    if end < 0:
        end = min(len(data), off + limit)
    return data[off:end].decode("latin-1", "replace")


def _hex(value: int, width: int = 0) -> str:
    return ("0x%0*X" % (width, value)) if width else ("0x%X" % value)


@dataclass(frozen=True)
class PeSection:
    name: str
    va: int
    vsize: int
    raw_ptr: int
    raw_size: int
    characteristics: int

    @property
    def flags(self) -> str:
        names = []
        if self.characteristics & 0x00000020:
            names.append("CODE")
        if self.characteristics & 0x00000040:
            names.append("INIT_DATA")
        if self.characteristics & 0x00000080:
            names.append("UNINIT_DATA")
        if self.characteristics & 0x02000000:
            names.append("DISCARDABLE")
        if self.characteristics & 0x20000000:
            names.append("EXEC")
        if self.characteristics & 0x40000000:
            names.append("READ")
        if self.characteristics & 0x80000000:
            names.append("WRITE")
        return " ".join(names) or "—"


@dataclass(frozen=True)
class PeImport:
    dll: str
    funcs: list[str]
    truncated: bool = False
    total: int = 0


@dataclass(frozen=True)
class PeExport:
    name: str
    ordinal: int
    rva: int
    forwarder: str | None = None


@dataclass
class PeFile:
    size: int
    pe_offset: int
    dos_stub: int
    machine: int
    machine_name: str
    timestamp: int
    characteristics: int
    is_64: bool
    optional_magic: int
    entry_rva: int
    image_base: int
    section_align: int
    file_align: int
    size_of_image: int
    size_of_headers: int
    subsystem: int
    subsystem_name: str
    dll_characteristics: int
    checksum: int
    data_dirs: list[tuple[str, int, int]] = field(default_factory=list)
    sections: list[PeSection] = field(default_factory=list)
    imports: list[PeImport] = field(default_factory=list)
    exports: list[PeExport] = field(default_factory=list)
    import_truncated: bool = False
    export_truncated: bool = False

    @property
    def timestamp_text(self) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))
        except (OSError, OverflowError, ValueError):
            return str(self.timestamp)

    @property
    def is_dll(self) -> bool:
        return bool(self.characteristics & 0x2000)

    @property
    def import_count(self) -> int:
        return sum(i.total or len(i.funcs) for i in self.imports)

    @property
    def export_count(self) -> int:
        return len(self.exports)


def looks_pe(data: bytes) -> bool:
    """只看 DOS 头 + PE 签名，不抛异常。"""
    try:
        if len(data) < 0x40 or data[:2] != b"MZ":
            return False
        off = _u32(data, 0x3C)
        return off + 4 <= len(data) and data[off:off + 4] == b"PE\0\0"
    except Exception:
        return False


def _rva_to_off(rva: int, sections: list[PeSection], size_headers: int,
                size: int) -> int | None:
    """RVA -> 文件偏移；落在头部按 RVA 直接算，否则查节区。"""
    if rva < size_headers and rva < size:
        return rva
    for sec in sections:
        if sec.va <= rva < sec.va + max(sec.vsize, sec.raw_size):
            delta = rva - sec.va
            if delta < sec.raw_size:
                off = sec.raw_ptr + delta
                if 0 <= off < size:
                    return off
            return None
    return None


def _parse_thunks(data: bytes, thunk_rva: int, sections: list[PeSection],
                  size_headers: int, is_64: bool) -> tuple[list[str], bool, int]:
    off = _rva_to_off(thunk_rva, sections, size_headers, len(data))
    if off is None:
        return [], False, 0
    width = 8 if is_64 else 4
    flag = (1 << 63) if is_64 else (1 << 31)
    funcs: list[str] = []
    total = 0
    cap = 4000
    while True:
        if off + width > len(data):
            break
        value = _u64(data, off) if is_64 else _u32(data, off)
        off += width
        if value == 0:
            break
        total += 1
        if len(funcs) >= cap:
            continue
        if value & flag:
            funcs.append("序号 %d" % (value & 0xFFFF))
        else:
            name_off = _rva_to_off(value & 0x7FFFFFFF, sections, size_headers,
                                   len(data))
            if name_off is None:
                funcs.append(_hex(value & 0x7FFFFFFF))
            else:
                funcs.append(_cstr(data, name_off + 2, 256) or _hex(value))
    return funcs, total > len(funcs), total


def _parse_imports(data: bytes, sections: list[PeSection], size_headers: int,
                   dir_rva: int, dir_size: int, is_64: bool):
    imports: list[PeImport] = []
    if not dir_rva:
        return imports, False
    off = _rva_to_off(dir_rva, sections, size_headers, len(data))
    if off is None:
        return imports, False
    truncated = False
    for i in range(512):
        desc = off + i * 20
        if desc + 20 > len(data):
            break
        oft, _ts, _fwd, name_rva, ft = struct.unpack_from("<IIIII", data, desc)
        if not (oft or name_rva or ft):
            break
        name_off = _rva_to_off(name_rva, sections, size_headers, len(data))
        dll = _cstr(data, name_off, 256) if name_off is not None else ""
        funcs, cut, total = _parse_thunks(
            data, oft or ft, sections, size_headers, is_64)
        imports.append(PeImport(dll or "（未命名）", funcs, cut, total))
        truncated = truncated or cut
    return imports, truncated


def _parse_exports(data: bytes, sections: list[PeSection], size_headers: int,
                   dir_rva: int, dir_size: int):
    exports: list[PeExport] = []
    if not dir_rva:
        return exports, False
    off = _rva_to_off(dir_rva, sections, size_headers, len(data))
    if off is None or off + 40 > len(data):
        return exports, False
    (characteristics, ts, major, minor, name_rva, base, nfuncs, nnames,
     funcs_rva, names_rva, ords_rva) = struct.unpack_from(
        "<IIHHIIIIIII", data, off)
    if nnames == 0:
        return exports, False
    names_off = _rva_to_off(names_rva, sections, size_headers, len(data))
    ords_off = _rva_to_off(ords_rva, sections, size_headers, len(data))
    funcs_off = _rva_to_off(funcs_rva, sections, size_headers, len(data))
    if names_off is None or ords_off is None or funcs_off is None:
        return exports, False
    cap = 4000
    truncated = nnames > cap
    for i in range(min(nnames, cap)):
        try:
            name_ptr = _u32(data, names_off + i * 4)
            ordinal_index = _u16(data, ords_off + i * 2)
            func_rva = _u32(data, funcs_off + ordinal_index * 4)
        except PeError:
            break
        name_off = _rva_to_off(name_ptr, sections, size_headers, len(data))
        name = _cstr(data, name_off, 512) if name_off is not None else ""
        ordinal = base + ordinal_index
        forwarder = None
        if dir_rva <= func_rva < dir_rva + max(dir_size, 1):
            f_off = _rva_to_off(func_rva, sections, size_headers, len(data))
            if f_off is not None:
                forwarder = _cstr(data, f_off, 512) or None
        exports.append(PeExport(name or ("序号 %d" % ordinal), ordinal,
                                func_rva, forwarder))
    return exports, truncated


def parse(data: bytes) -> PeFile:
    """解析 PE；无效文件抛 PeError。"""
    if len(data) < 0x40:
        raise PeError("文件太小，不是 PE")
    if data[:2] != b"MZ":
        raise PeError("缺少 MZ 头")
    pe_off = _u32(data, 0x3C)
    if pe_off + 24 > len(data):
        raise PeError("e_lfanew 越界：0x%X" % pe_off)
    if data[pe_off:pe_off + 4] != b"PE\0\0":
        raise PeError("缺少 PE\\0\\0 签名")
    coff = pe_off + 4
    (machine, nsections, timestamp, _psym, _nsym, size_opt,
     characteristics) = struct.unpack_from("<HHIIIHH", data, coff)
    if nsections > 96:
        raise PeError("节区数量异常：%d" % nsections)
    opt = coff + 20
    _need(data, opt, size_opt)
    if size_opt < 2:
        raise PeError("可选头太小")
    magic = _u16(data, opt)
    if magic == 0x10B:
        is_64 = False
        min_opt = 0x60
    elif magic == 0x20B:
        is_64 = True
        min_opt = 0x70
    else:
        raise PeError("未知的可选头 magic：0x%X" % magic)
    if size_opt < min_opt:
        raise PeError("可选头长度不足：0x%X" % size_opt)

    entry = _u32(data, opt + 0x10)
    if is_64:
        image_base = _u64(data, opt + 0x18)
        nrva_off = 0x6C
        dir_off = 0x70
    else:
        image_base = _u32(data, opt + 0x1C)
        nrva_off = 0x5C
        dir_off = 0x60
    section_align = _u32(data, opt + 0x20)
    file_align = _u32(data, opt + 0x24)
    size_image = _u32(data, opt + 0x38)
    size_headers = _u32(data, opt + 0x3C)
    checksum = _u32(data, opt + 0x40)
    subsystem = _u16(data, opt + 0x44)
    dll_chars = _u16(data, opt + 0x46)
    nrva = _u32(data, opt + nrva_off)
    count = min(nrva, 16)

    data_dirs: list[tuple[str, int, int]] = []
    for i in range(count):
        if dir_off + i * 8 + 8 > size_opt:
            break
        rva = _u32(data, opt + dir_off + i * 8)
        size = _u32(data, opt + dir_off + i * 8 + 4)
        data_dirs.append((DATA_DIR_NAMES[i] if i < len(DATA_DIR_NAMES)
                          else "目录 %d" % i, rva, size))

    sections: list[PeSection] = []
    sec_off = opt + size_opt
    for i in range(nsections):
        off = sec_off + i * 40
        if off + 40 > len(data):
            raise PeError("节区表越界（第 %d 个）" % i)
        raw_name = data[off:off + 8].split(b"\0", 1)[0]
        name = raw_name.decode("latin-1", "replace")
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        chars = _u32(data, off + 36)
        sections.append(PeSection(name, va, vsize, raw_ptr, raw_size, chars))

    def dir_info(index):
        for name, rva, size in data_dirs:
            if name == DATA_DIR_NAMES[index]:
                return rva, size
        return 0, 0

    imp_rva, imp_size = dir_info(1)
    exp_rva, exp_size = dir_info(0)
    imports, imp_trunc = _parse_imports(data, sections, size_headers,
                                        imp_rva, imp_size, is_64)
    exports, exp_trunc = _parse_exports(data, sections, size_headers,
                                        exp_rva, exp_size)

    return PeFile(
        size=len(data), pe_offset=pe_off, dos_stub=pe_off,
        machine=machine, machine_name=MACHINES.get(machine, "0x%X" % machine),
        timestamp=timestamp, characteristics=characteristics, is_64=is_64,
        optional_magic=magic, entry_rva=entry, image_base=image_base,
        section_align=section_align, file_align=file_align,
        size_of_image=size_image, size_of_headers=size_headers,
        subsystem=subsystem,
        subsystem_name=SUBSYSTEMS.get(subsystem, "0x%X" % subsystem),
        dll_characteristics=dll_chars, checksum=checksum,
        data_dirs=data_dirs, sections=sections, imports=imports,
        exports=exports, import_truncated=imp_trunc,
        export_truncated=exp_trunc)


# 更明确的调用名（查看器和测试用）
parse_pe = parse
