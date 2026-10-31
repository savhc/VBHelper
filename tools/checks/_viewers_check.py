# -*- coding: utf-8 -*-
"""特殊查看器自检：python _viewers_check.py（无网络）

覆盖：
  Hex 行格式化 / 搜索模式解析；
  PE 解析（真实 exe/dll：架构、节区、导入、导出）与 PeView 构建；
  Markdown 块解析 / Tk 渲染 / 链接标签；
  Editor 模式切换：.md 默认预览、二进制默认 PE/Hex、只读守卫。
"""
import glob
import gzip
import mmap
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import zipfile
from tkinter import ttk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from vbhelper import editor
from vbhelper.ui import scale
from vbhelper.ui.markdown import parse_blocks, render_markdown
from vbhelper.viewers import (ClassView, JarView, PeError, PeView,
                              format_hex_row, looks_pe, parse_hex_pattern,
                              parse_pe)
from vbhelper.viewers.hexview import HexView
from vbhelper.viewers.javaclass import class_listing, parse_class

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


class FakeApp:
    panel = None

    def __init__(self, root):
        self.root = root
        self.cfg = {"decompiler": {"auto_run": False}}

    def _center_on_panel(self, win):
        pass


def test_hex():
    data = bytes(range(32))
    off, hx, asc = format_hex_row(data, 0)
    check("Hex 偏移格式", off == "00000000", off)
    check("Hex 列含首尾字节", hx.startswith("00 01 02") and "0F" in hx, hx[:30])
    check("Hex ASCII 列", asc == "." * 16, repr(asc))
    off2, hx2, asc2 = format_hex_row(data, 16)
    check("Hex 第二行偏移", off2 == "00000010", off2)
    off3, hx3, asc3 = format_hex_row(data, 30)
    check("Hex 末行补空", hx3.rstrip().endswith("1E 1F") and asc3.rstrip() == "..",
          repr((hx3, asc3)))
    check("搜索 4D 5A 按 Hex", parse_hex_pattern("4D 5A") == b"MZ")
    check("搜索 4d5a 按 Hex", parse_hex_pattern("4d5a") == b"MZ")
    check("搜索 MZ 按文本", parse_hex_pattern("MZ") == b"MZ")
    check("搜索 0x4D 0x5A", parse_hex_pattern("0x4D 0x5A") == b"MZ")


def test_pe(root):
    exe = sys.executable
    with open(exe, "rb") as f:
        data = f.read()
    check("looks_pe 识别 exe", looks_pe(data) is True)
    check("looks_pe 拒绝文本", looks_pe(b"hello world") is False)
    pe = parse_pe(data)
    check("PE 架构识别", pe.machine_name in ("x64", "x86", "ARM64"),
          pe.machine_name)
    check("PE 入口/基址非零", pe.entry_rva > 0 and pe.image_base > 0)
    check("PE 节区非空", len(pe.sections) > 0, len(pe.sections))
    check("PE 导入非空", len(pe.imports) > 0, len(pe.imports))
    check("PE 导入计数", pe.import_count > 0, pe.import_count)
    view = PeView(root, data, on_goto=lambda off, label: None)
    root.update()
    check("PeView 树有节点", len(view.tree.get_children("")) >= 5,
          len(view.tree.get_children("")))
    view.destroy()

    dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "kernel32.dll")
    if os.path.isfile(dll):
        with open(dll, "rb") as f:
            dll_data = f.read()
        k = parse_pe(dll_data)
        check("kernel32 是 DLL", k.is_dll is True)
        check("kernel32 有导出", len(k.exports) > 0, len(k.exports))
        check("kernel32 节区名", any(s.name for s in k.sections))
    else:
        print("SKIP  未找到 kernel32.dll")

    dll32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "SysWOW64", "kernel32.dll")
    if os.path.isfile(dll32):
        with open(dll32, "rb") as f:
            pe32 = parse_pe(f.read())
        check("PE32 识别为 32 位", pe32.is_64 is False, pe32.optional_magic)
        check("PE32 有导入", len(pe32.imports) > 0, len(pe32.imports))
    else:
        print("SKIP  未找到 SysWOW64\\kernel32.dll")

    try:
        parse_pe(b"MZ" + b"\0" * 200)
        check("无效 PE 抛错", False)
    except PeError:
        check("无效 PE 抛错", True)


