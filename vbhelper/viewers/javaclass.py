# -*- coding: utf-8 -*-
"""Java .class 解析 + JVM 字节码反汇编（纯标准库）。

JAR 查看器用：类名/父类/接口/字段/方法/常量池/Code 属性，并把字节码
反汇编成 javap -c 风格。不依赖 JDK、CFR 或任何第三方库。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


class ClassFormatError(Exception):
    """class 文件结构不合法。"""


# ---------------------------------------------------------------------------
# 常量池
# ---------------------------------------------------------------------------

CP_NAMES = {
    1: "Utf8", 3: "Integer", 4: "Float", 5: "Long", 6: "Double",
    7: "Class", 8: "String", 9: "Fieldref", 10: "Methodref",
    11: "InterfaceMethodref", 12: "NameAndType", 15: "MethodHandle",
    16: "MethodType", 17: "Dynamic", 18: "InvokeDynamic",
    19: "Module", 20: "Package",
}

MH_KINDS = {
    1: "getField", 2: "getStatic", 3: "putField", 4: "putStatic",
    5: "invokeVirtual", 6: "invokeStatic", 7: "invokeSpecial",
    8: "newInvokeSpecial", 9: "invokeInterface",
}


def _mutf8(raw: bytes) -> str:
    # Java 的 modified UTF-8 用 C0 80 表示 NUL；其余按 UTF-8 解
    return raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "replace")


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class CPEntry:
    tag: int
    value: object


class ConstantPool:
    def __init__(self, entries: list[CPEntry | None]) -> None:
        self.entries = entries

    def entry(self, index: int) -> CPEntry:
        if not (0 < index < len(self.entries)) or self.entries[index] is None:
            raise ClassFormatError("常量池索引越界：#%d" % index)
        return self.entries[index]

    def utf(self, index: int) -> str:
        e = self.entry(index)
        if e.tag != 1:
            raise ClassFormatError("#%d 不是 Utf8" % index)
        return str(e.value)

    def class_name(self, index: int) -> str:
        if not index:
            return ""
        e = self.entry(index)
        if e.tag != 7:
            raise ClassFormatError("#%d 不是 Class" % index)
        return self.utf(int(e.value))

    def name_and_type(self, index: int) -> tuple[str, str]:
        e = self.entry(index)
        if e.tag != 12:
            raise ClassFormatError("#%d 不是 NameAndType" % index)
        name_i, desc_i = e.value
        return self.utf(int(name_i)), self.utf(int(desc_i))

    def ref(self, index: int) -> str:
        e = self.entry(index)
        if e.tag in (9, 10, 11):
            class_i, nat_i = e.value
            cls = self.class_name(int(class_i))
            name, desc = self.name_and_type(int(nat_i))
            return "%s.%s:%s" % (cls, name, desc)
        if e.tag == 7:
            return self.class_name(index)
        if e.tag == 8:
            return _quote(self.utf(int(e.value)))
        if e.tag == 1:
            return _quote(str(e.value))
        if e.tag == 15:
            kind, ref_i = e.value
            return "%s %s" % (MH_KINDS.get(int(kind), "handle"), self.ref(int(ref_i)))
        if e.tag == 16:
            return self.utf(int(e.value)) + " (method type)"
        if e.tag in (17, 18):
            bsm, nat_i = e.value
            name, desc = self.name_and_type(int(nat_i))
            return "bootstrap#%d %s:%s" % (int(bsm), name, desc)
        return "#%d" % index

    def ldc(self, index: int) -> str:
        e = self.entry(index)
        if e.tag == 3:
            return str(struct.unpack(">i", struct.pack(">I", int(e.value)))[0])
        if e.tag == 4:
            return repr(e.value)
        if e.tag == 5:
            return str(e.value) + "L"
        if e.tag == 6:
            return repr(e.value) + "D"
        if e.tag == 8:
            return _quote(self.utf(int(e.value)))
        return self.ref(index)

    def describe(self, index: int) -> str:
        e = self.entry(index)
        tag = CP_NAMES.get(e.tag, "Tag%d" % e.tag)
        if e.tag == 1:
            return '%s %s' % (tag, _quote(str(e.value)))
        if e.tag == 3:
            return "%s %d" % (tag, struct.unpack(">i", struct.pack(">I", int(e.value)))[0])
        if e.tag == 4:
            return "%s %r" % (tag, e.value)
        if e.tag in (5, 6):
            return "%s %r" % (tag, e.value)
        if e.tag == 7:
            return "Class #%d  // %s" % (int(e.value), self.class_name(index))
        if e.tag == 8:
            return "String #%d  // %s" % (int(e.value), _quote(self.utf(int(e.value))))
        if e.tag in (9, 10, 11):
            return "%s // %s" % (tag, self.ref(index))
        if e.tag == 12:
            name, desc = self.name_and_type(index)
            return "NameAndType %s:%s" % (name, desc)
        if e.tag == 15:
            return "MethodHandle // %s" % self.ref(index)
        if e.tag == 16:
            return "MethodType // %s" % self.utf(int(e.value))
        if e.tag in (17, 18):
            return "%s // %s" % (tag, self.ref(index))
        if e.tag in (19, 20):
            return "%s // %s" % (tag, self.utf(int(e.value)))
        return "%s #%r" % (tag, e.value)


def _read_constant_pool(r: "_Reader") -> ConstantPool:
    count = r.u2()
    entries: list[CPEntry | None] = [None] * count
    i = 1
    while i < count:
        tag = r.u1()
        slots = 1
        if tag == 1:
            raw = r.take(r.u2())
            value = _mutf8(raw)
        elif tag == 3:
            value = r.u4()
        elif tag == 4:
            value = struct.unpack(">f", r.take(4))[0]
        elif tag == 5:
            value = struct.unpack(">q", r.take(8))[0]
            slots = 2
        elif tag == 6:
            value = struct.unpack(">d", r.take(8))[0]
            slots = 2
        elif tag in (7, 8, 16, 19, 20):
            value = r.u2()
        elif tag in (9, 10, 11, 12, 17, 18):
            value = (r.u2(), r.u2())
        elif tag == 15:
            value = (r.u1(), r.u2())
        else:
            raise ClassFormatError("未知常量池 tag：%d（#%d）" % (tag, i))
        entries[i] = CPEntry(tag, value)
        i += slots
        if slots == 2:
            entries[i - 1] = None       # long/double 占两个槽
    return ConstantPool(entries)


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------

@dataclass
class Attr:
    name: str
    data: bytes


@dataclass
class FieldInfo:
    access: int
    name: str
    descriptor: str
    attributes: list[Attr] = field(default_factory=list)


@dataclass
class CodeInfo:
    max_stack: int
    max_locals: int
    code: bytes
    exceptions: list[tuple[int, int, int, str]] = field(default_factory=list)
    attributes: list[Attr] = field(default_factory=list)


@dataclass
class MethodInfo:
    access: int
    name: str
    descriptor: str
    attributes: list[Attr] = field(default_factory=list)
    code: CodeInfo | None = None


@dataclass
class ClassInfo:
    minor: int
    major: int
    access: int
    this_name: str
    super_name: str
    interfaces: list[str]
    fields: list[FieldInfo]
    methods: list[MethodInfo]
    attributes: list[Attr]
    pool: ConstantPool

    @property
    def flags(self) -> str:
        return format_flags(self.access, "class")

    @property
    def source_file(self) -> str:
        for attr in self.attributes:
            if attr.name == "SourceFile" and len(attr.data) >= 2:
                try:
                    return self.pool.utf(struct.unpack_from(">H", attr.data, 0)[0])
                except Exception:
                    return ""
        return ""


class _Reader:
    __slots__ = ("data", "pos", "size")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.size = len(data)

    def u1(self) -> int:
        if self.pos + 1 > self.size:
            raise ClassFormatError("读取越界")
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u2(self) -> int:
        if self.pos + 2 > self.size:
            raise ClassFormatError("读取越界")
        v = struct.unpack_from(">H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def u4(self) -> int:
        if self.pos + 4 > self.size:
            raise ClassFormatError("读取越界")
        v = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > self.size:
            raise ClassFormatError("读取越界")
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v


def _read_attributes(r: _Reader, pool: ConstantPool) -> list[Attr]:
    out = []
    for _ in range(r.u2()):
        name = pool.utf(r.u2())
        out.append(Attr(name, r.take(r.u4())))
    return out


def _parse_code(attr: Attr, pool: ConstantPool) -> CodeInfo:
    r = _Reader(attr.data)
    max_stack, max_locals = r.u2(), r.u2()
    code = r.take(r.u4())
    exceptions = []
    for _ in range(r.u2()):
        start, end, handler = r.u2(), r.u2(), r.u2()
        type_i = r.u2()
        exceptions.append((start, end, handler,
                           pool.class_name(type_i) if type_i else "any"))
    return CodeInfo(max_stack, max_locals, code, exceptions,
                    _read_attributes(r, pool))


# ---------------------------------------------------------------------------
# 访问标志 / 描述符
# ---------------------------------------------------------------------------

_CLASS_FLAGS = ((0x0001, "public"), (0x0010, "final"),
                (0x0200, "interface"), (0x0400, "abstract"),
                (0x1000, "synthetic"), (0x2000, "annotation"),
                (0x4000, "enum"), (0x8000, "module"))
_FIELD_FLAGS = ((0x0001, "public"), (0x0002, "private"), (0x0004, "protected"),
                (0x0008, "static"), (0x0010, "final"), (0x0040, "volatile"),
                (0x0080, "transient"), (0x1000, "synthetic"),
                (0x4000, "enum"))
_METHOD_FLAGS = ((0x0001, "public"), (0x0002, "private"), (0x0004, "protected"),
                 (0x0008, "static"), (0x0010, "final"), (0x0020, "synchronized"),
                 (0x0040, "bridge"), (0x0080, "varargs"), (0x0100, "native"),
                 (0x0400, "abstract"), (0x0800, "strict"),
                 (0x1000, "synthetic"))


def format_flags(value: int, kind: str) -> str:
    table = {"class": _CLASS_FLAGS, "field": _FIELD_FLAGS,
             "method": _METHOD_FLAGS}.get(kind, ())
    return " ".join(name for bit, name in table if value & bit)


_PRIM = {"B": "byte", "C": "char", "D": "double", "F": "float",
         "I": "int", "J": "long", "S": "short", "Z": "boolean", "V": "void"}


def _type_at(desc: str, i: int) -> tuple[str, int]:
    if i >= len(desc):
        raise ClassFormatError("描述符不完整：%r" % desc)
    c = desc[i]
    if c == "[":
        inner, j = _type_at(desc, i + 1)
        return inner + "[]", j
    if c == "L":
        end = desc.find(";", i)
        if end < 0:
            raise ClassFormatError("描述符不完整：%r" % desc)
        return desc[i + 1:end].replace("/", "."), end + 1
    return _PRIM.get(c, c), i + 1


def field_type(desc: str) -> str:
    try:
        return _type_at(desc, 0)[0]
    except Exception:
        return desc


def method_signature(desc: str) -> str:
    try:
        if not desc.startswith("("):
            return desc
        i, params = 1, []
        while i < len(desc) and desc[i] != ")":
            t, i = _type_at(desc, i)
            params.append(t)
        ret = "?"
        if i + 1 < len(desc):
            ret, _ = _type_at(desc, i + 1)
        return "(%s) %s" % (", ".join(params), ret)
    except Exception:
        return desc


# ---------------------------------------------------------------------------
# JVM 字节码
# ---------------------------------------------------------------------------

_OPS: dict[int, tuple[str, str]] = {}


def _ops(start: int, names: list[str], fmt: str = "none") -> None:
    for i, name in enumerate(names):
        _OPS[start + i] = (name, fmt)


_ops(0x00, ["nop", "aconst_null", "iconst_m1", "iconst_0", "iconst_1",
            "iconst_2", "iconst_3", "iconst_4", "iconst_5", "lconst_0",
            "lconst_1", "fconst_0", "fconst_1", "fconst_2", "dconst_0",
            "dconst_1"])
_OPS[0x10] = ("bipush", "s1")
_OPS[0x11] = ("sipush", "s2")
_OPS[0x12] = ("ldc", "cp1")
_OPS[0x13] = ("ldc_w", "cp2")
_OPS[0x14] = ("ldc2_w", "cp2")
_ops(0x15, ["iload", "lload", "fload", "dload", "aload"], "local")
_ops(0x1a, ["iload_0", "iload_1", "iload_2", "iload_3", "lload_0", "lload_1",
            "lload_2", "lload_3", "fload_0", "fload_1", "fload_2", "fload_3",
            "dload_0", "dload_1", "dload_2", "dload_3", "aload_0", "aload_1",
            "aload_2", "aload_3"])
_ops(0x2e, ["iaload", "laload", "faload", "daload", "aaload", "baload",
            "caload", "saload"])
_ops(0x36, ["istore", "lstore", "fstore", "dstore", "astore"], "local")
_ops(0x3b, ["istore_0", "istore_1", "istore_2", "istore_3", "lstore_0",
            "lstore_1", "lstore_2", "lstore_3", "fstore_0", "fstore_1",
            "fstore_2", "fstore_3", "dstore_0", "dstore_1", "dstore_2",
            "dstore_3", "astore_0", "astore_1", "astore_2", "astore_3"])
_ops(0x4f, ["iastore", "lastore", "fastore", "dastore", "aastore", "bastore",
            "castore", "sastore"])
_ops(0x57, ["pop", "pop2", "dup", "dup_x1", "dup_x2", "dup2", "dup2_x1",
            "dup2_x2", "swap"])
_ops(0x60, ["iadd", "ladd", "fadd", "dadd", "isub", "lsub", "fsub", "dsub",
            "imul", "lmul", "fmul", "dmul", "idiv", "ldiv", "fdiv", "ddiv",
            "irem", "lrem", "frem", "drem", "ineg", "lneg", "fneg", "dneg",
            "ishl", "lshl", "ishr", "lshr", "iushr", "lushr", "iand", "land",
            "ior", "lor", "ixor", "lxor"])
_OPS[0x84] = ("iinc", "iinc")
_ops(0x85, ["i2l", "i2f", "i2d", "l2i", "l2f", "l2d", "f2i", "f2l", "f2d",
            "d2i", "d2l", "d2f", "i2b", "i2c", "i2s"])
_ops(0x94, ["lcmp", "fcmpl", "fcmpg", "dcmpl", "dcmpg"])
_ops(0x99, ["ifeq", "ifne", "iflt", "ifge", "ifgt", "ifle"], "branch")
_ops(0x9f, ["if_icmpeq", "if_icmpne", "if_icmplt", "if_icmpge", "if_icmpgt",
            "if_icmple", "if_acmpeq", "if_acmpne"], "branch")
_OPS[0xa7] = ("goto", "branch")
_OPS[0xa8] = ("jsr", "branch")
_OPS[0xa9] = ("ret", "local")
_OPS[0xaa] = ("tableswitch", "tableswitch")
_OPS[0xab] = ("lookupswitch", "lookupswitch")
_ops(0xac, ["ireturn", "lreturn", "freturn", "dreturn", "areturn", "return"])
_ops(0xb2, ["getstatic", "putstatic", "getfield", "putfield"], "cp2")
_ops(0xb6, ["invokevirtual", "invokespecial", "invokestatic"], "cp2")
_OPS[0xb9] = ("invokeinterface", "invokeinterface")
_OPS[0xba] = ("invokedynamic", "invokedynamic")
_OPS[0xbb] = ("new", "cp2")
_OPS[0xbc] = ("newarray", "newarray")
_OPS[0xbd] = ("anewarray", "cp2")
_OPS[0xbe] = ("arraylength", "none")
_OPS[0xbf] = ("athrow", "none")
_OPS[0xc0] = ("checkcast", "cp2")
_OPS[0xc1] = ("instanceof", "cp2")
_OPS[0xc2] = ("monitorenter", "none")
_OPS[0xc3] = ("monitorexit", "none")
_OPS[0xc4] = ("wide", "wide")
_OPS[0xc5] = ("multianewarray", "multianewarray")
_OPS[0xc6] = ("ifnull", "branch")
_OPS[0xc7] = ("ifnonnull", "branch")
_OPS[0xc8] = ("goto_w", "branch4")
_OPS[0xc9] = ("jsr_w", "branch4")

_NEWARRAY = {4: "boolean", 5: "char", 6: "float", 7: "double",
             8: "byte", 9: "short", 10: "int", 11: "long"}


def disassemble(code: bytes, pool: ConstantPool) -> list[str]:
    """javap -c 风格；格式错误的指令标 <bad> 后停止，不抛异常。"""
    out: list[str] = []
    pc, n = 0, len(code)
    while pc < n:
        start = pc
        op = code[pc]
        pc += 1
        name, fmt = _OPS.get(op, ("op_%02X" % op, "none"))
        operands = ""
        try:
            if fmt == "s1":
                operands = str(struct.unpack(">b", bytes([code[pc]]))[0])
                pc += 1
            elif fmt == "s2":
                operands = str(struct.unpack_from(">h", code, pc)[0])
                pc += 2
            elif fmt == "local":
                operands = "#%d" % code[pc]
                pc += 1
            elif fmt == "cp1":
                idx = code[pc]
                pc += 1
                operands = pool.ldc(idx) if op == 0x12 else "#%d" % idx
            elif fmt == "cp2":
                idx = struct.unpack_from(">H", code, pc)[0]
                pc += 2
                if op in (0xb2, 0xb3, 0xb4, 0xb5, 0xb6, 0xb7, 0xb8):
                    operands = pool.ref(idx)
                elif op in (0x13, 0x14):
                    operands = pool.ldc(idx)
                elif op in (0xbb, 0xbd, 0xc0, 0xc1):
                    operands = pool.class_name(idx)
                else:
                    operands = "#%d" % idx
            elif fmt == "branch":
                off = struct.unpack_from(">h", code, pc)[0]
                pc += 2
                operands = "-> %d" % (start + off)
            elif fmt == "branch4":
                off = struct.unpack_from(">i", code, pc)[0]
                pc += 4
                operands = "-> %d" % (start + off)
            elif fmt == "iinc":
                operands = "%d, %d" % (code[pc],
                                       struct.unpack_from(">b", code, pc + 1)[0])
                pc += 2
            elif fmt == "newarray":
                operands = _NEWARRAY.get(code[pc], str(code[pc]))
                pc += 1
            elif fmt == "invokeinterface":
                idx = struct.unpack_from(">H", code, pc)[0]
                count = code[pc + 2]
                pc += 4
                operands = "%s, count=%d" % (pool.ref(idx), count)
            elif fmt == "invokedynamic":
                idx = struct.unpack_from(">H", code, pc)[0]
                pc += 4
                operands = pool.ref(idx)
            elif fmt == "multianewarray":
                idx = struct.unpack_from(">H", code, pc)[0]
                operands = "%s, dims=%d" % (pool.class_name(idx), code[pc + 2])
                pc += 3
            elif fmt == "tableswitch":
                p = start + 1 + ((4 - ((start + 1) % 4)) % 4)
                default = struct.unpack_from(">i", code, p)[0]
                low, high = struct.unpack_from(">ii", code, p + 4)
                p += 12
                targets = []
                for _ in range(max(0, high - low + 1)):
                    targets.append(start + struct.unpack_from(">i", code, p)[0])
                    p += 4
                pc = p
                operands = "default -> %d; %d..%d -> %s" % (
                    start + default, low, high,
                    ", ".join(str(t) for t in targets[:8]))
            elif fmt == "lookupswitch":
                p = start + 1 + ((4 - ((start + 1) % 4)) % 4)
                default = struct.unpack_from(">i", code, p)[0]
                npairs = struct.unpack_from(">i", code, p + 4)[0]
                p += 8
                pairs = []
                for _ in range(max(0, npairs)):
                    key = struct.unpack_from(">i", code, p)[0]
                    off = struct.unpack_from(">i", code, p + 4)[0]
                    pairs.append("%d->%d" % (key, start + off))
                    p += 8
                pc = p
                operands = "default -> %d; %s" % (start + default,
                                                 ", ".join(pairs[:8]))
            elif fmt == "wide":
                if pc < n:
                    wop = code[pc]
                    pc += 1
                    wname, _ = _OPS.get(wop, ("op_%02X" % wop, "none"))
                    if wop == 0x84:
                        operands = "%s %d, %d" % (
                            wname, struct.unpack_from(">H", code, pc)[0],
                            struct.unpack_from(">h", code, pc + 2)[0])
                        pc += 4
                    else:
                        operands = "%s #%d" % (
                            wname, struct.unpack_from(">H", code, pc)[0])
                        pc += 2
        except (IndexError, struct.error, ClassFormatError):
            out.append("%4d: %-16s <bad>" % (start, name))
            break
        if pc > n:
            out.append("%4d: %-16s <bad>" % (start, name))
            break
        out.append("%4d: %-16s %s" % (start, name, operands))
    return out


def method_lines(method: MethodInfo, pool: ConstantPool,
                 indent: str = "  ") -> list[str]:
    lines = ["%s%s %s%s" % (
        indent, format_flags(method.access, "method"),
        method.name, method_signature(method.descriptor))]
    code = method.code
    if code is None:
        lines.append(indent + "  (no code)")
        return lines
    lines.append("%s  stack=%d, locals=%d" % (indent, code.max_stack,
                                              code.max_locals))
    for line in disassemble(code.code, pool):
        lines.append(indent + "  " + line)
    if code.exceptions:
        lines.append(indent + "  Exception table:")
        for start, end, handler, type_name in code.exceptions:
            lines.append("%s    %d-%d -> %d  %s"
                         % (indent, start, end, handler, type_name))
    return lines


def class_listing(info: ClassInfo) -> list[str]:
    lines = []
    if info.source_file:
        lines.append('Compiled from "%s"' % info.source_file)
    kind = "interface" if info.access & 0x0200 else "class"
    lines.append("%s %s %s" % (info.flags, kind,
                               info.this_name.replace("/", ".")))
    if info.super_name:
        lines.append("  extends " + info.super_name.replace("/", "."))
    for name in info.interfaces:
        lines.append("  implements " + name.replace("/", "."))
    lines.append("  minor version: %d" % info.minor)
    lines.append("  major version: %d" % info.major)
    lines.append("  flags: (%s)" % info.flags)
    lines.append("")
    for f in info.fields:
        lines.append("  %s %s %s;" % (format_flags(f.access, "field"),
                                      field_type(f.descriptor), f.name))
    for m in info.methods:
        lines.extend(method_lines(m, info.pool))
        lines.append("")
    return lines


def constant_pool_lines(info: ClassInfo) -> list[str]:
    lines = ["Constant pool (%d slots):" % len(info.pool.entries)]
    for i in range(1, len(info.pool.entries)):
        try:
            if info.pool.entries[i] is None:
                continue
            lines.append("#%-4d = %s" % (i, info.pool.describe(i)))
        except Exception as e:
            lines.append("#%-4d = <bad: %s>" % (i, e))
    return lines


def parse_class(data: bytes) -> ClassInfo:
    r = _Reader(data)
    if r.u4() != 0xCAFEBABE:
        raise ClassFormatError("不是 class 文件（magic 不匹配）")
    minor, major = r.u2(), r.u2()
    pool = _read_constant_pool(r)
    access, this_i, super_i = r.u2(), r.u2(), r.u2()
    interfaces = [pool.class_name(r.u2()) for _ in range(r.u2())]

    def members():
        out = []
        for _ in range(r.u2()):
            acc = r.u2()
            name = pool.utf(r.u2())
            desc = pool.utf(r.u2())
            attrs = _read_attributes(r, pool)
            out.append((acc, name, desc, attrs))
        return out

    fields = [FieldInfo(*row) for row in members()]
    methods = []
    for acc, name, desc, attrs in members():
        code = None
        for attr in attrs:
            if attr.name == "Code":
                try:
                    code = _parse_code(attr, pool)
                except ClassFormatError:
                    code = None
                break
        methods.append(MethodInfo(acc, name, desc, attrs, code))
    attributes = _read_attributes(r, pool)
    return ClassInfo(
        minor=minor, major=major, access=access,
        this_name=pool.class_name(this_i),
        super_name=pool.class_name(super_i) if super_i else "",
        interfaces=interfaces, fields=fields, methods=methods,
        attributes=attributes, pool=pool)
