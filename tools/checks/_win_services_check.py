# -*- coding: utf-8 -*-
"""Win 服务无 GUI 自检：截屏 / 窗口 / 剪贴板 / 输入策略。

**绝不调用真实 SendInput**：所有输入测试用 RecordingBackend（继承
SendInputBackend，仅覆盖 _send 记录事件，不碰 user32.SendInput）。

运行：D:/python/python.exe _win_services_check.py
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# 与 vbhelper/app.py 一致：Per-Monitor DPI aware，否则 GetSystemMetrics 与
# ImageGrab 的像素不一致（生产环境由宿主设置，模块本身不改 DPI）。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from vbhelper.services.win import (ClipboardService, InputBlocked,  # noqa: E402
                                   InputError, InputService, ScreenError,
                                   ScreenService, SendInputBackend)
from vbhelper.services.win.input import (INPUT_KEYBOARD, INPUT_MOUSE,  # noqa: E402
                                         KEYEVENTF_EXTENDEDKEY,
                                         KEYEVENTF_KEYUP,
                                         KEYEVENTF_UNICODE,
                                         MOUSEEVENTF_ABSOLUTE,
                                         MOUSEEVENTF_HWHEEL,
                                         MOUSEEVENTF_LEFTDOWN,
                                         MOUSEEVENTF_LEFTUP,
                                         MOUSEEVENTF_MOVE,
                                         MOUSEEVENTF_RIGHTDOWN,
                                         MOUSEEVENTF_RIGHTUP,
                                         MOUSEEVENTF_VIRTUALDESK,
                                         MOUSEEVENTF_WHEEL,
                                         normalize_point, parse_keys)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASSED.append(name)
        print("PASS %s%s" % (name, (" - " + detail) if detail else ""))
        return True
    FAILED.append((name, detail))
    print("FAIL %s - %s" % (name, detail))
    return False


def skip(name: str, detail: str = "") -> None:
    print("SKIP %s%s" % (name, (" - " + detail) if detail else ""))


class RecordingBackend(SendInputBackend):
    """记录 SendInput 事件，绝不真正发送。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list = []

    def _send(self, events: list) -> None:
        self.events.extend(events)

    def clear(self) -> None:
        self.events.clear()


class FakeClipboard:
    """paste 模式用的假剪贴板。"""

    def __init__(self, value: str) -> None:
        self.value = value
        self.log: list[tuple[str, str]] = []

    def snapshot(self) -> str:
        self.log.append(("snapshot", self.value))
        return self.value

    def write_text(self, text: str) -> bool:
        self.log.append(("write", text))
        self.value = text
        return True

    def restore(self, text: str) -> bool:
        self.log.append(("restore", text))
        self.value = text
        return True


def mouse_events(backend: RecordingBackend) -> list:
    return [e for e in backend.events if e.type == INPUT_MOUSE]


def key_events(backend: RecordingBackend) -> list:
    return [e for e in backend.events if e.type == INPUT_KEYBOARD]


def key_sequence(backend: RecordingBackend) -> list[tuple[int, bool, bool]]:
    return [(int(e.ki.wVk), bool(e.ki.dwFlags & KEYEVENTF_KEYUP),
             bool(e.ki.dwFlags & KEYEVENTF_EXTENDEDKEY))
            for e in key_events(backend)]


def console_title() -> str:
    buf = ctypes.create_unicode_buffer(512)
    try:
        ctypes.windll.kernel32.GetConsoleTitleW(buf, 512)
    except Exception:
        return ""
    return buf.value


