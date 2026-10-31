"""快捷修改：轻量级代码预览 / 编辑窗。

只从目录树的**文件**右键菜单进入，文件夹不提供这个入口。

设计目标：
- 不装第三方依赖，Tk 自带 Text 就能编辑；
- 通用语法着色：按扩展名选语言，关键字、字符串、注释、数字分开上色，
  未知类型退回纯文本，绝不因为着色失败影响编辑；
- 编码自动识别（BOM / UTF-8 / GBK / GB18030 / Big5 / UTF-16 / Latin-1），
  也可以手动切换后重新读盘；
- 保存时保持原换行符（CRLF/LF/CR）和当前编码，Ctrl+S 直接存。
"""
import bisect
import codecs
import mmap
import os
import queue
import re
import threading
import tkinter as tk
import zipfile
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from . import core
from .core import *
from .ui import scale
from .ui.markdown import MarkdownView
from .viewers import ClassView, HexView, JarView, PeView, looks_pe
from .viewers.textview import PaginatedTextView

TITLE = "快捷修改"
MAX_OPEN_BYTES = 8 * 1024 * 1024      # 旧的读取上限（保留兼容）
MAX_EDIT_BYTES = 2 * 1024 * 1024      # 可编辑文本上限；超过改分页只读
SAMPLE_BYTES = 64 * 1024              # 判定文本/二进制只读开头
SMALL_BINARY_BYTES = 8 * 1024 * 1024  # 小二进制直接读 bytes，大文件走 mmap
MAX_BINARY_BYTES = 2 * 1024 * 1024 * 1024   # mmap 上限 2GB（不再整文件进内存）
LARGE_TEXT_PREFIX = "large-text:"
ASYNC_OPEN_BYTES = 8 * 1024 * 1024    # 超过此大小走后台读取 + 加载提示
JAR_EXTS = (".jar", ".war", ".ear", ".jmod")

VIEW_TEXT = "文本"
VIEW_HEX = "Hex"
VIEW_PE = "PE"
VIEW_JAR = "JAR"
VIEW_CLASS = "Class"
VIEW_MD = "MD 预览"
VIEW_LABELS = {"text": VIEW_TEXT, "hex": VIEW_HEX, "pe": VIEW_PE,
               "jar": VIEW_JAR, "class": VIEW_CLASS, "md": VIEW_MD}
VIEW_BY_LABEL = {v: k for k, v in VIEW_LABELS.items()}
MAX_HIGHLIGHT_CHARS = 400 * 1000      # 超大文件关掉着色，优先保证输入流畅
HIGHLIGHT_DELAY_MS = 260

MONO_FONTS = ("Cascadia Mono", "Consolas", "JetBrains Mono",
              "Source Code Pro", "Courier New")

# ------------------------- 编码 -------------------------
# (显示名, Python 编解码器, 保存时是否加 BOM)
ENCODINGS = (
    ("UTF-8", "utf-8", b""),
    ("UTF-8 BOM", "utf-8", codecs.BOM_UTF8),
    ("GBK", "gbk", b""),
    ("GB18030", "gb18030", b""),
    ("Big5", "big5", b""),
    ("Shift-JIS", "shift_jis", b""),
    ("UTF-16 LE", "utf-16-le", codecs.BOM_UTF16_LE),
    ("UTF-16 BE", "utf-16-be", codecs.BOM_UTF16_BE),
    ("UTF-32 LE", "utf-32-le", codecs.BOM_UTF32_LE),
    ("UTF-32 BE", "utf-32-be", codecs.BOM_UTF32_BE),
    ("Latin-1", "latin-1", b""),
)
ENC_LABELS = tuple(item[0] for item in ENCODINGS)
ENC_BY_LABEL = dict((label, (codec, bom)) for label, codec, bom in ENCODINGS)

# BOM 必须先长后短：UTF-32 LE 的前两个字节和 UTF-16 LE 相同
BOM_LABELS = (
    (codecs.BOM_UTF32_LE, "UTF-32 LE"),
    (codecs.BOM_UTF32_BE, "UTF-32 BE"),
    (codecs.BOM_UTF8, "UTF-8 BOM"),
    (codecs.BOM_UTF16_LE, "UTF-16 LE"),
    (codecs.BOM_UTF16_BE, "UTF-16 BE"),
)

# 识别顺序按实际出现频率排列：中文 Windows 环境中 GBK 高于 Big5/Shift-JIS
AUTO_ORDER = ("UTF-8", "GBK", "GB18030", "Big5", "Shift-JIS")

class EditorError(Exception):
    """打开/读盘失败，调用方负责弹提示。"""

def _strip_bom(raw):
    for bom, _label in BOM_LABELS:
        if raw.startswith(bom):
            return raw[len(bom):]
    return raw

def decode_with_label(raw, label):
    """按显示名解码；BOM 统一先剥掉，避免正文里出现不可见的 U+FEFF。"""
    codec, _bom = ENC_BY_LABEL[label]
    return _strip_bom(raw).decode(codec)

def encode_with_label(text, label):
    codec, bom = ENC_BY_LABEL[label]
    return bom + text.encode(codec)

def detect_encoding(raw):
    """返回 (解码后的文本, 显示名)。读不出来就抛 EditorError。"""
    if not raw:
        return "", "UTF-8"
    for bom, label in BOM_LABELS:
        if raw.startswith(bom):
            try:
                return decode_with_label(raw, label), label
            except UnicodeDecodeError as e:
                raise EditorError("文件带 %s BOM，但内容解码失败：%s" % (label, e))
    if looks_binary(raw):
        raise EditorError("这是二进制文件，不能用文本方式预览。")
    for label in AUTO_ORDER:
        try:
            return decode_with_label(raw, label), label
        except UnicodeDecodeError:
            pass
    # 最后兜底：Latin-1 对任意字节都不会失败，至少让人能看一眼
    return raw.decode("latin-1"), "Latin-1"

def looks_binary(raw):
    """粗略判断二进制：NUL 或过多控制字符。"""
    sample = raw[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    ctrl = 0
    for b in sample:
        if b < 32 and b not in (8, 9, 10, 12, 13):
            ctrl += 1
    return ctrl / float(len(sample)) > 0.10

def detect_newline(text):
    """返回 (保存时用的换行符, 显示名)。混合换行按出现最多的那种存。"""
    crlf = text.count("\r\n")
    rest = text.replace("\r\n", "")
    lf = rest.count("\n")
    cr = rest.count("\r")
    kinds = sum(1 for n in (crlf, lf, cr) if n)
    if kinds == 0:
        return "\n", "LF"
    if kinds > 1:
        name = "混合"
    else:
        name = "CRLF" if crlf else ("LF" if lf else "CR")
    best = max((crlf, "\r\n", "CRLF"), (lf, "\n", "LF"), (cr, "\r", "CR"))
    return best[1], name

def normalize_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")

def _binary_header(sample):
    """已知二进制签名：ZIP/JAR、class、PE、ELF。"""
    return sample.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",
                              b"\xca\xfe\xba\xbe", b"MZ", b"\x7fELF"))

def looks_class(data) -> bool:
    """class 文件魔数 CAFEBABE。"""
    try:
        return len(data) >= 4 and data[:4] == b"\xca\xfe\xba\xbe"
    except Exception:
        return False

def detect_sample_label(sample: bytes) -> str:
    """只用开头样本猜编码（大文本不整块解码）。"""
    for bom, label in BOM_LABELS:
        if sample.startswith(bom):
            return label
    for label in AUTO_ORDER:
        codec = ENC_BY_LABEL[label][0]
        try:
            dec = codecs.getincrementaldecoder(codec)("strict")
            dec.decode(sample, final=False)
            return label
        except Exception:
            continue
    return "Latin-1"

