"""提示词模板库的骨架：分类、反幻觉铁律、模板聚合、自定义模板与收藏的读写。

模板正文只写任务相关的部分；铁律在头尾各挂一份（开头管全程，结尾管输出），
这样 100+ 条模板不用各写一遍同样的约束，改约束也只改这一处。
"""
import json
import os

from .core import APP_NAME, save_cfg

PLACEHOLDER = "[请输入文本]"

# (key, 名称, 图标)；顺序就是界面里的顺序，Java 排最前
CATEGORIES = [
    ("java-bug", "Java · 修复 Bug", "tool"),
    ("java-new", "Java · 写新功能", "plus-circle"),
    ("java-port", "Java · 1:1 移植复刻", "copy"),
    ("java-review", "Java · 审查与重构", "search"),
    ("java-perf", "Java · 性能 / JVM / 并发", "zap"),
    ("java-frame", "Java · 框架专项", "layers"),
    ("c", "C 语言", "terminal"),
    ("python", "Python", "code"),
    ("re", "逆向工程", "eye"),
    ("skill", "写 Skill（技能包）", "package"),
    ("mcp", "写 MCP Server", "server"),
    ("agent", "Agent 与提示词工程", "cpu"),
    ("general", "通用工程", "tool"),
    ("meta", "反幻觉 / 自省 / 逼问", "target"),
    ("sec", "安全与 CTF", "shield"),
]
CAT_ORDER = [k for k, _, _ in CATEGORIES]
CAT_LABEL = {k: n for k, n, _ in CATEGORIES}
CAT_ICON = {k: i for k, _, i in CATEGORIES}

# 顶部铁律：管全程的行为
PRELUDE = """【开工前必读 · 铁律】
1. 不清楚就问我，一次把所有不确定的点问完再动手；禁止自己猜，禁止假设完就往下写。
2. 涉及 API、版本行为、配置项、报错含义这类事实，先联网核对官方文档再下结论。查不到就说查不到。
3. 禁止编造：文件名、类名、方法签名、行号、报错原文、链接、出处、版本号。拿不准就写「未确认」。
4. 只做我要求的事。不顺手重构、不顺手改格式、不动约定之外的代码。
5. 每一步都告诉我：做了什么、动了哪些文件、还剩什么没确定。"""

# 底部硬约束：管输出
HARD = """【必须遵守的输出规矩】
- 结论先行：第一句给答案，再展开依据。
- 分清「事实」与「推断」：事实要有出处或可验证；推断必须显式标注为推断。
- 有多个可行方案时，列出取舍让我选，不要替我拍板。
- 不能用工具或查不到的地方，直接说「我查不到」，禁止用看起来合理的内容填空。
- 如果我的要求本身有矛盾或做不到，先说清楚，不要硬做。
- 全程用中文；代码、命令、专有名词保留原文。"""

def full_text(tpl):
    """模板的完整正文：铁律 + 任务正文 + 输出规矩"""
    return "%s\n\n%s\n\n%s" % (PRELUDE, tpl.get("body", "").strip(), HARD)

# ---------------- 模板聚合 ----------------
from .prompts_java import ITEMS as _JAVA          # noqa: E402
from .prompts_code import ITEMS as _CODE          # noqa: E402
from .prompts_ai import ITEMS as _AI              # noqa: E402

BUILTIN = list(_JAVA) + list(_CODE) + list(_AI)
BUILTIN_BY_ID = {t["id"]: t for t in BUILTIN}

def cat_title(key):
    return CAT_LABEL.get(key, key)

def count_by_cat(items=None):
    out = {}
    for t in (items if items is not None else BUILTIN):
        out[t["cat"]] = out.get(t["cat"], 0) + 1
    return out