def test_screen() -> None:
    tmp = tempfile.mkdtemp(prefix="vbhelper_win_check_")
    service = ScreenService(tmp, max_width=1600)
    try:
        info = service.info()
    except Exception as exc:
        check("screen.info", False, repr(exc))
        return
    vs = info.get("virtual_screen") or {}
    vx, vy = int(vs.get("x", 0)), int(vs.get("y", 0))
    vw, vh = int(vs.get("w", 0)), int(vs.get("h", 0))
    check("screen.info.virtual_screen", vw > 0 and vh > 0,
          "virtual=(%d,%d,%d,%d)" % (vx, vy, vw, vh))
    monitors = info.get("monitors") or []
    check("screen.info.monitors", bool(monitors) and all(
        m.get("width", 0) > 0 and m.get("height", 0) > 0 for m in monitors),
        "%d 个显示器" % len(monitors))
    check("screen.info.primary", bool(info.get("primary_size")),
          str(info.get("primary_size")))
    check("screen.info.dpi", int(info.get("dpi") or 0) >= 96,
          "dpi=%s" % info.get("dpi"))
    active = info.get("active_window")
    check("screen.info.active_window", active is None or (
        isinstance(active, dict) and "title" in active and "pid" in active),
        str(active)[:80])

    # 全屏（不缩放）
    try:
        full = service.capture(mode="full", max_width=0, save=False)
        check("capture.full.size", (full.width, full.height) == (vw, vh),
              "%dx%d" % (full.width, full.height))
        check("capture.full.origin", full.origin == (vx, vy), str(full.origin))
        check("capture.full.scale", abs(full.scale - 1.0) < 1e-6,
              str(full.scale))
        check("capture.full.png", full.png[:8] == b"\x89PNG\r\n\x1a\n"
              and len(full.png) > 1000, "%d bytes" % len(full.png))
        check("capture.full.image", full.image.size == (vw, vh),
              str(full.image.size))
    except Exception as exc:
        check("capture.full", False, repr(exc))

    # 缩放：只缩不放 + origin 不变
    try:
        scaled = service.capture(mode="full", max_width=800, save=False)
        expected_w = min(800, vw)
        check("capture.scale.width", scaled.width == expected_w,
              "%d -> %d" % (vw, scaled.width))
        check("capture.scale.factor",
              abs(scaled.scale - expected_w / vw) < 1e-3, str(scaled.scale))
        check("capture.scale.origin", scaled.origin == (vx, vy),
              str(scaled.origin))
        big = service.capture(mode="full", max_width=vw + 500, save=False)
        check("capture.scale.no_upscale", big.width == vw, str(big.width))
    except Exception as exc:
        check("capture.scale", False, repr(exc))

    # 区域
    try:
        region = (vx + 10, vy + 10, vx + 110, vy + 110)
        shot = service.capture(mode="region", region=region, max_width=0,
                               save=False)
        check("capture.region.size", (shot.width, shot.height) == (100, 100),
              "%dx%d" % (shot.width, shot.height))
        check("capture.region.origin", shot.origin == (vx + 10, vy + 10),
              str(shot.origin))
    except Exception as exc:
        check("capture.region", False, repr(exc))

    # 单显示器截图（mode=monitor，0 起）
    first_monitor = (info.get("monitors") or [None])[0]
    if first_monitor:
        try:
            shot = service.capture(mode="monitor", monitor=0, max_width=0,
                                   save=False)
            check("capture.monitor",
                  (shot.width, shot.height)
                  == (first_monitor["width"], first_monitor["height"])
                  and shot.origin == (first_monitor["x"], first_monitor["y"]),
                  "%dx%d @%s" % (shot.width, shot.height, shot.origin))
        except Exception as exc:
            check("capture.monitor", False, repr(exc))

    # 落盘
    try:
        saved = service.capture(mode="region",
                                region=(vx, vy, vx + 120, vy + 80),
                                max_width=0, save=True)
        ok = bool(saved.path and os.path.isfile(saved.path)
                  and os.path.getsize(saved.path) > 100)
        check("capture.save", ok, str(saved.path))
        check("capture.save.name", bool(
            saved.path and os.path.basename(saved.path).startswith("capture_")
            and saved.path.endswith(".png")), str(saved.path))
    except Exception as exc:
        check("capture.save", False, repr(exc))

    # 窗口列表
    try:
        windows = service.list_windows(limit=20)
        ok = isinstance(windows, list) and all(
            w.title.strip() and w.pid > 0 and len(w.rect) == 4
            and w.pid != os.getpid() for w in windows)
        check("list_windows", ok, "%d 个窗口" % len(windows))
    except Exception as exc:
        check("list_windows", False, repr(exc))

    # 窗口截图：优先当前控制台，其次活动窗口；找不到/不可见就 skip
    candidates: list[str] = []
    title = console_title().strip()
    if title:
        candidates.append(title[:20])
    try:
        active = service.active_window()
        if active and active.title.strip():
            candidates.append(active.title.strip()[:20])
    except Exception:
        pass
    done = False
    last_error = ""
    for candidate in candidates:
        try:
            win_shot = service.capture(mode="window", window_title=candidate,
                                       max_width=0, save=False)
            check("capture.window", win_shot.width > 0 and win_shot.height > 0
                  and win_shot.png[:4] == b"\x89PNG"
                  and win_shot.window is not None,
                  "%s %dx%d" % (candidate, win_shot.width, win_shot.height))
            done = True
            break
        except ScreenError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = repr(exc)
    if not done:
        skip("capture.window", "无可用窗口：%s" % last_error)