def read_file_any(path):
    """读盘：文本返回 (raw, text, 编码, 换行, 换行名)；二进制 text=None。

    二进制不再整文件读内存：小文件读 bytes，大文件用 mmap（只读），
    所以 Hex/PE/JAR 可以打开远超 64MB 的文件；文本仍限制 8MB。
    """
    if not os.path.isfile(path):
        raise EditorError("文件不存在：%s" % path)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise EditorError("读不到文件大小：%s" % e)
    if size > MAX_BINARY_BYTES:
        raise EditorError("文件超过 %d GB，暂不支持查看。"
                          % (MAX_BINARY_BYTES // (1024 ** 3)))
    try:
        with open(path, "rb") as f:
            sample = f.read(min(size, SAMPLE_BYTES)) if size else b""
    except OSError as e:
        raise EditorError("读取失败：%s" % e)
    # 先认 BOM：UTF-16/32 的正文里全是 NUL，先过 looks_binary 会被误判成二进制
    has_bom = any(sample.startswith(bom) for bom, _label in BOM_LABELS)
    if not has_bom and (_binary_header(sample) or looks_binary(sample)):
        if size <= SMALL_BINARY_BYTES:
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError as e:
                raise EditorError("读取失败：%s" % e)
        else:
            try:
                with open(path, "rb") as f:
                    raw = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError) as e:
                raise EditorError("映射文件失败：%s" % e)
        return raw, None, "binary", "\n", "LF"
    if size > MAX_EDIT_BYTES:
        # 大文本：不整块读/解码，也不塞进可编辑 Text；交给分页只读视图。
        label = detect_sample_label(sample)
        try:
            with open(path, "rb") as f:
                raw = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError) as e:
            raise EditorError("映射文件失败：%s" % e)
        return raw, None, LARGE_TEXT_PREFIX + label, "\n", "LF"
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise EditorError("读取失败：%s" % e)
    try:
        text, label = detect_encoding(raw)
    except EditorError:
        return raw, None, "binary", "\n", "LF"
    newline, newline_name = detect_newline(text)
    return raw, normalize_newlines(text), label, newline, newline_name

def read_text_file(path):
    """文本专用：二进制直接拒绝（保留给老调用方）。"""
    data = read_file_any(path)
    if data[1] is None:
        raise EditorError("这是二进制文件，不能用文本方式预览。")
    return data

# ------------------------- 语言定义 -------------------------
def _lang(name, exts=(), kw=(), strings=(), comments=(), extras=(),
          ignore_case=False):
    return {
        "name": name,
        "exts": tuple(exts),
        "kw": tuple(dict.fromkeys(kw)),
        "strings": tuple(strings),
        "comments": tuple(comments),
        "extras": tuple(extras),
        "ignore_case": bool(ignore_case),
    }

_PY_KW = """
False None True and as assert async await break class continue def del elif else
except finally for from global if import in is lambda nonlocal not or pass raise
return self try while with yield match case
""".split()

_CLIKE_KW = """
abstract alignas alignof and asm assert async await auto bool boolean break byte
case catch char char16_t char32_t class companion const constexpr const_cast
continue data declare default defer delete do double dynamic_cast elif else enum
explicit export extends extern false final finally float for friend from func
function get global goto guard if implements import in init inline instanceof int
interface internal is late lateinit let long match module mutable namespace native
new noexcept not nullptr object of operator or override package permits private
protected protocol public readonly record ref register rethrows return sealed
self short signed sizeof some static static_assert static_cast strictfp struct
super suspend switch synchronized template this throw throws trait transient true
try type typedef typename typeof union unsafe unsigned using val var virtual void
volatile when where while with yield
""".split()

_GO_KW = """
break case chan const continue default defer else fallthrough for func go goto if
import interface map package range return select struct switch type var nil iota
true false
""".split()

_RS_KW = """
as async await break const continue crate dyn else enum extern false fn for if impl
in let loop match mod move mut pub ref return self Self static struct super trait
true type unsafe use where while
""".split()

_SQL_KW = """
SELECT FROM WHERE INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE ALTER DROP
INDEX JOIN LEFT RIGHT INNER OUTER FULL ON GROUP BY ORDER HAVING LIMIT OFFSET UNION
ALL DISTINCT AS AND OR NOT NULL IS IN BETWEEN LIKE CASE WHEN THEN ELSE END PRIMARY
KEY FOREIGN REFERENCES DEFAULT UNIQUE CHECK VIEW DATABASE TRUNCATE GRANT REVOKE
COMMIT ROLLBACK TRANSACTION BEGIN DECLARE PROCEDURE FUNCTION TRIGGER WITH RECURSIVE
EXISTS
""".split()

_SH_KW = """
if then else elif fi for while until in do done case esac function return export
local readonly declare unset shift source alias echo cd pwd exit set trap
""".split()

_PS_KW = """
if elseif else switch foreach for while do until function param return begin
process end try catch finally throw filter class using namespace break continue
trap
""".split()

_BAT_KW = """
if else exist defined not errorlevel for in do goto call set echo exit start
""".split()

_LUA_KW = """
and break do else elseif end false for function goto if in local nil not or repeat
return then true until while
""".split()

_RB_KW = """
alias and begin break case class def defined do else elsif end ensure false for if
in module next nil not or redo rescue retry return self super then true undef
unless until when while yield
""".split()

_PHP_KW = """
abstract and array as break callable case catch class clone const continue declare
default do echo else elseif empty enddeclare endfor endforeach endif endswitch
endwhile eval exit extends final finally fn for foreach function global goto if
implements include include_once instanceof insteadof interface isset list match
namespace new or print private protected public readonly require require_once
return static switch throw trait try unset use var while xor yield
""".split()

_MAKE_KW = """
include define endef ifeq ifneq ifdef ifndef else endif export unexport override
vpath
""".split()

_CMAKE_KW = """
cmake_minimum_required project add_executable add_library target_link_libraries
target_include_directories target_compile_options set if elseif else endif foreach
endforeach function endfunction include find_package message option install
configure_file
""".split()

_DOCKER_KW = """
FROM RUN CMD LABEL EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG ONBUILD
STOPSIGNAL HEALTHCHECK SHELL MAINTAINER
""".split()

_C_STR = (
    r'"(?:\\.|[^"\\\n])*"',
    r"'(?:\\.|[^'\\\n])*'",
)
_JS_STR = _C_STR + (r"`(?:\\.|[^`\\])*`",)
_PY_STR = (
    r'"""(?:\\.|(?!""").)*"""',
    r"'''(?:\\.|(?!''').)*'''",
    r'"(?:\\.|[^"\\\n])*"',
    r"'(?:\\.|[^'\\\n])*'",
)
_SQL_STR = (
    r"'(?:''|[^'\n])*'",
    r'"(?:\\.|[^"\\\n])*"',
)
_YAML_STR = (
    r'"(?:\\.|[^"\\\n])*"',
    r"'(?:''|[^'\n])*'",
)
_PS_STR = (
    r'"(?:`[\s\S]|[^"`])*"',
    r"'(?:''|[^'\n])*'",
)

_C_COM = (r"/\*[\s\S]*?\*/", r"//[^\n]*")
_C_EXTRA = ((r"^[ \t]*#[ \t]*[A-Za-z_]\w*", "key"),)
_JSON_KEY = (r'"(?:\\.|[^"\\\n])*"(?=\s*:)', "key")

