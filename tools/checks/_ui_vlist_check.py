# -*- coding: utf-8 -*-
"""UI 虚拟列表自检：virtual_list（vbhelper/ui）。

root.withdraw() 隐藏窗口；视口尺寸用桩替换（_view_w/_view_h 注入），
逻辑断言为主。不写任何配置文件，不碰真实数据。
"""
import os
import sys
import time

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


class E:
    """伪事件对象。"""

    def __init__(self, x=0, y=0, delta=0):
        self.x, self.y, self.delta = x, y, delta


def main():
    root = tk.Tk()
    root.withdraw()

    from vbhelper.ui import scale, theme, virtual_list
    scale.init(root)
    tm = theme.ThemeManager(root, {})
    VL = virtual_list.VirtualList

    drawn = []

    def render(canvas, row, index, x, y, w, h, state):
        drawn.append(index)
        t = state["theme"]
        bg = t.color("selected" if state["selected"]
                     else "hover" if state["hover"] else "bg")
        canvas.create_rectangle(x, y, x + w, y + h, fill=bg, outline="")
        canvas.create_text(x + 10, y + h // 2, text=str(row), anchor="w",
                           fill=t.color("text"), font=t.font(12))

    def make(rh=40, **kw):
        drawn.clear()
        vl = VL(root, tm, row_height=rh, render_row=render, **kw)
        vl.pack()
        vl._view_w = lambda: 320   # 桩视口：未映射窗口拿不到真实尺寸
        vl._view_h = lambda: 240
        return vl

    # ------------------------------------------------------------------
    # 1. 固定行高 100k 行
    # ------------------------------------------------------------------
    vl = make()
    rows = list(range(100000))
    t0 = time.perf_counter()
    vl.set_rows(rows, selected=0)
    dt = time.perf_counter() - t0
    check("100k set_rows 固定行高耗时", dt < 1.0, "%.3fs" % dt)
    check("selection 取值", vl.selection() == 0)
    check("固定行高总高", vl._total == 100000 * 40,
          "total=%s" % vl._total)
    check("固定行高整除定位", vl._index_at_y(40 * 500 + 20) == 500 and
          vl._index_at_y(0) == 0)
    check("初始视口只画前 6 行", set(drawn) == set(range(6)),
          "drawn=%s" % sorted(set(drawn)))
    n_items = len(vl._canvas.find_all())
    check("视口外行不创建 item(固定)", n_items <= 6 * 2 + 2,
          "items=%d" % n_items)

    # scroll_to 精度：首/中/尾
    vl.scroll_to(0)
    check("scroll_to(0) 回顶", vl._scroll == 0.0 and
          vl._index_at_y(vl._scroll) == 0)
    vl.scroll_to(99999)
    mx = vl._max_scroll()
    check("scroll_to 末行钳底", abs(vl._scroll - mx) < 1e-6 and
          mx == 100000 * 40 - 240, "scroll=%s max=%s" % (vl._scroll, mx))
    last_top = vl._y_at(99999)
    check("末行完整可见", vl._scroll <= last_top and
          last_top + 40 <= vl._scroll + 240)
    vl.scroll_to(50000)
    check("scroll_to 中间居中",
          abs((vl._y_at(50000) - vl._scroll) - (240 - 40) / 2) < 1e-6,
          "off=%s" % (vl._y_at(50000) - vl._scroll))

    # ------------------------------------------------------------------
    # 2. 滚轮：一次约 3 行
    # ------------------------------------------------------------------
    vl.scroll_to(0)
    drawn.clear()
    vl._wheel(E(delta=-120))
    check("滚轮一格 3 行", abs(vl._scroll - 120) < 1e-6,
          "scroll=%s" % vl._scroll)
    check("滚轮后视口首行", vl._index_at_y(vl._scroll) == 3)
    check("滚轮只画视口行", set(drawn) <= set(range(3, 9)),
          "drawn=%s" % sorted(set(drawn)))
    vl._wheel(E(delta=120))
    check("滚轮向上回顶", vl._scroll == 0.0)

    t0 = time.perf_counter()
    for i in range(100):
        vl._scroll_to(i * 37.0)
    dt = time.perf_counter() - t0
    check("100 次滚动重绘耗时", dt < 2.0, "%.3fs" % dt)

    # ------------------------------------------------------------------
    # 3. 可变行高 100k 行：前缀和 + 二分
    # ------------------------------------------------------------------
    def rh(row, index):
        return 20 + (index % 3) * 30   # 20/50/80 循环

    vl2 = make(rh)
    t0 = time.perf_counter()
    vl2.set_rows(rows)
    dt = time.perf_counter() - t0
    check("100k set_rows 可变行高耗时", dt < 3.0, "%.3fs" % dt)
    heights = [rh(r, i) for i, r in enumerate(rows)]
    pre = [0]
    for h in heights:
        pre.append(pre[-1] + h)
    check("可变行高前缀和", vl2._total == pre[-1] and
          vl2._y_at(12345) == pre[12345] and vl2._y_at(0) == 0,
          "total=%s" % vl2._total)
    check("可变行高总高=逐行求和", vl2._total == sum(heights))
    check("可变行高二分定位",
          vl2._index_at_y(pre[7]) == 7 and vl2._index_at_y(pre[7] - 1) == 6
          and vl2._index_at_y(0) == 0)
    n_items = len(vl2._canvas.find_all())
    check("视口外行不创建 item(可变)", n_items < 40,
          "items=%d" % n_items)
    vl2.scroll_to(50000)
    check("可变行高 scroll_to 居中可见",
          vl2._scroll <= pre[50000] and pre[50001] <= vl2._scroll + 240,
          "scroll=%s top=%s" % (vl2._scroll, pre[50000]))

    # refresh_heights：外部状态变化后重算
    mode = {"big": False}

    def rh2(row, index):
        return 80 if mode["big"] else 20

    vl2b = make(rh2)
    vl2b.set_rows(list(range(100)))
    check("refresh_heights 前总高", vl2b._total == 2000)
    mode["big"] = True
    vl2b.refresh_heights()
    check("refresh_heights 重算前缀和", vl2b._total == 8000,
          "total=%s" % vl2b._total)

    # ------------------------------------------------------------------
    # 4. refresh_row 只重绘目标行
    # ------------------------------------------------------------------
    vl3 = make()
    vl3.set_rows(list(range(100)))
    before = dict(vl3._row_items)
    n_items = len(vl3._canvas.find_all())
    vl3.refresh_row(1)
    after = vl3._row_items
    check("refresh_row 邻行 item 不变",
          after[0] == before[0] and after[2] == before[2],
          "%s / %s" % (after.get(0), before[0]))
    check("refresh_row 目标行换新 item",
          after[1] != before[1] and after[1][0] > before[1][1],
          "%s -> %s" % (before[1], after[1]))
    check("refresh_row item 总数不变",
          len(vl3._canvas.find_all()) == n_items)
    vl3.refresh_row(90)   # 视口外：安全 no-op
    check("refresh_row 视口外安全",
          len(vl3._canvas.find_all()) == n_items and 90 not in vl3._row_items)

    # hover 用定点重绘：从无 hover 到有 hover 只重画新行
    drawn.clear()
    vl3._hover_move(E(x=5, y=45))
    check("hover 定点重绘", vl3._hover == 1 and set(drawn) == {1},
          "hover=%s drawn=%s" % (vl3._hover, sorted(set(drawn))))
    drawn.clear()
    vl3._hover_move(E(x=5, y=85))   # hover 1 -> 2：两行都要重画
    check("hover 切换重画两行", vl3._hover == 2 and set(drawn) == {1, 2},
          "hover=%s drawn=%s" % (vl3._hover, sorted(set(drawn))))

    # ------------------------------------------------------------------
    # 5. 键盘导航
    # ------------------------------------------------------------------
    vl4 = make()
    vl4.set_rows(list(range(1000)), selected=0)
    for _ in range(10):
        vl4._key_move_down(E())
    check("Down 连按 10 次", vl4.selection() == 10,
          "sel=%s" % vl4.selection())
    # ensure_visible 把选中行底对齐视口底：scroll = y(10)+h-view
    check("Down 跟随滚动", abs(vl4._scroll - (10 * 40 + 40 - 240)) < 1e-6,
          "scroll=%s" % vl4._scroll)
    vl4._key_move_up(E())
    check("Up 回退", vl4.selection() == 9)
    vl4._key_page_down(E())
    check("PgDn 翻页选中", vl4.selection() == 9 + 6,
          "sel=%s" % vl4.selection())
    vl4._key_page_up(E())
    check("PgUp 翻页选中", vl4.selection() == 9)
    vl4._key_home(E())
    check("Home 回顶", vl4.selection() == 0 and vl4._scroll == 0.0)
    vl4._key_end(E())
    check("End 到尾", vl4.selection() == 999 and
          vl4._scroll == vl4._max_scroll())
    check("visible_range", vl4.visible_range() == (994, 1000),
          "range=%s" % (vl4.visible_range(),))

    # ------------------------------------------------------------------
    # 6. 回调：render_row 状态 dict / on_activate / on_context
    # ------------------------------------------------------------------
    states = []

    def render2(canvas, row, index, x, y, w, h, state):
        states.append((index, state["selected"], state["hover"]))
        canvas.create_rectangle(x, y, x + w, y + h,
                                fill="#222222", outline="")

    vl5 = VL(root, tm, row_height=40, render_row=render2)
    vl5.pack()
    vl5._view_w = lambda: 320
    vl5._view_h = lambda: 240
    vl5.set_rows(list(range(10)), selected=2)
    s2 = [s for s in states if s[0] == 2][-1]
    check("状态 dict 传 selected", s2 == (2, True, False), "s2=%s" % (s2,))
    vl5._hover_move(E(x=5, y=45))
    s1 = [s for s in states if s[0] == 1][-1]
    check("状态 dict 传 hover", s1 == (1, False, True), "s1=%s" % (s1,))

    hits, ctx = [], []
    vl6 = make(on_activate=lambda row, i: hits.append((row, i)),
               on_context=lambda e, i, row: ctx.append(i))
    vl6.set_rows(list(range(100)), selected=5)
    vl6._activate(E(x=5, y=5))
    check("双击激活", hits == [(0, 0)], "hits=%s" % hits)
    vl6._context(E(x=5, y=85))
    check("右键 on_context 选中", ctx == [2] and vl6.selection() == 2,
          "ctx=%s sel=%s" % (ctx, vl6.selection()))
    vl6._key_return(E())
    check("回车激活当前选中", hits[-1] == (2, 2), "hits=%s" % hits)

    # ------------------------------------------------------------------
    # 7. 空态：文案、action 按钮可点
    # ------------------------------------------------------------------
    vl7 = make()
    vl7.set_rows([])
    texts = [i for i in vl7._canvas.find_withtag("empty")
             if vl7._canvas.type(i) == "text"]
    check("默认空文案", bool(texts) and
          vl7._canvas.itemcget(texts[0], "text") == "暂无数据",
          "text=%r" % (vl7._canvas.itemcget(texts[0], "text") if texts
                       else None,))
    acts = []
    vl7.set_empty("加载失败", action_text="重试",
                  action=lambda: acts.append(1))
    check("空态文案+按钮", len(vl7._canvas.find_withtag("empty")) >= 2 and
          len(vl7._canvas.find_withtag("empty_action")) == 2)
    check("空态按钮注册点击绑定",
          bool(vl7._canvas.tag_bind("empty_action", "<Button-1>")))
    vl7._empty_clicked()
    check("空态按钮回调触发", acts == [1], "acts=%s" % acts)
    vl7.set_rows(list(range(10)))
    check("有数据后空态消失",
          not vl7._canvas.find_withtag("empty"))
    vl7.set_rows([])
    check("清空后空态复现", vl7._canvas.find_withtag("empty"))

    # ------------------------------------------------------------------
    # 8. set_rows 选项：keep_scroll / selected 清空
    # ------------------------------------------------------------------
    vl8 = make()
    vl8.set_rows(list(range(1000)))
    vl8.scroll_to(500)
    s_before = vl8._scroll
    vl8.set_rows(list(range(1000)), keep_scroll=True, selected=7)
    check("keep_scroll 保留偏移", abs(vl8._scroll - s_before) < 1e-6 and
          vl8.selection() == 7)
    vl8.set_rows(list(range(1000)))
    check("默认回顶+清空选中", vl8._scroll == 0.0 and
          vl8.selection() == -1)

    # ------------------------------------------------------------------
    # 9. 滚动条：自动隐藏 / 点击跳转 / 拖动
    # ------------------------------------------------------------------
    vl_small = make()
    vl_small.set_rows(list(range(5)))
    check("内容不满一屏隐藏滚动条",
          not vl_small._canvas.find_withtag("bar"))
    vl9 = make()
    vl9.set_rows(list(range(1000)))
    check("内容超一屏显示滚动条",
          bool(vl9._canvas.find_withtag("bar")))
    vl9._press(E(x=319, y=200))
    check("点滚动条轨道跳转", vl9._scroll > 30000 and vl9._bar_drag,
          "scroll=%s" % vl9._scroll)
    vl9._motion(E(x=319, y=20))
    check("拖动滚动条", 0 < vl9._scroll < 5000,
          "scroll=%s" % vl9._scroll)
    vl9._release(E(x=319, y=20))
    check("松开结束拖动", not vl9._bar_drag)

    # ------------------------------------------------------------------
    # 10. 主题切换重绘 + 销毁退订
    # ------------------------------------------------------------------
    vl10 = make()
    vl10.set_rows(list(range(100)))
    bg1 = str(vl10._canvas.cget("bg"))
    tm.set_preset("nord")
    bg2 = str(vl10._canvas.cget("bg"))
    check("主题切换重绘底色", bg2 != bg1, "%s -> %s" % (bg1, bg2))
    vl10.destroy()
    try:
        tm.set_preset("amber")
        ok = True
    except Exception:
        ok = False
    check("销毁后主题切换不炸", ok)

    root.destroy()

    print()
    if FAILS:
        print("结果：失败 %d 项 -> %s" % (len(FAILS), FAILS))
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
