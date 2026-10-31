"""版本解析与比较。

四种生态的常见写法归一成同一个 key 再比较；解析不了就返回 None，
上层一律标 ``unknown``——宁可说不知道，也不瞎报版本落后。

key 是 11 个整数：release 补齐 6 段 + 阶段 + 预发布档位 + 预发布序号
+ 补丁序号 + dev 序号。

阶段：0 = dev（1.2.dev3）、1 = 预发布（a/b/rc/SNAPSHOT）、2 = 正式版。

"""
from __future__ import annotations

import re

# 版本串本体：release + 可选 pre/post/dev/final + 可选 build metadata。
# "20240101.120000-1" 这类 SNAPSHOT 时间戳解析不了，直接返回 None。
_FULL = re.compile(
    r"^\s*[vV]?(?P<rel>\d+(?:[._\-]\d+)*)"
    r"(?:[._\-]?(?P<pre>a|alpha|b|beta|c|rc|pre|preview|snapshot"
    r"|next|nightly|canary)[._\-]?(?P<pren>\d*))?"
    r"(?:[._\-]?(?P<post>post|rev|r)[._\-]?(?P<postn>\d*))?"
    r"(?:[._\-]?(?P<dev>dev)[._\-]?(?P<devn>\d*))?"
    r"(?:[._\-]?(?P<final>final|ga|release|stable))?"
    r"(?:\+(?P<local>[A-Za-z0-9.\-]+))?"
    r"\s*$",
    re.IGNORECASE,
)

# 预发布档位：a < b < rc。SNAPSHOT/next 这类没约定顺序的一律按 rc 档。
_PRE_RANK = {"a": 1, "alpha": 1, "b": 2, "beta": 2,
             "c": 3, "rc": 3, "pre": 3, "preview": 3,
             "snapshot": 3, "next": 3, "nightly": 3, "canary": 3}

_WILDCARD = {"", "*", "x", "latest", "any", "release"}
_URL_LIKE = ("file:", "git:", "git+", "http:", "https:", "workspace:",
             "npm:", "link:", "path:", "github:", "portal:")

STATUS_LABEL = {
    "ok": "正常",
    "outdated": "过期",
    "major": "大版本落后",
    "vulnerable": "有漏洞",
    "unknown": "未知",
}

# 严重度从高到低，排序/统计都按这个顺序
STATUS_ORDER = ("vulnerable", "major", "outdated", "ok", "unknown")

def parse(value) -> tuple[int, ...] | None:
    """版本串 -> 可比较 key；解析不了返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _WILDCARD:
        return None
    match = _FULL.match(text)
    if not match:
        return None
    release = [int(part) for part in re.split(r"[._\-]", match.group("rel"))]
    release = release[:6]
    release += [0] * (6 - len(release))

    pre = (match.group("pre") or "").lower()
    post = (match.group("post") or "").lower()
    dev = (match.group("dev") or "").lower()
    if pre:
        stage = 1
    elif dev:
        stage = 0
    else:
        stage = 2
    key = tuple(release) + (
        stage,
        _PRE_RANK.get(pre, 3) if pre else 0,
        int(match.group("pren") or 0) if pre else 0,
        int(match.group("postn") or 0) if post else 0,
        int(match.group("devn") or 0) if dev else 0,
    )
    return key

def compare(left, right) -> int | None:
    """比较两个版本串：1 / 0 / -1；任一解析不了返回 None。"""
    a, b = parse(left), parse(right)
    if a is None or b is None:
        return None
    return (a > b) - (a < b)

def major(value) -> int | None:
    """主版本号；解析不了返回 None。"""
    key = parse(value)
    return key[0] if key else None

def clean_declared(declared, ecosystem: str = "pypi") -> str | None:
    """从声明串里取出可用于比较的版本号。

    ``^1.2.3`` / ``>=1.0,<2`` / ``[1.0,2.0)`` / ``1.x`` 这类范围取能确定
    的一端；混进 git/url/变量的一律返回 None（不猜）。
    """
    if declared is None:
        return None
    text = str(declared).strip()
    if text.lower() in _WILDCARD:
        return None
    low = text.lower()
    if low.startswith(_URL_LIKE) or "${" in text:
        return None

    if ecosystem == "pypi":
        text = text.split(";", 1)[0]            # 环境标记
        text = re.sub(r"\[[^\]]*\]", "", text)   # extras
        if " @ " in text or text.startswith("@"):
            return None
        for part in text.split(","):
            part = part.strip()
            if part.startswith("!="):            # 排除项不做「当前版本」
                continue
            match = re.match(r"^(?:==|===|>=|<=|~=|>|<)?\s*([0-9][^\s*]*)", part)
            if match:
                return match.group(1).rstrip(".") or None
        return None

    if ecosystem == "npm":
        if low.startswith(_URL_LIKE):
            return None
        if "||" in text:
            text = text.split("||", 1)[0]
        if " - " in text:
            text = text.split(" - ", 1)[0]
        text = text.strip()
        text = re.sub(r"^(?:[<>=^~]+)?\s*", "", text)
        text = text.strip()
        if text[:1] in ("v", "V") and text[1:2].isdigit():
            text = text[1:]
        text = re.sub(r"\.(?:x|X|\*)$", "", text)
        return text if re.match(r"^[0-9]", text) else None

    if ecosystem == "maven":
        text = text.strip()
        if text.startswith(("[", "(")) and text.endswith(("]", ")")):
            inner = text[1:-1]
            if "," in inner:
                lower_bound = inner.split(",", 1)[0].strip()
                text = lower_bound or inner.split(",", 1)[1].strip()
            else:
                text = inner
        return text if re.match(r"^[0-9]", text) else None

    if ecosystem == "go":
        text = text.strip()
        return text if re.match(r"^v?[0-9]", text) else None

    return None

def status_for(current, latest, *, vulnerable: bool = False) -> str:
    """依赖状态：vulnerable > major > outdated > ok > unknown。"""
    if vulnerable:
        return "vulnerable"
    if not current or not latest:
        return "unknown"
    order = compare(latest, current)
    if order is None:
        return "unknown"
    if order <= 0:
        return "ok"
    cur_major, new_major = major(current), major(latest)
    if cur_major is None or new_major is None:
        return "outdated"
    return "major" if new_major > cur_major else "outdated"