LANGS = {
    "python": _lang("Python", (".py", ".pyw", ".pyi"), _PY_KW, _PY_STR,
                    (r"#[^\n]*",), ((r"@[A-Za-z_]\w*", "key"),)),
    "javascript": _lang("JavaScript", (".js", ".mjs", ".cjs", ".jsx"), _CLIKE_KW,
                        _JS_STR, _C_COM,
                        ((r"\b(?:true|false|null|undefined|NaN|Infinity)\b", "kw"),)),
    "typescript": _lang("TypeScript", (".ts", ".tsx", ".mts", ".cts"), _CLIKE_KW,
                        _JS_STR, _C_COM),
    "java": _lang("Java", (".java",), _CLIKE_KW, _C_STR, _C_COM),
    "c_cpp": _lang("C / C++", (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
                               ".hh", ".hxx", ".m", ".mm"), _CLIKE_KW, _C_STR,
                   _C_COM, _C_EXTRA),
    "csharp": _lang("C#", (".cs",), _CLIKE_KW, _C_STR, _C_COM, _C_EXTRA),
    "kotlin": _lang("Kotlin", (".kt", ".kts"), _CLIKE_KW, _C_STR, _C_COM),
    "swift": _lang("Swift", (".swift",), _CLIKE_KW, _C_STR, _C_COM),
    "scala": _lang("Scala", (".scala", ".sc"), _CLIKE_KW, _C_STR, _C_COM),
    "dart": _lang("Dart", (".dart",), _CLIKE_KW, _C_STR, _C_COM),
    "groovy": _lang("Groovy", (".groovy", ".gradle"), _CLIKE_KW, _C_STR, _C_COM),
    "go": _lang("Go", (".go",), _GO_KW, (r'"(?:\\.|[^"\\\n])*"', r"`[^`]*`"),
                _C_COM),
    "rust": _lang("Rust", (".rs",), _RS_KW, _C_STR, _C_COM),
    "php": _lang("PHP", (".php", ".phtml"), _PHP_KW,
                 (r'"(?:\\.|[^"\\\n])*"', r"'(?:\\.|[^'\\\n])*'"),
                 (r"/\*[\s\S]*?\*/", r"//[^\n]*", r"#[^\n]*"),
                 ((r"\$[A-Za-z_]\w*", "key"),)),
    "ruby": _lang("Ruby", (".rb", ".rake"), _RB_KW,
                  (r'"(?:\\.|[^"\\\n])*"', r"'(?:\\.|[^'\\\n])*'"),
                  (r"#[^\n]*",), ((r"(?<!:):[A-Za-z_]\w*[?!]?", "key"),)),
    "lua": _lang("Lua", (".lua",), _LUA_KW,
                 (r'"(?:\\.|[^"\\\n])*"', r"'(?:\\.|[^'\\\n])*'", r"\[\[[\s\S]*?\]\]"),
                 (r"--\[\[[\s\S]*?\]\]", r"--[^\n]*")),
    "sql": _lang("SQL", (".sql",), _SQL_KW, _SQL_STR,
                 (r"/\*[\s\S]*?\*/", r"--[^\n]*"), ignore_case=True),
    "shell": _lang("Shell", (".sh", ".bash", ".zsh", ".fish", ".ksh"), _SH_KW,
                   (r'"(?:\\.|[^"\\\n])*"', r"'[^'\n]*'"),
                   (r"#[^\n]*",), ((r"\$(?:\{[^}\n]+\}|\w+)", "key"),)),
    "powershell": _lang("PowerShell", (".ps1", ".psm1", ".psd1"), _PS_KW,
                        _PS_STR, (r"<#[\s\S]*?#>", r"#[^\n]*"),
                        ((r"\$[\w:]+", "key"),), ignore_case=True),
    "batch": _lang("Batch", (".bat", ".cmd"), _BAT_KW,
                   (r'"[^"\n]*"',),
                   (r"^[ \t]*(?:[Rr][Ee][Mm]\b|::)[^\n]*",),
                   ((r"%[^%\n]+%", "key"),), ignore_case=True),
    "html": _lang("HTML", (".html", ".htm", ".xhtml"), (), _C_STR,
                  (r"<!--[\s\S]*?-->",), ((r"<[^>\n]+>", "tag"),)),
    "xml": _lang("XML", (".xml", ".svg", ".xaml", ".plist", ".csproj",
                         ".vcxproj", ".props", ".targets"), (), _C_STR,
                 (r"<!--[\s\S]*?-->",), ((r"<[^>\n]+>", "tag"),)),
    "css": _lang("CSS", (".css", ".scss", ".sass", ".less"), (), _C_STR,
                 (r"/\*[\s\S]*?\*/",),
                 ((r"[-A-Za-z]+(?=\s*:)", "key"),
                  (r"\$[-\w]+", "key"), (r"@[-\w]+", "key"))),
    "json": _lang("JSON", (".json",), ("true", "false", "null"),
                  (r'"(?:\\.|[^"\\\n])*"',), (), (_JSON_KEY,)),
    "jsonc": _lang("JSON with Comments", (".jsonc", ".json5"),
                   ("true", "false", "null"),
                   (r'"(?:\\.|[^"\\\n])*"',), _C_COM, (_JSON_KEY,)),
    "yaml": _lang("YAML", (".yaml", ".yml"),
                  ("true", "false", "null", "yes", "no", "on", "off"),
                  _YAML_STR, (r"#[^\n]*",),
                  ((r"^[ \t-]*[\w.]+(?=\s*:)", "key"),)),
    "toml": _lang("TOML", (".toml",), ("true", "false"), _YAML_STR,
                  (r"#[^\n]*",),
                  ((r"^[ \t]*[\w.-]+(?=\s*=)", "key"),
                   (r"^[ \t]*\[[^\]\n]+\]", "tag"))),
    "ini": _lang("INI", (".ini", ".cfg", ".conf", ".properties"),
                 (), (r'"(?:\\.|[^"\\\n])*"',), (r"[#;][^\n]*",),
                 ((r"^[ \t]*[^#;\s][^=:\n]*(?=\s*[=:])", "key"),
                  (r"^[ \t]*\[[^\]\n]+\]", "tag"))),
    "markdown": _lang("Markdown", (".md", ".markdown"), (), (), (),
                      ((r"^#{1,6}[ \t].*$", "head"),
                       (r"^```.*$", "code"),
                       (r"`[^`\n]+`", "code"),
                       (r"\*\*[^*\n]+\*\*", "head"),
                       (r"\[[^\]\n]+\]\([^)\n]+\)", "tag"))),
    "make": _lang("Makefile", (".mk",), _MAKE_KW, _C_STR, (r"#[^\n]*",),
                  ((r"\$\([^)\n]+\)", "key"),
                   (r"^[A-Za-z0-9_.-]+(?=\s*:)", "tag"))),
    "cmake": _lang("CMake", (), _CMAKE_KW, _C_STR, (r"#[^\n]*",),
                   ((r"\$\{[^}\n]+\}", "key"),), ignore_case=True),
    "docker": _lang("Dockerfile", (), _DOCKER_KW, _C_STR, (r"#[^\n]*",),
                    ignore_case=True),
    "plain": _lang("纯文本", (".txt", ".log", ".csv", ".tsv")),
}

EXT_LANG = {}
for _key, _cfg in LANGS.items():
    for _ext in _cfg["exts"]:
        EXT_LANG[_ext] = _key

SPECIAL_NAMES = {
    "dockerfile": ("docker", "Dockerfile"),
    "makefile": ("make", "Makefile"),
    "gnumakefile": ("make", "Makefile"),
    "cmakelists.txt": ("cmake", "CMake"),
    "gemfile": ("ruby", "Ruby"),
    "rakefile": ("ruby", "Ruby"),
    "jenkinsfile": ("groovy", "Jenkinsfile"),
    "vagrantfile": ("ruby", "Ruby"),
}

def lang_for(path):
    base = os.path.basename(path).lower()
    special = SPECIAL_NAMES.get(base)
    if special:
        key, name = special
        cfg = dict(LANGS[key])
        cfg["name"] = name
        return cfg
    if base.startswith(".env"):
        return LANGS["ini"]
    key = EXT_LANG.get(os.path.splitext(base)[1])
    return LANGS[key] if key else LANGS["plain"]

# ------------------------- 着色引擎 -------------------------
NUMBER_RE = re.compile(
    r"(?<![\w.])(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|"
    r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?!\w)"
)

TAG_STYLE = (
    ("num", "#b5cea8"),
    ("kw", "#c586c0"),
    ("key", "#9cdcfe"),
    ("tag", "#569cd6"),
    ("head", "#dcdcaa"),
    ("code", "#d7ba7d"),
    ("str", "#ce9178"),
    ("com", "#6a9955"),
)
TAG_NAMES = tuple(name for name, _color in TAG_STYLE)

_RE_CACHE = {}

class _IndexMap:
    """把字符偏移换成 Tk 的 line.col。

    直接写 "1.0+Nc" 看着简单，但 Tk 每次都要从头数字符：几百 KB 的文件、
    几万个 token 就是几十秒。line.col 走的是 Tk 自己的行索引，快一个量级
    以上，这是大文件着色不卡的关键。
    """
    __slots__ = ("starts",)

    def __init__(self, text):
        self.starts = [0]
        pos = text.find("\n")
        while pos != -1:
            self.starts.append(pos + 1)
            pos = text.find("\n", pos + 1)

    def at(self, pos):
        line = bisect.bisect_right(self.starts, pos) - 1
        if line < 0:
            line = 0
        elif line >= len(self.starts):
            line = len(self.starts) - 1
        return line + 1, pos - self.starts[line]

def _compiled(lang):
    """按语言缓存 3 个正则：字符串/注释、关键字、附加规则。"""
    key = lang["name"]
    hit = _RE_CACHE.get(key)
    if hit is not None:
        return hit

    alts = []
    for i, pat in enumerate(lang["strings"]):
        alts.append("(?P<str%d>%s)" % (i, pat))
    for i, pat in enumerate(lang["comments"]):
        alts.append("(?P<com%d>%s)" % (i, pat))
    token_re = re.compile("|".join(alts), re.S | re.M) if alts else None

    kw_re = None
    if lang["kw"]:
        words = sorted(lang["kw"], key=len, reverse=True)
        flags = re.I if lang["ignore_case"] else 0
        kw_re = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in words),
                           flags)

    extras = []
    for pat, tag in lang["extras"]:
        extras.append((re.compile(pat, re.M | re.I), tag))

    hit = (token_re, kw_re, tuple(extras))
    _RE_CACHE[key] = hit
    return hit

def _pick_mono(win):
    try:
        families = set(tkfont.families(win))
        for name in MONO_FONTS:
            if name in families:
                return name
    except Exception:
        pass
    return "Courier New"

def _short_path(path, limit=88):
    if len(path) <= limit:
        return path
    return "…" + path[-(limit - 1):]

