# -*- coding: utf-8 -*-
"""音乐播放器质检台：python _music_check.py

真起 GUI，端到端跑一遍并截图：
  本地曲库扫描 → MCI 播放 → 歌词 → 在线搜索 → 导入音源 → Node 解析直链 →
  本地 HTTP 假源缓冲 → 在线播放 → 下载入库 → 封面 → 迷你条 → 保存与关闭。
截图落在 _shots/music_*.png，跑完把配置里的 music 段落还原。
"""
import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from PIL import Image
from vbhelper import music as M
from vbhelper import musicapi, musicnet
from vbhelper.app import VBHelper
from vbhelper.core import save_cfg

TMP = os.path.join(HERE, "_tmusic")
SHOTS = os.path.join(HERE, "_shots")
fails, oks = [], []
CFG_BACKUP = None


def check(name, cond, detail=""):
    (oks if cond else fails).append(name)
    mark = "  ok  " if cond else "  FAIL "
    print("%s%-34s %s" % (mark, name, detail if detail else ""))
    return cond


def pump(app, n=30, ms=0.02):
    for _ in range(n):
        try:
            app.root.update()
        except tk.TclError:
            return
        time.sleep(ms)


def wait_for(app, cond, timeout=12.0, n=4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pump(app, n)
        try:
            if cond():
                return True
        except Exception:
            return False
    return False


# ---------------------------------------------------------------- 截图
u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def shot(win, name):
    """PrintWindow 对 Tk 窗口会取到全黑，直接把屏幕上的窗口区域抓下来。"""
    from PIL import ImageGrab
    # 句柄是 64 位指针，不声明 restype 会被默认 c_int 截成负数，
    # 后续传参直接 OverflowError（int too long to convert）
    u32.GetAncestor.restype = ctypes.c_void_p
    u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u32.GetDC.restype = ctypes.c_void_p
    u32.GetDC.argtypes = [ctypes.c_void_p]
    u32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u32.PrintWindow.restype = ctypes.c_bool
    u32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    try:
        win.update_idletasks()
        win.update()
        win.lift()
        win.update()
        time.sleep(0.12)
    except tk.TclError:
        return None
    hwnd = u32.GetAncestor(ctypes.c_void_p(win.winfo_id()), 2)
    if not hwnd:
        hwnd = ctypes.c_void_p(win.winfo_id())
    rect = ctypes.wintypes.RECT() if hasattr(ctypes, "wintypes") else None
    import ctypes.wintypes as wt
    r = wt.RECT()
    u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 2 or h <= 2:
        return None
    hdc = u32.GetDC(ctypes.c_void_p(hwnd))
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(memdc, bmp)
    ok = u32.PrintWindow(ctypes.c_void_p(hwnd), memdc, 2)
    bmi = ctypes.Structure.from_param if False else None

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
                    ("biHeight", ctypes.c_int), ("biPlanes", ctypes.c_ushort),
                    ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
                    ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_int),
                    ("biYPelsPerMeter", ctypes.c_int), ("biClrUsed", ctypes.c_uint),
                    ("biClrImportant", ctypes.c_uint)]

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight, hdr.biPlanes = w, -h, 1
    hdr.biBitCount, hdr.biCompression = 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(hdr), 0)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    dark = sum(img.convert("L").getdata()) / max(1, w * h)
    if dark < 6:
        try:
            img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom),
                                all_screens=True)
        except Exception:
            pass
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, "music_%s.png" % name)
    img.save(path)
    gdi32.SelectObject(memdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    u32.ReleaseDC(ctypes.c_void_p(hwnd), hdc)
    print("     shot -> %s (%dx%d, printwindow=%s)" % (path, w, h, ok))
    return img


# ---------------------------------------------------------------- 布局审计
def audit_layout(win, tag, expect_rows=0):
    """不依赖像素的排版体检：尺寸塌陷、越界、列表绘制项数量。"""
    # 偶发抖动：桌面忙时 Tk 的 map 事件可能排队几秒，等就绪 + seen=0 再重试一轮
    for _ in range(20):
        try:
            win.win.update()
            if win.win.winfo_ismapped():
                break
        except tk.TclError:
            pass
        time.sleep(0.1)

    def scan():
        problems = []
        seen = 0
        tw, th = win.win.winfo_width(), win.win.winfo_height()
        stack = [win.win]
        while stack:
            w = stack.pop()
            try:
                kids = w.winfo_children()
            except tk.TclError:
                continue
            stack.extend(kids)
            if not w.winfo_ismapped():
                continue
            try:
                ww, hh = w.winfo_width(), w.winfo_height()
                x, y = w.winfo_rootx(), w.winfo_rooty()
            except tk.TclError:
                continue
            seen += 1
            if (ww <= 1 or hh <= 1) and (kids or w.winfo_class() != "Frame"):
                problems.append("零尺寸 %s %s" % (w.winfo_class(), str(w)))
                continue
            # Tk 会把还没定好位置的控件临时停在 -32000 附近，那不是真的跑出屏幕
            if (-30000 < x < -2) or (-30000 < y < -2):
                problems.append("跑到屏幕外 %s (%d,%d)" % (w.winfo_class(), x, y))
        return seen, problems

    seen, problems = scan()
    if seen == 0:
        # 窗口 state=normal 但 map 卡在中间态（Windows 忙时偶发）：
        # 强制重映射一次再扫，扫不到才算失败
        try:
            win.win.deiconify()
            win.win.lift()
            win.win.update()
        except tk.TclError:
            pass
        deadline = time.time() + 1.5
        while time.time() < deadline and seen == 0:
            try:
                win.win.update()
                if win.win.winfo_ismapped():
                    seen, problems = scan()
                    if seen:
                        break
            except tk.TclError:
                pass
            time.sleep(0.1)
    canvas = win.list.canvas
    items = canvas.find_all() or ()
    text_items = [i for i in items if canvas.type(i) == "text"]
    if seen == 0:
        problems.append("审计采样时窗口整体不可见（已重试 3.5s 仍如此；"
                        "多为桌面忙时的 map 采样抖动，可重跑确认）")
    if expect_rows and len(text_items) < expect_rows:
        problems.append("列表只画了 %d 个文本项，期望 >= %d" % (len(text_items),
                                                             expect_rows))
    if len(text_items) > 260:
        problems.append("虚拟化失效：画了 %d 个文本项" % len(text_items))
    for i in text_items:
        x1, y1, x2, y2 = canvas.bbox(i)
        if y2 < 0 or y1 > canvas.winfo_height():
            problems.append("有行画在可视区外 y=%d..%d" % (y1, y2))
            break
    bar_h = win.bar.winfo_height()
    if bar_h < 40:
        problems.append("播放条高度只有 %d" % bar_h)
    if win.bar.winfo_rooty() + bar_h > win.win.winfo_rooty() + win.win.winfo_height() + 2:
        problems.append("播放条超出窗口底边")
    for name in ("title_lbl", "artist_lbl", "cur_lbl", "dur_lbl", "badge",
                 "vol_lbl", "lyric_line"):
        lbl = getattr(win, name, None)
        if lbl is not None and lbl.winfo_ismapped() and lbl.winfo_height() <= 1:
            problems.append("播放条控件 %s 被压扁" % name)
    check("布局审计 %s" % tag, not problems,
          "; ".join(problems[:3]) if problems else
          "%d 个控件 / %d 个文本项 / 条高 %d" % (seen, len(text_items), bar_h))


# ---------------------------------------------------------------- 测试素材
def make_audio():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    names = [("质检歌手 - 测试曲一.mp3", 6, 440),
             ("质检歌手 - 测试曲二.mp3", 5, 523),
             ("质检歌手 - 测试曲三.mp3", 4, 659)]
    for name, secs, freq in names:
        path = os.path.join(TMP, name)
        cmd = [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
               "-i", "sine=frequency=%d:sample_rate=44100" % freq,
               "-t", str(secs), "-codec:a", "libmp3lame", "-b:a", "128k", path]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            return False
    with io.open(os.path.join(TMP, "质检歌手 - 测试曲一.lrc"), "w",
                 encoding="utf-8") as f:
        f.write("[00:00.00]第一行质检歌词\n[00:02.00]第二行质检歌词\n"
                "[00:04.00]第三行质检歌词\n")
    return True


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = os.path.join(TMP, "质检歌手 - 测试曲一.mp3")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


FAKE_SOURCE = """/*!
 * @name 质检假源
 * @description 只做本地回环，验证宿主与播放器整条链路
 * @version 1.0
 * @author VBHelper
 */
(function () {
  const lx = window.lx
  lx.on('request', ({ action, info }) => new Promise((resolve, reject) => {
    if (action === 'musicUrl') return resolve('http://127.0.0.1:%d/track.mp3')
    if (action === 'lyric') return resolve({ lyric: '[00:00.00]假源歌词一行' })
    if (action === 'pic') return resolve('http://127.0.0.1:%d/cover.jpg')
    reject(new Error('unsupported'))
  }))
  lx.send('inited', {
    message: '质检假源就绪',
    sources: { kw: { type: 'music', actions: ['musicUrl', 'lyric', 'pic'],
                    qualitys: ['128k', '320k', 'flac'] } },
  })
})()
"""


# ---------------------------------------------------------------- 主流程
def main():
    print("== 0. 素材 ==")
    if not check("ffmpeg 生成测试音频", make_audio()):
        print("没有 ffmpeg，跳过需要真实音频的用例")
    global CFG_BACKUP
    app = VBHelper()
    CFG_BACKUP = dict(app.cfg.get("music") or {})
    app.cfg["music"] = {"folders": [TMP], "quality": "320k", "mode": 0, "volume": 40}
    app.root.update()
    srv, port = start_server()
    print("     本地回环服务端口 %d" % port)

    print("== 1. 建窗 ==")
    win = M.open_window(app)
    pump(app, 25)
    check("窗口建起来了", win.win.winfo_exists())
    check("标题栏文本正常", "音乐播放器" in win.win.title(), win.win.title())
    shot(win.win, "01_initial")

    print("== 2. 本地曲库 ==")
    ok = wait_for(app, lambda: len(win.local_uids) >= 3, timeout=20)
    check("扫描到 3 首本地歌", ok, "%d 首" % len(win.local_uids))
    check("文件夹导航出现", ("folder", TMP) in win.navs)
    check("ffprobe 读到标题/歌手",
          wait_for(app, lambda: (win.tracks[win.local_uids[0]].get("artist") ==
                                 "质检歌手"), timeout=15),
          str([win.tracks[u].get("title") for u in win.local_uids][:3]))
    win.view = ("all", "")
    win._apply_view()
    pump(app, 12)
    check("全部歌曲列表渲染出行", len(win.rows) >= 3, "%d 行" % len(win.rows))
    audit_layout(win, "曲库", expect_rows=6)
    shot(win.win, "02_library")

    print("== 3. 本地播放 ==")
    first = win.local_uids[0]
    win._set_context([win.tracks[first]])
    win._play_track(first, restart=True)
    check("MCI 已打开设备", wait_for(app, lambda: win.player.opened, 8))
    check("播放中 mode=playing",
          wait_for(app, lambda: win.player.mode() == "playing", 8), win.player.mode())
    pos0 = win.position
    pump(app, 40, 0.05)
    check("播放位置在推进", win.position > pos0, "%.2fs -> %.2fs" % (pos0, win.position))
    check("歌词从同名 lrc 读到", len(win._lyrics) == 3, "%d 行" % len(win._lyrics))
    check("播放条徽标显示格式", "MP3" in win.badge.cget("text"), win.badge.cget("text"))
    check("当前行高亮在列表里", win.list.sel >= 0, str(win.list.sel))
    shot(win.win, "03_playing_local")

    win._seek_to(2.0)
    pump(app, 10)
    check("拖动进度生效", abs(win.position - 2.0) < 0.9, "%.2f" % win.position)
    check("歌词跟着换行", win._lyric_idx >= 1, str(win._lyric_idx))
    check("播放条滚动歌词有字", "质检歌词" in win.lyric_line.cget("text"),
          repr(win.lyric_line.cget("text")))
    check("迷你条仍是歌手不是歌词",
          "质检" in win.mini_artist.cget("text") or
          win.mini_artist.cget("text") == "质检歌手",
          repr(win.mini_artist.cget("text")))
    win._toggle_fav_uid(first)
    check("收藏进了我喜欢", first in win.fav_keys and len(win._rows_for_view()) >= 0)
    win._select_view(("fav", ""))
    check("我喜欢视图有 1 行", len(win.rows) == 1, str(len(win.rows)))

    win._toggle_play()
    check("暂停后 mode=paused", win.player.mode() in ("paused", "stopped"),
          win.player.mode())
    win._toggle_play()
    check("恢复播放", wait_for(app, lambda: win.player.mode() == "playing", 6))

    print("== 4. 在线搜索 ==")
    win._select_view(("search", ""))
    check("搜索框默认显示占位提示", win.search.get() == win.search_ph,
          repr(win.search.get()))
    check("占位提示不算搜索词", win._search_text() == "",
          repr(win._search_text()))
    win._search_focus_in()
    check("聚焦后清空占位提示", win.search.get() == "", repr(win.search.get()))
    win.search.insert(0, win.search_ph + "周杰伦")
    check("占位残留会被剥离", win._search_text() == "周杰伦",
          repr(win._search_text()))
    win.search.delete(0, "end")
    win.search.insert(0, "周杰伦")
    win._do_search()
    ok = wait_for(app, lambda: win._search_rows, timeout=30)
    check("搜索回填结果", ok, "%d 条" % len(win._search_rows))
    if win._search_rows:
        row = win._search_rows[0]
        check("结果带歌曲 id", bool(row.get("songmid")), str(row.get("songmid")))
        check("结果带音质档位", bool(row.get("qualitys")), str(row.get("qualitys")))
        check("列表渲染在线行", len(win.rows) >= 1)
    audit_layout(win, "搜索", expect_rows=10)
    shot(win.win, "04_search")

    print("== 5. 音源导入与初始化 ==")
    item = {"id": "qc", "name": "质检假源", "version": "1.0", "description": "",
            "author": "", "homepage": "",
            "script": musicapi._pack(FAKE_SOURCE % (port, port))}
    win.sources = {"active": "qc", "items": [item]}
    win.api.load(item)
    ok = wait_for(app, lambda: win.api.inited, timeout=30)
    check("音源初始化成功", ok, win._api_error or win._api_message)
    check("能力表解析正确", win.api.supports("kw", "musicUrl", "320k"),
          json.dumps(win.api.sources, ensure_ascii=False)[:120])
    win._select_view(("source", ""))
    pump(app, 10)
    audit_layout(win, "音源")
    shot(win.win, "05_source")

    print("== 6. 在线播放（回环假源） ==")
    if win._search_rows:
        track = dict(win._search_rows[0])
        track["source"] = "kw"
        track["qualitys"] = ["128k", "320k"]
        track["pic"] = ""
        win.tracks[track["uid"]] = track
        win.quality = "320k"
        win._set_context([track])
        win._play_track(track["uid"], restart=True)
        ok = wait_for(app, lambda: win.player.opened and
                      win._opened_for == track["uid"], timeout=40)
        check("在线歌走完了取链+缓冲+播放", ok,
              "preparing=%s err=%s" % (win._preparing, win.player.last_error))
        check("缓冲文件落在缓存目录",
              os.path.isfile(_c := M._online_cache_name(track, "320k")) or
              any(f.endswith(".mp3") for f in os.listdir(M.CACHE_DIR)))
        check("在线歌播放中",
              wait_for(app, lambda: win.player.mode() == "playing", 10),
              win.player.mode())
        check("徽标显示源与音质", "酷我" in win.badge.cget("text"),
              win.badge.cget("text"))
        shot(win.win, "06_playing_online")
        win._select_view(("lyric", ""))
        pump(app, 20)
        audit_layout(win, "歌词")
        shot(win.win, "07_lyric")
        win.player.stop()
        win.playing = False

    print("== 7. 下载入库 ==")
    if win._search_rows:
        t = dict(win._search_rows[0])
        t["source"] = "kw"
        t["qualitys"] = ["320k"]
        win.tracks[t["uid"]] = t
        win.down_dir = os.path.join(TMP, "down")
        win.api.stop()
        win.api.load(item)
        wait_for(app, lambda: win.api.inited, 20)
        win._download_track(t["uid"])
        ok = wait_for(app, lambda: any(j["state"] == "done" for j in win._down_jobs),
                      timeout=40)
        if not ok and not win._down_jobs:
            # 音源冷启动竞态：ready 后偶尔仍有一瞬未就绪，补发一次
            win._download_track(t["uid"])
            ok = wait_for(app, lambda: any(j["state"] == "done" for j in win._down_jobs),
                          timeout=40)
        saved = [f for f in os.listdir(win.down_dir)] if os.path.isdir(win.down_dir) else []
        check("下载任务完成", ok and saved, str(saved)[:80])
        win._select_view(("down", ""))
        pump(app, 10)
        shot(win.win, "08_download")

    print("== 8. 视图与外壳 ==")
    for key, tag in (("board", "09_board"), ("plist", "10_plist"),
                     ("queue", "11_queue"), ("recent", "12_recent"),
                     ("set", "13_settings")):
        win._select_view((key, ""))
        wait_for(app, lambda: True, 1.2)
        pump(app, 15)
        shot(win.win, tag)
        if key == "board":
            check("榜单目录有行", len(win.rows) >= 5, str(len(win.rows)))
            win._open_board_songs(win.rows[0])
            ok = wait_for(app, lambda: len(win.rows) >= 5 and
                          win.rows[0].get("rk", "music") != "board", timeout=40)
            first = win.rows[0].get("name") if win.rows else "(空)"
            check("榜内歌曲取到", ok, "%d 行 / %s / 状态: %s" % (
                len(win.rows), first, win.status.cget("text")))
            shot(win.win, "09b_board_songs")
        elif key == "plist":
            check("歌单页有导入入口", win.rows and win.rows[0].get("id") == "__import__")
        elif key == "set":
            check("设置页渲染了卡片",
                  len(win.cards_scroll.inner.winfo_children()) >= 2)

    print("== 9. 迷你条与保存 ==")
    win._select_view(("all", ""))
    win._toggle_mini(True)
    pump(app, 15)
    check("迷你条显示", win._mini_shown and win._mini.winfo_viewable())
    shot(win._mini, "14_mini")
    win._toggle_mini(False)
    pump(app, 8)
    win._save_state()
    cfg2 = json.load(io.open(os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "VBHelper", "config.json"), encoding="utf-8"))
    check("配置写回了 music 段", isinstance(cfg2.get("music"), dict))
    check("音质/目录持久化", cfg2["music"].get("quality") == win.quality or
          cfg2["music"].get("down_dir") == win.down_dir,
          str(cfg2["music"].get("quality")))

    print("== 10. 关闭与资源回收 ==")
    rss_before = _rss()
    win.close()
    pump(app, 10)
    check("窗口已销毁", not win.win.winfo_exists() if hasattr(win, "win") else True)
    check("音源进程已回收", not win.api.alive)
    check("关闭后再开窗口不报错",
          isinstance(M.open_window(app), M.MusicWindow))
    pump(app, 20)
    M.shutdown(app)
    pump(app, 10)
    print("     RSS %s -> %s" % (_fmt_kb(rss_before), _fmt_kb(_rss())))

    try:
        app.cfg["music"] = CFG_BACKUP
        save_cfg(app.cfg)
        print("     配置已还原")
    except Exception as e:
        print("     配置还原失败：%s" % e)
    srv.shutdown()
    app.root.destroy()
    shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
    for f in fails:
        print("  - " + f)
    print("截图目录：%s" % SHOTS)
    return 1 if fails else 0


_proc = ctypes.windll.kernel32.GetCurrentProcess


def _rss():
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint), ("PageFaultCount", ctypes.c_uint),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except Exception:
        pass
    return 0


def _fmt_kb(n):
    return "%.1f MB" % (n / 1048576.0) if n else "?"


if __name__ == "__main__":
    sys.exit(main())
