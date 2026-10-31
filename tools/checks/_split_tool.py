# -*- coding: utf-8 -*-
"""一次性工具：把 vbhelper.py 按方法归属拆成 vbhelper 包（拆分后可直接删除本文件）"""
import ast
import os
import re

SRC = "vbhelper.py"
OUT = "vbhelper"

# 模块 -> (mixin 类名, 方法清单)
MODULES = {
    "scan": ("ScanMixin",
             "_submit_scan,_pump_results,_scan_done,_preheat_task,_enqueue_preheat,"
             "_preheat_tick,_scan_cached"),
    "meta": ("MetaMixin",
             "_init_marks,_meta_key,_meta_abs,_load_meta,_save_meta,_note_icon,"
             "_init_mark_tags,_mark_tag,_row_tags,_find_iid,_mark_menu,_apply_mark,"
             "_del_note,_set_note,_flash_status,_open_note,_note_dialog,_save_note"),
    "floating": ("FloatingMixin",
                 "_init_style,_build_ball,_set_ball_text,_place_ball,_press,_motion,"
                 "_release,_active_win,_snap,_park,_unpark,_stop_slide,_slide,_in_handle,"
                 "_mouse_over,_hover_poll,_ensure_panel,_btn,_popup,_center_on_panel,"
                 "_menu_window,_run_menu_item,_settings_window,_switch,_set_option,"
                 "_init_resizer,_rz_press,_resize_to,_toggle_panel,_expand,_collapse,"
                 "_toggle_topmost,_pin_icon,_lock_icon,_toggle_lock,_set_lock,_pick_dir,"
                 "_update_status,_save_ball_pos,_quit,_do_quit,_ball_menu"),
    "tree": ("TreeMixin",
             "_children_by_tag,_refresh,_load,_open_item,_tree_click,_key_expand,"
             "_user_expand,_insert_rows,_ensure_loaded,_on_loaded,_drill,_expand_all,"
             "_expand_queue,_collapse_all,_tree_dclick,_tree_menu,_mk_menu,_explorer,"
             "_copy,_copy_selected,_show_props,_pbtn,_dir_size"),
}

text = open(SRC, encoding="utf-8").read()
lines = text.splitlines(keepends=True)
tree = ast.parse(text)
cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VBHelper"][0]

spans = {}
for m in cls.body:
    if isinstance(m, ast.FunctionDef):
        start = m.lineno - 1
        for d in m.decorator_list:
            start = min(start, d.lineno - 1)
        spans[m.name] = (start, m.end_lineno)


def fix(code):
    """去掉 global FONT 声明，把 FONT 改成跨模块可更新的 core.FONT"""
    code = re.sub(r"^[ \t]*global FONT[ \t]*\n", "", code, flags=re.M)
    code = re.sub(r"(?<![\w.])FONT(?![\w])", "core.FONT", code)
    return code


os.makedirs(OUT, exist_ok=True)

# ---- core.py：常量 + 顶层工具函数（类之前的全部内容） ----
head = []
for n in tree.body:
    if isinstance(n, ast.ClassDef):
        break
    head.append("".join(lines[n.lineno - 1:n.end_lineno]))
core_src = ('# -*- coding: utf-8 -*-\n'
            '"""常量、配色与通用工具（从单文件版原样搬运）"""\n\n'
            + "\n\n".join(head) + "\n")
open(os.path.join(OUT, "core.py"), "w", encoding="utf-8").write(core_src)

# ---- 各 mixin 模块 ----
for mod, (cname, names) in MODULES.items():
    names = names.split(",")
    missing = [n for n in names if n not in spans]
    if missing:
        raise SystemExit("归属表里有找不到的方法: %s" % missing)
    body = []
    for n in names:
        s, e = spans[n]
        body.append(fix("".join(lines[s:e])) + "\n")
    src = ('# -*- coding: utf-8 -*-\n'
           '"""%s 相关逻辑（从单文件版拆分）"""\n'
           'from . import core\n'
           'from .core import *\n\n\n'
           'class %s:\n' % (mod, cname)) + "".join(body)
    open(os.path.join(OUT, mod + ".py"), "w", encoding="utf-8").write(src)

# ---- app.py：主类组装 + 入口 ----
init_src = fix("".join(lines[spans["__init__"][0]:spans["__init__"][1]]))
app_src = ('# -*- coding: utf-8 -*-\n'
           '"""VBHelper 主类：把各功能模块拼在一起"""\n'
           'from . import core\n'
           'from .core import *\n'
           'from .floating import FloatingMixin\n'
           'from .meta import MetaMixin\n'
           'from .scan import ScanMixin\n'
           'from .tree import TreeMixin\n\n\n'
           'class VBHelper(FloatingMixin, MetaMixin, ScanMixin, TreeMixin):\n'
           + init_src + "\n\n"
           'def main():\n'
           '    app = VBHelper()\n'
           '    app.root.mainloop()\n')
open(os.path.join(OUT, "app.py"), "w", encoding="utf-8").write(app_src)

open(os.path.join(OUT, "__init__.py"), "w", encoding="utf-8").write(
    '# -*- coding: utf-8 -*-\n"""VBHelper 包：氛围编程目录树悬浮助手"""\n'
    '__version__ = "1.0.0"\n')
open(os.path.join(OUT, "__main__.py"), "w", encoding="utf-8").write(
    '# -*- coding: utf-8 -*-\nfrom .app import main\n\nif __name__ == "__main__":\n    main()\n')

# 把用到的名字补进 core（import * 会带过去）
used = []
for mod, (_, names) in MODULES.items():
    used.extend(names.split(","))
print("生成完成:", sorted(os.listdir(OUT)))
print("方法总数:", len(spans), "| 已分配:", sum(len(v[1].split(',')) for v in MODULES.values()) + 1)