# ------------------------- 编辑窗口 -------------------------
class Editor:
    def __init__(self, app, path, data):
        self.app = app
        self.path = path
        self.raw = data[0]
        enc = data[2] or ""
        self.is_large_text = enc.startswith(LARGE_TEXT_PREFIX)
        self.encoding = (enc[len(LARGE_TEXT_PREFIX):] if self.is_large_text
                         else enc)
        self.is_binary = data[1] is None and not self.is_large_text
        self.text = data[1] or ""
        self.newline = data[3]
        self.newline_name = data[4]
        self.lang = lang_for(path)
        self._mode = self._default_mode()
        try:
            self._load_mtime = os.path.getmtime(path)
        except OSError:
            self._load_mtime = None
        self._dirty = False
        self._loading = False
        self._in_modified = False
        self._highlight_job = None
        self._highlight_on = True
        self._gutter_lines = -1
        self.win = tk.Toplevel(app.root)
        try:
            self._build()
            self._apply_data()
            if not self._set_view_mode(self._mode, initial=True):
                # 初始视图失败也保证不白屏：二进制回退 Hex，文本回退编辑
                fallback = "hex" if self.is_binary else "text"
                if fallback != self._mode:
                    self._set_view_mode(fallback, initial=True)
        except Exception:
            # 半成品窗口不能留给 open_editor：它只拿到 None，隐藏窗口会越攒越多
            try:
                self._close_raw()
            except Exception:
                pass
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            raise
        self.app._center_on_panel(self.win)
        self.win.deiconify()
        try:
            self.win.attributes("-topmost", True)
            self.win.after(420, self._drop_topmost)
        except tk.TclError:
            pass
        self.win.lift()
        self.win.focus_force()
        self.win.after(80, self._initial_focus)

    def _initial_focus(self):
        if self._mode == "text" and not self.is_large_text:
            self.txt.focus_set()

    # ---------------- 外壳 ----------------
    def _build(self):
        self.win.title(TITLE + " - " + display_name(self.path))
        self.win.configure(bg=C_BG)
        self.win.geometry("980x680")
        self.win.minsize(640, 420)
        self.win.withdraw()
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        family = _pick_mono(self.win)
        self.mono = tkfont.Font(root=self.win, family=family, size=10)

        head = tk.Frame(self.win, bg=C_BG)
        head.pack(fill="x", padx=14, pady=(10, 0))
        left = tk.Frame(head, bg=C_BG)
        left.pack(side="left", fill="x", expand=True)
        self.title_lab = tk.Label(left, text=display_name(self.path), bg=C_BG,
                                  fg=C_TEXT, font=(core.FONT, 11, "bold"),
                                  anchor="w")
        self.title_lab.pack(fill="x")
        self.path_lab = tk.Label(left, text=_short_path(self.path), bg=C_BG,
                                 fg=C_MUTED, font=(core.FONT, 8), anchor="w")
        self.path_lab.pack(fill="x")

        btns = tk.Frame(head, bg=C_BG)
        btns.pack(side="right", padx=(10, 0))
        self.save_btn = self._button(btns, "保存", self.save, accent=True)
        self.save_btn.pack(side="right")
        self._button(btns, "关闭", self.close).pack(side="right", padx=(0, 6))
        self._button(btns, "重新载入", self.reload).pack(side="right", padx=(0, 6))
        self._button(btns, "打开…", self._open_other).pack(side="right",
                                                           padx=(0, 6))

        tools = tk.Frame(self.win, bg=C_BG)
        tools.pack(fill="x", padx=14, pady=(7, 0))
        tk.Label(tools, text="编码", bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 8)).pack(side="left")
        enc_value = self.encoding if self.encoding in ENC_LABELS else "UTF-8"
        self.enc_var = tk.StringVar(master=self.win, value=enc_value)
        self._init_combo_style()
        self.enc_box = ttk.Combobox(tools, textvariable=self.enc_var,
                                    values=ENC_LABELS, state="readonly",
                                    width=13, style="VB.TCombobox",
                                    font=(core.FONT, 8))
        self.enc_box.pack(side="left", padx=(6, 0))
        self.enc_box.bind("<<ComboboxSelected>>", self._on_encoding)
        tk.Label(tools, text="查看", bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 8)).pack(side="left", padx=(14, 0))
        self.mode_var = tk.StringVar(master=self.win,
                                     value=VIEW_LABELS[self._mode])
        self.mode_box = ttk.Combobox(tools, textvariable=self.mode_var,
                                     values=tuple(VIEW_LABELS.values()),
                                     state="readonly", width=10,
                                     style="VB.TCombobox", font=(core.FONT, 8))
        self.mode_box.pack(side="left", padx=(6, 0))
        self.mode_box.bind("<<ComboboxSelected>>", self._on_mode_select)
        self.lang_lab = tk.Label(tools, text="语法：" + self.lang["name"], bg=C_BG,
                                 fg=C_MUTED, font=(core.FONT, 8))
        self.lang_lab.pack(side="right")

        tk.Frame(self.win, bg=C_BORDER, height=1).pack(fill="x", padx=14,
                                                       pady=(7, 0))

        # 查找/替换栏（Ctrl+F 或 Ctrl+H 展开，Escape 收起）
        self.find_frame = tk.Frame(self.win, bg=C_BG)
        self._find_visible = False
        self._find_var = tk.StringVar()
        self._repl_var = tk.StringVar()
        self._find_tag = "find_match"

        frow = tk.Frame(self.find_frame, bg=C_BG)
        frow.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(frow, text="查找", bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 8)).pack(side="left")
        self._find_entry = tk.Entry(frow, textvariable=self._find_var, bg=C_PANEL,
                                    fg=C_TEXT, insertbackground=C_TEXT, bd=0,
                                    relief="flat", font=("Consolas", 9),
                                    highlightthickness=1,
                                    highlightbackground=C_BORDER,
                                    highlightcolor=C_ACCENT)
        self._find_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self._find_entry.bind("<Return>", lambda e: self._find_next())
        self._find_entry.bind("<Shift-Return>", lambda e: self._find_prev())
        self._find_count = tk.Label(frow, text="", bg=C_BG, fg=C_MUTED,
                                    font=(core.FONT, 8), width=8)
        self._find_count.pack(side="left")
        self._button(frow, "上一个", self._find_prev).pack(side="left", padx=2)
        self._button(frow, "下一个", self._find_next).pack(side="left", padx=2)

        self._repl_row = tk.Frame(self.find_frame, bg=C_BG)
        self._repl_row.pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(self._repl_row, text="替换", bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 8)).pack(side="left")
        self._repl_entry = tk.Entry(self._repl_row, textvariable=self._repl_var,
                                    bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                                    bd=0, relief="flat", font=("Consolas", 9),
                                    highlightthickness=1,
                                    highlightbackground=C_BORDER,
                                    highlightcolor=C_ACCENT)
        self._repl_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self._button(self._repl_row, "替换", self._replace_one).pack(side="left", padx=2)
        self._button(self._repl_row, "全部替换", self._replace_all).pack(side="left", padx=2)
        tk.Frame(self.find_frame, bg=C_BG, height=6).pack(fill="x")

        status = tk.Frame(self.win, bg=C_PANEL)
        status.pack(side="bottom", fill="x")
        tk.Frame(status, bg=C_BORDER, height=1).pack(fill="x")
        self.pos_lab = tk.Label(status, text="第 1 行，第 1 列", bg=C_PANEL,
                                fg=C_MUTED, font=(core.FONT, 8))
        self.pos_lab.pack(side="left", padx=(14, 8), pady=4)
        self.state_lab = tk.Label(status, text="已保存", bg=C_PANEL, fg=C_MUTED,
                                  font=(core.FONT, 8))
        self.state_lab.pack(side="right", padx=(8, 14), pady=4)
        self.info_lab = tk.Label(status, text="", bg=C_PANEL, fg=C_MUTED,
                                 font=(core.FONT, 8))
        self.info_lab.pack(side="right", padx=8, pady=4)

        self.body = body = tk.Frame(self.win, bg=C_BG)
        body.pack(fill="both", expand=True, padx=14, pady=(6, 8))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        self.gutter = tk.Text(
            body, width=4, bg=C_BG, fg="#5c6370", bd=0, relief="flat",
            highlightthickness=0, padx=6, pady=6, font=self.mono,
            wrap="none", takefocus=0, cursor="arrow", state="disabled",
            spacing1=0, spacing2=0, spacing3=0,
        )
        self.gutter.grid(row=0, column=0, sticky="ns")
        self.gutter.tag_configure("cur", foreground=C_TEXT, background="#2b2e37")
        self.gutter.bind("<MouseWheel>", self._gutter_wheel)

        self.txt = tk.Text(
            body, bg=C_PANEL, fg=C_TEXT, insertbackground="#ffffff",
            selectbackground=C_SELECT, selectforeground="#ffffff",
            inactiveselectbackground=C_PANEL, bd=0, relief="flat",
            highlightthickness=0, padx=10, pady=6, wrap="none",
            undo=True, autoseparators=True, maxundo=-1,
            font=self.mono, tabs=(max(8, self.mono.measure("    ")),),
            spacing1=0, spacing2=0, spacing3=0, insertwidth=2,
        )
        self.txt.grid(row=0, column=1, sticky="nsew")
        for tag, color in TAG_STYLE:
            self.txt.tag_configure(tag, foreground=color)
        # 查找命中高亮：必须等 self.txt 建好后再配 tag（原来写在 _build 开头会 AttributeError）
        self.txt.tag_configure(self._find_tag, background="#4a5060",
                               foreground="#ffffff")

        self.vsb = ttk.Scrollbar(body, style="VB.Vertical.TScrollbar",
                                 command=self.txt.yview)
        self.vsb.grid(row=0, column=2, sticky="ns")
        self.txt.configure(yscrollcommand=self._sync_scroll)

        self.hsb = ttk.Scrollbar(body, orient="horizontal",
                                 style="VB.Horizontal.TScrollbar",
                                 command=self.txt.xview)
        self.hsb.grid(row=1, column=1, sticky="ew")
        self.txt.configure(xscrollcommand=self.hsb.set)

        self.txt.bind("<<Modified>>", self._on_modified)
        self.txt.bind("<KeyRelease>", self._update_cursor)
        self.txt.bind("<ButtonRelease-1>", self._update_cursor)
        self.win.bind("<Control-s>", self._key_save)
        self.win.bind("<Control-w>", self._key_close)
        self.win.bind("<F5>", self._key_reload)
        self.win.bind("<Control-f>", self._show_find)
        self.win.bind("<Control-h>", self._show_replace)
        self.win.bind("<F3>", self._find_next)
        self.win.bind("<Shift-F3>", self._find_prev)
        self.win.bind("<Escape>", self._hide_find)
        # 特殊查看器按需创建，挂在 body 的位置上（不预先 pack）
        self.view_host = tk.Frame(self.win, bg=C_BG)
        self._views = {}

    def _button(self, parent, text, cmd, accent=False):
        return tk.Button(parent, text=text, command=cmd,
                         bg=C_ACCENT if accent else C_PANEL,
                         fg="white" if accent else C_TEXT,
                         activebackground=C_ACCENT_H if accent else C_HOVER,
                         activeforeground="white", relief="flat", bd=0,
                         padx=12, pady=4, font=(core.FONT, 9), cursor="hand2")

    def _init_combo_style(self):
        st = ttk.Style()
        try:
            st.configure(
                "VB.TCombobox", fieldbackground=C_PANEL, background=C_PANEL,
                foreground=C_TEXT, arrowcolor=C_MUTED, bordercolor=C_BORDER,
                lightcolor=C_BORDER, darkcolor=C_BORDER, padding=3,
            )
            st.map("VB.TCombobox",
                   fieldbackground=[("readonly", C_PANEL)],
                   foreground=[("readonly", C_TEXT)],
                   selectbackground=[("readonly", C_PANEL)],
                   selectforeground=[("readonly", C_TEXT)])
            st.configure("VB.Horizontal.TScrollbar", background="#3a3d46",
                         troughcolor=C_PANEL, bordercolor=C_PANEL,
                         arrowcolor=C_MUTED, borderwidth=0)
            st.map("VB.Horizontal.TScrollbar",
                   background=[("active", "#4a4e59")])
        except tk.TclError:
            pass

    def _drop_topmost(self):
        try:
            self.win.attributes("-topmost", False)
        except tk.TclError:
            pass

    # ---------------- 数据 ----------------
    def _apply_data(self):
        self._loading = True
        try:
            self.txt.delete("1.0", "end")
            if not self.is_binary and not self.is_large_text:
                self.txt.insert("1.0", self.text)
            self.txt.edit_reset()
            self.txt.edit_modified(False)
            self.txt.mark_set("insert", "1.0")
            self.txt.see("1.0")
        finally:
            self._loading = False
        self._dirty = False
        self._set_title()
        if not self.is_binary and not self.is_large_text:
            self._refresh_gutter()
            self._raise_tag_priority()
            self._schedule_highlight(immediate=True)
        self._update_status()

    # ---------------- 查看模式 ----------------
    def _default_mode(self):
        ext = os.path.splitext(self.path)[1].lower()
        if self.is_large_text:
            return "text"           # 分页只读文本，不是可编辑 Text
        if self.is_binary:
            if ext in JAR_EXTS and zipfile.is_zipfile(self.path):
                return "jar"
            if looks_class(self.raw) or ext == ".class":
                return "class"
            return "pe" if looks_pe(self.raw) else "hex"
        if ext in (".md", ".markdown"):
            return "md"
        return "text"

    def _view_info(self):
        mode = VIEW_LABELS.get(self._mode, self._mode)
        size = core.human_size(len(self.raw))
        if self.is_large_text:
            if self._mode == "text":
                return "文本 · 大文件只读 · %s" % size
            return "%s · 大文本 · %s" % (mode, size)
        if self._mode == "jar":
            return "JAR · %s" % size
        if self._mode == "class":
            return "Class · 类文件 · %s" % size
        if self.is_binary:
            kind = "PE" if looks_pe(self.raw) else "二进制"
            if mode == kind:
                return "%s · %s" % (mode, size)
            return "%s · %s · %s" % (mode, kind, size)
        return "%s · Markdown · %s" % (mode, size)

    def _on_mode_select(self, _e=None):
        mode = VIEW_BY_LABEL.get(self.mode_var.get())
        if mode is not None:
            self._set_view_mode(mode)
        try:
            self.mode_box.selection_clear()
        except tk.TclError:
            pass

    def _set_view_mode(self, mode, initial=False):
        """切换查看模式；返回是否成功。失败时选择框回退。"""
        if mode not in VIEW_LABELS:
            return False
        if mode == "text" and self.is_binary:
            if not initial:
                messagebox.showinfo(
                    TITLE, "二进制文件不支持文本编辑，请用 Hex / PE / JAR / Class 查看。",
                    parent=self.win)
            self.mode_var.set(VIEW_LABELS[self._mode])
            return False
        if mode == "jar" and not (self.is_binary
                                  and zipfile.is_zipfile(self.path)):
            # 单个 class 文件是常见误点：直接切到 Class 视图，不弹“不是 JAR”
            if self.is_binary and looks_class(self.raw):
                return self._set_view_mode("class", initial)
            if not initial:
                messagebox.showinfo(
                    TITLE, "这个文件不是 JAR/ZIP 归档。", parent=self.win)
            self.mode_var.set(VIEW_LABELS[self._mode])
            return False
        if mode == "class" and not (
                self.is_binary
                and (looks_class(self.raw)
                     or os.path.splitext(self.path)[1].lower() == ".class")):
            if not initial:
                messagebox.showinfo(
                    TITLE, "这个文件不是 .class（扩展名和 CAFEBABE 都不匹配）。",
                    parent=self.win)
            self.mode_var.set(VIEW_LABELS[self._mode])
            return False
        if mode == "pe" and not looks_pe(self.raw):
            if not initial:
                messagebox.showinfo(
                    TITLE, "这个文件没有 PE 签名（不是 DLL/EXE 结构）。",
                    parent=self.win)
            self.mode_var.set(VIEW_LABELS[self._mode])
            return False
        if mode == "md" and (self.is_binary or self.is_large_text):
            if not initial:
                messagebox.showinfo(TITLE, "大文本/二进制不能按 Markdown 预览。",
                                    parent=self.win)
            self.mode_var.set(VIEW_LABELS[self._mode])
            return False
        # 离开文本模式前补一次脏检查：<<Modified>> 可能还排在事件队列里
        if self._mode == "text" and not self.is_binary:
            try:
                if self.txt.edit_modified():
                    self._dirty = True
                    self._set_title()
            except tk.TclError:
                pass

        self._mode = mode
        self.mode_var.set(VIEW_LABELS[mode])
        if self._find_visible:
            try:
                self.find_frame.pack_forget()
            except tk.TclError:
                pass
            self._find_visible = False
            try:
                self.txt.tag_remove(self._find_tag, "1.0", "end")
            except tk.TclError:
                pass

        is_text = (mode == "text")
        use_editor = is_text and not self.is_large_text
        writable = use_editor and not self.is_binary
        try:
            self.enc_box.configure(state="readonly" if writable else "disabled")
            if writable:
                self.save_btn.configure(
                    state="normal", bg=C_ACCENT, fg="white",
                    activebackground=C_ACCENT_H, activeforeground="white")
            else:
                self.save_btn.configure(
                    state="disabled", bg=C_PANEL, fg=C_MUTED,
                    activebackground=C_PANEL, activeforeground=C_MUTED)
            self.lang_lab.configure(text={
                "text": ("大文本只读" if self.is_large_text
                         else "语法：" + self.lang["name"]),
                "hex": "十六进制", "pe": "PE 结构", "jar": "JAR 结构",
                "class": "Class 结构", "md": "Markdown 预览",
            }[mode])
        except tk.TclError:
            pass

        if use_editor:
            try:
                self.view_host.pack_forget()
            except tk.TclError:
                pass
            if not self.body.winfo_manager():
                self.body.pack(fill="both", expand=True, padx=14, pady=(6, 8))
            self.txt.focus_set()
        else:
            try:
                self.body.pack_forget()
            except tk.TclError:
                pass
            view_mode = ("textview" if (is_text and self.is_large_text)
                         else mode)
            view = self._ensure_view(view_mode)
            for child in self.view_host.winfo_children():
                if child is not view:
                    try:
                        child.pack_forget()
                    except tk.TclError:
                        pass
            view.pack(fill="both", expand=True)
            if not self.view_host.winfo_manager():
                self.view_host.pack(fill="both", expand=True,
                                    padx=14, pady=(6, 8))
        self._update_status()
        return True

    def _ensure_view(self, mode):
        if mode == "md":
            # Markdown 可能刚从文本模式改过：每次预览都按 Text 里的当前内容重渲染
            old = self._views.pop("md", None)
            if old is not None:
                try:
                    old.destroy()
                except tk.TclError:
                    pass
            view = MarkdownView(self.view_host,
                                self.txt.get("1.0", "end-1c"),
                                on_link=self._open_link)
            self._views[mode] = view
            return view
        view = self._views.get(mode)
        if view is not None:
            try:
                if view.winfo_exists():
                    return view
            except tk.TclError:
                pass
            self._views.pop(mode, None)
        if mode == "hex":
            # Hex 列比正文更需要横向空间：用像素字号小一号，150% DPI 下才不会
            # 把 ASCII 列挤出窗口（编辑器正文仍用 self.mono）
            hex_font = tkfont.Font(root=self.win,
                                   family=self.mono.actual("family"),
                                   size=-scale.s(9))
            view = HexView(self.view_host, self.raw, font=hex_font,
                           on_status=self._on_hex_status)
        elif mode == "pe":
            view = PeView(self.view_host, self.raw, on_goto=self._pe_goto)
        elif mode in ("jar", "class"):
            app = getattr(self, "app", None)
            cfg = getattr(app, "cfg", None) if app is not None else None
            save = None
            if isinstance(cfg, dict):
                def save():
                    core.save_cfg(cfg)
            if mode == "jar":
                view = JarView(self.view_host, self.path, cfg=cfg,
                               save_cfg=save)
            else:
                try:
                    view = ClassView(self.view_host, self.raw, self.path,
                                     cfg=cfg, save_cfg=save)
                except Exception as exc:
                    view = tk.Frame(self.view_host, bg=C_BG)
                    tk.Label(view, text="Class 视图构建失败", bg=C_BG,
                             fg=C_TEXT, font=(core.FONT, 11, "bold")).pack(
                        pady=(24, 6))
                    tk.Label(view, text=str(exc), bg=C_BG, fg=C_MUTED,
                             font=(core.FONT, 9), wraplength=700,
                             justify="left").pack()
        elif mode == "textview":
            codec = ENC_BY_LABEL.get(self.encoding, ("utf-8", b""))[0]
            view = PaginatedTextView(self.view_host, self.path, codec,
                                     encoding_name=self.encoding)
        else:
            raise ValueError("unknown view mode: %r" % mode)
        self._views[mode] = view
        return view

    def _reset_views(self):
        for view in list(self._views.values()):
            try:
                view.destroy()
            except tk.TclError:
                pass
        self._views.clear()
        try:
            if self.view_host.winfo_manager():
                self.view_host.pack_forget()
        except tk.TclError:
            pass

    def _on_hex_status(self, offset, total):
        try:
            self.pos_lab.configure(text="偏移 0x%X / 0x%X" % (offset, total))
        except tk.TclError:
            pass

    def _pe_goto(self, offset, label=""):
        if not self._set_view_mode("hex"):
            return
        view = self._views.get("hex")
        if view is not None:
            view.goto(offset)
        try:
            self.pos_lab.configure(text="%s @ 0x%X" % (label or "节区", offset))
        except tk.TclError:
            pass

    def _open_link(self, url):
        low = (url or "").strip().lower()
        if not low.startswith(("http://", "https://", "mailto:")):
            try:
                self.state_lab.configure(text="不支持打开该链接", fg=C_MUTED)
            except tk.TclError:
                pass
            return "break"
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
        return "break"

    def _raise_tag_priority(self):
        """字符串/注释压住关键字；JSON 键再压住字符串。"""
        if self.is_binary or self.is_large_text:
            return
        try:
            for tag in ("str", "com"):
                self.txt.tag_raise(tag)
            if self.lang["name"] in ("JSON", "JSON with Comments"):
                self.txt.tag_raise("key")
            elif self.lang["name"] == "Markdown":
                self.txt.tag_raise("head")
                self.txt.tag_raise("code")
        except tk.TclError:
            pass

    def _set_title(self):
        mark = " *" if self._dirty else ""
        name = display_name(self.path)
        self.win.title("%s%s - %s" % (name, mark, TITLE))
        self.title_lab.configure(text=name + mark)

    def _update_status(self):
        if self.is_large_text and self._mode == "text":
            try:
                self.pos_lab.configure(text="")
                self.info_lab.configure(text=self._view_info())
                self.state_lab.configure(text="只读", fg=C_MUTED)
            except tk.TclError:
                pass
            return
        if self._mode != "text" or self.is_binary:
            try:
                self.pos_lab.configure(text="")
                self.info_lab.configure(text=self._view_info())
                self.state_lab.configure(
                    text="只读" if self.is_binary else "预览", fg=C_MUTED)
            except tk.TclError:
                pass
            return
        try:
            line, col = self.txt.index("insert").split(".")
            self.pos_lab.configure(text="第 %s 行，第 %d 列" % (line, int(col) + 1))
        except (tk.TclError, ValueError):
            pass
        info = "%s · %s · %s" % (self.lang["name"], self.encoding,
                                 self.newline_name)
        if not self._highlight_on:
            info += " · 着色已关闭"
        try:
            self.info_lab.configure(text=info)
            if self._dirty:
                self.state_lab.configure(text="未保存", fg="#e2c08d")
            else:
                self.state_lab.configure(text="已保存", fg=C_MUTED)
        except tk.TclError:
            pass

    # ---------------- 行号 / 滚动 ----------------
    def _sync_scroll(self, first, last):
        try:
            self.vsb.set(first, last)
            self.gutter.yview_moveto(first)
        except tk.TclError:
            pass

    def _gutter_wheel(self, e):
        step = int(-e.delta / 120) * 3
        if step == 0:
            step = -3 if e.delta > 0 else 3
        try:
            self.txt.yview_scroll(step, "units")
        except tk.TclError:
            pass
        return "break"

    def _refresh_gutter(self):
        if self.is_binary or self.is_large_text:
            return
        text = self.txt.get("1.0", "end-1c")
        lines = text.count("\n") + 1
        if lines == self._gutter_lines:
            try:
                self.gutter.yview_moveto(self.txt.yview()[0])
            except tk.TclError:
                pass
            return
        self._gutter_lines = lines
        width = max(3, len(str(lines))) + 1
        try:
            self.gutter.configure(width=width, state="normal")
            self.gutter.delete("1.0", "end")
            self.gutter.insert("1.0", "\n".join(str(i) for i in range(1, lines + 1)))
            self.gutter.configure(state="disabled")
            self.gutter.yview_moveto(self.txt.yview()[0])
        except tk.TclError:
            pass

    def _update_cursor(self, _e=None):
        if self.is_binary or self.is_large_text or self._mode != "text":
            return
        try:
            line, col = self.txt.index("insert").split(".")
            self.pos_lab.configure(text="第 %s 行，第 %d 列" % (line, int(col) + 1))
            self.gutter.tag_remove("cur", "1.0", "end")
            self.gutter.tag_add("cur", line + ".0", line + ".end")
        except (tk.TclError, ValueError):
            pass

    # ---------------- 修改 / 着色 ----------------
    def _on_modified(self, _e=None):
        if self.is_binary or self.is_large_text or self._mode != "text":
            return
        if self._loading or self._in_modified:
            return
        try:
            if not self.txt.edit_modified():
                return
        except tk.TclError:
            return
        self._in_modified = True
        try:
            self.txt.edit_modified(False)
        finally:
            self._in_modified = False
        self._dirty = True
        self._set_title()
        self._refresh_gutter()
        self._update_status()
        self._schedule_highlight()

    def _schedule_highlight(self, immediate=False):
        if self.is_binary or self.is_large_text:
            return
        if self._highlight_job is not None:
            try:
                self.win.after_cancel(self._highlight_job)
            except tk.TclError:
                pass
            self._highlight_job = None
        if immediate:
            self._highlight_job = self.win.after(30, self._highlight)
        else:
            self._highlight_job = self.win.after(HIGHLIGHT_DELAY_MS,
                                                 self._highlight)

    def _highlight(self):
        self._highlight_job = None
        if self.is_binary or self.is_large_text:
            return
        try:
            if not self.txt.winfo_exists():
                return
            text = self.txt.get("1.0", "end-1c")
            for tag in TAG_NAMES:
                self.txt.tag_remove(tag, "1.0", "end")
            if len(text) > MAX_HIGHLIGHT_CHARS:
                self._highlight_on = False
                self._update_status()
                return
            self._highlight_on = True
            index = _IndexMap(text)
            token_re, kw_re, extras = _compiled(self.lang)
            if token_re is not None:
                for m in token_re.finditer(text):
                    tag = ("com" if m.lastgroup.startswith("com") else "str")
                    self._tag_span(tag, index.at(m.start()), index.at(m.end()))
            if kw_re is not None:
                for m in kw_re.finditer(text):
                    self._tag_span("kw", index.at(m.start()), index.at(m.end()))
            for m in NUMBER_RE.finditer(text):
                self._tag_span("num", index.at(m.start()), index.at(m.end()))
            for extra_re, tag in extras:
                for m in extra_re.finditer(text):
                    self._tag_span(tag, index.at(m.start()), index.at(m.end()))
            self._raise_tag_priority()
            self._update_status()
        except tk.TclError:
            pass

    def _tag_span(self, tag, start, end):
        if end == start:
            return
        self.txt.tag_add(tag, "%d.%d" % start, "%d.%d" % end)

    # ---------------- 编码 / 载入 ----------------
    def _on_encoding(self, _e=None):
        if self.is_binary or self.is_large_text or self._mode != "text":
            self.enc_var.set(self.encoding if self.encoding in ENC_LABELS
                             else "UTF-8")
            return
        label = self.enc_var.get()
        if label == self.encoding:
            return
        if not self._confirm_discard("切换编码"):
            self.enc_var.set(self.encoding)
            return
        try:
            with open(self.path, "rb") as f:
                raw = f.read(MAX_EDIT_BYTES + 1)
            if len(raw) > MAX_EDIT_BYTES:
                raise EditorError("文件太大，无法重新载入。")
            text = decode_with_label(raw, label)
        except (OSError, UnicodeDecodeError, EditorError) as e:
            messagebox.showerror(TITLE, "用 %s 解码失败：\n%s" % (label, e),
                                 parent=self.win)
            self.enc_var.set(self.encoding)
            return
        self.raw = raw
        self.encoding = label
        self.newline, self.newline_name = detect_newline(text)
        self.text = normalize_newlines(text)
        self._apply_data()
        self.enc_box.selection_clear()
        self._update_status()

    def reload(self):
        if not self._confirm_discard("重新载入"):
            return
        try:
            data = read_file_any(self.path)
        except EditorError as e:
            messagebox.showerror(TITLE, str(e), parent=self.win)
            return
        self._reset_views()
        self._close_raw()
        raw, text, enc, newline, newline_name = data
        self.raw = raw
        self.is_large_text = enc.startswith(LARGE_TEXT_PREFIX)
        self.encoding = (enc[len(LARGE_TEXT_PREFIX):] if self.is_large_text
                         else enc)
        self.is_binary = text is None and not self.is_large_text
        self.text = text or ""
        self.newline = newline
        self.newline_name = newline_name
        self.enc_var.set(self.encoding if self.encoding in ENC_LABELS
                         else "UTF-8")
        self._apply_data()
        self._mode = self._default_mode()
        self._set_view_mode(self._mode)
        self.enc_box.selection_clear()
        self._update_status()

    def _confirm_discard(self, action):
        if not self._dirty:
            return True
        return messagebox.askyesno(
            TITLE,
            "当前文件有未保存的修改，%s会丢掉这些修改。\n继续吗？" % action,
            parent=self.win)

    # ---------------- 保存 / 关闭 ----------------
    def save(self):
        if self.is_binary or self.is_large_text:
            return False
        if self._mode != "text":
            if not self._dirty:
                return False
            # 在预览里按 Ctrl+S 或关窗选保存：先回到文本，再走正常写盘流程
            if not self._set_view_mode("text"):
                return False
        # 外部修改检测：AI 工具刚改过的文件不能被静默覆盖
        if self._load_mtime is not None:
            try:
                cur = os.path.getmtime(self.path)
                if cur != self._load_mtime:
                    if not messagebox.askyesno(
                            TITLE,
                            "「%s」已被其他程序修改过。\n\n仍要保存并覆盖外部改动吗？"
                            % display_name(self.path),
                            parent=self.win):
                        return False
            except OSError:
                pass
        text = self.txt.get("1.0", "end-1c")
        text = normalize_newlines(text).replace("\n", self.newline)
        label = self.encoding
        try:
            data = encode_with_label(text, label)
        except UnicodeEncodeError as e:
            fallback = "UTF-8 BOM" if ENC_BY_LABEL[label][1] else "UTF-8"
            if not messagebox.askyesno(
                    TITLE,
                    "当前编码（%s）存不下文件里的某些字符：\n%s\n\n"
                    "改用 %s 保存吗？" % (label, e, fallback),
                    parent=self.win):
                return False
            label = fallback
            try:
                data = encode_with_label(text, label)
            except UnicodeEncodeError as e2:
                messagebox.showerror(TITLE, "保存失败：%s" % e2, parent=self.win)
                return False
        try:
            with open(self.path, "wb") as f:
                f.write(data)
        except OSError as e:
            messagebox.showerror(TITLE, "写入失败：%s" % e, parent=self.win)
            return False
        self.raw = data
        self.encoding = label
        self.enc_var.set(label)
        self._dirty = False
        try:
            self._load_mtime = os.path.getmtime(self.path)
        except OSError:
            pass
        try:
            self.txt.edit_modified(False)
        except tk.TclError:
            pass
        self._set_title()
        self.state_lab.configure(text="已保存", fg="#89d185")
        self._update_status()
        return True

    def _key_save(self, _e=None):
        self.save()
        return "break"

    def _key_reload(self, _e=None):
        self.reload()
        return "break"

    def _key_close(self, _e=None):
        self.close()
        return "break"

    def _open_other(self):
        """万能查看入口：不经过目录树，直接选任意文件。"""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.win, title="打开文件 - " + APP_NAME)
        if path:
            open_editor(self.app, path)

    # ---- 查找 / 替换 ----

    def _show_find(self, _e=None):
        if self.is_binary or self.is_large_text or self._mode != "text":
            return "break"
        if not self._find_visible:
            self.find_frame.pack(fill="x", after=self.find_frame.master.winfo_children()[2])
            self._find_visible = True
        self._repl_row.pack_forget()
        self._find_entry.focus_set()
        self._find_entry.select_range(0, "end")
        return "break"

    def _show_replace(self, _e=None):
        if self.is_binary or self.is_large_text or self._mode != "text":
            return "break"
        if not self._find_visible:
            self.find_frame.pack(fill="x", after=self.find_frame.master.winfo_children()[2])
            self._find_visible = True
        self._repl_row.pack(fill="x", padx=14, pady=(4, 0))
        self._find_entry.focus_set()
        self._find_entry.select_range(0, "end")
        return "break"

    def _hide_find(self, _e=None):
        if self._find_visible:
            self.find_frame.pack_forget()
            self._find_visible = False
            self.txt.tag_remove(self._find_tag, "1.0", "end")
            if (not self.is_binary and not self.is_large_text
                    and self._mode == "text"):
                self.txt.focus_set()
        return "break"

    def _search_all(self, needle):
        """返回所有匹配的 (start, end) 索引列表"""
        if not needle:
            return []
        hits = []
        pos = "1.0"
        while True:
            idx = self.txt.search(needle, pos, stopindex="end", nocase=True)
            if not idx:
                break
            end = "%s+%dc" % (idx, len(needle))
            hits.append((idx, end))
            pos = end
        return hits

    def _highlight_and_count(self):
        needle = self._find_var.get()
        self.txt.tag_remove(self._find_tag, "1.0", "end")
        if not needle:
            self._find_count.configure(text="")
            return []
        hits = self._search_all(needle)
        for s, e in hits:
            self.txt.tag_add(self._find_tag, s, e)
        self._find_count.configure(text="%d 个匹配" % len(hits) if hits else "无匹配")
        return hits

    def _find_next(self, _e=None):
        if self.is_binary or self.is_large_text or self._mode != "text":
            return "break"
        needle = self._find_var.get()
        if not needle:
            return "break"
        hits = self._highlight_and_count()
        if not hits:
            return "break"
        cur = self.txt.index(tk.INSERT)
        for s, e in hits:
            if self.txt.compare(s, ">", cur):
                self.txt.mark_set(tk.INSERT, s)
                self.txt.see(s)
                self._select_match(s, e)
                return "break"
        # 回绕到第一个
        self.txt.mark_set(tk.INSERT, hits[0][0])
        self.txt.see(hits[0][0])
        self._select_match(*hits[0])
        return "break"

    def _find_prev(self, _e=None):
        if self.is_binary or self.is_large_text or self._mode != "text":
            return "break"
        needle = self._find_var.get()
        if not needle:
            return "break"
        hits = self._highlight_and_count()
        if not hits:
            return "break"
        cur = self.txt.index(tk.INSERT)
        for s, e in reversed(hits):
            if self.txt.compare(e, "<=", cur):
                self.txt.mark_set(tk.INSERT, s)
                self.txt.see(s)
                self._select_match(s, e)
                return "break"
        # 回绕到最后一个
        self.txt.mark_set(tk.INSERT, hits[-1][0])
        self.txt.see(hits[-1][0])
        self._select_match(*hits[-1])
        return "break"

    def _select_match(self, s, e):
        self.txt.tag_remove("sel", "1.0", "end")
        self.txt.tag_add("sel", s, e)

    def _replace_one(self, _e=None):
        needle = self._find_var.get()
        if not needle:
            return "break"
        try:
            sel_s = self.txt.index("sel.first")
            sel_e = self.txt.index("sel.last")
        except tk.TclError:
            self._find_next()
            return "break"
        selected = self.txt.get(sel_s, sel_e)
        if selected.lower() != needle.lower():
            self._find_next()
            return "break"
        self.txt.delete(sel_s, sel_e)
        self.txt.insert(sel_s, self._repl_var.get())
        self._find_next()
        return "break"

    def _replace_all(self, _e=None):
        needle = self._find_var.get()
        if not needle:
            return "break"
        hits = self._search_all(needle)
        if not hits:
            self._find_count.configure(text="无匹配")
            return "break"
        repl = self._repl_var.get()
        # 从后往前替换，避免索引偏移
        for s, e in reversed(hits):
            self.txt.delete(s, e)
            self.txt.insert(s, repl)
        self._find_count.configure(text="已替换 %d 处" % len(hits))
        self._highlight_and_count()
        return "break"

    def close(self):
        if not self.alive():
            return True
        if self._dirty:
            answer = messagebox.askyesnocancel(
                TITLE,
                "「%s」有未保存的修改，保存吗？" % display_name(self.path),
                parent=self.win)
            if answer is None:
                return False
            if answer and not self.save():
                return False
        self._destroy()
        return True

    def _close_raw(self):
        """关闭 mmap（大二进制只映射不读入内存，窗口关掉必须解除映射）。"""
        raw = getattr(self, "raw", None)
        if isinstance(raw, mmap.mmap):
            try:
                raw.close()
            except Exception:
                pass
        self.raw = b""

    def _destroy(self):
        if self._highlight_job is not None:
            try:
                self.win.after_cancel(self._highlight_job)
            except tk.TclError:
                pass
            self._highlight_job = None
        # 先销毁视图（可能还引用 raw），再解映射
        self._reset_views()
        self._close_raw()
        wins = getattr(self.app, "_editor_wins", None)
        if isinstance(wins, dict):
            wins.pop(os.path.normcase(os.path.abspath(self.path)), None)
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def alive(self):
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def bring(self):
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:
            pass