def test_markdown(root):
    src = ("# 标题\n\n这是 **粗体** 和 `code`，还有 [链接](https://example.com)。\n\n"
           "- 项目一\n- 项目二\n\n```python\nprint(1)\n```\n\n> 引用\n\n---\n")
    blocks = parse_blocks(src)
    kinds = [b["type"] for b in blocks]
    check("Markdown 块解析", kinds[:4] == ["heading", "paragraph", "list", "list"],
          kinds[:6])
    check("Markdown 代码块", any(b["type"] == "code" and b["lang"] == "python"
                                  for b in blocks))
    check("Markdown 引用/分隔线", "quote" in kinds and "hr" in kinds, kinds)
    txt = tk.Text(root)
    links = render_markdown(txt, src)
    root.update()
    check("Markdown 渲染标题", "标题" in txt.get("1.0", "end"))
    check("Markdown 渲染粗体/代码", "粗体" in txt.get("1.0", "end")
          and "code" in txt.get("1.0", "end"))
    check("Markdown 链接标签", len(links) >= 1 and "https://example.com" in links.values(),
          links)
    txt.destroy()


def find_notebook(widget):
    for child in widget.winfo_children():
        if isinstance(child, ttk.Notebook):
            return child
        found = find_notebook(child)
        if found is not None:
            return found
    return None


def find_tagged_text(widget, tag):
    for child in widget.winfo_children():
        if isinstance(child, tk.Text):
            try:
                if tag in child.tag_names():
                    return child
            except tk.TclError:
                pass
        found = find_tagged_text(child, tag)
        if found is not None:
            return found
    return None


def _java_tool(name):
    for path in (r"D:\java\java21\bin\%s.exe" % name,
                 r"D:\java\Java25\bin\%s.exe" % name,
                 shutil.which(name)):
        if path and os.path.isfile(path):
            return path
    return None