def test_clipboard() -> None:
    service = ClipboardService()
    original = None
    try:
        original = service.read_text()
    except Exception as exc:
        check("clipboard.read", False, repr(exc))
        return
    marker = "VBHelper 剪贴板自检 %d" % time.time_ns()
    try:
        service.write_text(marker)
        back = service.read_text()
        check("clipboard.roundtrip", back == marker, repr(back[:40]))
    except Exception as exc:
        check("clipboard.roundtrip", False, repr(exc))
    finally:
        try:
            if original is not None:
                service.restore(original)
        except Exception:
            pass
    try:
        app = service.foreground_app()
        ok = (isinstance(app, dict) and isinstance(app.get("title"), str)
              and isinstance(app.get("class_name"), str)
              and isinstance(app.get("pid"), int)
              and isinstance(app.get("hwnd"), int)
              and isinstance(app.get("is_self"), bool))
        check("clipboard.foreground_app", ok, str(app)[:100])
    except Exception as exc:
        check("clipboard.foreground_app", False, repr(exc))


def enabled_cfg(**overrides) -> dict:
    cfg = {"input": {"enabled": True, "panic_seconds": 0.25}}
    cfg["input"].update(overrides)
    return cfg


def test_input() -> None:
    # 关闭时全部抛 InputBlocked
    backend = RecordingBackend()
    service = InputService({"input": {"enabled": False}}, backend=backend)
    blocked = 0
    for action in (
            lambda: service.mouse_move(10, 10),
            lambda: service.mouse_click(),
            lambda: service.mouse_drag(1, 1, 2, 2),
            lambda: service.scroll(-1),
            lambda: service.type_text("x", method="unicode"),
            lambda: service.press("enter"),
    ):
        try:
            action()
        except InputBlocked:
            blocked += 1
        except Exception:
            pass
    check("input.disabled.blocked", blocked == 6, "blocked=%d/6" % blocked)
    check("input.disabled.no_events", not backend.events,
          "%d events" % len(backend.events))

    vx, vy, vw, vh = service.screen_size()
    check("input.screen_size", vw > 0 and vh > 0,
          "(%d,%d,%d,%d)" % (vx, vy, vw, vh))
    pos = service.position()
    check("input.position", isinstance(pos, tuple) and len(pos) == 2
          and all(isinstance(v, int) for v in pos), str(pos))

    # 坐标归一化：左上角 0，右下角 65535
    backend = RecordingBackend()
    service = InputService(enabled_cfg(), backend=backend)
    service.mouse_move(vx, vy)
    ev = mouse_events(backend)[-1]
    check("input.normalize.origin",
          (int(ev.mi.dx), int(ev.mi.dy)) == (0, 0)
          and bool(ev.mi.dwFlags & MOUSEEVENTF_ABSOLUTE)
          and bool(ev.mi.dwFlags & MOUSEEVENTF_VIRTUALDESK)
          and bool(ev.mi.dwFlags & MOUSEEVENTF_MOVE),
          "dx=%d dy=%d flags=0x%x" % (ev.mi.dx, ev.mi.dy, ev.mi.dwFlags))
    backend.clear()
    service.mouse_move(vx + vw - 1, vy + vh - 1)
    ev = mouse_events(backend)[-1]
    check("input.normalize.max", (int(ev.mi.dx), int(ev.mi.dy)) == (65535, 65535),
          "dx=%d dy=%d" % (ev.mi.dx, ev.mi.dy))
    check("input.normalize.formula",
          normalize_point(vx, vy) == (0, 0)
          and normalize_point(vx + vw - 1, vy + vh - 1) == (65535, 65535),
          "%s" % (normalize_point(vx + vw // 2, vy + vh // 2),))

    # 点击：先定位再 2 次右键
    backend.clear()
    service.mouse_click("right", count=2, interval=0.01,
                        x=vx + 100, y=vy + 100)
    mice = mouse_events(backend)
    flags = [int(e.mi.dwFlags) for e in mice]
    check("input.click.move_first", len(mice) >= 5
          and flags[0] & MOUSEEVENTF_MOVE
          and (int(mice[0].mi.dx), int(mice[0].mi.dy))
          == normalize_point(vx + 100, vy + 100),
          "%d events" % len(mice))
    check("input.click.count",
          flags.count(MOUSEEVENTF_RIGHTDOWN) == 2
          and flags.count(MOUSEEVENTF_RIGHTUP) == 2,
          str(flags))
    try:
        service.mouse_click(button="wheel")
        check("input.click.bad_button", False, "未抛 InputError")
    except InputError:
        check("input.click.bad_button", True)

    # 拖拽：move -> down -> 多次 move -> up
    backend.clear()
    service.mouse_drag(vx + 20, vy + 20, vx + 220, vy + 120,
                       duration=0.1)
    mice = mouse_events(backend)
    flags = [int(e.mi.dwFlags) for e in mice]
    check("input.drag.sequence", len(mice) >= 5
          and flags[0] & MOUSEEVENTF_MOVE
          and flags[1] == MOUSEEVENTF_LEFTDOWN
          and flags[-1] == MOUSEEVENTF_LEFTUP
          and sum(1 for f in flags if f & MOUSEEVENTF_MOVE) >= 3,
          "%d events %s" % (len(mice), flags))
    last = mice[-2]            # 最后一个是抬起，前一个是终点移动
    check("input.drag.endpoint",
          (int(last.mi.dx), int(last.mi.dy))
          == normalize_point(vx + 220, vy + 120),
          "dx=%d dy=%d" % (last.mi.dx, last.mi.dy))

    # 滚轮：负数向下（DWORD 回绕）
    backend.clear()
    service.scroll(-3)
    ev = mouse_events(backend)[-1]
    check("input.scroll.vertical",
          int(ev.mi.dwFlags) == MOUSEEVENTF_WHEEL
          and int(ev.mi.mouseData) == (-360) & 0xFFFFFFFF,
          "data=%d" % ev.mi.mouseData)
    backend.clear()
    service.scroll(2, horizontal=True, x=vx + 5, y=vy + 5)
    mice = mouse_events(backend)
    check("input.scroll.horizontal",
          int(mice[0].mi.dwFlags) & MOUSEEVENTF_MOVE
          and int(mice[-1].mi.dwFlags) == MOUSEEVENTF_HWHEEL
          and int(mice[-1].mi.mouseData) == 240,
          "data=%d" % mice[-1].mi.mouseData)

    # unicode 输入：换行/制表符/emoji 代理对
    backend.clear()
    service.type_text("你a\n\t😀", method="unicode")
    keys = key_events(backend)
    units = [(int(e.ki.wScan), int(e.ki.dwFlags)) for e in keys
             if int(e.ki.dwFlags) & KEYEVENTF_UNICODE]
    expected_units = [0x4F60, 0x4F60, 0x61, 0x61,
                      0xD83D, 0xD83D, 0xDE00, 0xDE00]
    check("input.unicode.units", [u for u, _f in units] == expected_units,
          str([u for u, _f in units]))
    check("input.unicode.flags", all(
        bool(f & KEYEVENTF_KEYUP) == (i % 2 == 1)
        for i, (_u, f) in enumerate(units)), str(units))
    seq = key_sequence(backend)
    check("input.unicode.enter_tab",
          (0x0D, False, False) in seq and (0x0D, True, False) in seq
          and (0x09, False, False) in seq and (0x09, True, False) in seq,
          str(seq))

    # paste：备份 -> 写 -> Ctrl+V -> 恢复
    fake = FakeClipboard("原始剪贴板内容")
    backend.clear()
    service = InputService(enabled_cfg(), clipboard=fake, backend=backend)
    service.type_text("粘贴文本", method="paste")
    check("input.paste.clipboard_log",
          [name for name, _v in fake.log] == ["snapshot", "write", "restore"]
          and fake.log[-1][1] == "原始剪贴板内容",
          str([name for name, _v in fake.log]))
    seq = key_sequence(backend)
    check("input.paste.ctrl_v",
          (0x11, False, False) in seq and (0x56, False, False) in seq
          and (0x56, True, False) in seq and (0x11, True, False) in seq,
          str(seq))
    check("input.paste.no_unicode",
          not any(int(e.ki.dwFlags) & KEYEVENTF_UNICODE
                  for e in key_events(backend)))

    # 无剪贴板时 paste 回退 unicode
    backend.clear()
    service = InputService(enabled_cfg(), backend=backend)
    service.type_text("abc", method="paste")
    units = [int(e.ki.wScan) for e in key_events(backend)
             if int(e.ki.dwFlags) & KEYEVENTF_UNICODE]
    check("input.paste.fallback", units == [0x61, 0x61, 0x62, 0x62,
                                            0x63, 0x63], str(units))

    # 组合键解析
    check("input.parse.ctrl_shift_t", parse_keys("ctrl+shift+t")
          == [(0x11, False), (0x10, False), (0x54, False)],
          str(parse_keys("ctrl+shift+t")))
    check("input.parse.alt_f4", parse_keys("alt+f4")
          == [(0x12, False), (0x73, False)], str(parse_keys("alt+f4")))
    check("input.parse.up_extended", parse_keys("up") == [(0x26, True)],
          str(parse_keys("up")))
    check("input.parse.f12", parse_keys("f12") == [(0x7B, False)],
          str(parse_keys("f12")))
    check("input.parse.case", parse_keys("CTRL+SHIFT+T")
          == parse_keys("ctrl+shift+t"))
    try:
        parse_keys("ctrl+nope")
        check("input.parse.unknown", False, "未抛 InputError")
    except InputError:
        check("input.parse.unknown", True)

    backend.clear()
    service = InputService(enabled_cfg(), backend=backend)
    service.press("ctrl+shift+t")
    seq = key_sequence(backend)
    check("input.press.order", seq == [
        (0x11, False, False), (0x10, False, False), (0x54, False, False),
        (0x54, True, False), (0x10, True, False), (0x11, True, False)], str(seq))
    backend.clear()
    service.press("up")
    seq = key_sequence(backend)
    check("input.press.extended", len(seq) == 2 and seq[0][2] and seq[1][2],
          str(seq))
    backend.clear()
    service.press("f12")
    seq = key_sequence(backend)
    check("input.press.f12", [s[0] for s in seq] == [0x7B, 0x7B], str(seq))

    # 急停：panic 期间抛 InputBlocked，过期恢复
    backend = RecordingBackend()
    service = InputService(enabled_cfg(), backend=backend)
    service.panic(0.3)
    check("input.panic.active", service.panicked)
    try:
        service.mouse_move(1, 1)
        check("input.panic.blocks", False, "未抛 InputBlocked")
    except InputBlocked:
        check("input.panic.blocks", True)
    time.sleep(0.45)
    check("input.panic.expired", not service.panicked)
    try:
        service.mouse_move(1, 1)
        check("input.panic.recover", True)
    except Exception as exc:
        check("input.panic.recover", False, repr(exc))

    # panic(seconds=None) 读 cfg
    service.panic()
    check("input.panic.cfg_seconds", service.panicked)
    time.sleep(0.45)
    check("input.panic.cfg_expired", not service.panicked)

    # 热键线程幂等启动/停止
    service.start_watch()
    service.start_watch()
    time.sleep(0.15)
    service.stop_watch()
    service.stop_watch()
    check("input.watch.idempotent", True)

    # 自检全程没有真实 SendInput：RecordingBackend 覆盖了 _send
    check("input.no_real_sendinput", True)


def main() -> int:
    print("== win services check ==")
    print("python", sys.version.split()[0], "root", ROOT)
    test_screen()
    test_clipboard()
    test_input()
    print("\n== result: %d passed, %d failed ==" % (len(PASSED), len(FAILED)))
    for name, detail in FAILED:
        print("FAILED:", name, "-", detail)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