# ---------------- 自定义模板（存在配置里） ----------------
def custom(app):
    """配置里的自定义模板，顺手把坏数据过滤掉"""
    out = []
    for raw in (app.cfg.get("prompt_custom") or []):
        if not isinstance(raw, dict):
            continue
        body = str(raw.get("body") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not title and not body:
            continue
        cat = raw.get("cat") if raw.get("cat") in CAT_ORDER else "general"
        out.append({
            "id": str(raw.get("id") or ""),
            "cat": cat,
            "title": title or "未命名模板",
            "use": str(raw.get("use") or ""),
            "tags": str(raw.get("tags") or ""),
            "body": body,
            "custom": True,
        })
    return out

def save_custom(app, items):
    app.cfg["prompt_custom"] = items
    save_cfg(app.cfg)

def merged(app):
    """内置 + 自定义，自定义排前面（自己写的更常用）"""
    return custom(app) + BUILTIN

def find(app, tid):
    for t in merged(app):
        if t["id"] == tid:
            return t
    return None

def new_custom_id(items, name=""):
    """给自定义模板分配一个不冲突的 id"""
    used = {t["id"] for t in items}
    n = 1
    base = "my-%d"
    while (base % n) in used:
        n += 1
    return base % n

# ---------------- 收藏 / 最近使用 ----------------
def favourites(app):
    fav = app.cfg.get("prompt_fav")
    return [x for x in fav if isinstance(x, str)] if isinstance(fav, list) else []

def toggle_fav(app, tid):
    """收藏/取消收藏，返回操作后是否已收藏"""
    fav = favourites(app)
    if tid in fav:
        fav.remove(tid)
        on = False
    else:
        fav.insert(0, tid)
        on = True
    app.cfg["prompt_fav"] = fav
    save_cfg(app.cfg)
    return on

def recent(app):
    r = app.cfg.get("prompt_recent")
    return [x for x in r if isinstance(x, str)] if isinstance(r, list) else []

def push_recent(app, tid, keep=24):
    """复制过的模板进「最近使用」，最前面是刚用的"""
    r = [x for x in recent(app) if x != tid]
    r.insert(0, tid)
    app.cfg["prompt_recent"] = r[:keep]
    save_cfg(app.cfg)

# ---------------- 占位符处理 ----------------
# 判断哪个占位符是「放路径」的：看它前面那行标签里有没有这些词。
# 模板都把这个槽写在最前面，所以取第一个命中的就够准，不用做复杂的语义判断。
PATH_HINTS = ("路径", "文件", "目录", "位置", "项目", "模块", "仓库")

def line_before(text, idx):
    """取占位符前面那一行的内容，去掉列表符号与编号，拿来当填写提示"""
    head = text[:idx].rstrip()
    line = head.splitlines()[-1] if head else ""
    line = line.strip()
    while line[:1] in ("-", "*", "•", "·", "#", ">"):
        line = line[1:].strip()
    i = 0
    while i < len(line) and (line[i].isdigit() or line[i] in ".、)） "):
        i += 1
    if i and i < len(line):
        line = line[i:].strip()
    return line.rstrip("：:").strip()

def slots(text):
    """列出所有占位符及其提示标签，用于界面上的「跳到下一个空位」"""
    out, start = [], 0
    while True:
        i = text.find(PLACEHOLDER, start)
        if i < 0:
            return out
        out.append((i, i + len(PLACEHOLDER), line_before(text, i)))
        start = i + len(PLACEHOLDER)

def fill_path(text, path):
    """把路径填进第一个「看起来是路径槽」的占位符，返回 (新文本, 是否填了)"""
    if not path:
        return text, False
    for i, end, label in slots(text):
        if any(h in label for h in PATH_HINTS):
            return text[:i] + path + text[end:], True
    return text, False

def path_slot_label(text):
    """第一个路径槽的说明文字；没有就返回空串"""
    for _i, _e, label in slots(text):
        if any(h in label for h in PATH_HINTS):
            return label
    return ""

# ---------------- 导入 / 导出 ----------------
EXPORT_VERSION = 1

def export_text(app, only_custom=False):
    """导出成 json 文本，方便备份与分享"""
    from datetime import datetime
    items = custom(app) if only_custom else merged(app)
    data = {
        "app": APP_NAME,
        "version": EXPORT_VERSION,
        "exported": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "templates": [{k: v for k, v in t.items() if k != "custom"} for t in items],
    }
    return json.dumps(data, ensure_ascii=False, indent=1)

def export_file(app, path, only_custom=False):
    with open(path, "w", encoding="utf-8") as f:
        f.write(export_text(app, only_custom))

def parse_import(raw):
    """解析导入文件；返回 (模板列表, 错误信息)。容忍纯列表与包装对象两种格式"""
    try:
        data = json.loads(raw)
    except Exception as e:
        return [], "不是合法的 JSON：%s" % e
    items = data.get("templates") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], "文件里没找到模板列表（应该是顶层数组，或者对象里的 templates 字段）。"
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        body = str(it.get("body") or "").strip()
        title = str(it.get("title") or "").strip()
        if not body and not title:
            continue
        cat = it.get("cat") if it.get("cat") in CAT_ORDER else "general"
        out.append({"id": "", "cat": cat, "title": title or "导入的模板",
                    "use": str(it.get("use") or ""), "tags": str(it.get("tags") or ""),
                    "body": body, "custom": True})
    if not out:
        return [], "文件里没有可用的模板（每条至少要有标题或正文）。"
    return out, ""

def import_into(app, parsed):
    """把解析结果并进自定义模板；返回新增条数（id 冲突的都当新模板加）"""
    items = custom(app)
    n = 0
    for t in parsed:
        t = dict(t)
        t["id"] = new_custom_id(items + [t], t["title"])
        items.insert(0, t)
        n += 1
    save_custom(app, items)
    return n

def default_export_path(app):
    home = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(home):
        home = os.path.expanduser("~")
    return os.path.join(home, "VBHelper-提示词模板.json")