def test_jar(root):
    javac, jartool = _java_tool("javac"), _java_tool("jar")
    if not (javac and jartool):
        print("SKIP  未找到 javac/jar")
        return
    tmp = tempfile.mkdtemp(prefix="vbjarchk_")
    try:
        src = os.path.join(tmp, "Demo.java")
        with open(src, "w", encoding="utf-8") as f:
            f.write("package demo;\npublic class Demo implements Runnable {\n"
                    "  private int value = 42;\n"
                    "  public void run() { System.out.println(\"hi \" + value); }\n"
                    "}\n")
        classes = os.path.join(tmp, "classes")
        r = subprocess.run([javac, "-d", classes, src], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        check("javac 编译测试类", r.returncode == 0, r.stderr[:120])
        if r.returncode:
            return
        with open(os.path.join(tmp, "readme.txt"), "w", encoding="utf-8") as f:
            f.write("hello jar\n")
        jar_path = os.path.join(tmp, "demo.jar")
        r = subprocess.run([jartool, "cf", jar_path, "-C", classes, ".",
                            "-C", tmp, "readme.txt"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        check("jar 打包测试包", r.returncode == 0 and os.path.isfile(jar_path),
              r.stderr[:120])
        if r.returncode:
            return

        cfg = {"decompiler": {"command": "cmd /c echo DECOMP {input}", "auto_run": False}}
        view = JarView(root, jar_path, cfg=cfg)
        view.pack(fill="both", expand=True)
        root.update()
        check("JAR 树有根条目", len(view.tree.get_children("")) >= 2,
              len(view.tree.get_children("")))
        cls_names = [e.filename for e in view._entries
                     if e.filename.endswith(".class")]
        check("JAR 内 class 计数", len(cls_names) >= 1, cls_names[:3])
        if cls_names:
            view._show_entry(cls_names[0])
            root.update()
            child = view.preview.winfo_children()[0] if \
                view.preview.winfo_children() else None
            check("JAR 选中 class 显示 ClassView",
                  isinstance(child, ClassView), type(child).__name__)
            if isinstance(child, ClassView):
                info = child.info
                check("class 结构解析",
                      info.this_name == "demo/Demo" and len(info.methods) >= 2,
                      (info.this_name, len(info.methods)))
                listing = "\n".join(class_listing(info))
                check("class 字节码反汇编",
                      "aload_0" in listing and "invokevirtual" in listing)
                nb = find_notebook(child)
                if nb is not None:
                    nb.select(1)          # 字节码页签按需构建
                    root.update()
                btxt = find_tagged_text(child, "op")
                check("字节码有颜色标记", btxt is not None
                      and len(btxt.tag_ranges("op")) > 0
                      and len(btxt.tag_ranges("ref")) > 0,
                      len(btxt.tag_ranges("op")) if btxt else 0)
                child._run_decompile()
                for _ in range(200):
                    root.update()
                    time.sleep(0.01)
                    if not child._dec_running:
                        break
                dec_out = child.dec_text.get("1.0", "end")
                check("反编译命令异步执行", "DECOMP" in dec_out,
                      dec_out[:80].replace("\n", " "))
        view.destroy()

        # 内嵌 JAR：outer.jar 内含 lib/inner.jar
        outer = os.path.join(tmp, "outer.jar")
        with zipfile.ZipFile(outer, "w") as z:
            z.write(jar_path, "lib/inner.jar")
            z.writestr("readme.txt", "outer\n")
        ov = JarView(root, outer)
        ov.pack(fill="both", expand=True)
        root.update()
        ov._show_entry("lib/inner.jar")
        root.update()
        ov._open_nested("lib/inner.jar", os.path.getsize(jar_path))
        for _ in range(300):
            root.update()
            time.sleep(0.01)
            if not ov._nested_busy:
                break
        check("内嵌 JAR 可进入", ov._current_label == "lib/inner.jar"
              and len(ov._stack) == 1, ov._current_label)
        nested_cls = [e.filename for e in ov._entries
                      if e.filename.endswith(".class")]
        check("内嵌 JAR 有 class", bool(nested_cls), nested_cls[:2])
        ov._back()
        root.update()
        check("内嵌 JAR 可返回", ov._current_label == "outer.jar"
              and not ov._stack, ov._current_label)
        ov.destroy()

        app = FakeApp(root)
        ed = editor.open_editor(app, jar_path)
        check("编辑器 JAR 默认模式", ed is not None and ed._mode == "jar",
              None if ed is None else ed._mode)
        if ed is not None:
            check("JAR 视图已建", "jar" in ed._views)
            check("JAR 只读禁止保存", ed.save() is False)
            check("JAR 可切 Hex", ed._set_view_mode("hex") is True
                  and "hex" in ed._views)
            check("JAR 不允许文本", ed._set_view_mode("text") is False)
            ed._dirty = False
            ed.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_large_binary(root):
    tmp = tempfile.mkdtemp(prefix="vbbigchk_")
    path = os.path.join(tmp, "big.bin")
    raw = None
    try:
        with open(path, "wb") as f:
            f.truncate(70 * 1024 * 1024)
        raw, text, enc, _nl, _nln = editor.read_file_any(path)
        check("70MB 二进制走 mmap", isinstance(raw, mmap.mmap),
              type(raw).__name__)
        check("70MB 被识别为二进制", text is None and enc == "binary")
        app = FakeApp(root)
        ed = editor.open_editor(app, path, sync=True)
        check("70MB 编辑器可打开", ed is not None and ed._mode == "hex",
              None if ed is None else ed._mode)
        if ed is not None:
            held = ed.raw
            check("70MB Hex 视图已建", "hex" in ed._views)
            ed._dirty = False
            ed.close()
            check("关闭后 mmap 已释放",
                  isinstance(held, mmap.mmap) and held.closed is True)
    finally:
        if isinstance(raw, mmap.mmap):
            try:
                raw.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_large_jar(root):
    candidates = glob.glob(os.path.join(
        os.path.expanduser("~"), ".gradle", "caches", "*",
        "generated-gradle-jars", "*.jar"))
    big = [p for p in candidates if os.path.getsize(p) > 64 * 1024 * 1024]
    if not big:
        print("SKIP  未找到 >64MB 的 JAR")
        return
    path = max(big, key=os.path.getsize)
    t0 = time.time()
    view = JarView(root, path)
    view.pack(fill="both", expand=True)
    root.update()
    cost = time.time() - t0
    check("185MB 级 JAR 可打开", len(view._entries) > 1000,
          len(view._entries))
    check("大 JAR 建树 <5s", cost < 5.0, "%.2fs" % cost)
    cls_names = [e.filename for e in view._entries
                 if e.filename.endswith(".class")]
    check("大 JAR 有 class", bool(cls_names), len(cls_names))
    if cls_names:
        view._show_entry(cls_names[0])
        root.update()
        child = view.preview.winfo_children()[0] if \
            view.preview.winfo_children() else None
        check("大 JAR 点开 class 正常", isinstance(child, ClassView),
              type(child).__name__)
    view.destroy()


def test_class_direct(root):
    javac = _java_tool("javac")
    if not javac:
        print("SKIP  未找到 javac")
        return
    tmp = tempfile.mkdtemp(prefix="vbclschk_")
    try:
        src = os.path.join(tmp, "Solo.java")
        with open(src, "w", encoding="utf-8") as f:
            f.write("public class Solo { public void hi() { "
                    "System.out.println(\"x\"); } }\n")
        r = subprocess.run([javac, "-d", tmp, src], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            check("javac 编译单 class", False, r.stderr[:120])
            return
        cls = os.path.join(tmp, "Solo.class")
        app = FakeApp(root)
        ed = editor.open_editor(app, cls)
        check("单个 class 默认 Class 模式",
              ed is not None and ed._mode == "class",
              None if ed is None else ed._mode)
        if ed is not None:
            view = ed._views.get("class")
            check("单个 class 显示 ClassView", isinstance(view, ClassView),
                  type(view).__name__)
            check("单个 class 结构解析",
                  view is not None and view.info.this_name == "Solo")
            check("单个 class 只读", ed.save() is False)
            check("class 误选 JAR 自动切 Class",
                  ed._set_view_mode("jar") is True and ed._mode == "class")
            ed._dirty = False
            ed.close()

        # 前置头 + 标准 class：按偏移恢复
        with open(cls, "rb") as f:
            cdata = f.read()
        prefixed = os.path.join(tmp, "prefixed.class")
        with open(prefixed, "wb") as f:
            f.write(b"PACKHDR!!" + cdata)
        ed = editor.open_editor(app, prefixed)
        cv = ed._views.get("class") if ed is not None else None
        check("带前置头的 class 可恢复",
              isinstance(cv, ClassView) and cv.class_offset == 9
              and getattr(cv, "info", None) is not None,
              None if cv is None else getattr(cv, "class_offset", None))
        if ed is not None:
            ed._dirty = False
            ed.close()

        # gzip 压缩的 class：自动解压后应直接进完整 ClassView
        gz = gzip.compress(cdata)
        gzcv = ClassView(root, gz, "packed.class", cfg={})
        gzcv.pack(fill="both", expand=True)
        root.update()
        check("gzip class 自动解压",
              getattr(gzcv, "info", None) is not None
              and "gzip" in getattr(gzcv, "decode_note", ""),
              getattr(gzcv, "decode_note", ""))
        nb = find_notebook(gzcv)
        check("解压后是完整 Class 页签",
              nb is not None and len(nb.tabs()) == 5,
              len(nb.tabs()) if nb is not None else None)
        gzcv.destroy()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nonstandard_class(root):
    tmp = tempfile.mkdtemp(prefix="vbweirdcls_")
    try:
        bogus = os.path.join(tmp, "weird.class")
        with open(bogus, "wb") as f:
            f.write(b"NOT A JVM CLASS\n" + bytes(range(64)))
        app = FakeApp(root)
        ed = editor.open_editor(app, bogus)
        check("非标准 .class 仍进 Class 模式",
              ed is not None and ed._mode == "class",
              None if ed is None else ed._mode)
        if ed is not None:
            cv = ed._views.get("class")
            check("非标准 .class 有容错视图", isinstance(cv, ClassView))
            if isinstance(cv, ClassView):
                nb = find_notebook(cv)
                check("容错视图有 Hex/文本/说明/解码",
                      nb is not None
                      and [nb.tab(t, "text") for t in nb.tabs()]
                      == ["Hex", "文本", "说明", "解码"],
                      [nb.tab(t, "text") for t in nb.tabs()]
                      if nb is not None else None)
            check("非标准 .class 只读", ed.save() is False)
            ed._dirty = False
            ed.close()

        # JAR 里一个伪 .class：应显示容错视图，而不是空/异常
        jar = os.path.join(tmp, "weird.jar")
        with zipfile.ZipFile(jar, "w") as z:
            z.writestr("fake.class", b"just text, not a class\n")
            z.writestr("readme.txt", "x\n")
        view = JarView(root, jar)
        view.pack(fill="both", expand=True)
        root.update()
        view._show_entry("fake.class")
        root.update()
        child = view.preview.winfo_children()[0] if \
            view.preview.winfo_children() else None
        check("JAR 内伪 class 有容错视图", isinstance(child, ClassView),
              type(child).__name__)
        if isinstance(child, ClassView):
            nb = find_notebook(child)
            check("JAR 伪 class 页签",
                  nb is not None
                  and [nb.tab(t, "text") for t in nb.tabs()]
                  == ["Hex", "文本", "说明", "解码"])
        view.destroy()

        # 伪装成 .class 的 ZIP：Class 容错，同时可以切 JAR
        fake_zip = os.path.join(tmp, "zip.class")
        with zipfile.ZipFile(fake_zip, "w") as z:
            z.writestr("a.txt", "hello\n")
        ed = editor.open_editor(app, fake_zip)
        check("伪装 .class 默认容错 Class",
              ed is not None and ed._mode == "class",
              None if ed is None else ed._mode)
        if ed is not None:
            check("伪装 ZIP 可切 JAR",
                  ed._set_view_mode("jar") is True and ed._mode == "jar")
            ed._dirty = False
            ed.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_kbox_sample(root):
    sample = r"D:/CRKToolsBox/CTFDeobf/sample/1.0.jar"
    if not os.path.isfile(sample):
        print("SKIP  未找到 KBox 样本")
        return
    jv = JarView(root, sample, cfg={"decompiler": {"auto_run": False}})
    jv.pack(fill="both", expand=True)
    root.update()
    check("KBox JAR 可打开", len(jv._entries) > 100, len(jv._entries))
    names = ["shit/zen/modules/impl/combat/cg.class",
             "shit/zen/modules/impl/misc/jh.class",
             "shit/zen/render/CustomFont$GlyphEntry.class"]
    for name in names:
        jv._show_entry(name)
        root.update()
        cv = jv.preview.winfo_children()[0] if             jv.preview.winfo_children() else None
        info = getattr(cv, "info", None)
        check("KBox 自动解密 " + name.split("/")[-1],
              isinstance(cv, ClassView) and info is not None
              and "KBox" in getattr(cv, "decode_note", ""),
              getattr(cv, "decode_note", None))
    jv.destroy()


def test_custom_decoder(root):
    javac = _java_tool("javac")
    if not javac:
        print("SKIP  未找到 javac")
        return
    tmp = tempfile.mkdtemp(prefix="vbdecoderchk_")
    try:
        src = os.path.join(tmp, "Decoded.java")
        with open(src, "w", encoding="utf-8") as f:
            f.write("public class Decoded { public int x(){ return 1; } }")
        r = subprocess.run([javac, "-d", tmp, src], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            check("自定义解码器 javac", False, r.stderr[:120])
            return
        valid = os.path.join(tmp, "Decoded.class")
        script = os.path.join(tmp, "decrypt.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys, shutil" + chr(10))
            f.write("shutil.copy(" + repr(valid) + ", sys.argv[2])" + chr(10))
        cmd = ('"%s" "%s" {input} {output}'
               % (sys.executable, script))
        bogus = os.path.join(tmp, "bogus.class")
        with open(bogus, "wb") as f:
            f.write(b"encrypted-class-bytes" + bytes(range(32)))
        cfg = {"decoder": {"command": cmd}}
        cv = ClassView(root, open(bogus, "rb").read(), "bogus.class", cfg=cfg)
        cv.pack(fill="both", expand=True)
        root.update()
        nb = find_notebook(cv)
        check("非标准 class 有解码页签",
              nb is not None and "解码" in [nb.tab(t, "text") for t in nb.tabs()])
        cv._run_decoder()
        for _ in range(300):
            root.update()
            time.sleep(0.01)
            if not cv._decode_running:
                break
        check("自定义解码器后可解析",
              getattr(cv, "info", None) is not None
              and getattr(cv, "decode_note", "") == "自定义解码器",
              (getattr(cv, "info", None) is not None,
               getattr(cv, "decode_note", "")))
        nb = find_notebook(cv)
        check("解码后切到完整结构", nb is not None and len(nb.tabs()) == 5,
              len(nb.tabs()) if nb is not None else None)
        cv.destroy()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_decompile(root):
    try:
        from vbhelper.viewers.decompiler import detect_decompiler
    except Exception:
        print("SKIP  反编译识别模块不可用")
        return
    info = detect_decompiler(refresh=True)
    if info is None:
        print("SKIP  本机未找到可用反编译器")
        return
    check("自动识别本机反编译器", bool(info.command and info.java),
          info.label)
    try:
        from vbhelper.viewers.decompiler import find_java_for_recaf
        rjava = find_java_for_recaf()
    except Exception:
        rjava = None
    check("Recaf 使用 Java 22+", bool(rjava), rjava)
    javac = _java_tool("javac")
    if not javac:
        print("SKIP  未找到 javac")
        return
    tmp = tempfile.mkdtemp(prefix="vbautochk_")
    try:
        src = os.path.join(tmp, "Child.java")
        with open(src, "w", encoding="utf-8") as f:
            f.write(chr(10).join([
                "class Parent<T> { T get(){ return null; } }",
                "public class Child extends Parent<String> {",
                '  @Override String get(){ return "x"; }',
                "}",
                "",
            ]))
        r = subprocess.run([javac, "-d", tmp, src], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            check("自动反编译 javac", False, r.stderr[:120])
            return
        with open(os.path.join(tmp, "Child.class"), "rb") as f:
            data = f.read()
        cfg = {}
        cv = ClassView(root, data, "Child.class", cfg=cfg)
        cv.pack(fill="both", expand=True)
        root.update()
        check("ClassView 自动填入命令",
              bool((cfg.get("decompiler") or {}).get("command")))
        before = len(cv.mtree.get_children(""))
        cv.hide_synth.set(True)
        cv._refresh_members()
        root.update()
        after = len(cv.mtree.get_children(""))
        check("去噪隐藏 bridge/synthetic", after < before, (before, after))
        for _ in range(100):
            root.update()
            time.sleep(0.01)
            if cv._dec_running:
                break
        for _ in range(400):
            root.update()
            time.sleep(0.01)
            if not cv._dec_running:
                break
        out = cv.dec_text.get("1.0", "end")
        check("自动反编译出源码", "class Child" in out,
              out[:100].replace(chr(10), " "))
        cv.destroy()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_large_text(root):
    tmp = tempfile.mkdtemp(prefix="vbbigtxtchk_")
    path = os.path.join(tmp, "big.log")
    raw = None
    try:
        line = "2026-10-31 INFO hello 日志行\n"
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            n = 0
            while n < 3 * 1024 * 1024:
                f.write(line)
                n += len(line.encode("utf-8"))
        raw, text, enc, _nl, _nln = editor.read_file_any(path)
        check("大文本走分页标记",
              text is None and enc.startswith(editor.LARGE_TEXT_PREFIX), enc)
        app = FakeApp(root)
        ed = editor.open_editor(app, path, sync=True)
        check("大文本编辑器打开", ed is not None and ed._mode == "text",
              None if ed is None else ed._mode)
        if ed is not None:
            check("大文本是只读分页", ed.is_large_text and ed.save() is False)
            tv = ed._views.get("textview")
            check("大文本视图已建", tv is not None)
            check("大文本未使用编辑 Text", not ed.body.winfo_manager())
            if tv is not None:
                page1, off1, size = tv.current_page()
                tv.next()
                root.update()
                page2, off2, _ = tv.current_page()
                check("大文本可翻页", page2 == page1 + 1 and off2 > off1,
                      (page1, page2, off2))
                tv.prev()
                root.update()
                check("大文本可回页", tv.current_page()[0] == 1)
            check("大文本可切 Hex", ed._set_view_mode("hex") is True)
            check("大文本可回文本", ed._set_view_mode("text") is True)
            held = ed.raw
            ed._dirty = False
            ed.close()
            check("大文本关闭释放 mmap",
                  isinstance(held, mmap.mmap) and held.closed is True)
    finally:
        if isinstance(raw, mmap.mmap):
            try:
                raw.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_async_open(root):
    tmp = tempfile.mkdtemp(prefix="vbasyncchk_")
    path = os.path.join(tmp, "async.bin")
    try:
        with open(path, "wb") as f:
            f.truncate(9 * 1024 * 1024)
        app = FakeApp(root)
        res = editor.open_editor(app, path)       # >8MB，应走异步
        check("大文件异步开始（不阻塞返回）", res is None)
        wins = getattr(app, "_editor_wins", {})
        key = os.path.normcase(os.path.abspath(path))
        for _ in range(300):
            root.update()
            time.sleep(0.01)
            if key in wins:
                break
        ed = wins.get(key)
        check("大文件异步完成", ed is not None,
              None if ed is None else ed._mode)
        check("异步加载框已关闭",
              not getattr(app, "_editor_loaders", {}))
        if ed is not None:
            check("异步打开为 Hex", ed._mode == "hex", ed._mode)
            ed._dirty = False
            ed.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_editor(root):
    app = FakeApp(root)
    notes = []
    editor.messagebox.showinfo = lambda *a, **k: notes.append(("info", a))
    editor.messagebox.showerror = lambda *a, **k: notes.append(("error", a))
    editor.messagebox.showwarning = lambda *a, **k: notes.append(("warn", a))
    editor.messagebox.askyesno = lambda *a, **k: True

    tmp = tempfile.mkdtemp(prefix="vbview_")
    try:
        md = os.path.join(tmp, "demo.md")
        with open(md, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 标题\n\n正文 **粗体**。\n")
        ed = editor.open_editor(app, md)
        check("打开 .md", ed is not None)
        if ed is not None:
            check("md 默认预览", ed._mode == "md", ed._mode)
            check("md 视图已建", "md" in ed._views)
            check("预览模式禁止保存", ed.save() is False)
            check("切回文本", ed._set_view_mode("text") is True
                  and ed._mode == "text")
            check("文本内容正确", ed.txt.get("1.0", "end-1c").startswith("# 标题"))
            ed.txt.insert("end", "\n\n## 新章节\n")
            root.update()          # 让 <<Modified>> 先跑，再切预览
            ed._set_view_mode("md")
            md_view = ed._views.get("md")
            check("MD 预览跟随编辑",
                  md_view is not None
                  and "新章节" in md_view.txt.get("1.0", "end"))
            check("预览态有修改时保存会回到文本",
                  ed.save() is True and ed._mode == "text", ed._mode)
            with open(md, "r", encoding="utf-8") as f:
                check("预览态保存写回文件", "新章节" in f.read())
            ed._dirty = False
            ed.close()

        java = os.path.join(tmp, "Demo.java")
        with open(java, "w", encoding="utf-8", newline="\n") as f:
            f.write("public class Demo { int x = 1; }\n")
        ed = editor.open_editor(app, java)
        check("打开 .java", ed is not None and ed._mode == "text",
              None if ed is None else ed._mode)
        if ed is not None:
            check("java 文本内容", "public class Demo" in ed.txt.get("1.0", "end"))
            ed._dirty = False
            ed.close()

        # 真实 PE：默认 PE 模式，Hex 可搜索 MZ
        ed = editor.open_editor(app, sys.executable)
        check("打开 exe", ed is not None)
        if ed is not None:
            check("exe 默认 PE", ed._mode == "pe", ed._mode)
            check("PE 视图已建", "pe" in ed._views)
            check("PE 视图解析成功", getattr(ed._views.get("pe"), "pe", None)
                  is not None)
            check("切到 Hex", ed._set_view_mode("hex") is True
                  and "hex" in ed._views)
            hexv = ed._views.get("hex")
            hexv.find_var.set("MZ")
            check("Hex 搜索 MZ", hexv.find_next() >= 1)
            hexv.goto(0x100)
            check("Hex 跳转到 0x100", hexv._sel_row == 0x10, hexv._sel_row)
            check("二进制禁止文本模式", ed._set_view_mode("text") is False)
            check("二进制禁止保存", ed.save() is False)
            ed._dirty = False
            ed.close()

        # 普通二进制：默认 Hex，PE 模式被拒绝
        binp = os.path.join(tmp, "raw.bin")
        with open(binp, "wb") as f:
            f.write(bytes(range(256)) * 8)
        ed = editor.open_editor(app, binp)
        check("打开 raw.bin", ed is not None and ed._mode == "hex",
              None if ed is None else ed._mode)
        if ed is not None:
            check("raw 切 PE 被拒绝", ed._set_view_mode("pe") is False
                  and ed._mode == "hex")
            ed._dirty = False
            ed.close()

        check("打开过程没有错误弹窗",
              not [n for n in notes if n[0] in ("error", "warn")], notes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    root = tk.Tk()
    root.withdraw()
    scale.init(root)
    try:
        ttk.Style(root).theme_use("clam")
    except tk.TclError:
        pass
    editor.messagebox.showinfo = lambda *a, **k: None
    editor.messagebox.showerror = lambda *a, **k: None
    editor.messagebox.showwarning = lambda *a, **k: None
    editor.messagebox.askyesno = lambda *a, **k: True
    try:
        test_hex()
        test_pe(root)
        test_markdown(root)
        test_jar(root)
        test_large_binary(root)
        test_large_jar(root)
        test_class_direct(root)
        test_nonstandard_class(root)
        test_custom_decoder(root)
        test_kbox_sample(root)
        test_auto_decompile(root)
        test_large_text(root)
        test_async_open(root)
        test_editor(root)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    print("\n结果：%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