def _close_payload(data) -> None:
    raw = data[0] if data else None
    if isinstance(raw, mmap.mmap):
        try:
            raw.close()
        except Exception:
            pass

def _finish_editor(app, path, data, parent=None):
    parent = parent or getattr(app, "panel", None) or app.root
    try:
        ed = Editor(app, path, data)
    except Exception as exc:
        try:
            messagebox.showerror(TITLE, "打开失败：%s" % exc, parent=parent)
        except Exception:
            pass
        return None
    wins = getattr(app, "_editor_wins", None)
    if wins is None:
        wins = {}
        app._editor_wins = wins
    wins[os.path.normcase(os.path.abspath(path))] = ed
    return ed

def _open_editor_async(app, path, parent, key):
    """大文件后台读取：先弹加载框，读完回主线程建窗口。"""
    loaders = getattr(app, "_editor_loaders", None)
    if loaders is None:
        loaders = {}
        app._editor_loaders = loaders
    old = loaders.get(key)
    if old is not None:
        try:
            old.deiconify()
            old.lift()
            return None
        except tk.TclError:
            loaders.pop(key, None)

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    win = tk.Toplevel(app.root)
    win.title(TITLE)
    win.configure(bg=C_BG)
    win.resizable(False, False)
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass
    w, h = 480, 120
    try:
        sw = app.root.winfo_screenwidth()
        sh = app.root.winfo_screenheight()
    except Exception:
        sw, sh = 1280, 800
    win.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                  max(0, (sh - h) // 2)))
    tk.Label(win, text="正在打开…", bg=C_BG, fg=C_TEXT,
             font=(core.FONT, 11, "bold"), anchor="w").pack(
        fill="x", padx=20, pady=(18, 2))
    tk.Label(win, text="%s · %s" % (display_name(path),
                                    core.human_size(size)),
             bg=C_BG, fg=C_MUTED, font=(core.FONT, 9), anchor="w").pack(
        fill="x", padx=20)
    tk.Label(win, text="大文件在后台读取/映射，界面不会被整块内容卡住。",
             bg=C_BG, fg=C_MUTED, font=(core.FONT, 8), anchor="w").pack(
        fill="x", padx=20, pady=(4, 0))
    loaders[key] = win
    canceled = threading.Event()
    results: queue.Queue = queue.Queue()

    def on_close():
        canceled.set()
        loaders.pop(key, None)
        try:
            win.destroy()
        except tk.TclError:
            pass

    win.protocol("WM_DELETE_WINDOW", on_close)

    def work():
        try:
            data = read_file_any(path)
        except Exception as exc:
            if not canceled.is_set():
                results.put(("err", exc))
            return
        if canceled.is_set():
            _close_payload(data)
            return
        results.put(("ok", data))

    def poll():
        try:
            alive = bool(win.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:
            canceled.set()
            loaders.pop(key, None)
            return
        try:
            tag, payload = results.get_nowait()
        except queue.Empty:
            app.root.after(50, poll)
            return
        loaders.pop(key, None)
        if tag == "err":
            try:
                win.destroy()
            except tk.TclError:
                pass
            messagebox.showerror(TITLE, "打开失败：%s" % payload,
                                 parent=parent)
            return
        data = payload
        wins = getattr(app, "_editor_wins", None)
        old_ed = wins.get(key) if isinstance(wins, dict) else None
        if old_ed is not None and old_ed.alive():
            old_ed.bring()
            _close_payload(data)
            try:
                win.destroy()
            except tk.TclError:
                pass
            return
        # 建窗口会解析 JAR/PE/大文本；加载框先留着，完成后统一关
        ed = _finish_editor(app, path, data, parent)
        try:
            win.destroy()
        except tk.TclError:
            pass

    threading.Thread(target=work, daemon=True).start()
    app.root.after(50, poll)
    return None

def open_editor(app, path, *, sync=False):
    """打开（或聚焦）某个文件的快捷修改窗口；只接受文件。

    大文件（>8MB）默认后台读取并显示加载框；测试/批处理可传 sync=True。
    """
    path = os.path.abspath(path)
    parent = getattr(app, "panel", None) or app.root
    if not os.path.isfile(path):
        messagebox.showerror(TITLE, "只能打开文件：%s" % path, parent=parent)
        return None
    wins = getattr(app, "_editor_wins", None)
    if wins is None:
        wins = {}
        app._editor_wins = wins
    key = os.path.normcase(path)
    old = wins.get(key)
    if old is not None:
        if old.alive():
            old.bring()
            return old
        wins.pop(key, None)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if (not sync and size > ASYNC_OPEN_BYTES
            and getattr(app, "root", None) is not None):
        return _open_editor_async(app, path, parent, key)
    try:
        data = read_file_any(path)
    except EditorError as e:
        messagebox.showwarning(TITLE, str(e), parent=parent)
        return None
    return _finish_editor(app, path, data, parent)
