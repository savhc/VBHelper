# -*- coding: utf-8 -*-
"""UI 控件与窗口壳自检：widgets.py / window.py（vbhelper/ui）。

逻辑断言为主；需要真实窗口映射的段落用 deiconify + update_idletasks
后再 withdraw。不写任何配置文件（cfg 用纯内存 dict）。
"""
import copy
import os
import sys
import time
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tkinter as tk

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("PASS  %s" % name)
    else:
        print("FAIL  %s  %s" % (name, detail))
        FAILS.append(name)


def bg_image_attached(widget):
    """画布主背景图已挂上（itemcget 非空）。"""
    try:
        canvas = getattr(widget, "_canvas", widget)   # Card 的画布在属性里
        item = getattr(widget, "_bg_item", None)
        if item is None:
            return False
        img = canvas.itemcget(item, "image")
        return bool(img)
    except Exception as e:
        print("      (%s)" % e)
        return False


def main():
    root = tk.Tk()
    root.withdraw()

    from vbhelper.ui import icons, render, scale, theme, widgets, window
    from vbhelper.ui.widgets import ConfirmDialog
    from PIL import Image

    scale.init(root)
    cfg = {"theme": {"preset": "midnight", "accent": "#7C9BFF",
                     "animations": "full"},
           "win_geometry": {}}
    cfg_snapshot = copy.deepcopy(cfg)
    tm = theme.ThemeManager(root, cfg)
    _, tokens = tm.current()

    # ------------------------------------------------------------------
    # 12 个控件全部实例化
    # ------------------------------------------------------------------
    host = tk.Frame(root, bg=tokens.bg)
    host.pack(fill="both", expand=True)

    card = widgets.Card(host, "基础设置", "外观与行为")
    card.pack(fill="both", expand=True, padx=scale.s(8), pady=scale.s(4))
    tk.Label(card.body, text="卡片内容行").pack(padx=scale.s(8),
                                                 pady=scale.s(8))

    btn_p = widgets.PillButton(host, "主操作", lambda: None, kind="primary")
    btn_s = widgets.PillButton(host, "次操作", lambda: None, kind="secondary")
    btn_g = widgets.PillButton(host, "幽灵", lambda: None, kind="ghost")
    btn_d = widgets.PillButton(host, "删除", lambda: None, kind="danger")
    for b in (btn_p, btn_s, btn_g, btn_d):
        b.pack(side="left", padx=scale.s(4), pady=scale.s(4))

    ibtn = widgets.IconButton(host, "refresh", lambda: None, tooltip="刷新")
    ibtn.pack(side="left", padx=scale.s(4))

    tog = widgets.ToggleSwitch(host, value=True,
                               command=lambda v: None)
    tog.pack(side="left", padx=scale.s(4))

    seg = widgets.SegmentedControl(host, ["全部", "进行中", "已完成"], "全部",
                                   command=lambda v: None)
    seg.pack(side="left", padx=scale.s(4))

    sentry = widgets.SearchEntry(host, "搜索…", on_change=lambda t: None)
    sentry.pack(fill="x", padx=scale.s(8), pady=scale.s(4))

    side = widgets.SidebarItem(host, "folder", "文件", count=3, active=True,
                               command=lambda t: None)
    side.pack(fill="x", padx=scale.s(8), pady=scale.s(2))
    side2 = widgets.SidebarItem(host, "star", "收藏", active=False)
    side2.pack(fill="x", padx=scale.s(8), pady=scale.s(2))

    badge_n = widgets.Badge(host, "NEW", tone="neutral")
    badge_a = widgets.Badge(host, "推荐", tone="accent")
    badge_s = widgets.Badge(host, "OK", tone="success")
    badge_w = widgets.Badge(host, "注意", tone="warning")
    badge_d = widgets.Badge(host, "失败", tone="danger")
    for b in (badge_n, badge_a, badge_s, badge_w, badge_d):
        b.pack(side="left", padx=scale.s(4))

    empty = widgets.EmptyState(host, "folder", "还没有收藏",
                               "点击右上角添加", action_text="去添加",
                               action=lambda: None)
    empty.pack(fill="both", expand=True, padx=scale.s(8), pady=scale.s(4))

    overlay = widgets.LoadingOverlay(host, "正在加载…")

    check("控件可实例化", True)

    # 真实映射后检查背景图挂载
    root.deiconify()
    root.update_idletasks()
    root.update()
    root.withdraw()

    check("PillButton 背景图挂上", bg_image_attached(btn_p))
    check("IconButton 背景图挂上", bg_image_attached(ibtn))
    check("ToggleSwitch 背景图挂上", bg_image_attached(tog))
    check("SegmentedControl 背景图挂上", bg_image_attached(seg))
    check("SidebarItem 背景图挂上", bg_image_attached(side))
    check("Badge 背景图挂上", bg_image_attached(badge_n))
    check("Card 背景图挂上", bg_image_attached(card))
    overlay.hide()
    check("LoadingOverlay 可隐藏", not overlay.winfo_ismapped())

    # ------------------------------------------------------------------
    # PillButton 四种 kind 颜色来自 tokens
    # ------------------------------------------------------------------
    check("pill primary=accent", btn_p._colors["normal"] == tokens.accent,
          "got=%s want=%s" % (btn_p._colors["normal"], tokens.accent))
    check("pill primary hover/pressed",
          btn_p._colors["hover"] == tokens.accent_hover and
          btn_p._colors["pressed"] == tokens.accent_pressed)
    check("pill secondary=elevated",
          btn_s._colors["normal"] == tokens.elevated,
          "got=%s" % btn_s._colors["normal"])
    check("pill secondary border=border", btn_s._border == tokens.border)
    check("pill ghost=父底色",
          btn_g._colors["normal"] == btn_g.master["bg"] or
          btn_g._colors["normal"] == tokens.bg,
          "got=%s" % btn_g._colors["normal"])
    check("pill danger=danger",
          btn_d._colors["normal"] == tokens.danger,
          "got=%s" % btn_d._colors["normal"])
    check("pill 文字色存在", all(isinstance(b._fg, str) for b in
                                 (btn_p, btn_s, btn_g, btn_d)))

    # ------------------------------------------------------------------
    # 状态色
    # ------------------------------------------------------------------
    check("toggle on 填充=accent", tog._value is True)
    tog.set(False)
    root.update_idletasks()
    check("toggle off 切换生效", tog.get() is False)
    check("sidebar active=selected", side._active is True)
    side.set_active(False)
    check("sidebar 去高亮", side._active is False)
    check("badge neutral=elevated", badge_n._fill == tokens.elevated,
          "got=%s" % badge_n._fill)
    check("badge accent=accent_soft", badge_a._fill == tokens.accent_soft,
          "got=%s" % badge_a._fill)
    check("seg get", seg.get() == "全部")
    seg.set("进行中", fire=False)
    check("seg set 生效", seg.get() == "进行中")

    # SearchEntry 交互
    sentry.set("关键词")
    check("SearchEntry set/get", sentry.get() == "关键词")
    sentry.clear()
    root.update_idletasks()
    check("SearchEntry 清空+占位符", sentry.get() == "" and sentry._ph_on)

    # ------------------------------------------------------------------
    # apply_theme 不抛错 + set_theme 广播
    # ------------------------------------------------------------------
    nord = theme.build_preset("nord").tokens
    widgets_to_theme = [card, btn_p, btn_s, btn_g, btn_d, ibtn, tog, seg,
                        sentry, side, side2, badge_n, badge_a, badge_s,
                        badge_w, badge_d, empty, overlay]
    try:
        for w in widgets_to_theme:
            w.apply_theme(nord)
        root.update_idletasks()
        check("全部控件 apply_theme 不抛错", True)
    except Exception as e:
        check("全部控件 apply_theme 不抛错", False, repr(e))

    widgets.set_theme(nord)
    root.update_idletasks()
    check("set_theme 广播后 pill 换色",
          btn_p._colors["normal"] == nord.accent,
          "got=%s want=%s" % (btn_p._colors["normal"], nord.accent))
    widgets.set_theme(tokens)   # 换回默认
    root.update_idletasks()
    check("set_theme 换回默认", btn_p._colors["normal"] == tokens.accent)

    # ------------------------------------------------------------------
    # Toast：自动消失 + 多条不叠死
    # ------------------------------------------------------------------
    root.deiconify()
    root.update_idletasks()
    t1 = widgets.Toast(root, "第一条", duration=3000)
    root.update_idletasks()
    t2 = widgets.Toast(root, "第二条", duration=50)
    root.update_idletasks()
    y1 = t1.winfo_y()
    y2 = t2.winfo_y()
    check("多条 Toast 纵向排布", y1 != y2 and min(y1, y2) >= 0,
          "y1=%s y2=%s" % (y1, y2))
    t1.destroy()
    check("Toast 手动销毁", not t1.winfo_exists())
    # 等待 50ms 的自动消失
    deadline = time.time() + 3.0
    while t2.winfo_exists() and time.time() < deadline:
        root.update()
        time.sleep(0.01)
    check("Toast 自动消失(50ms)", not t2.winfo_exists())
    root.withdraw()

    # ------------------------------------------------------------------
    # ConfirmDialog：True/False（程序化点击内部按钮）
    # ------------------------------------------------------------------
    box = {}

    def open_ok():
        dlg = ConfirmDialog(root, "确认操作", "要继续吗？")
        box["ok"] = dlg.result

    def click(btn_name):
        for w in root.winfo_children():
            if isinstance(w, ConfirmDialog):
                getattr(w, btn_name).invoke()
                return

    root.after(150, lambda: click("btn_ok"))
    open_ok()
    check("ConfirmDialog 确定返回 True", box.get("ok") is True,
          "got=%r" % box.get("ok"))

    def open_cancel():
        dlg = ConfirmDialog(root, "确认操作", "要删除吗？", danger=True)
        box["cancel"] = dlg.result

    root.after(150, lambda: click("btn_cancel"))
    open_cancel()
    check("ConfirmDialog 取消返回 False", box.get("cancel") is False,
          "got=%r" % box.get("cancel"))

    # ------------------------------------------------------------------
    # FeatureWindow / WindowManager
    # ------------------------------------------------------------------
    ctx = types.SimpleNamespace(root=root, theme=tm, cfg=cfg)
    wm = window.WindowManager(root, tm)

    def factory():
        return window.FeatureWindow(ctx, "feat1", "测试窗口",
                                    size=(640, 480), min_size=(400, 300))

    w1 = wm.open("feat1", factory)
    root.update_idletasks()
    w1b = wm.open("feat1", factory)
    check("WindowManager 单例", w1 is w1b,
          "%s vs %s" % (id(w1), id(w1b)))
    check("FeatureWindow content/actions 存在",
          isinstance(w1.content(), tk.Frame) and
          isinstance(w1.actions(), tk.Frame))
    w1.set_status("就绪")
    check("FeatureWindow set_status",
          w1._status_lbl["text"] == "就绪")

    w1.geometry("500x400+60+60")
    root.update_idletasks()
    geo = w1.remember_geometry("feat1")
    check("remember_geometry 写入 cfg",
          cfg["win_geometry"].get("feat1") == geo and geo.startswith("500x400"),
          "geo=%r cfg=%r" % (geo, cfg["win_geometry"].get("feat1")))

    wm.close("feat1")
    check("close 后窗口销毁", not w1.winfo_exists())
    w2 = wm.open("feat1", factory)
    root.update_idletasks()
    check("close 后重开是新窗口", w2 is not w1)
    check("重开恢复几何", w2.geometry().startswith("500x400"),
          "geo=%r" % w2.geometry())
    check("ESC 绑定存在", bool(w2.bind("<Escape>")))

    w3 = wm.open("feat2", lambda: window.FeatureWindow(
        ctx, "feat2", "窗口二", size=(500, 400), min_size=(300, 240)))
    root.update_idletasks()
    check("第二个 feature 独立", wm.get("feat2") is w3)
    try:
        w2.apply_theme()
        w3.apply_theme()
        check("FeatureWindow apply_theme 不抛错", True)
    except Exception as e:
        check("FeatureWindow apply_theme 不抛错", False, repr(e))
    wm.on_theme_changed()
    root.update_idletasks()
    check("WindowManager on_theme_changed 不抛错", True)

    # DWM：普通环境（含老系统回退）静默不崩
    try:
        window.apply_dark_mode(0, True)
        window.enable_round_corners(0)
        window.set_caption_colors(0, "#15171E", "#D7DBE6")
        window.round_window(w3)
        window.circle_window(w3)
        check("DWM/region 调用静默不崩", True)
    except Exception as e:
        check("DWM/region 调用静默不崩", False, repr(e))

    wm.close_all()
    root.update_idletasks()
    check("close_all 后全部销毁",
          not w2.winfo_exists() and not w3.winfo_exists())

    # ------------------------------------------------------------------
    # cfg 只被 win_geometry 动过（theme 段未被控件写坏）
    # ------------------------------------------------------------------
    check("cfg.theme 未被改写", cfg.get("theme") == cfg_snapshot.get("theme"),
          "%r" % cfg.get("theme"))

    host.destroy()
    root.destroy()

    print()
    if FAILS:
        print("FAILED: %d 项 -> %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("ALL PASS: _ui_widgets_check.py")


if __name__ == "__main__":
    main()
