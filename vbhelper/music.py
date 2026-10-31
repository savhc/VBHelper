"""音乐播放器：本地曲库 + 在线播放 + LRC 歌词 + 迷你条。

功能组织参考 lx-music-desktop：在线部分同样是「内置接口找歌，导入的音源脚本
取播放直链」的分工——搜索/榜单/歌单/歌词/封面走 musicnet 的平台直连，
直链解析交给 musicapi 在 Node 子进程里跑的 .js 音源。

播放走 Windows MCI（winmm），不引入第三方音频库：
- mp3 / wav / wma / aac 直接播放；
- flac / m4a / ogg 等先用 ffmpeg 转成 320k mp3 缓存后播放；
- 在线歌先整轨缓冲到本地缓存再播，所以 flac 能放、能拖进度；
- WAV 的 MCI 通道不支持应用内音量，界面会给出提示。

界面是 Tkinter 手绘：列表虚拟化只画可见行，封面最多留 24 张，
音源进程空闲 45 秒销毁，尽量不给这台 8G 的机器添负担。
"""
import bisect
import ctypes
import hashlib
import http.server
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import core
from .core import *
from .ui import icons, render
from .ui.scroll import bind_canvas_wheel

CREATE_NO_WINDOW = 0x08000000

AUDIO_EXTS = {
    ".mp3", ".wav", ".wma", ".aac", ".m4a", ".flac", ".ogg", ".opus",
    ".ape", ".alac", ".aiff", ".aif", ".wv",
}
NATIVE_EXTS = {".mp3", ".wav", ".wma", ".aac", ".mid", ".midi"}

CACHE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME, "music_cache")
META_CACHE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME, "music_meta.json")
CACHE_MAX_BYTES = 400 * 1024 * 1024
CACHE_MAX_FILES = 80
MAX_TRACKS = 8000

MODE_LABELS = ("列表", "单曲", "随机")
MODE_TITLES = ("列表循环", "单曲循环", "随机播放")

COVER_TINTS = (
    "#2a3242", "#2e2a38", "#243230", "#332b2a",
    "#26303a", "#2b2e38", "#2a3030", "#302a3a",
)

SEARCH_PH = "搜索歌曲、歌手、专辑"

LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
LRC_OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.I)

_TOOL_CACHE = {}

def _normcase(path):
    return os.path.normcase(os.path.abspath(path))

def _fmt_sec(sec):
    try:
        sec = max(0, int(sec))
    except Exception:
        sec = 0
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)

def _short_dir(path, limit=19):
    """左栏只放得下二十来个字符，长路径保留盘符和最后一级目录。"""
    text = path.rstrip("\\/")
    if len(text) <= limit:
        return text
    drive, rest = os.path.splitdrive(text)
    tail = os.path.basename(text)
    if drive and tail:
        return "%s\\…\\%s" % (drive, tail)
    return "…" + text[-(limit - 1):]

def _friendly_mci_error(rc):
    return {
        261: "当前格式不支持应用内音量调节",
        263: "播放设备尚未打开",
        277: "MCI 无法识别这个音频格式",
    }.get(rc, "播放失败（MCI 错误 %d）" % rc)

def _find_tool(name):
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    found = shutil.which(name)
    if not found:
        bases = []
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        bases.append(exe_dir)
        for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(key)
            if base:
                bases.append(os.path.join(base, "ffmpeg", "bin"))
        if os.environ.get("LOCALAPPDATA"):
            bases.append(os.path.join(
                os.environ["LOCALAPPDATA"], "Microsoft", "WinGet", "Links"))
        for base in bases:
            cand = os.path.join(base, name + ".exe")
            if os.path.isfile(cand):
                found = cand
                break
    _TOOL_CACHE[name] = found or ""
    return found or ""

def _cache_target(src):
    """按源文件路径、大小、修改时间做缓存键：文件换了就重新转。"""
    try:
        st = os.stat(src)
        raw = "%s|%d|%d|320" % (_normcase(src), st.st_size, int(st.st_mtime))
        key = hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:20]
        return os.path.join(CACHE_DIR, key + ".mp3")
    except Exception:
        return ""

def _filename_meta(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\s*\d{1,3}\s*[-._ ]+\s*", "", stem).strip() or stem
    title = stem
    artist = ""
    m = re.match(r"^(.*?)\s+-\s+(.+)$", stem)
    if m:
        left, right = m.group(1).strip(), m.group(2).strip()
        if left and right and not left.isdigit() and len(left) <= 40:
            artist, title = left, right
    return title, artist

def _cover_tint(title):
    total = sum(ord(c) for c in (title or "music"))
    return COVER_TINTS[total % len(COVER_TINTS)]

def _ffprobe_meta(path, ffprobe):
    if not ffprobe:
        return None
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration:format_tags=title,artist,album",
        "-of", "json", path,
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=10,
                            creationflags=CREATE_NO_WINDOW)
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
        fmt = data.get("format") or {}
        tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
        out = {}
        for key in ("title", "artist", "album"):
            value = tags.get(key)
            if value:
                out[key] = str(value).strip()
        try:
            dur = float(fmt.get("duration") or 0)
            if dur > 0:
                out["dur"] = dur
        except (TypeError, ValueError):
            pass
        return out
    except Exception:
        return None

def _decode_lrc(raw):
    for enc in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", "replace")

def parse_lrc(text):
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    offset = 0
    m = LRC_OFFSET.search(text)
    if m:
        try:
            offset = int(m.group(1))
        except ValueError:
            offset = 0
    rows = []
    for line in text.split("\n"):
        stamps = list(LRC_TIME.finditer(line))
        if not stamps:
            continue
        lyric = line[stamps[-1].end():].strip()
        for st in stamps:
            minute = int(st.group(1))
            second = int(st.group(2))
            frac = st.group(3) or "0"
            if len(frac) == 1:
                ms = int(frac) * 100
            elif len(frac) == 2:
                ms = int(frac) * 10
            else:
                ms = int(frac[:3])
            at = minute * 60000 + second * 1000 + ms + offset
            rows.append((max(0, at), lyric))
    rows.sort(key=lambda r: r[0])
    # 双语 lrc：翻译行与原文时间戳相同（±30ms）且文本不同，并成 (t, 原文, 翻译)
    merged = []
    i = 0
    n = len(rows)
    while i < n:
        t, text = rows[i]
        tr = ""
        if text and i + 1 < n:
            t2, text2 = rows[i + 1]
            if abs(t2 - t) <= 30 and text2 and text2 != text:
                tr = text2
                i += 1
        merged.append((t, text, tr))
        i += 1
    return merged

def find_lrc(path):
    if not path:
        return ""
    stem = os.path.splitext(path)[0]
    for cand in (stem + ".lrc", stem + ".LRC", path + ".lrc", path + ".LRC"):
        if os.path.isfile(cand):
            return cand
    return ""

def load_lyrics(path):
    lrc = find_lrc(path)
    if not lrc:
        return []
    try:
        with open(lrc, "rb") as f:
            return parse_lrc(_decode_lrc(f.read()))
    except Exception:
        return []

def _sniff_audio(path):
    """看文件头判断是不是音频：解析接口挂了常返回 JSON/HTML 错误页（HTTP 还是 200），
    存成 .mp3 后 MCI 能 open 却立刻停——就是「播放中断：格式不受支持」的真相。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return True
    if len(head) < 8:
        return bool(head)
    if head[:3] == b"ID3" or head[:4] == b"fLaC" or head[:4] == b"OggS" \
            or head[:4] == b"RIFF":
        return True
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:    # MPEG 帧同步
        return True
    if head[4:8] == b"ftyp":                            # mp4/m4a
        return True
    return False

# ---------------------------------------------------------------- 播放引擎

class MciPlayer:
    """MCI 播放器：一个别名复用一个设备，切歌先 close 再 open。"""

    ALIAS = "vbhelper_music"

    def __init__(self):
        self._send = None
        self._geterr = None
        self._buf = ctypes.create_unicode_buffer(512)
        self._ebuf = ctypes.create_unicode_buffer(512)
        try:
            self._send = ctypes.windll.winmm.mciSendStringW
            self._send.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                   ctypes.c_uint, ctypes.c_void_p]
            self._send.restype = ctypes.c_uint
            self._geterr = ctypes.windll.winmm.mciGetErrorStringW
            self._geterr.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
            self._geterr.restype = ctypes.c_bool
        except Exception:
            self._send = None
        self._open = False
        self._path = ""
        self._length = 0
        self._volume = 70
        self.volume_ok = True
        self.last_error = ""

    def _cmd(self, cmd):
        if self._send is None:
            return 1, ""
        try:
            rc = self._send(cmd, self._buf, len(self._buf), None)
            return rc, (self._buf.value or "").strip()
        except Exception:
            return 1, ""

    def _err(self, rc):
        if not rc:
            return ""
        if self._geterr is not None:
            try:
                if self._geterr(rc, self._ebuf, len(self._ebuf)):
                    text = (self._ebuf.value or "").strip()
                    if text:
                        return text
            except Exception:
                pass
        return _friendly_mci_error(rc)

    def open(self, path):
        self.close()
        self.last_error = ""
        ext = os.path.splitext(path)[1].lower()
        if ext == ".wav":
            dev = "waveaudio"
        elif ext in (".mid", ".midi"):
            dev = "sequencer"
        else:
            dev = "mpegvideo"
        rc, _ = self._cmd('open "%s" type %s alias %s' % (path, dev, self.ALIAS))
        if rc:
            self.last_error = self._err(rc)
            return False, self.last_error
        self._open = True
        self._path = path
        self._cmd("set %s time format milliseconds" % self.ALIAS)
        _, val = self._cmd("status %s length" % self.ALIAS)
        try:
            self._length = max(0, int(val))
        except (TypeError, ValueError):
            self._length = 0
        rc, _ = self._cmd("setaudio %s volume to %d" % (self.ALIAS, int(self._volume * 10)))
        self.volume_ok = rc == 0
        return True, ""

    def close(self):
        if self._open:
            self._cmd("close %s" % self.ALIAS)
        self._open = False
        self._path = ""
        self._length = 0

    def play(self, from_ms=None):
        if not self._open:
            return False
        if from_ms is not None:
            rc, _ = self._cmd("seek %s to %d" % (self.ALIAS, max(0, int(from_ms))))
            if rc:
                return False
        rc, _ = self._cmd("play %s" % self.ALIAS)
        if rc:
            self.last_error = self._err(rc)
        return rc == 0

    def pause(self):
        if not self._open:
            return False
        rc, _ = self._cmd("pause %s" % self.ALIAS)
        return rc == 0

    def resume(self):
        if not self._open:
            return False
        rc, _ = self._cmd("resume %s" % self.ALIAS)
        return rc == 0

    def stop(self):
        if not self._open:
            return False
        rc, _ = self._cmd("stop %s" % self.ALIAS)
        return rc == 0

    def seek(self, ms):
        if not self._open:
            return False
        rc, _ = self._cmd("seek %s to %d" % (self.ALIAS, max(0, int(ms))))
        return rc == 0

    def set_volume(self, volume):
        self._volume = max(0, min(100, int(volume)))
        if not self._open:
            return True
        rc, _ = self._cmd("setaudio %s volume to %d" % (self.ALIAS, int(self._volume * 10)))
        self.volume_ok = rc == 0
        return self.volume_ok

    def mode(self):
        if not self._open:
            return ""
        _, val = self._cmd("status %s mode" % self.ALIAS)
        return val

    def position(self):
        if not self._open:
            return 0
        _, val = self._cmd("status %s position" % self.ALIAS)
        try:
            return max(0, int(val))
        except (TypeError, ValueError):
            return 0

    def length(self):
        return self._length

    @property
    def opened(self):
        return self._open

class MediaPrep:
    """转码与原生路径统一成一条后台队列，切歌时取消旧的 ffmpeg。"""

    def __init__(self, results):
        self.ffmpeg = _find_tool("ffmpeg")
        self._results = results
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._proc = None
        self._gen = 0
        self._closed = False
        self._last_cleanup = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, path, gen, force=False):
        self._q.put((path, int(gen), bool(force)))

    def cancel(self, gen):
        with self._lock:
            self._gen = max(self._gen, int(gen))
            proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def close(self):
        self._closed = True
        self.cancel(1 << 30)
        try:
            self._q.put_nowait((None, 0, False))
        except Exception:
            pass

    def _run(self):
        while not self._closed:
            try:
                path, gen, force = self._q.get(timeout=0.4)
            except queue.Empty:
                continue
            if path is None:
                break
            with self._lock:
                if gen < self._gen:
                    continue
            if not force and os.path.splitext(path)[1].lower() in NATIVE_EXTS:
                self._results.put((gen, path, True, ""))
                continue
            target = _cache_target(path)
            if target and os.path.isfile(target):
                self._results.put((gen, target, True, ""))
                continue
            if not self.ffmpeg:
                self._results.put((gen, "", False,
                                   "需要 ffmpeg 才能播放 FLAC / M4A / OGG。\n"
                                   "把 ffmpeg.exe 放进 PATH 或程序目录后重开播放器即可。"))
                continue
            ok, msg = self._convert(path, target, gen)
            if not self._closed:
                self._results.put((gen, target if ok else "", ok, msg))

    def _convert(self, path, target, gen):
        if not target:
            return False, "无法创建转码缓存目录。"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except Exception as e:
            return False, "无法创建转码缓存：%s" % e
        tmp = target + ".part"
        cmd = [
            self.ffmpeg, "-v", "error", "-y", "-i", path, "-vn",
            "-codec:a", "libmp3lame", "-b:a", "320k", "-f", "mp3", tmp,
        ]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE,
                                    creationflags=CREATE_NO_WINDOW)
            with self._lock:
                if gen < self._gen:
                    proc.kill()
                self._proc = proc
            _, err = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            if proc is not None:
                proc.kill()
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False, "转码超时，这个文件可能太大或已损坏。"
        except Exception as e:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False, "转码失败：%s" % e
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
        if proc is None or proc.returncode != 0:
            try:
                os.remove(tmp)
            except Exception:
                pass
            text = (err or b"").decode("utf-8", "replace").strip().splitlines()
            return False, (text[-1] if text else "ffmpeg 转码失败。")
        try:
            os.replace(tmp, target)
        except Exception as e:
            return False, "写入转码缓存失败：%s" % e
        self._cleanup()
        return True, ""

    def _cleanup(self):
        now = time.time()
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        try:
            files = []
            for name in os.listdir(CACHE_DIR):
                # 全部音频扩展都纳入清点：以前只认 .mp3，纯在线播 flac 时
                # 缓存文件永远不被清理，磁盘会一直涨
                if os.path.splitext(name)[1].lower() not in AUDIO_EXTS \
                        and not name.endswith(".part"):
                    continue
                p = os.path.join(CACHE_DIR, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
            files.sort()
            total = sum(f[1] for f in files)
            while files and (len(files) > CACHE_MAX_FILES or total > CACHE_MAX_BYTES):
                _, size, p = files.pop(0)
                try:
                    os.remove(p)
                    total -= size
                except OSError:
                    pass
        except Exception:
            pass

def _meta_worker(q, results, stop, ffprobe):
    while not stop.is_set():
        try:
            path = q.get(timeout=0.4)
        except queue.Empty:
            continue
        if path is None or stop.is_set():
            break
        meta = _ffprobe_meta(path, ffprobe)
        if meta:
            results.put((path, meta))

def _scan_worker(folders, gen, stop, results, limit):
    songs = []
    seen = set()
    truncated = False
    skip = {"$recycle.bin", "system volume information", "node_modules", ".git"}
    try:
        for root in folders:
            if stop.is_set():
                return
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                if stop.is_set():
                    return
                dirnames[:] = sorted(
                    [d for d in dirnames
                     if not d.startswith(".") and d.lower() not in skip],
                    key=str.lower)
                for name in sorted(filenames, key=str.lower):
                    if stop.is_set():
                        return
                    if len(songs) >= limit:
                        truncated = True
                        break
                    if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                        continue
                    path = os.path.join(dirpath, name)
                    key = _normcase(path)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    songs.append({
                        "path": path,
                        "folder": root,
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                    })
                if truncated:
                    break
            if truncated:
                break
    except Exception:
        pass
    results.put(("scan", gen, songs, truncated))

def _load_meta_cache():
    try:
        with open(META_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, dict):
            return {k: v for k, v in items.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}

def _save_meta_cache(items):
    try:
        os.makedirs(os.path.dirname(META_CACHE), exist_ok=True)
        tmp = META_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": items}, f, ensure_ascii=False)
        os.replace(tmp, META_CACHE)
    except Exception:
        pass

from . import musicapi, musicnet

# ---------------------------------------------------------------- 配色与常量

M_BG = "#14171d"        # 窗口底
M_SURF = "#1a1e26"      # 导航、播放条
M_SURF2 = "#20252f"     # 输入框、hover
M_SURF3 = "#272d39"     # 选中、卡片
M_LINE = "#2b3240"
M_TEXT = "#dfe3ea"
M_MUTED = "#878e9c"
M_DIM = "#5b6371"
M_ACC = "#6f8dff"       # 柔和主色
M_ACC_H = "#8aa4ff"
M_ACC_BG = "#22304c"    # 选中底
M_ONLINE = "#9b8cff"
M_GOOD = "#5fbf8b"
M_WARN = "#e0956a"
M_BAD = "#e0686a"

ROW_H = 34
CARD_H = 62

QUALITY_ORDER = ("flac24bit", "flac", "320k", "128k")
QUALITY_LABEL = {"128k": "128k", "320k": "320k", "flac": "flac",
                 "flac24bit": "flac24", "wav": "wav"}
SOURCE_LABEL = {"kw": "酷我", "kg": "酷狗", "tx": "QQ", "wy": "网易", "mg": "咪咕",
                "local": "本地"}
KIND_LABEL = {"music": "歌曲", "songlist": "歌单", "artist": "歌手", "album": "专辑"}
ONLINE_EXTS = {"128k": ".mp3", "320k": ".mp3", "flac": ".flac",
               "flac24bit": ".flac", "wav": ".wav"}

VIEW_TITLE = {
    "search": "搜索", "board": "榜单", "plist": "歌单",
    "all": "全部歌曲", "fav": "我喜欢的音乐", "recent": "最近播放",
    "queue": "播放队列", "lyric": "歌词", "source": "音源管理",
    "down": "下载", "set": "设置",
}

# ---------------------------------------------------------------- 曲目模型

def _local_uid(path):
    return "L:" + _normcase(path)

def _online_uid(source, songmid):
    return "O:%s:%s" % (source, songmid)

def make_local(path, mtime=0.0, size=0, title="", artist="", album="", dur=0.0):
    ft, fa = _filename_meta(path)
    return {
        "uid": _local_uid(path), "kind": "local", "path": path,
        "title": title or ft, "artist": artist or fa, "album": album,
        "dur": float(dur or 0), "mtime": mtime, "size": size,
        "source": "local", "folder": os.path.dirname(path),
    }

def make_online(info):
    """把 musicnet 返回的搜索结果规成统一曲目。"""
    t = {
        "uid": _online_uid(info.get("source", ""), info.get("songmid", "")),
        "kind": "online", "source": info.get("source", ""),
        "songmid": info.get("songmid", ""),
        "albumid": info.get("albumid", ""),
        "title": info.get("name") or "未知歌曲",
        "artist": info.get("singer") or "",
        "album": info.get("albumName") or "",
        "dur": float(info.get("dur") or 0),
        "interval": info.get("interval") or "",
        "qualitys": list(info.get("qualitys") or []),
        "sizes": dict(info.get("sizes") or {}),
        "pic": info.get("pic") or "",
    }
    for key in ("hash", "albumAudioId", "strMediaMid", "albumMid", "songId",
                "copyrightId", "lrcUrl", "mrcUrl", "trcUrl"):
        if info.get(key) is not None:
            t[key] = info[key]
    return t

def _fmt_interval(dur):
    try:
        sec = int(dur)
    except (TypeError, ValueError):
        return ""
    return "%02d:%02d" % (sec // 60, sec % 60)

def api_music_info(track):
    """按 toOldMusicInfo 的扁平结构组装给音源脚本的 musicInfo。

    字段名与嵌套结构需与 userApi 的 musicInfo 旧格式一致。
    """
    qualitys = track.get("qualitys") or []
    sizes = track.get("sizes") or {}
    info = {
        "name": track.get("title") or "",
        "singer": track.get("artist") or "",
        "source": track.get("source") or "",
        "songmid": track.get("songmid") or "",
        "interval": track.get("interval") or _fmt_interval(track.get("dur")),
        "albumName": track.get("album") or "",
        "img": track.get("pic") or "",
        "typeUrl": {},
        "albumId": track.get("albumid") or "",
        "types": [{"type": q, "size": sizes.get(q)} for q in qualitys],
        "_types": {q: {"size": sizes.get(q)} for q in qualitys},
    }
    source = track.get("source")
    if source == "kg":
        info["hash"] = track.get("hash") or ""
        info["albumAudioId"] = track.get("albumAudioId")
    elif source == "tx":
        info["strMediaMid"] = track.get("strMediaMid") or ""
        info["albumMid"] = track.get("albumMid")
        info["songId"] = track.get("songId") or ""
    elif source == "mg":
        info["copyrightId"] = track.get("copyrightId") or ""
        info["lrcUrl"] = track.get("lrcUrl")
        info["mrcUrl"] = track.get("mrcUrl")
        info["trcUrl"] = track.get("trcUrl")
    return info

def pick_quality(track, prefer):
    """在曲目可用音质里挑一个：优先目标音质，往下退化，再往上试。"""
    qualitys = track.get("qualitys") or []
    if not qualitys:
        return ""
    if prefer in qualitys:
        return prefer
    order = list(QUALITY_ORDER)
    try:
        start = order.index(prefer)
    except ValueError:
        start = len(order) - 2
    for i in range(start, -1, -1):
        if order[i] in qualitys:
            return order[i]
    for i in range(start + 1, len(order)):
        if order[i] in qualitys:
            return order[i]
    return qualitys[0]

def _fmt_size_short(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1e8:
        return "%.1f亿" % (n / 1e8)
    if n >= 1e4:
        return "%.1f万" % (n / 1e4)
    return "%d" % int(n)

def _migrate_uids(items):
    """旧版把裸路径存进 fav/recent，统一成 L: 前缀的 uid。

    不迁移的话升级后我喜欢和最近播放会整列变空——数据没丢但界面认不出。
    """
    out = []
    for item in items or []:
        if not isinstance(item, str) or not item:
            continue
        if item.startswith("L:") or item.startswith("O:"):
            out.append(item)
        else:
            out.append(_local_uid(item))
    return out

def _online_cache_name(track, quality):
    raw = "online|%s|%s|%s" % (track.get("source"), track.get("songmid"), quality)
    key = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]
    return os.path.join(CACHE_DIR, key + ONLINE_EXTS.get(quality, ".mp3"))

# ---------------------------------------------------------------- 小控件

class _Slider:
    """细轨道滑杆：手柄是一枚实心圆点，不带白圈；拖动只动画面，松手才提交。"""

    def __init__(self, parent, bg, on_change=None, on_commit=None,
                 height=14, knob=5, width=0):
        self.canvas = tk.Canvas(parent, width=width or 20, height=height, bg=bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.value = 0.0
        self.dragging = False
        self.knob = knob
        self._on_change = on_change
        self._on_commit = on_commit
        self._track = "#343b49"
        self._fill = M_ACC
        self._drawn = None       # 脏检查：(value, w, h, dragging, track, fill)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)

    def set_colors(self, track=None, fill=None):
        if track:
            self._track = track
        if fill:
            self._fill = fill
        self._draw()

    def set(self, value):
        if self.dragging:
            return
        try:
            value = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            value = 0.0
        if abs(value - self.value) < 1e-4:
            return
        self.value = value
        self._draw()

    def _w(self):
        return max(2, self.canvas.winfo_width())

    def _pad(self):
        return self.knob + 1

    def _x_for(self, value):
        return self._pad() + value * (self._w() - self._pad() * 2)

    def _value_for(self, x):
        return max(0.0, min(1.0, (x - self._pad()) / max(1.0, self._w() - self._pad() * 2)))

    def _draw(self):
        c = self.canvas
        try:
            w = max(2, c.winfo_width())
            h = c.winfo_height()
        except tk.TclError:
            return
        # 脏检查：值/尺寸/拖拽态/配色都没变就不重画（250ms 进度 tick 的主要开销就在这）
        state = (round(self.value, 3), w, h, self.dragging,
                 self._track, self._fill)
        if state == self._drawn:
            return
        self._drawn = state
        c.delete("all")
        y = h // 2
        x = self._x_for(self.value)
        c.create_line(self._pad(), y, w - self._pad(), y, fill=self._track,
                      width=2, capstyle=tk.ROUND)
        if self.value > 0.001:
            c.create_line(self._pad(), y, x, y, fill=self._fill, width=2,
                          capstyle=tk.ROUND)
        # 拖拽时手柄放大 2px，替代 LX 的拖拽预览层：给「正在拖」一个明确的视觉态
        k = self.knob + (2 if self.dragging else 0)
        c.create_oval(x - k, y - k, x + k, y + k, fill="#e9eefb", outline="")

    def _notify(self):
        if self._on_change:
            self._on_change(self.value)

    def _press(self, e):
        self.dragging = True
        self.value = self._value_for(e.x)
        self._draw()
        self._notify()

    def _move(self, e):
        if not self.dragging:
            return
        self.value = self._value_for(e.x)
        self._draw()
        self._notify()

    def _release(self, e):
        if not self.dragging:
            return
        self.dragging = False
        self.value = self._value_for(e.x)
        self._draw()
        self._notify()
        if self._on_commit:
            self._on_commit(self.value)

class _RoundButton:
    """实心圆形按钮：主色底、白色 Feather 图标、悬停提亮，不画边框。

    播放/暂停等图标统一走 icons.draw()，不再用 ▶/❚❚ 这类会被系统
    fallback 成彩色 emoji 的字符。set_text 只留给「…」这类纯文本占位。
    """

    def __init__(self, parent, text, command, bg, diameter=44):
        self.diameter = diameter
        self.command = command
        self._text = text
        self._icon = None
        self._hover = False
        self.canvas = tk.Canvas(parent, width=diameter, height=diameter, bg=bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.bind("<Button-1>", lambda e: self.command())
        self.canvas.bind("<Enter>", lambda e: self._set_hover(True))
        self.canvas.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def pack(self, **kw):
        self.canvas.pack(**kw)

    def set_text(self, text):
        if self._icon is not None or text != self._text:
            self._icon = None
            self._text = text
            self._draw()

    def set_icon(self, kind):
        """kind: 'play' | 'pause' | None（None 回到文本模式）。"""
        if kind != self._icon:
            self._icon = kind
            self._draw()

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _draw(self):
        c = self.canvas
        c.delete("all")
        d = self.diameter
        c.create_oval(1, 1, d - 1, d - 1, outline="",
                      fill=M_ACC_H if self._hover else M_ACC)
        if self._icon:
            isz = max(12, int(d * 0.52))
            ph = _icon_photo(self._icon, isz, "#ffffff")
            c.create_image(d / 2, d / 2, image=ph)
            self._photo = ph
            return
        c.create_text(d / 2, d / 2, text=self._text, fill="#ffffff",
                      font=(core.FONT, max(8, int(d / 3.2)), "bold"))

def _icon_photo(name, size, color):
    """音乐模块的 Feather 图标图片（render.photo 负责缓存）。"""
    return render.photo(icons.draw(name, size, color),
                        key="music:ico:%s:%d:%s" % (name, size, color))

def _text_button(parent, text, command, bg, fg=None, size=9, padx=6, pady=3,
                 hover_bg=None, tip=None, icon=None):
    color = fg or M_MUTED

    def paint(c):
        ph = _icon_photo(icon, max(12, size + 5), c)
        lbl.configure(image=ph)
        lbl._icon_photo = ph

    kw = {}
    if icon:
        kw = {"image": _icon_photo(icon, max(12, size + 5), color),
              "compound": "left" if text else "center"}
    lbl = tk.Label(parent, text=text, bg=bg, fg=color,
                   font=(core.FONT, size), cursor="hand2", padx=padx, pady=pady,
                   **kw)
    if icon:
        lbl._icon_photo = kw["image"]
        lbl._icon_name = icon
    lbl.bind("<Button-1>", lambda e: command())

    def enter(_e):
        lbl.configure(bg=hover_bg or M_SURF3, fg=M_TEXT)
        if icon:
            paint(M_TEXT)

    def leave(_e):
        lbl.configure(bg=bg, fg=color)
        if icon:
            paint(color)

    lbl.bind("<Enter>", enter)
    lbl.bind("<Leave>", leave)
    return lbl

def _icon_button(parent, name, command, bg, size=16, color=None, hover=None,
                 padx=4, pady=2):
    """纯图标按钮：悬停时按新颜色重画，图片对象挂在控件上防回收。"""
    color = color or M_TEXT
    hover = hover or M_ACC_H

    def paint(c):
        ph = _icon_photo(name, size, c)
        lbl.configure(image=ph)
        lbl._icon_photo = ph

    lbl = tk.Label(parent, bg=bg, cursor="hand2", padx=padx, pady=pady)
    paint(color)
    lbl.bind("<Button-1>", lambda e: command())
    lbl.bind("<Enter>", lambda e: (lbl.configure(bg=M_SURF3), paint(hover)))
    lbl.bind("<Leave>", lambda e: (lbl.configure(bg=bg), paint(color)))
    return lbl

def _flat_button(parent, text, command, primary=False, size=9):
    btn = tk.Button(
        parent, text=text, command=command,
        bg=M_ACC if primary else M_SURF2,
        fg="#ffffff" if primary else M_TEXT,
        activebackground=M_ACC_H if primary else M_SURF3,
        activeforeground="#ffffff" if primary else M_TEXT,
        relief="flat", bd=0, padx=12, pady=4, highlightthickness=0,
        takefocus=0, font=(core.FONT, size), cursor="hand2")
    btn.bind("<Enter>", lambda e: btn.configure(bg=M_ACC_H if primary else M_SURF3))
    btn.bind("<Leave>", lambda e: btn.configure(bg=M_ACC if primary else M_SURF2))
    return btn

class _ChipRow:
    """一行胶囊按钮，支持分组单选（类型 / 源 / 音质）。"""

    def __init__(self, parent, bg, on_pick):
        self.frame = tk.Frame(parent, bg=bg)
        self.on_pick = on_pick
        self.chips = []

    def add_group(self, group, items, current):
        for value, label in items:
            chip = tk.Label(self.frame, text=label, font=(core.FONT, 8),
                            padx=9, pady=2, cursor="hand2", bd=1,
                            relief="flat")
            chip.data = (group, value)
            chip.pack(side="left", padx=(0, 5))
            chip.bind("<Button-1>", lambda e, c=chip: self.on_pick(*c.data))
            chip.bind("<Enter>", lambda e, c=chip: self._paint(c, True))
            chip.bind("<Leave>", lambda e, c=chip: self._paint(c, False))
            self.chips.append(chip)
        self.set_current(group, current)

    def set_current(self, group, value):
        for chip in self.chips:
            if chip.data[0] == group:
                chip.selected = chip.data[1] == value
            self._paint(chip, False)

    @staticmethod
    def _paint(chip, hover):
        if getattr(chip, "selected", False):
            chip.configure(bg=M_ACC_BG, fg="#bcd0ff", activebackground=M_ACC_BG,
                           highlightbackground=M_LINE)
        elif hover:
            chip.configure(bg=M_SURF3, fg=M_TEXT, highlightbackground=M_SURF3)
        else:
            chip.configure(bg=M_SURF2, fg=M_MUTED, highlightbackground=M_SURF2)

    def pack(self, **kw):
        self.frame.pack(**kw)

    def grid(self, **kw):
        self.frame.grid(**kw)

class _List:
    """虚拟化曲目列表：只画视口内的行。

    滚动用「绝对像素偏移 + 行高前缀和 + 二分」做单一真相，避免 top/off 双状态漂移；
    行高可以不一致（列表行 34px，卡片行更高），8000 行也只渲染可见的二十几行。
    """

    def __init__(self, parent, bg):
        self.canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0)
        self.rows = []
        self.render = None
        self.on_play = None
        self.on_menu = None
        self.on_end = None
        self.scroll_w = 9
        self.scroll = 0.0
        self.hover = -1
        self.sel = -1
        self._heights = []
        self._prefix = [0.0]
        self._total = 0.0
        self._bar_drag = False
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self._scroll_to(self.scroll - 120))
        self.canvas.bind("<Button-5>", lambda e: self._scroll_to(self.scroll + 120))
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover_move)
        self.canvas.bind("<Leave>", self._hover_leave)
        self.canvas.bind("<Button-3>", self._menu)

    # ---- 数据 ----
    def pack(self, **kw):
        self.canvas.pack(**kw)

    def grid(self, **kw):
        self.canvas.grid(**kw)

    def pack_forget(self):
        self.canvas.pack_forget()

    def winfo_ismapped(self):
        return self.canvas.winfo_ismapped()

    def set_rows(self, rows, render=None, sel=-1, keep_scroll=False):
        self.rows = rows or []
        if render is not None:
            self.render = render
        heights = [self._row_h(r) for r in self.rows]
        prefix = [0.0]
        for h in heights:
            prefix.append(prefix[-1] + h)
        self._heights = heights
        self._prefix = prefix
        self._total = prefix[-1] if prefix else 0.0
        if not keep_scroll:
            self.scroll = 0.0
        if sel >= 0:
            self.sel = sel
        self._clamp()
        self.redraw()

    def _row_h(self, row):
        if self.render:
            try:
                h = self.render(row, "h")
                return int(h) if h else ROW_H
            except Exception:
                return ROW_H
        return ROW_H

    # ---- 几何 ----
    def _view_h(self):
        return max(1, self.canvas.winfo_height())

    def _view_w(self):
        return max(1, self.canvas.winfo_width())

    def content_w(self):
        return max(20, self._view_w() - self.scroll_w - 8)

    def _max_scroll(self):
        return max(0.0, self._total - self._view_h())

    def _clamp(self):
        self.scroll = max(0.0, min(self.scroll, self._max_scroll()))

    def _index_at_y(self, y):
        """绝对像素 y 落在第几行（用前缀和二分）。"""
        i = bisect.bisect_right(self._prefix, y) - 1
        return max(0, min(len(self._rows_or_0()) - 1, i))

    def _rows_or_0(self):
        return self.rows or [None]

    def _index_at_pointer(self, py):
        if not self.rows:
            return -1
        return self._index_at_y(self.scroll + max(0, py))

    # ---- 滚动 ----
    def _scroll_to(self, value):
        self.scroll = value
        self._clamp()
        self.redraw()
        if self.scroll >= self._max_scroll():
            self._load_more()

    def _wheel(self, e):
        # 一格滚三行：按像素倍数算会快到站不住
        self._scroll_to(self.scroll - (e.delta / 120.0) * ROW_H * 3)
        return "break"

    def scroll_to_index(self, index, center=True):
        if not (0 <= index < len(self._heights)):
            return
        view = self._view_h()
        y = self._prefix[index]
        h = self._heights[index]
        if center:
            self._scroll_to(y - view / 2 + h / 2)
        elif y < self.scroll:
            self._scroll_to(y)
        elif y + h > self.scroll + view:
            self._scroll_to(y + h - view)

    # ---- 绘制 ----
    def redraw(self):
        c = self.canvas
        c.delete("all")
        if not self.rows or not self.render:
            return
        w = self._view_w() - self.scroll_w - 8
        view = self._view_h()
        i = self._index_at_y(self.scroll)
        y = self._prefix[i] - self.scroll
        while i < len(self.rows) and y < view:
            if y + self._heights[i] > 0:
                try:
                    self.render(self.rows[i], "draw", c, i, 0, y, w,
                                self._heights[i], i == self.hover, i == self.sel)
                except Exception:
                    pass
            y += self._heights[i]
            i += 1
        self._draw_bar(w + self.scroll_w + 8, view)

    def _draw_bar(self, w, view):
        if self._total <= view or not self.rows:
            return
        x = w - self.scroll_w + 1
        c = self.canvas
        c.create_rectangle(x, 3, x + self.scroll_w - 3, view - 3,
                           fill="#191d25", outline="")
        knob = max(26.0, view * view / self._total)
        room = max(1.0, view - knob - 6)
        pos = (self.scroll / self._max_scroll()) * room if self._max_scroll() > 0 else 0
        c.create_rectangle(x + 1, 3 + pos, x + self.scroll_w - 4,
                           3 + pos + knob, fill="#3a4250", outline="",
                           tags="knob")

    def _bar_hit(self, x, y):
        return (x >= self._view_w() - self.scroll_w and
                3 <= y <= self._view_h() - 3)

    def _bar_jump(self, y, view):
        knob = max(26.0, view * view / self._total)
        room = max(1.0, view - knob - 6)
        ratio = max(0.0, min(1.0, (y - 3) / room))
        self._scroll_to(ratio * self._max_scroll())

    # ---- 交互 ----
    def _press(self, e):
        view = self._view_h()
        if self._bar_hit(e.x, e.y):
            self._bar_drag = True
            self._bar_jump(e.y, view)
            return
        index = self._index_at_pointer(e.y)
        if index < 0:
            return
        self.sel = index
        self.redraw()

    def _motion(self, e):
        if self._bar_drag:
            self._bar_jump(e.y, self._view_h())

    def _release(self, e):
        if self._bar_drag:
            self._bar_drag = False
            return
        index = self._index_at_pointer(e.y)
        if index >= 0 and index == self.sel and callable(self.on_play):
            self.on_play(self.rows[index], index)

    def _hover_move(self, e):
        index = self._index_at_pointer(e.y)
        if index != self.hover:
            self.hover = index
            self.redraw()

    def _hover_leave(self, e):
        if self.hover != -1:
            self.hover = -1
            self.redraw()

    def _load_more(self):
        if callable(self.on_end) and self.rows:
            self.on_end()

    def _menu(self, e):
        index = self._index_at_pointer(e.y)
        if 0 <= index < len(self.rows) and callable(self.on_menu):
            self.sel = index
            self.redraw()
            self.on_menu(e, self.rows[index], index)

    def selected_track(self):
        if 0 <= self.sel < len(self.rows):
            return self.rows[self.sel]
        return None

class _ScrollPane:
    """卡片类页面（音源、下载、设置）用的竖向滚动容器。"""

    def __init__(self, parent, bg):
        self.frame = tk.Frame(parent, bg=bg)
        self.canvas = tk.Canvas(self.frame, bg=bg, highlightthickness=0, bd=0,
                                yscrollincrement=24)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._width)
        # 卡片页的子控件会截走 canvas 的事件；绑到窗口并按 canvas 范围过滤
        bind_canvas_wheel(self.frame.winfo_toplevel(), self.canvas,
                          area=self.canvas, units=3)

    def _sync(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0))

    def _width(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def clear(self):
        for child in list(self.inner.winfo_children()):
            child.destroy()
        self.canvas.yview_moveto(0)

    def add(self, widget):
        widget.pack(in_=self.inner, fill="x", padx=12, pady=(0, 8))
        return widget

    def fill(self):
        outer = tk.Frame(self.inner, bg=M_BG)
        outer.pack(fill="x", padx=12, pady=(0, 8))
        return outer

    def pack(self, **kw):
        self.frame.pack(**kw)

    def grid(self, **kw):
        self.frame.grid(**kw)

class CoverCache:
    """封面缩略图缓存：只留最近 24 张，超出就丢，PhotoImage 由 Tk 持有。"""

    MAX = 24

    def __init__(self):
        self._imgs = OrderedDict()      # key -> PhotoImage（只准主线程创建）
        self._raw = OrderedDict()       # key -> PIL.Image（后台线程只碰 PIL）
        self._blob = OrderedDict()      # url -> 原图字节（resize 重裁不重下）
        self._blurs = OrderedDict()     # url -> PIL.Image 歌词页模糊压暗底图
        self._missing = set()
        self._q = queue.Queue()
        self._thread = None
        self._stop = threading.Event()

    def ready(self, url, size):
        """取已就位的图；后台拿到 PIL 原图后在主线程转 PhotoImage 并缓存。"""
        if not url:
            return None
        key = (url, size)
        if key in self._imgs:
            self._imgs.move_to_end(key)
            return self._imgs[key]
        if key in self._raw:
            # 后台已消费：真图转 PhotoImage；False 为失败占位，不可重复入队，否则会无限重下
            raw = self._raw[key]
            if raw is not None:
                try:
                    from PIL import ImageTk
                    self._imgs[key] = ImageTk.PhotoImage(raw)
                except Exception:
                    self._imgs[key] = None
            else:
                self._imgs[key] = None
            self._raw.pop(key, None)
            self._imgs.move_to_end(key)
            return self._imgs[key]
        if self._refit(url, size):
            return self.ready(url, size)    # 原图在手：重裁一次就有
        if key not in self._missing:
            self._missing.add(key)
            self._q.put(key)
            self._start()
        return None

    def _refit(self, url, size):
        """原图字节在缓存里就按新尺寸中心重裁（窗口 resize 不触发重新下载）。"""
        blob = self._blob.get(url)
        if not blob:
            return False
        try:
            from PIL import Image, ImageOps
            im = Image.open(io.BytesIO(blob)).convert("RGB")
            self._raw[(url, size)] = ImageOps.fit(
                im, (size, size), Image.Resampling.BILINEAR)
            return True
        except Exception:
            return False

    def blur_ready(self, url):
        """歌词页背景图（模糊+压暗）。封面原图在手上就现场磨一张，之后走缓存。"""
        if not url:
            return None
        im = self._blurs.get(url)
        if im is not None:
            return im
        for (u, _s), raw in list(self._raw.items()):
            if u != url or raw is None:
                continue
            try:
                from PIL import ImageEnhance, ImageFilter
                big = raw.resize((720, 720), Image.Resampling.BILINEAR)
                dark = ImageEnhance.Brightness(
                    big.filter(ImageFilter.GaussianBlur(30))).enhance(0.55)
                self._blurs[url] = dark
                while len(self._blurs) > 6:
                    self._blurs.popitem(last=False)
                return dark
            except Exception:
                return None
        return None

    def drop(self, url):
        for key in [k for k in self._imgs if k[0] == url]:
            self._imgs.pop(key, None)
        for key in [k for k in self._raw if k[0] == url]:
            self._raw.pop(key, None)
        self._blob.pop(url, None)
        self._blurs.pop(url, None)

    def _start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        import io
        try:
            from PIL import Image, ImageFilter, ImageEnhance, ImageOps
        except Exception:
            return
        while not self._stop.is_set():
            try:
                url, size = self._q.get(timeout=0.5)
            except queue.Empty:
                break
            try:
                blob = _fetch_bytes(url)
                im = Image.open(io.BytesIO(blob))
                im = im.convert("RGB")
                self._blob[url] = blob
                while len(self._blob) > 12:
                    self._blob.popitem(last=False)
                # 中心裁成方形：thumbnail 只缩不裁，长条封面会在方框里溢出
                im = ImageOps.fit(im, (size, size), Image.Resampling.BILINEAR)
                self._raw[(url, size)] = im
                # 顺手产歌词页背景：放大→高斯模糊→压暗，纯 PIL 一次成本几十毫秒
                try:
                    big = im.resize((720, 720), Image.Resampling.BILINEAR)
                    dark = ImageEnhance.Brightness(
                        big.filter(ImageFilter.GaussianBlur(30))).enhance(0.55)
                    self._blurs[url] = dark
                    while len(self._blurs) > 6:
                        self._blurs.popitem(last=False)
                except Exception:
                    pass
            except Exception:
                self._raw[(url, size)] = None
            self._missing.discard((url, size))
            while len(self._raw) > self.MAX:
                self._raw.popitem(last=False)

    def busy(self):
        return bool(self._missing)

    def close(self):
        self._stop.set()
        self._imgs.clear()
        self._raw.clear()
        self._blob.clear()
        self._blurs.clear()
        self._missing.clear()

def _ssl_context():
    """下载/抓取用的 SSL context。

    音乐直链多跑在 CDN 上，证书链经常缺中间证书，Windows 系统证书存储
    又可能没有这个根，Python 默认校验就会报 certificate verify failed。
    标准 lx-music 在 Node 里下载用的是 Mozilla 完整 CA bundle，这里用
    certifi（同为 Mozilla bundle）对齐；拿不到就退回系统默认，不硬失败。
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

def _fetch_bytes(url, limit=2 * 1024 * 1024, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.kuwo.cn/"})
    with musicapi.music_opener().open(req, timeout=timeout) as resp:
        return resp.read(limit)

class StreamHub:
    """本地流式代理：把正在下载的缓存文件以 HTTP 暴露给 MCI，边下边播。

    MCI 的 mpegvideo 对 http 源走 DirectShow 渐进播放（实测：限速喂 60s mp3，
    秒出声、时长正确、位置前进），不等整轨下完。Range 请求按已下载字节供给，
    读到下载进度边缘就等下载线程追上——seek 和慢网都成立。
    """

    READ_MARGIN = 64 * 1024       # 不读最尾部：给写线程留出半块缓冲

    def __init__(self):
        self._srv = None
        self._port = 0
        self._items = {}          # token -> {"path": .part, "total": 0, "got_fn": fn}
        self._lock = threading.Lock()

    def start(self):
        if self._srv is not None:
            return
        try:
            self._srv = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), self._make_handler())
            self._srv.daemon_threads = True
            self._port = self._srv.server_address[1]
            threading.Thread(target=self._srv.serve_forever,
                             daemon=True, name="stream-hub").start()
        except Exception:
            self._srv = None

    def register(self, token, path, total, got_fn):
        with self._lock:
            self._items[token] = {"path": path, "total": int(total or 0),
                                  "got_fn": got_fn}

    def unregister(self, token):
        with self._lock:
            self._items.pop(token, None)

    def url_for(self, token):
        # 路径必须带 .mp3：老 DirectShow filter 按 URL 扩展名识别内容
        return "http://127.0.0.1:%d/s/%s.mp3" % (self._port, token)

    def stop(self):
        srv = self._srv
        self._srv = None
        with self._lock:
            self._items.clear()
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    def _make_handler(self):
        """返回一个 handler 类（socketserver 要类不是实例/方法）。"""
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            # 保持 HTTP/1.0：MCI 背后的 DirectShow source filter 是老组件，
            # 对 1.1 keep-alive 兼容性差（实测 open 直接失败）
            def do_GET(self):
                name = self.path.rsplit("/", 1)[-1]
                token = name[:-4] if name.endswith(".mp3") else name
                with outer._lock:
                    item = outer._items.get(token)
                if item is None:
                    self.send_error(404)
                    return
                got = 0
                try:
                    got = int((item["got_fn"] or (lambda: 0))())
                except Exception:
                    pass
                safe_end = max(0, got - outer.READ_MARGIN)
                rng = self.headers.get("Range") or ""
                start = 0
                partial = False
                if rng.startswith("bytes="):
                    try:
                        start = int(rng.split("=")[1].split("-")[0])
                        partial = True
                    except (ValueError, IndexError):
                        start = 0
                total = item["total"]
                if total and start >= total:
                    self.send_error(416)
                    return
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", "audio/mpeg")
                if partial:
                    self.send_header("Content-Range", "bytes %d-%d/%s"
                                     % (start, (total or 0) - 1 if total else "*",
                                        total if total else "*"))
                if total:
                    self.send_header("Content-Length", str(total - start))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                try:
                    with open(item["path"], "rb") as f:
                        f.seek(start)
                        pos = start
                        while not outer._stopped():
                            # 当前可安全供给的字节数（跟随下载进度）
                            try:
                                got = int((item["got_fn"] or (lambda: 0))())
                            except Exception:
                                got = pos
                            safe_end = max(0, got - outer.READ_MARGIN)
                            if total:
                                safe_end = min(safe_end, total)
                            if pos >= safe_end:
                                if total and pos >= total:
                                    break               # 供给完毕
                                time.sleep(0.1)         # 等下载线程追上
                                continue
                            chunk = f.read(min(64 * 1024, int(safe_end - pos)))
                            if not chunk:
                                time.sleep(0.1)
                                continue
                            self.wfile.write(chunk)
                            pos += len(chunk)
                except (ConnectionAbortedError, ConnectionResetError,
                        BrokenPipeError, OSError):
                    pass
                except Exception:
                    pass

            def log_message(self, *a):
                pass

        return H     # 返回类本身，socketserver 负责按 (request, addr, server) 实例化

    def _stopped(self):
        return self._srv is None

# ---------------------------------------------------------------- 缓冲与下载

class Streamer:
    """一条后台下载通道：在线播放要先把整轨缓冲下来，顺手支持「下载到本地」。

    播放用的缓冲文件走 LRU 清理，用户显式下载的文件写到下载目录不动它。
    """

    def __init__(self, results, hub=None):
        self._results = results
        self._hub = hub
        self._q = queue.Queue()
        self._jobs = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, url, target, meta, keep=False, stream=False):
        with self._lock:
            self._seq += 1
            jid = self._seq
            self._jobs[jid] = {"url": url, "target": target, "keep": keep,
                               "cancel": False, "meta": meta, "stream": stream,
                               "token": "t%d" % jid}
        self._q.put(jid)
        return jid

    def is_downloading(self, uid):
        """这首的播放缓冲是否还在下（流式 stall 守卫要用）。"""
        with self._lock:
            return any(j["meta"] == ("play", uid) and not j["cancel"]
                       for j in self._jobs.values())

    def cancel(self, jid):
        with self._lock:
            job = self._jobs.get(jid)
            if job is not None:
                job["cancel"] = True

    def cancel_meta(self, meta_key):
        with self._lock:
            for job in self._jobs.values():
                if job["meta"] == meta_key:
                    job["cancel"] = True

    def close(self):
        self.cancel_meta(None)
        with self._lock:
            for job in self._jobs.values():
                job["cancel"] = True

    def _run(self):
        while True:
            try:
                jid = self._q.get(timeout=0.4)
            except queue.Empty:
                continue
            with self._lock:
                job = self._jobs.get(jid)
            if job is None or job["cancel"]:
                continue
            self._download(jid, job)

    def _download(self, jid, job):
        import urllib.request
        url = job["url"]
        target = job["target"]
        meta = job["meta"]
        tmp = target + ".part"
        opener = musicapi.music_opener()
        streaming = bool(job.get("stream")) and self._hub is not None
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "http://www.kuwo.cn/"})
            with opener.open(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                last_emit = 0.0
                announced = False
                with open(tmp, "wb") as f:
                    while True:
                        with self._lock:
                            if job["cancel"]:
                                break
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        job["got"] = got       # hub 的供给进度读这里，必须同步
                        now = time.time()
                        if streaming and not announced and total and \
                                got >= min(192 * 1024, total * 0.04 + 1):
                            # 已有足够数据让 DirectShow 初始化：注册到本地流
                            # 服务并通知主线程立即开播，不等整轨
                            announced = True
                            self._hub.register(job["token"], tmp, total,
                                               lambda: job["got"])
                            self._results.put(
                                ("early", meta, self._hub.url_for(job["token"])))
                        if now - last_emit > 0.2:
                            last_emit = now
                            self._results.put(("progress", meta, got, total))
                if job["cancel"]:
                    raise _Cancelled()
                if streaming and announced:
                    self._hub.unregister(job["token"])
            if got <= 0:
                raise OSError("空响应")
            if not (streaming and announced):
                os.replace(tmp, target)
            else:
                # 流式期间 .part 一直被本地服务读着：多试几次 replace，
                # 实在占用着就留给 _cleanup 兜底回收（不影响本次播放）
                for _ in range(5):
                    try:
                        os.replace(tmp, target)
                        break
                    except OSError:
                        time.sleep(0.2)
            self._results.put(("done", meta, target, got, total))
        except _Cancelled:
            if streaming and announced:
                self._hub.unregister(job["token"])
            self._drop(tmp)
            self._results.put(("cancel", meta, "", 0, 0))
        except Exception as e:
            if streaming and announced:
                self._hub.unregister(job["token"])
            self._drop(tmp)
            self._results.put(("error", meta, str(e)[:160], 0, 0))
        finally:
            with self._lock:
                self._jobs.pop(jid, None)

    @staticmethod
    def _drop(path):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

class _Cancelled(Exception):
    pass

# ---------------------------------------------------------------- 主窗口

_window = None

def open_window(app):
    """菜单入口：已经开着就拉到前面，绝不重复建第二个播放器。"""
    global _window
    if _window is not None:
        try:
            if _window.win.winfo_exists():
                _window.bring()
                return _window
        except tk.TclError:
            pass
        _window = None
    _window = MusicWindow(app)
    return _window

def shutdown(app):
    global _window
    if _window is not None:
        try:
            _window.close()
        except Exception:
            pass

class MusicWindow:
    def __init__(self, app):
        self.app = app
        self.root = app.root
        self._init_state()
        self._build_window()
        self._build_ui()
        self._build_rail_foot()
        self._build_mini()
        self._bind_keys()
        self._paint_all()
        self._start_workers()
        self._apply_view()
        self._pump_job = self.win.after(80, self._pump)
        self._tick_job = self.win.after(250, self._tick)
        self._slow_job = self.win.after(2000, self._slow_tick)
        if self.folders:
            self._scan_all()
        self.win.after(120, self._show_initial)

    # ---- 状态 ----
    def _init_state(self):
        cfg = self.app.cfg
        m = cfg.get("music")
        if not isinstance(m, dict):
            m = {}
            cfg["music"] = m
        self.mcfg = m

        self.folders = [p for p in (m.get("folders") or []) if isinstance(p, str)]
        self.fav_uids = _migrate_uids(m.get("fav") or [])
        self.fav_keys = set(self.fav_uids)

        try:
            self.volume = max(0, min(100, int(m.get("volume", 62))))
        except (TypeError, ValueError):
            self.volume = 62
        try:
            self.mode = int(m.get("mode", 0)) % 3
        except (TypeError, ValueError):
            self.mode = 0
        self.quality = m.get("quality") if m.get("quality") in QUALITY_LABEL else "320k"
        self.down_dir = m.get("down_dir") or os.path.join(
            os.path.expanduser("~"), "Music", APP_NAME)

        # 曲目总表：本地 + 在线，全部按 uid 索引
        self.tracks = {}
        self.local_uids = []
        for raw in m.get("online") or []:
            if isinstance(raw, dict) and raw.get("uid"):
                self.tracks[raw["uid"]] = raw
        self.recent_uids = _migrate_uids(m.get("recent") or [])
        self.view = ("all", "")
        self.rows = []
        self.ctx_uids = []          # 当前播放上下文
        self.current = None
        self.current_uid = ""
        self.position = 0.0
        self.duration = 0.0
        self.playing = False
        self._preparing = False
        self._opened_for = ""
        self._play_started_at = 0.0

        self._lyrics = []
        self._lyric_times = []
        self._lyric_idx = -1
        self._lyric_for = ""
        self._lyric_cache = OrderedDict()
        # 歌词平滑滚动：scroll/scroll_to 单位是「行」（浮点），动画帧逼近目标；
        # manual_until 之前用户手动浏览接管，不跟随播放
        self._lyric_scroll = 0.0
        self._lyric_scroll_to = 0.0
        self._lyric_anim_job = None
        self._lyric_manual = 0.0
        self._lyric_drawn = None       # (idx, round(scroll*10), w, h) 脏检查
        self._lyric_fade_cache = None  # 上下边缘渐隐条 (top, bottom, h)（懒生成）

        self._scanning = False
        self._truncated = False
        self._scan_gen = 0
        self._scan_stop = threading.Event()
        self._closed = False
        self._mini = None
        self._mini_shown = False
        self._mini_drag = None
        self._save_job = None
        self._hint_job = None
        self._meta_flush_job = None
        self._cover_job = None

        self._meta_cache = _load_meta_cache()
        self._meta_queued = set()
        self._meta_dirty = False
        self._meta_stop = threading.Event()
        self._meta_results = queue.Queue()
        self._meta_queue = queue.Queue()
        self._ffprobe = _find_tool("ffprobe")
        self._scan_results = queue.Queue()
        self._prep_results = queue.Queue()
        self._prep_gen = 0
        self.player = MciPlayer()
        self.prep = MediaPrep(self._prep_results)

        self._net_results = queue.Queue()
        self._api_results = queue.Queue()
        self.api = musicapi.LxRuntime(self._api_results)
        self.sources = musicapi.load_sources()
        self._api_state = "idle"        # idle / loading / ready / error
        self._api_error = ""
        self._api_message = ""
        self._req_map = {}              # 请求 key -> (uid, 用途)
        self._url_deadline = 0.0        # 当前解析+缓冲的总超时点（time.time() 秒）
        self._url_tries = 0             # 本曲直链解析已自动重试的次数
        self._net_gen = 0
        self._net_busy = False
        self._pending_play_uid = ""
        self._down_jobs = []            # 下载任务
        self._down_seq = 0
        self.stream_hub = StreamHub()
        self.stream_hub.start()
        self.streamer = Streamer(self._net_results, self.stream_hub)
        self.covers = CoverCache()

        self._search_rows = []
        self._board_rows = []
        self._plist_rows = []
        self._search_page = 1
        self._search_all_page = 1
        self._board_page = 1
        self._board_all_page = 1
        self._search_hint = ""
        self._board_cur = None
        self._plist_cur = None
        self._net_cbs = {}
        self._pic_requested = set()
        self._pending_key = None
        self._prep_owner = ""
        self._import_win = None
        self._f_row = tkfont.Font(family=core.FONT, size=9)
        self._f_row2 = tkfont.Font(family=core.FONT, size=8)
        self._f_tag = tkfont.Font(family=core.FONT, size=7)
        self.board_source = "kw"
        self.plist_source = "kw"
        self.search_source = "kw"
        self.search_kind = "music"
        self.my_plists = [p for p in (m.get("playlists") or [])
                          if isinstance(p, dict) and p.get("id")]
        self._list_title = ""
        self._list_sub = ""

        last_uid = m.get("last") if isinstance(m.get("last"), str) else ""
        try:
            self._last_pos = max(0, int(m.get("pos") or 0))
        except (TypeError, ValueError):
            self._last_pos = 0
        if last_uid and last_uid in self.tracks:
            self.current = self.tracks[last_uid]
            self.current_uid = last_uid
            self.position = self._last_pos / 1000.0
            self.duration = float(self.current.get("dur") or 0)

    # ---- 窗口外壳 ----
    def _build_window(self):
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.win.title("音乐播放器 - " + APP_NAME)
        self.win.configure(bg=M_BG)
        self.win.minsize(880, 520)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.win.attributes("-topmost", bool(self.mcfg.get("topmost", False)))
        except Exception:
            pass
        self._place_window()

    def _place_window(self):
        work = core.work_area()
        fit = getattr(self.app, "_fit_work_area", None)
        if callable(fit):
            try:
                w, h = fit(1060, 660)
            except Exception:
                w, h = 1060, 660
        else:
            w = max(880, min(1060, work[2] - work[0] - 24))
            h = max(520, min(660, work[3] - work[1] - 44))
        geom = self.mcfg.get("geom")
        placed = False
        if isinstance(geom, str):
            m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geom)
            if m:
                ww = max(880, min(int(m.group(1)), work[2] - work[0] - 24))
                hh = max(520, min(int(m.group(2)), work[3] - work[1] - 44))
                x, y = int(m.group(3)), int(m.group(4))
                if (work[0] <= x and work[1] <= y and
                        x + ww <= work[2] and y + hh + 36 <= work[3]):
                    self.win.geometry("%dx%d%+d%+d" % (ww, hh, x, y))
                    placed = True
        if not placed:
            self.win.geometry("%dx%d" % (w, h))
            center = getattr(self.app, "_center_on_panel", None)
            if callable(center):
                self.win.after(60, lambda: center(self.win))

    def _show_initial(self):
        if self._closed:
            return
        try:
            self.win.attributes("-topmost", True)
            self.win.deiconify()
            self.win.lift()
        except tk.TclError:
            pass

    # ---- 布局 ----
    def _build_ui(self):
        # 底栏先 pack：side=bottom 从窗口底部先拿走自己的高度，
        # 内容区只拿剩余——否则窗口高度紧张时进度条行会被裁掉一半
        self._build_bar()
        body = tk.Frame(self.win, bg=M_BG)
        body.pack(fill="both", expand=True)
        self.rail = self._build_rail(body)
        self.rail.pack(side="left", fill="y")
        tk.Frame(body, bg=M_LINE, width=1).pack(side="left", fill="y")
        self.content = tk.Frame(body, bg=M_BG)
        self.content.pack(side="left", fill="both", expand=True)
        self._build_list_pane()
        self._build_lyric_pane()
        self._build_cards_pane()

    def _build_rail(self, parent):
        rail = tk.Frame(parent, bg=M_SURF, width=186)
        rail.pack_propagate(False)
        inner = tk.Frame(rail, bg=M_SURF)
        inner.pack(fill="both", expand=True, padx=8, pady=(12, 6))
        self.nav_inner = inner
        self.navs = {}

        def group(label):
            tk.Label(inner, text=label, bg=M_SURF, fg=M_DIM,
                     font=(core.FONT, 8)).pack(anchor="w", padx=2, pady=(0, 5))

        def divider():
            tk.Frame(inner, bg=M_LINE, height=1).pack(fill="x", padx=2, pady=7)

        group("在 线")
        self._add_nav(("search", ""), "搜索", "search")
        self._add_nav(("board", ""), "榜单", "bar-chart-2")
        self._add_nav(("plist", ""), "歌单", "list")
        divider()
        group("本 地")
        self._add_nav(("all", ""), "全部歌曲", "music")
        self._add_nav(("fav", ""), "我喜欢的音乐", "heart")
        self._add_nav(("recent", ""), "最近播放", "clock")
        self._add_nav(("queue", ""), "播放队列", "menu")
        divider()

        head = tk.Frame(inner, bg=M_SURF)
        head.pack(fill="x")
        tk.Label(head, text="文件夹", bg=M_SURF, fg=M_DIM,
                 font=(core.FONT, 8)).pack(side="left", padx=2)
        _icon_button(head, "folder-plus", self.add_folder, M_SURF,
                     size=14, color=M_DIM, hover=M_TEXT,
                     padx=5, pady=1).pack(side="right")
        self.folder_box = tk.Frame(inner, bg=M_SURF)
        self.folder_box.pack(fill="x")
        self._rebuild_folder_navs()

        divider()
        group("其 他")
        self._add_nav(("lyric", ""), "歌词", "file-text")
        self._add_nav(("source", ""), "音源管理", "sliders")
        self._add_nav(("down", ""), "下载", "download")
        self._add_nav(("set", ""), "设置", "settings")
        return rail

    def _build_rail_foot(self):
        """底部两块文字要等导航铺完再挂。

        先挂底部会让最后一个导航项在 pack 时触发整棵树的重新排版，
        实测白吃 390ms；窗口打开的延迟基本都在这儿。
        """
        rail = self.rail
        self.src_state = tk.Label(rail, text="", bg=M_SURF, fg=M_DIM,
                                  font=(core.FONT, 7), anchor="w", wraplength=162,
                                  justify="left")
        self.src_state.pack(side="bottom", fill="x", padx=10, pady=(2, 8))
        tk.Label(rail, text="空格 播放/暂停   ←/→ 切歌\n↑/↓ 音量   M 迷你模式   / 搜索",
                 bg=M_SURF, fg=M_DIM, font=(core.FONT, 7), justify="left",
                 anchor="w", wraplength=162).pack(side="bottom", fill="x",
                                                  padx=10, pady=(2, 4))

    def _nav_parent(self, key):
        return self.folder_box if key[0] == "folder" else self.nav_inner

    def _rebuild_folder_navs(self):
        for child in list(self.folder_box.winfo_children()):
            child.destroy()
        for key in [k for k in self.navs if k[0] == "folder"]:
            self.navs.pop(key, None)
        for path in self.folders:
            self._add_nav(("folder", path), _short_dir(path), "")
        self._paint_navs()

    def _add_nav(self, key, label, icon):
        parent = self._nav_parent(key)
        folder = key[0] == "folder"
        row = tk.Frame(parent, bg=M_SURF, cursor="hand2",
                       height=26 if folder else 29)
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)
        bar = tk.Frame(row, bg=M_SURF, width=3)
        bar.pack(side="left", fill="y")
        ico = None
        if not folder:
            ico = tk.Label(row, bg=M_SURF)
            ph = _icon_photo(icon, 16, M_DIM)
            ico.configure(image=ph)
            ico._icon_photo = ph
            ico.pack(side="left", padx=(3, 0))
        num = tk.Label(row, text="", bg=M_SURF, fg=M_DIM, font=(core.FONT, 8))
        num.pack(side="right", padx=(0, 4))
        txt = tk.Label(row, text=label, bg=M_SURF,
                       fg=M_MUTED if folder else M_TEXT,
                       font=(core.FONT, 8 if folder else 9), anchor="w")
        txt.pack(side="left", fill="x", expand=True, padx=(4, 2) if folder else 0)
        parts = {"row": row, "bar": bar, "ico": ico, "txt": txt, "num": num,
                 "icon": icon}
        self.navs[key] = parts
        widgets = [w for w in (row, ico, txt, num) if w is not None]
        for w in widgets:
            w.bind("<Button-1>", lambda e, k=key: self._select_view(k))
            w.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
            w.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
            if folder:
                w.bind("<Button-3>", lambda e, p=key[1]: self._folder_menu(e, p))
        self._paint_nav(key)
        return row

    def _paint_nav(self, key):
        parts = self.navs.get(key)
        if not parts:
            return
        active = self.view == key
        bg = M_ACC_BG if active else M_SURF
        parts["row"].configure(bg=bg)
        parts["txt"].configure(bg=bg, fg=M_TEXT if active else M_MUTED)
        parts["num"].configure(bg=bg, fg=M_MUTED if active else M_DIM)
        parts["bar"].configure(bg=M_ACC if active else bg)
        if parts.get("ico"):
            col = M_ACC_H if active else M_DIM
            ph = _icon_photo(parts["icon"], 16, col)
            parts["ico"].configure(bg=bg, image=ph)
            parts["ico"]._icon_photo = ph

    def _paint_navs(self):
        for key in self.navs:
            self._paint_nav(key)

    def _nav_hover(self, key, on):
        if self.view == key:
            return
        parts = self.navs.get(key)
        if not parts:
            return
        bg = M_SURF2 if on else M_SURF
        for name in ("row", "txt", "num", "bar"):
            widget = parts.get(name)
            if widget is not None:
                widget.configure(bg=bg)
        if parts.get("ico"):
            parts["ico"].configure(bg=bg)

    # ---- 内容区：列表页 ----
    def _build_list_pane(self):
        pane = tk.Frame(self.content, bg=M_BG)
        self.list_pane = pane
        head = tk.Frame(pane, bg=M_BG)
        head.pack(fill="x", padx=18, pady=(14, 4))
        title_row = tk.Frame(head, bg=M_BG)
        title_row.pack(fill="x")
        self.view_title = tk.Label(title_row, text="全部歌曲", bg=M_BG, fg=M_TEXT,
                                   font=(core.FONT, 14, "bold"), anchor="w")
        self.view_title.pack(side="left")
        self.view_count = tk.Label(title_row, text="", bg=M_SURF2, fg=M_MUTED,
                                   font=(core.FONT, 8), padx=6, pady=1)
        self.view_count.pack(side="left", padx=(8, 0))
        self.head_right = tk.Frame(title_row, bg=M_BG)
        self.head_right.pack(side="right")
        self.tools = tk.Frame(head, bg=M_BG)
        self.tools.pack(fill="x", pady=(9, 0))

        # 搜索框
        self.search_box = tk.Frame(self.tools, bg=M_SURF2, bd=1, relief="flat",
                                   highlightbackground=M_LINE)
        self.search = tk.Entry(self.search_box, bg=M_SURF2, fg=M_TEXT,
                               insertbackground=M_TEXT, relief="flat", bd=0,
                               font=(core.FONT, 9))
        self._search_icon = _icon_photo("search", 14, M_DIM)
        tk.Label(self.search_box, image=self._search_icon, bg=M_SURF2).pack(
            side="left", padx=(7, 0))
        self.search.pack(side="left", fill="x", expand=True, padx=(3, 7), pady=4)
        self.search.bind("<FocusIn>", self._search_focus_in)
        self.search.bind("<FocusOut>", self._search_focus_out)
        self.search.bind("<Return>", lambda e: self._do_search())
        self.search.bind("<KeyRelease>", self._on_search_key)
        self._search_placeholder()

        self.chips = _ChipRow(self.tools, M_BG, self._on_chip)
        self.play_all_btn = _flat_button(self.tools, "播放全部",
                                         lambda: self._play_rows_all(), True)
        self.add_btn = _flat_button(self.tools, "添加文件夹", self.add_folder, True)
        self.rescan_btn = _flat_button(self.tools, "重新扫描", self._scan_all)
        self.back_btn = _text_button(self.tools, "返回", self._back_from_detail,
                                     M_BG, fg=M_MUTED, size=9,
                                     icon="arrow-left")

        self.status = tk.Label(head, text="", bg=M_BG, fg=M_MUTED,
                               font=(core.FONT, 8), anchor="w")
        self.status.pack(fill="x", pady=(6, 0))

        self.list = _List(pane, M_BG)
        self.list.pack(fill="both", expand=True, padx=(10, 6), pady=(2, 8))
        self.list.on_play = lambda row, i: self._on_row_activate(row, i)
        self.list.on_menu = self._on_row_menu
        self.list.on_end = self._on_list_end
        self.list.render = self._render_row
        self.empty = tk.Label(pane, text="", bg=M_BG, fg=M_DIM,
                              font=(core.FONT, 10), justify="center")
        self.col_head = tk.Frame(pane, bg=M_BG)

    def _search_placeholder(self, text="搜索歌曲、歌手、歌单"):
        self.search.delete(0, "end")
        self.search.insert(0, text)
        self.search.configure(fg=M_DIM)
        self.search_ph = text

    def _search_focus_in(self, _e=None):
        """聚焦：高亮边框；占位文字只是提示，不能跟着用户输入进搜索词。"""
        self.search.configure(highlightcolor=M_ACC)
        ph = getattr(self, "search_ph", "")
        if ph and self.search.get() == ph:
            self.search.delete(0, "end")
            self.search.configure(fg=M_TEXT)

    def _search_focus_out(self, _e=None):
        """失焦且没输入内容：把占位提示放回去。"""
        if not self.search.get().strip():
            self._search_placeholder()

    def _search_text(self):
        val = self.search.get().strip()
        ph = getattr(self, "search_ph", "")
        if not val or val == ph:
            return ""
        # 防御：旧状态/异常焦点下占位文字可能残留在真实输入前面
        if ph and val.startswith(ph):
            val = val[len(ph):].strip()
        return val

    def _on_search_key(self, e):
        if e.keysym in ("Up", "Down", "Return", "Escape"):
            return
        ph = getattr(self, "search_ph", "")
        if ph and self.search.get() == ph:
            return
        if ph and self.search.get().startswith(ph):
            self.search.delete(0, len(ph))
            self.search.configure(fg=M_TEXT)
        self._schedule_search()

    def _schedule_search(self):
        job = getattr(self, "_search_job", None)
        if job is not None:
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass
        if self.view[0] == "search":
            self._search_job = self.win.after(420, self._do_search)
        else:
            self._search_job = self.win.after(160, self._refresh_rows)

    def _on_chip(self, group, value):
        if group == "src":
            self.search_source = value
            self.chips.set_current("src", value)
            if self.view[0] == "search":
                self._do_search()
            elif self.view[0] == "board":
                self.board_source = value
                self._open_board_list()
            elif self.view[0] == "plist":
                self.plist_source = value
                self._open_plist_list()
        elif group == "kind":
            self.search_kind = value
            self.chips.set_current("kind", value)
            self._do_search()
        elif group == "q":
            self.quality = value
            self.chips.set_current("q", value)
            self.mcfg["quality"] = value
            self._save_state_soon()
            self._hint("默认音质：%s" % value, 2000)

    # ---- 内容区：歌词页 ----
    def _build_lyric_pane(self):
        pane = tk.Frame(self.content, bg=M_BG)
        self.lyric_pane = pane
        self.lyric_canvas = tk.Canvas(pane, bg=M_BG, highlightthickness=0, bd=0)
        self.lyric_canvas.pack(fill="both", expand=True)
        self.lyric_canvas.bind("<Configure>", lambda e: self._lyric_invalidate())
        self.lyric_canvas.bind("<Button-1>", self._lyric_press)
        self.lyric_canvas.bind("<B1-Motion>", self._lyric_motion)
        self.lyric_canvas.bind("<ButtonRelease-1>", self._lyric_release)
        self.lyric_canvas.bind("<MouseWheel>", self._lyric_wheel)
        self._lyric_press_y = 0
        self._lyric_dragged = False
        self._font_lyric = tkfont.Font(family=core.FONT, size=10)
        self._font_lyric_on = tkfont.Font(family=core.FONT, size=16, weight="bold")
        self._font_lyric_tr = tkfont.Font(family=core.FONT, size=9)
        self._blur_photos = OrderedDict()   # url -> PhotoImage（歌词页背景，主线程转）

    # ---- 内容区：卡片区（音源/下载/设置）----
    def _build_cards_pane(self):
        pane = tk.Frame(self.content, bg=M_BG)
        self.cards_pane = pane
        head = tk.Frame(pane, bg=M_BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        self.cards_title = tk.Label(head, text="", bg=M_BG, fg=M_TEXT,
                                    font=(core.FONT, 14, "bold"), anchor="w")
        self.cards_title.pack(side="left")
        self.cards_right = tk.Frame(head, bg=M_BG)
        self.cards_right.pack(side="right")
        self.cards_sub = tk.Label(pane, text="", bg=M_BG, fg=M_MUTED,
                                  font=(core.FONT, 8), anchor="w", justify="left")
        self.cards_sub.pack(fill="x", padx=18)
        self.cards_scroll = _ScrollPane(pane, M_BG)
        self.cards_scroll.pack(fill="both", expand=True, padx=4, pady=(10, 8))

    # ---- 底部播放条 ----
    def _build_bar(self):
        # 高度必须锁死：mid 是 propagate(False) 的空 frame，对 bar 的请求高度
        # 贡献为 1px，bar 会塌缩到 left 的 58px，mid 里的进度条行被窗口底边裁掉
        bar = tk.Frame(self.win, bg=M_SURF, height=76)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        tk.Frame(self.win, bg=M_LINE, height=1).pack(fill="x", side="bottom")
        self.bar = bar

        left = tk.Frame(bar, bg=M_SURF)
        left.pack(side="left", padx=(10, 0), pady=8)
        self.cover_box = tk.Frame(left, bg=M_SURF3, width=42, height=42)
        self.cover_box.pack(side="left")
        self.cover_box.pack_propagate(False)
        self.cover_lbl = tk.Label(self.cover_box, bg=M_SURF3)
        self.cover_lbl.place(relx=0.5, rely=0.5, anchor="center")
        self._cover_ph = _icon_photo("music", 20, M_ACC_H)
        self.cover_lbl.configure(image=self._cover_ph)
        self.cover_img = tk.Label(self.cover_box, bg=M_SURF3, bd=0)
        info = tk.Frame(left, bg=M_SURF, width=190, height=58)
        info.pack(side="left", padx=(9, 0))
        info.pack_propagate(False)   # 固定宽高，长歌名才不会把播放条撑开
        # 58：150% DPI 实测两行（10pt bold + 8pt）需要 ~52px，留余量不裁底
        self.title_lbl = tk.Label(info, text="未在播放", bg=M_SURF, fg=M_TEXT,
                                  font=(core.FONT, 10, "bold"), anchor="w")
        self.title_lbl.pack(fill="x")
        self.artist_lbl = tk.Label(info, text=self._ellipsize(
            "导入文件夹或在线搜索后开始", self._f_row2, 178),
            bg=M_SURF, fg=M_MUTED, font=(core.FONT, 8), anchor="w")
        self.artist_lbl.pack(fill="x", pady=(2, 0))
        self.fav_btn = tk.Label(left, bg=M_SURF, cursor="hand2")
        self.fav_btn.pack(side="left", padx=(8, 0))
        self.fav_btn.bind("<Button-1>", lambda e: self._toggle_fav())
        self.fav_btn.bind("<Enter>", lambda e: self._paint_fav_btn(
            hover_color="#e6a0a0"))
        self.fav_btn.bind("<Leave>", lambda e: self._paint_fav_btn())
        self._paint_fav_btn(False)

        right = tk.Frame(bar, bg=M_SURF)
        right.pack(side="right", padx=(0, 12))
        self.mini_btn = _text_button(right, "迷你", lambda: self._toggle_mini(True),
                                     M_SURF, fg=M_DIM, size=8, padx=5)
        self.mini_btn.pack(side="right")
        self.badge = tk.Label(right, text="", bg=M_SURF3, fg=M_MUTED,
                              font=(core.FONT, 7), padx=5, pady=1)
        self.badge.pack(side="right", padx=(0, 6))
        tk.Label(right, text="音量", bg=M_SURF, fg=M_MUTED,
                 font=(core.FONT, 7)).pack(side="left")
        self.vol_slider = _Slider(right, M_SURF, self._vol_change,
                                  self._vol_commit, height=13, knob=4, width=72)
        self.vol_slider.canvas.pack(side="left", padx=5)
        self.vol_slider.canvas.bind("<MouseWheel>",
                                    lambda e: self._nudge_volume(
                                        5 if e.delta > 0 else -5))
        self.vol_lbl = tk.Label(right, text="62%", bg=M_SURF, fg=M_DIM,
                                font=(core.FONT, 7), width=4, anchor="e")
        self.vol_lbl.pack(side="left")
        self.vol_lbl.bind("<Double-Button-1>", lambda e: self._toggle_mute())
        self._volume_before_mute = 0

        mid = tk.Frame(bar, bg=M_SURF)
        mid.pack(side="left", fill="both", expand=True, padx=12, pady=6)
        mid.pack_propagate(False)
        ctrl = tk.Frame(mid, bg=M_SURF)
        ctrl.pack(fill="x")
        ctrl.columnconfigure(0, weight=1)
        ctrl.columnconfigure(2, weight=1)
        self.lyric_line = tk.Label(ctrl, text="", bg=M_SURF, fg=M_MUTED,
                                   font=(core.FONT, 9), anchor="w")
        self.lyric_line.grid(row=0, column=0, sticky="w")
        group = tk.Frame(ctrl, bg=M_SURF)
        group.grid(row=0, column=1)
        self.mode_btn = tk.Label(group, text=MODE_LABELS[0], bg=M_SURF, fg=M_MUTED,
                                 font=(core.FONT, 8), cursor="hand2", padx=6, pady=1)
        self.mode_btn.pack(side="left", padx=(0, 6))
        self.mode_btn.bind("<Button-1>", lambda e: self._cycle_mode())
        self.mode_btn.bind("<Enter>", lambda e: self.mode_btn.configure(bg=M_SURF3))
        self.mode_btn.bind("<Leave>", lambda e: self.mode_btn.configure(
            bg=M_SURF, fg=M_ACC_H if self.mode else M_MUTED))
        self.prev_btn = _text_button(group, "", self._prev, M_SURF,
                                     fg=M_TEXT, size=12, padx=5,
                                     icon="skip-back")
        self.prev_btn.pack(side="left")
        self.play_btn = _RoundButton(group, "", self._toggle_play, M_SURF, 32)
        self.play_btn.set_icon("play")
        self.play_btn.canvas.pack(side="left", padx=6)
        self.next_btn = _text_button(group, "", self._next, M_SURF,
                                     fg=M_TEXT, size=12, padx=5,
                                     icon="skip-forward")
        self.next_btn.pack(side="left")
        self.addlater_btn = _text_button(ctrl, "稍后播放",
                                         self._add_current_later, M_SURF,
                                         fg=M_DIM, size=8, padx=5,
                                         icon="plus")
        self.addlater_btn.grid(row=0, column=2, sticky="e")

        prog = tk.Frame(mid, bg=M_SURF)
        prog.pack(fill="x", pady=(2, 0))
        self.cur_lbl = tk.Label(prog, text="0:00", bg=M_SURF, fg=M_DIM,
                                font=(core.FONT, 7))
        self.cur_lbl.pack(side="left")
        self.dur_lbl = tk.Label(prog, text="0:00", bg=M_SURF, fg=M_DIM,
                                font=(core.FONT, 7))
        self.dur_lbl.pack(side="right")
        self.seek_slider = _Slider(prog, M_SURF, self._seek_change,
                                   self._seek_commit, height=13, knob=4)
        self.seek_slider.canvas.pack(side="left", fill="x", expand=True, padx=8)

    # ---- 视图切换 ----
    FILTERABLE_VIEWS = ("search", "all", "folder", "fav", "recent", "queue")

    def _select_view(self, key):
        if self._closed:
            return
        if key[0] == "folder" and not any(
                _normcase(p) == _normcase(key[1]) for p in self.folders):
            return
        if key != self.view:
            # 列表内的筛选文本属于上一个视图，留着会让人以为新视图是空的
            self._search_placeholder()
        self.view = key
        self._paint_navs()
        self.mcfg["view"] = list(key)
        self._apply_view()

    def _pane_kind(self):
        kind = self.view[0]
        if kind in ("lyric",):
            return "lyric"
        if kind in ("source", "down", "set"):
            return "cards"
        return "list"

    def _show_pane(self):
        want = self._pane_kind()
        for name, frame in (("list", self.list_pane), ("lyric", self.lyric_pane),
                            ("cards", self.cards_pane)):
            if name == want:
                if not frame.winfo_ismapped():
                    frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

    def _apply_view(self):
        self._show_pane()
        kind = self.view[0]
        for widget in (self.search_box, self.chips.frame, self.play_all_btn,
                       self.add_btn, self.rescan_btn, self.back_btn):
            widget.pack_forget()
        for child in list(self.head_right.winfo_children()):
            child.destroy()
        self._list_title = ""
        if kind == "board":
            self._open_board_list()
            return
        if kind == "plist":
            self._open_plist_list()
            return
        if kind == "search":
            self._build_chips()
            self.search_box.pack(side="left", fill="x", expand=True)
            self.chips.frame.pack(side="left", pady=(8, 0))
        elif kind in ("all", "folder"):
            self.play_all_btn.pack(side="left")
            self.add_btn.pack(side="left", padx=(6, 0))
            self.rescan_btn.pack(side="left", padx=(6, 0))
            self.search_box.pack(side="left", fill="x", expand=True, padx=(10, 0))
        elif kind in ("fav", "recent", "queue"):
            self.play_all_btn.pack(side="left")
            self.search_box.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self._update_head()
        self._refresh_rows()

    def _build_chips(self):
        for child in list(self.chips.frame.winfo_children()):
            child.destroy()
        self.chips.chips = []
        kind = self.view[0]
        if kind == "search":
            self.chips.add_group("kind", [("music", "歌曲"), ("songlist", "歌单")],
                                 self.search_kind)
            tk.Frame(self.chips.frame, bg=M_BG, width=10).pack(side="left")
            self.chips.add_group("src", [(s, SOURCE_LABEL[s]) for s in musicnet.SOURCES],
                                 self.search_source)
            tk.Frame(self.chips.frame, bg=M_BG, width=10).pack(side="left")
            self.chips.add_group("q", [(q, QUALITY_LABEL[q]) for q in
                                       ("128k", "320k", "flac", "flac24bit")],
                                 self.quality)
        elif kind == "board":
            self.chips.add_group("src", [(s, SOURCE_LABEL[s]) for s in
                                         ("kw", "kg", "tx", "mg")],
                                 self.board_source)
            self.chips.add_group("q", [(q, QUALITY_LABEL[q]) for q in
                                       ("128k", "320k", "flac", "flac24bit")],
                                 self.quality)
        elif kind == "plist":
            self.chips.add_group("src", [(s, SOURCE_LABEL[s]) for s in
                                         ("kw", "tx")], self.plist_source)
            self.chips.add_group("q", [(q, QUALITY_LABEL[q]) for q in
                                       ("128k", "320k", "flac", "flac24bit")],
                                 self.quality)
        self.chips.frame.pack_configure()

    def _view_label(self):
        return VIEW_TITLE.get(self.view[0], "")

    def _update_head(self):
        kind = self.view[0]
        title = self._view_label()
        if kind == "folder":
            title = _short_dir(self.view[1], 26)
        elif kind == "board" and self._list_title:
            title = self._list_title
        elif kind == "plist" and self._list_title:
            title = self._list_title
        try:
            self.view_title.configure(text=title)
        except tk.TclError:
            pass

    def _back_from_detail(self):
        kind = self.view[0]
        if kind == "board":
            self._list_title = ""
            self._open_board_list()
        elif kind == "plist":
            self._list_title = ""
            self._open_plist_list()

    def _update_nav_counts(self):
        counts = {
            ("all", ""): len(self.local_uids),
            ("fav", ""): len(self.fav_uids),
            ("recent", ""): len(self.recent_uids),
            ("queue", ""): len(self.ctx_uids),
            ("source", ""): len(self.sources["items"]),
            ("down", ""): len([j for j in self._down_jobs if j["state"] != "done"]),
        }
        for folder in self.folders:
            key = ("folder", folder)
            n = 0
            for uid in self.local_uids:
                track = self.tracks.get(uid)
                if track and _normcase(track.get("folder", "")) == _normcase(folder):
                    n += 1
            counts[key] = n
        for key, parts in self.navs.items():
            if key in counts:
                parts["num"].configure(text=str(counts[key]) if counts[key] else "")
            elif key[0] == "source":
                parts["num"].configure(text=str(len(self.sources["items"])) or "")

    # ---- 行数据 ----
    def _match_text(self, track, text):
        if not text:
            return True
        hay = track.get("_match")
        if hay is None:
            # 小写索引缓存：3000 行每次过滤都拼+lower 是主线程的隐形大头
            hay = ("%s %s %s" % (track.get("title"), track.get("artist"),
                                 track.get("album"))).lower()
            track["_match"] = hay
        return text in hay

    def _rows_for_view(self):
        kind = self.view[0]
        text = self._search_text().lower() if kind in self.FILTERABLE_VIEWS else ""
        if kind == "search":
            if self.search_kind == "songlist":
                return [r for r in self._search_rows
                        if not text or text in (r.get("name") or "").lower()]
            return [r for r in self._search_rows
                    if self._match_text(r, text)]
        if kind == "board":
            return [r for r in self._board_rows
                    if r.get("rk") == "board" or self._match_text(r, text)]
        if kind == "plist":
            return [r for r in self._plist_rows
                    if r.get("rk") == "plist" or self._match_text(r, text)]
        if kind == "recent":
            rows = [self.tracks[u] for u in self.recent_uids if u in self.tracks]
        elif kind == "fav":
            rows = [self.tracks[u] for u in self.fav_uids if u in self.tracks]
        elif kind == "queue":
            rows = [self.tracks[u] for u in self.ctx_uids if u in self.tracks]
        else:
            rows = []
            for uid in self.local_uids:
                track = self.tracks.get(uid)
                if not track:
                    continue
                if kind == "folder" and _normcase(track.get("folder", "")) != \
                        _normcase(self.view[1]):
                    continue
                rows.append(track)
        return [r for r in rows if self._match_text(r, text)]

    def _refresh_rows(self):
        if self._closed:
            return
        if self._pane_kind() != "list":
            if self.view[0] == "source":
                self._render_source_cards()
            elif self.view[0] == "down":
                self._render_down_cards()
            elif self.view[0] == "set":
                self._render_settings_cards()
            return
        rows = self._rows_for_view()
        self.rows = rows
        sel = -1
        for i, row in enumerate(rows):
            if row.get("uid") == self.current_uid:
                sel = i
                break
        try:
            self.list.set_rows(rows, self._render_row, sel=sel)
        except tk.TclError:
            return
        self._update_status()

    def _update_status(self):
        if self._closed:
            return
        kind = self.view[0]
        text = ""
        if self._scanning:
            text = "正在扫描文件夹…"
        elif self._net_busy:
            text = "在线请求中…"
        elif self._api_state == "loading":
            text = "音源初始化中…"
        elif kind in ("all", "folder", "fav", "recent", "queue"):
            if self._truncated:
                text = "文件过多，已截断到 %d 条（F5 重扫）" % MAX_TRACKS
            elif not self.folders and kind == "all":
                text = ""
            else:
                text = "%d 首" % len(self.rows)
        elif kind == "search" and self._search_hint:
            text = self._search_hint
        try:
            self.status.configure(text=text)
            self.view_count.configure(text=str(len(self.rows)))
        except tk.TclError:
            pass

    def _hint(self, text, ms=4000):
        if self._closed:
            return
        self._status_keep = self.status.cget("text") if hasattr(self, "status") else ""
        try:
            self.status.configure(text=text[:150])
        except tk.TclError:
            return
        if self._hint_job is not None:
            try:
                self.win.after_cancel(self._hint_job)
            except tk.TclError:
                pass
        self._hint_job = self.win.after(ms, self._clear_hint)

    def _clear_hint(self):
        self._hint_job = None
        self._update_status()

    # ---- 行渲染 ----
    def _render_row(self, row, mode, *args):
        rk = row.get("rk", "music")
        if mode == "h":
            return 54 if rk in ("plist", "board") else ROW_H
        canvas, index, _zero, y, w, h, hover, sel = args
        playing = row.get("uid") == self.current_uid
        if rk in ("plist", "board"):
            return self._render_card(canvas, row, index, y, w, h, hover, sel)
        return self._render_music(canvas, row, index, y, w, h, hover, sel, playing)

    def _render_music(self, c, row, index, y, w, h, hover, sel, playing):
        bg = M_BG
        if playing:
            bg = "#1a2231"
        elif sel:
            bg = "#1e2530"
        elif hover:
            bg = M_SURF2
        if bg != M_BG:
            c.create_rectangle(2, y, w, y + h, fill=bg, outline="", width=0)
        dur_w, idx_w = 46, 26
        album_w = int(w * 0.24)
        artist_w = int(w * 0.26)
        title_w = max(90, w - idx_w - dur_w - album_w - artist_w - 24)
        x = 6
        if playing:
            ph = _icon_photo("play", 10, M_ACC)
            c.create_image(x + idx_w // 2, y + h / 2, image=ph)
            self._row_icon_photo = ph
        else:
            c.create_text(x + idx_w // 2, y + h / 2, text=str(index + 1),
                          anchor="center", fill=M_DIM, font=(core.FONT, 8))
        x += idx_w
        online = row.get("kind") == "online"
        title = row.get("title") or ""
        fg = M_ACC if playing else ("#dfe3ea" if (sel or hover) else "#c3c9d4")
        shown = self._ellipsize(title, self._f_row, title_w)
        c.create_text(x, y + h / 2, text=shown, anchor="w", fill=fg,
                      font=self._f_row)
        used = 0
        if online:
            tags = []
            src = SOURCE_LABEL.get(row.get("source"), row.get("source") or "")
            if src:
                tags.append((src, "#2a3446", "#9fb4d8"))
            q = row.get("quality") or ""
            if q:
                tags.append((QUALITY_LABEL.get(q, q), "#33294a", "#bfaef0"))
            for textv, bgc, fgc in tags:
                tw = self._f_tag.measure(textv) + 8
                if used + tw > min(150, title_w - self._f_row.measure(shown)):
                    break
                tx = x + self._f_row.measure(shown) + 6 + used
                c.create_rectangle(tx, y + h / 2 - 7, tx + tw, y + h / 2 + 7,
                                   fill=bgc, outline="", width=0)
                c.create_text(tx + tw / 2, y + h / 2, text=textv, anchor="center",
                              fill=fgc, font=self._f_tag)
                used += tw + 3
        x += title_w + 10
        c.create_text(x, y + h / 2,
                      text=self._ellipsize(row.get("artist") or "", self._f_row2,
                                           artist_w),
                      anchor="w", fill=M_MUTED, font=self._f_row2)
        x += artist_w + 8
        c.create_text(x, y + h / 2,
                      text=self._ellipsize(row.get("album") or "", self._f_row2,
                                           album_w),
                      anchor="w", fill="#6e7684", font=self._f_row2)
        c.create_text(w - 4, y + h / 2, text=_fmt_sec(row.get("dur") or 0),
                      anchor="e", fill=M_DIM, font=self._f_row2)

    def _render_card(self, c, row, index, y, w, h, hover, sel):
        bg = M_SURF if (hover or sel) else "#181c23"
        c.create_rectangle(2, y + 2, w, y + h - 2, fill=bg, outline=M_LINE)
        x = 14
        c.create_rectangle(x, y + 12, x + 30, y + 42, fill="#242b38", outline="")
        ph = _icon_photo(row.get("lead") or "list", 18, "#8ea3c9")
        c.create_image(x + 15, y + 27, image=ph)
        self._card_icon_photo = ph
        x += 42
        c.create_text(x, y + 17,
                      text=self._ellipsize(row.get("name") or "", self._f_row,
                                           w - 140),
                      anchor="w", fill=M_TEXT, font=self._f_row)
        c.create_text(x, y + 36,
                      text=self._ellipsize(row.get("sub") or "", self._f_row2,
                                           w - 140),
                      anchor="w", fill=M_MUTED, font=self._f_row2)
        if row.get("state_text"):
            textv = row["state_text"]
            tw = self._f_row2.measure(textv) + 4
            c.create_text(w - 8 - tw / 2, y + 27, text=textv, anchor="center",
                          fill=row.get("state_color") or M_MUTED,
                          font=self._f_row2)

    def _on_row_activate(self, row, index):
        rk = row.get("rk", "music")
        if rk == "board":
            self._open_board_songs(row)
        elif rk == "plist":
            if row.get("id") == "__import__":
                self._paste_playlist()
            else:
                self._open_plist_songs(row)
        else:
            self._set_context(self.rows)
            self._play_track(row["uid"], restart=True)

    def _on_row_menu(self, e, row, index):
        rk = row.get("rk", "music")
        if rk in ("board", "plist"):
            if rk == "plist" and row.get("id") != "__import__":
                m = tk.Menu(self.win, tearoff=0, bg=M_SURF, fg=M_TEXT,
                            activebackground=M_ACC, activeforeground="#ffffff",
                            bd=0, relief="flat", font=(core.FONT, 9))
                m.add_command(label="播放全部",
                              command=lambda: self._open_plist_songs(row, True))
                m.add_command(label="从我的歌单移除",
                              command=lambda: self._remove_plist(row))
                m.post(e.x_root, e.y_root)
            return
        track = row
        m = tk.Menu(self.win, tearoff=0, bg=M_SURF, fg=M_TEXT,
                    activebackground=M_ACC, activeforeground="#ffffff",
                    bd=0, relief="flat", font=(core.FONT, 9))
        m.add_command(label="播放", command=lambda: self._play_track(
            track["uid"], restart=True))
        m.add_command(label="下一首播放", command=lambda: self._play_later(track))
        fav = track["uid"] in self.fav_keys
        m.add_command(label="取消收藏" if fav else "收藏",
                      command=lambda: self._toggle_fav_uid(track["uid"]))
        if track.get("kind") == "online":
            m.add_command(label="下载到本地",
                          command=lambda: self._download_track(track["uid"]))
        else:
            m.add_command(label="在资源管理器中显示",
                          command=lambda: self._reveal(track.get("path")))
        m.add_separator()
        m.add_command(label="复制歌名与歌手",
                      command=lambda: self._copy("%s - %s" % (
                          track.get("artist"), track.get("title"))))
        if track.get("kind") == "online":
            m.add_command(label="复制歌曲 ID",
                          command=lambda: self._copy(str(track.get("songmid"))))
        else:
            m.add_command(label="复制完整路径",
                          command=lambda: self._copy(track.get("path")))
        m.post(e.x_root, e.y_root)

    def _on_list_end(self):
        """滚到底自动翻页。"""
        kind = self.view[0]
        if kind == "search":
            if self._search_page < self._search_all_page and not self._net_busy:
                self._search_page += 1
                self._run_search(append=True)
        elif kind == "board":
            if self._board_page < self._board_all_page and not self._net_busy:
                self._board_page += 1
                self._run_board_songs(self._board_cur, append=True)

    # ---- 文件夹与扫描 ----
    def add_folder(self, path=None):
        if path is None:
            try:
                path = filedialog.askdirectory(
                    parent=self.win, mustexist=True, title="选择音乐文件夹",
                    initialdir=os.path.expanduser("~"))
            except tk.TclError:
                return
        if not path:
            return
        path = os.path.normpath(os.path.abspath(path))
        if not os.path.isdir(path):
            messagebox.showwarning(APP_NAME, "这个文件夹不存在。", parent=self.win)
            return
        if any(_normcase(path) == _normcase(p) for p in self.folders):
            self._hint("这个文件夹已经在列表里了", 2500)
            return
        self.folders.append(path)
        self.mcfg["folders"] = self.folders
        save_cfg(self.app.cfg)
        self._rebuild_folder_navs()
        self.view = ("folder", path)
        self._paint_navs()
        self._apply_view()
        self._scan_all()

    def _remove_folder(self, path):
        if not messagebox.askyesno(
                APP_NAME, "从曲库移除这个文件夹？\n%s\n\n不会删除磁盘上的文件。"
                % path, parent=self.win):
            return
        self.folders = [p for p in self.folders
                        if _normcase(p) != _normcase(path)]
        self.mcfg["folders"] = self.folders
        save_cfg(self.app.cfg)
        if self.view[0] == "folder" and _normcase(self.view[1]) == _normcase(path):
            self.view = ("all", "")
        self._rebuild_folder_navs()
        self._apply_view()
        self._scan_all()

    def _folder_menu(self, event, path):
        m = tk.Menu(self.win, tearoff=0, bg=M_SURF, fg=M_TEXT,
                    activebackground=M_ACC, activeforeground="#ffffff",
                    bd=0, relief="flat", font=(core.FONT, 9))
        m.add_command(label="重新扫描", command=self._scan_all)
        m.add_command(label="打开这个文件夹", command=lambda: self._open_path(path))
        m.add_separator()
        m.add_command(label="移除这个文件夹", command=lambda: self._remove_folder(path))
        try:
            m.post(event.x_root, event.y_root)
        except tk.TclError:
            pass

    def _scan_all(self):
        if self._closed:
            return
        try:
            self._scan_stop.set()
        except Exception:
            pass
        stop = threading.Event()
        self._scan_stop = stop
        self._scan_gen += 1
        gen = self._scan_gen
        self._scanning = True
        self._update_status()
        folders = [p for p in self.folders if os.path.isdir(p)]
        if not folders:
            self._scanning = False
            self.local_uids = []
            for uid in [u for u, t in self.tracks.items() if t.get("kind") == "local"]:
                self.tracks.pop(uid, None)
            self._refresh_rows()
            self._update_nav_counts()
            return
        threading.Thread(target=_scan_worker,
                         args=(folders, gen, stop, self._scan_results, MAX_TRACKS),
                         daemon=True).start()

    def _apply_scan(self, gen, entries, truncated):
        if gen != self._scan_gen:
            return
        self._scanning = False
        self._truncated = bool(truncated)
        seen = set()
        uids = []
        for item in entries:
            path = item["path"]
            track = self.tracks.get(_local_uid(path))
            if track is None:
                track = make_local(path, item["mtime"], item["size"])
            else:
                track["mtime"] = item["mtime"]
                track["size"] = item["size"]
                track["folder"] = item["folder"]
            key = track["uid"]
            if key in seen:
                continue
            seen.add(key)
            self.tracks[key] = track
            uids.append(key)
        self.local_uids = uids
        for uid in [u for u, t in self.tracks.items()
                    if t.get("kind") == "local" and u not in seen]:
            self.tracks.pop(uid, None)
        self._queue_meta_for_new_paths()
        if self.current and self.current.get("kind") == "local":
            found = self.tracks.get(self.current["uid"])
            if found is not None:
                self.current = found
                if not self.duration:
                    self.duration = float(found.get("dur") or 0)
        self._refresh_rows()
        self._paint_now()
        self._update_nav_counts()

    def _queue_meta_for_new_paths(self):
        if not self._ffprobe:
            return
        for uid in self.local_uids:
            track = self.tracks.get(uid)
            if not track:
                continue
            key = track["uid"]
            if float(track.get("dur") or 0) > 0 or (track.get("title") and
                                                    track.get("artist")):
                continue
            cached = self._meta_cache.get(_normcase(track["path"]))
            if cached:
                try:
                    fresh = (abs(float(cached.get("m", 0)) - track["mtime"]) < 1e-6
                             and int(cached.get("s", -1)) == int(track["size"]))
                except (TypeError, ValueError):
                    fresh = False
                if fresh:
                    track["title"] = cached.get("title") or track["title"]
                    track["artist"] = cached.get("artist") or track["artist"]
                    track["album"] = cached.get("album") or ""
                    try:
                        track["dur"] = float(cached.get("dur") or 0)
                    except (TypeError, ValueError):
                        pass
                    continue
            if key not in self._meta_queued:
                self._meta_queued.add(key)
                self._meta_queue.put(track["path"])

    def _start_workers(self):
        self._start_meta_worker()
        if self.api.node_path():
            self._api_load_active()

    def _start_meta_worker(self):
        if not self._ffprobe:
            return
        threading.Thread(target=_meta_worker,
                         args=(self._meta_queue, self._meta_results,
                               self._meta_stop, self._ffprobe),
                         daemon=True).start()

    # ---- 播放 ----
    def _set_context(self, rows):
        uids = [r["uid"] for r in rows if isinstance(r, dict) and r.get("uid")]
        if uids:
            self.ctx_uids = uids
            self._update_nav_counts()

    def _play_track(self, uid, restart=True, pos_ms=None):
        track = self.tracks.get(uid)
        if track is None or self._closed:
            return
        self._prep_gen += 1
        gen = self._prep_gen
        self.prep.cancel(gen)
        self.streamer.cancel_meta(("play", self.current_uid))
        # 切歌作废旧的直链请求：不取消的话，旧回执迟到会清掉新请求的 _pending_key，
        # 还会在 node 里白跑一趟占住解析通道（对齐 LX 的 gettingUrlId 竞态保护）
        stale = getattr(self, "_pending_key", None)
        if stale:
            self.api.cancel(stale)
            self._req_map.pop(stale, None)
            self._pending_key = None
        self._url_tries = 0
        self._url_deadline = 0.0
        old = self.current_uid
        self.current = track
        self.current_uid = uid
        if restart:
            self.position = 0.0
            self._last_pos = 0
        elif pos_ms is not None:
            self.position = max(0.0, pos_ms / 1000.0)
        self.duration = float(track.get("dur") or 0)
        self.playing = True
        self._preparing = False
        self._paint_now()
        self._paint_progress()
        self._paint_play()
        self._refresh_marker(old, uid)
        self._push_recent(uid)
        self._load_lyrics(track)
        self._paint_cover(track)
        if track.get("kind") == "local":
            self._play_local(track)
        else:
            self._play_online(track)

    def _play_local(self, track):
        path = track.get("path") or ""
        if not os.path.isfile(path):
            self.playing = False
            self._paint_play()
            self._hint("文件不存在或已被移动", 4000)
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in NATIVE_EXTS:
            self._prep_owner = track["uid"]
            if not self._open_and_play(path, track["uid"], int(self.position * 1000)):
                return
        else:
            if not self.prep.ffmpeg:
                self.playing = False
                self._paint_play()
                self._hint("这个格式需要 ffmpeg 转码，当前没有找到 ffmpeg。", 6000)
                self._ask_open_download(
                    "ffmpeg", "缺少 ffmpeg",
                    "FLAC / M4A / OGG 这类格式需要 ffmpeg 转码后才能播放。\n\n"
                    "下载后把 ffmpeg.exe 放进 VBHelper 程序目录（或加入 PATH），"
                    "重开播放器即可。")
                return
            self.player.stop()
            self._preparing = True
            self._prep_owner = track["uid"]
            self._url_deadline = time.time() + 120.0
            self._hint("正在准备音频…", 15000)
            self.prep.submit(path, self._prep_gen)

    def _play_online(self, track):
        item = musicapi.active_item(self.sources)
        if item is None:
            self.playing = False
            self._preparing = False
            self._paint_play()
            self._hint("还没导入音源：去「音源管理」导入 .js 音源脚本后就能在线听", 6000)
            self._select_view(("source", ""))
            return
        if not self.api.inited:
            if not self.api.alive and not self.api.loading:
                self._api_load_active()
            self._pending_play_uid = track["uid"]
            self._hint("音源初始化中，稍等…", 10000)
            return
        source = track.get("source") or ""
        quality = self._effective_quality(track)
        if not quality:
            self.playing = False
            self._paint_play()
            hint = "当前音源解析不了这首"
            if not self.api.supports(source, "musicUrl"):
                hint += "：%s 不在音源能力里" % SOURCE_LABEL.get(source, source)
                if self.api.sources:
                    hint += "（音源支持：%s）" % "、".join(
                        SOURCE_LABEL.get(s, s) for s in self.api.sources)
            self._hint(hint, 6000)
            return
        track["quality"] = quality
        key = "u|%s|%s" % (track["uid"], quality)
        self._req_map[key] = (track["uid"], "url", quality, None)
        self._pending_key = key
        self._preparing = True
        # 总超时预算：解析 + 缓冲 60s，超时走 _on_url_timeout 重试/跳曲（对齐 LX 的 100s 兜底思路）
        self._url_deadline = time.time() + 60.0
        self._paint_play()
        self._hint("正在解析直链（%s）…" % quality, 15000)
        if not self.api.request("musicUrl", source,
                                {"type": quality,
                                 "musicInfo": api_music_info(track)}, key):
            self._req_map.pop(key, None)
            # 不再要求用户「再点一次」：保持 preparing，挂起续播意图，音源就绪后自动续播
            self._pending_play_uid = track["uid"]
            self._hint("音源进程未就绪，正在重启音源…", 8000)
            self._api_load_active()

    def _on_url_timeout(self):
        """解析+缓冲超时兜底：重试一次，再失败 5 秒后自动切下一首（对齐 LX autoSkipOnError）。"""
        key = self._pending_key
        uid = self.current_uid
        if key:
            self.api.cancel(key)
            self._req_map.pop(key, None)
            self._pending_key = None
        self.streamer.cancel_meta(("play", uid))
        if self._url_tries < 1:
            self._url_tries += 1
            self._hint("解析超时，正在自动重试…", 10000)
            track = self.tracks.get(uid)
            if track is not None:
                self._play_online(track)
            return
        self._preparing = False
        self.playing = False
        self._url_deadline = 0.0
        self._paint_play()
        self._hint("这首一直解析不出来，5 秒后自动切下一首", 5000)
        self.win.after(5000, lambda: self._auto_next(uid))

    def _url_retry(self, err):
        """直链解析失败：延迟 2-6 秒自动重试一次，再失败 5 秒后自动切下一首（对齐 LX）。"""
        uid = self.current_uid
        if self._url_tries < 1:
            self._url_tries += 1
            delay = random.randint(2, 6) * 1000
            self._hint("%s，%d 秒后自动重试…" % (err[:60], delay // 1000), delay)
            self.win.after(delay, lambda: self._url_retry_go(uid))
            return
        self._url_deadline = 0.0
        self._hint("%s，5 秒后自动切下一首" % err[:60], 5000)
        self.win.after(5000, lambda: self._auto_next(uid))

    def _url_retry_go(self, uid):
        if self._closed or uid != self.current_uid or not self._preparing:
            return
        track = self.tracks.get(uid)
        if track is not None:
            self._play_online(track)

    def _auto_next(self, from_uid):
        """失败自动跳曲：仅当还停在同一首（用户没手动切走/换歌）才跳。"""
        if self._closed or self.current_uid != from_uid or self.playing:
            return
        self._next(auto=True)

    def _api_broken(self):
        """node 进程意外退出：清掉挂在它身上的请求并自动重启续播。

        不清的后果：_preparing 永远 True、按钮永远「…」、_req_map 残留把
        _online_busy 喂成恒真导致 node 永不被空闲回收——就是「没报错也不播」的元凶。
        """
        if self.api.alive or self.api.loading:
            return          # 主动重启导致的正常退出，不做异常处理
        key = self._pending_key
        if not key:
            return
        self._req_map.pop(key, None)
        self._pending_key = None
        uid = self.current_uid
        track = self.tracks.get(uid)
        if self._preparing and track is not None and track.get("kind") == "online":
            self._hint("音源进程已退出，正在自动重启…", 8000)
            self._pending_play_uid = uid
            self._api_load_active()

    def _buffer_url(self, uid, url, quality, purpose="play", jid=None):
        track = self.tracks.get(uid)
        if track is None:
            return
        if purpose == "dl":
            safe = re.sub(r'[\\/:*?"<>|]', " ",
                          "%s - %s" % (track.get("artist"), track.get("title")))
            target = os.path.join(self.down_dir,
                                  safe.strip()[:120] + ONLINE_EXTS.get(quality, ".mp3"))
            meta = ("dl", jid)
        else:
            target = _online_cache_name(track, quality)
            meta = ("play", uid)
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            if purpose == "dl":
                self._down_finish(jid, target)
            else:
                self._play_ready_file(uid, target)
            return
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except OSError as e:
            self._hint("无法创建缓存目录：%s" % e, 5000)
            return
        if purpose == "play":
            # 缓冲期独立预算 90s（flac 大文件慢网），超时同样走 _on_url_timeout
            self._url_deadline = time.time() + 90.0
            # mp3 系走流式：本地 hub 边下边喂 MCI，几秒内出声；flac 系仍整轨+转码
            ext = ONLINE_EXTS.get(quality, ".mp3")
            self.streamer.submit(url, target, meta, stream=(ext == ".mp3"))
        else:
            self.streamer.submit(url, target, meta)

    def _play_ready_file(self, uid, path):
        track = self.tracks.get(uid)
        if track is None or uid != self.current_uid:
            return
        if uid == self._opened_for and self.player.opened and self.playing:
            return      # 流式播放中收到同一首的 done 回执：只表示缓存写完，不重新 open
        if not path.startswith("http") and not _sniff_audio(path):
            # 接口挂了常返回 JSON/HTML 错误页（HTTP 还是 200），存成缓存后
            # MCI open 成功但立即停止：不能按「格式不支持」报错
            self._url_deadline = 0.0
            self._preparing = False
            self.playing = False
            self._paint_play()
            try:
                os.remove(path)
            except OSError:
                pass
            self._hint("这个直链返回的不是音频（接口可能已失效），"
                       "换个音源或音质试试", 10000)
            return
        ext = os.path.splitext(path)[1].lower()
        self._preparing = False
        if ext in NATIVE_EXTS:
            self._prep_owner = uid
            self._open_and_play(path, uid, int(self.position * 1000))
            return
        if not self.prep.ffmpeg:
            self.playing = False
            self._paint_play()
            self._hint("需要 ffmpeg 才能解码这个格式", 5000)
            self._ask_open_download(
                "ffmpeg", "缺少 ffmpeg",
                "在线无损（FLAC 等）需要 ffmpeg 转码后才能播放。\n\n"
                "下载后把 ffmpeg.exe 放进 VBHelper 程序目录（或加入 PATH），"
                "重开播放器即可。也可以换 128k/320k 音质，mp3 不需要 ffmpeg。")
            return
        self.player.stop()
        self._preparing = True
        self._prep_owner = uid
        self._url_deadline = time.time() + 120.0
        self._hint("正在解码 %s…" % ext, 15000)
        self.prep.submit(path, self._prep_gen)

    def _open_and_play(self, playable, owner_uid, pos_ms=0):
        if self._closed or owner_uid != self.current_uid:
            return False
        ok, err = self.player.open(playable)
        if not ok:
            track = self.tracks.get(owner_uid)
            src = (track or {}).get("path") if (track or {}).get("kind") == "local" else ""
            if src and os.path.splitext(src)[1].lower() in NATIVE_EXTS and \
                    self.prep.ffmpeg and not self._preparing:
                self._preparing = True
                self._hint("正在转码后重试…", 15000)
                self.prep.cancel(self._prep_gen)
                self._prep_owner = owner_uid
                self.prep.submit(src, self._prep_gen, force=True)
                return True
            self.playing = False
            self._preparing = False
            self._paint_play()
            self._hint("播放失败：%s" % (err or "格式不支持"), 6000)
            return False
        self._preparing = False
        self._opened_for = owner_uid
        self.player.set_volume(self.volume)
        if not self.player.volume_ok:
            self._hint("这个通道的音量由系统控制（WAV 不支持应用内音量）", 4000)
        length = self.player.length() / 1000.0
        track = self.tracks.get(owner_uid)
        if length > 0 and (self.duration <= 0 or
                           abs(self.duration - length) > 2.0):
            self.duration = length
            if track is not None:
                track["dur"] = length
        if pos_ms:
            limit = max(0, int(self.duration * 1000) - 500) if self.duration else pos_ms
            pos_ms = max(0, min(pos_ms, limit))
            self.player.seek(pos_ms)
            self.position = pos_ms / 1000.0
        if not self.player.play():
            self.playing = False
            self._paint_play()
            self._hint("播放失败：%s" % (self.player.last_error or "设备忙"), 6000)
            return False
        self.playing = True
        self._play_started_at = time.time()
        self._url_deadline = 0.0
        self._paint_play()
        self._paint_progress()
        self._clear_hint()
        try:
            self.prep._cleanup()    # 播放就绪也清一次缓存（以前只有转码后才清）
        except Exception:
            pass
        return True

    def _toggle_play(self):
        if self._closed:
            return
        if self._preparing:
            self._prep_gen += 1
            self.prep.cancel(self._prep_gen)
            self.streamer.cancel_meta(("play", self.current_uid))
            key = getattr(self, "_pending_key", None)
            if key:
                self.api.cancel(key)
                self._req_map.pop(key, None)
                self._pending_key = None
            self._url_deadline = 0.0
            self._preparing = False
            self.playing = False
            self._paint_play()
            self._hint("已取消", 2000)
            return
        if not self.current:
            rows = [r for r in self.rows if r.get("uid")]
            if rows:
                self._set_context(rows)
                self._play_track(rows[0]["uid"], restart=True)
            elif self.local_uids:
                self._play_track(self.local_uids[0], restart=True)
            else:
                self._hint("先导入文件夹或在线搜索", 3000)
            return
        if self.playing:
            self.player.pause()
            self.playing = False
            self._paint_play()
            return
        if self.player.opened and self._opened_for == self.current_uid:
            if self.duration > 0 and self.position >= self.duration - 0.4:
                self.player.seek(0)
                self.position = 0.0
            if self.player.mode() == "paused":
                self.player.resume()
            else:
                self.player.play()
            self.playing = True
            self._play_started_at = time.time()
            self._paint_play()
            return
        uid = self.current_uid
        if self.tracks.get(uid, {}).get("kind") == "online":
            self._play_online(self.tracks[uid])
        else:
            self._play_track(uid, restart=False, pos_ms=int(self.position * 1000))

    def _current_index(self):
        for i, uid in enumerate(self.ctx_uids):
            if uid == self.current_uid:
                return i
        return -1

    def _next(self, auto=False):
        if self._closed:
            return
        if self._preparing:
            self._prep_gen += 1
            self.prep.cancel(self._prep_gen)
            self.streamer.cancel_meta(("play", self.current_uid))
            self._preparing = False
        uids = self.ctx_uids or self.local_uids
        if not uids:
            return
        idx = self._current_index()
        if self.mode == 2 and len(uids) > 1:
            pick = idx
            while pick == idx:
                pick = random.randrange(len(uids))
        else:
            pick = 0 if idx < 0 else (idx + 1) % len(uids)
        self._play_track(uids[pick], restart=True)

    def _prev(self):
        if self._preparing:
            self._prep_gen += 1
            self.prep.cancel(self._prep_gen)
            self.streamer.cancel_meta(("play", self.current_uid))
            self._preparing = False
        if self.position > 3.0:
            self._seek_to(0.0)
            return
        uids = self.ctx_uids or self.local_uids
        if not uids:
            return
        idx = self._current_index()
        self._play_track(uids[(idx - 1) % len(uids)], restart=True)

    def _play_later(self, track):
        uid = track["uid"]
        if uid in self.ctx_uids:
            self.ctx_uids.remove(uid)
        idx = self._current_index()
        pos = (idx + 1) if idx >= 0 else len(self.ctx_uids)
        self.ctx_uids.insert(min(pos, len(self.ctx_uids)), uid)
        self._update_nav_counts()
        self._hint("已加入下一首播放", 2200)
        if self.view[0] == "queue":
            self._refresh_rows()

    def _add_current_later(self):
        if self.current:
            self._play_later(self.current)

    def _play_rows_all(self):
        rows = [r for r in self.rows if r.get("uid")]
        if not rows:
            if self.view[0] in ("all", "folder"):
                self._hint("先添加音乐文件夹", 3000)
            else:
                self._hint("当前列表没有歌曲", 3000)
            return
        self._set_context(rows)
        self._play_track(rows[0]["uid"], restart=True)

    # ---- 进度与音量 ----
    def _seek_change(self, frac):
        self.position = frac * self.duration
        try:
            self.cur_lbl.configure(text=_fmt_sec(self.position))
        except tk.TclError:
            pass
        self._sync_lyrics()

    def _seek_commit(self, frac):
        self._seek_to(frac * self.duration)

    def _seek_to(self, sec):
        self.position = max(0.0, min(sec, self.duration or sec))
        if self.player.opened and self._opened_for == self.current_uid:
            self.player.seek(int(self.position * 1000))
            if self.playing:
                self.player.play()
        self._paint_progress()
        self._sync_lyrics()

    def _on_end(self):
        if self.mode == 1:
            self.position = 0.0
            if self.player.opened:
                self.player.seek(0)
                self.player.play()
                self._play_started_at = time.time()
                self.playing = True
                self._paint_play()
                self._paint_progress()
        else:
            self._next(auto=True)

    def _tick(self):
        if self._closed:
            return
        try:
            if self.playing and self._preparing:
                # 解析/缓冲超时兜底（_preparing 期间 _tick 的 MCI 分支本来就短路）
                if self._url_deadline and time.time() > self._url_deadline:
                    self._url_deadline = 0.0
                    self._on_url_timeout()
            elif self.playing and not self._preparing and self.player.opened \
                    and self._opened_for == self.current_uid:
                mode = self.player.mode()
                if mode == "stopped":
                    if self.streamer.is_downloading(self.current_uid) \
                            and self.position > 0.5 \
                            and (not self.duration or
                                 self.position < self.duration - 2.0):
                        # 缓冲落后于播放位置：原地等待，不得判定为播放结束或中断
                        self.player.seek(int(self.position * 1000))
                        self.player.play()
                    elif time.time() - self._play_started_at < 1.0 and self.position < 0.4:
                        self.playing = False
                        self._paint_play()
                        self._hint("播放中断：音频格式可能不受支持", 5000)
                    else:
                        self._on_end()
                elif mode == "playing":
                    if not self.seek_slider.dragging:
                        pos = self.player.position()
                        if pos >= 0:
                            self.position = pos / 1000.0
                        self._paint_progress()
                        self._sync_lyrics()
        except tk.TclError:
            return
        except Exception:
            pass
        if not self._closed:
            try:
                self._tick_job = self.win.after(250, self._tick)
            except tk.TclError:
                pass

    def _slow_tick(self):
        if self._closed:
            return
        try:
            self.api.poll(busy=self._online_busy())
        except Exception:
            pass
        try:
            if self._down_running():
                self._update_nav_counts()
                if self.view[0] == "down":
                    self._render_down_cards()
        except Exception:
            pass
        if not self._closed:
            try:
                self._slow_job = self.win.after(2000, self._slow_tick)
            except tk.TclError:
                pass

    def _effective_quality(self, track):
        """在「这首歌有的档位」和「音源能解析的档位」里挑一个能两头满足的。

        榜单这类接口不给音质信息（qualitys 为空），此时直接按音源能力来。
        """
        source = track.get("source") or ""
        api_q = self.api.qualitys(source)
        own = track.get("qualitys") or []
        if not self.api.supports(source, "musicUrl"):
            return ""
        pool = [q for q in own if q in api_q] if own else list(api_q)
        if not pool:
            pool = [self.quality] if self.quality in api_q else list(api_q)
        return pick_quality({"qualitys": pool}, self.quality)

    def _online_busy(self):
        return bool(self._req_map) or (self.playing and
                                       (self.current or {}).get("kind") == "online")

    # ---- 后台结果通道 ----
    def _pump(self):
        if self._closed:
            return
        self._pump_scan()
        self._pump_prep()
        self._pump_meta()
        self._pump_net()
        self._pump_api()
        if self.covers.busy():
            self._paint_cover(self.current)
        if not self._closed:
            try:
                self._pump_job = self.win.after(80, self._pump)
            except tk.TclError:
                pass

    def _pump_scan(self):
        while True:
            try:
                kind, gen, entries, truncated = self._scan_results.get_nowait()
            except queue.Empty:
                break
            if kind == "scan" and not self._closed:
                try:
                    self._apply_scan(gen, entries, truncated)
                except tk.TclError:
                    return

    def _pump_prep(self):
        while True:
            try:
                gen, playable, ok, msg = self._prep_results.get_nowait()
            except queue.Empty:
                break
            if gen != self._prep_gen or self._closed:
                continue
            owner = getattr(self, "_prep_owner", self.current_uid)
            if not ok:
                self._preparing = False
                self.playing = False
                self.player.close()
                self._paint_play()
                self._hint(("播放失败：%s" % (msg or "转码失败"))[:120], 8000)
                continue
            if not owner or owner != self.current_uid:
                continue
            self._preparing = False
            self._open_and_play(playable, owner, int(self.position * 1000))

    def _pump_meta(self):
        changed_current = False
        while True:
            try:
                path, meta = self._meta_results.get_nowait()
            except queue.Empty:
                break
            uid = _local_uid(path)
            self._meta_queued.discard(uid)
            song = self.tracks.get(uid)
            if song is None:
                continue
            if meta.get("title"):
                song["title"] = meta["title"]
            if meta.get("artist"):
                song["artist"] = meta["artist"]
            if meta.get("album"):
                song["album"] = meta["album"]
            if meta.get("dur"):
                song["dur"] = float(meta["dur"])
            self._meta_cache[_normcase(path)] = {
                "m": song.get("mtime"), "s": song.get("size"),
                "title": song.get("title"), "artist": song.get("artist"),
                "album": song.get("album"), "dur": song.get("dur"),
            }
            self._meta_dirty = True
            if uid == self.current_uid:
                if not self.duration and song.get("dur"):
                    self.duration = float(song["dur"])
                changed_current = True
        if changed_current:
            self._paint_now()
            self._paint_progress()
            self._refresh_rows()
        if self._meta_dirty and self._meta_flush_job is None:
            try:
                self._meta_flush_job = self.win.after(3000, self._flush_meta)
            except tk.TclError:
                pass

    def _flush_meta(self):
        self._meta_flush_job = None
        if not self._meta_dirty:
            return
        _save_meta_cache(self._meta_cache)
        self._meta_dirty = False

    def _pump_net(self):
        got = 0
        while True:
            try:
                msg = self._net_results.get_nowait()
            except queue.Empty:
                break
            got += 1
            kind = msg[0]
            if kind == "lyric":
                _, uid, text = msg
                self._on_lyric_loaded(uid, text)
            elif kind == "pic":
                _, uid, url = msg
                self._on_pic_loaded(uid, url)
            elif kind in ("ok", "fail"):
                _, token, payload, err = msg
                self._on_net_reply(token, payload, err)
            elif kind in ("progress", "done", "error", "cancel", "early"):
                self._on_stream_msg(msg)
            if got > 40:
                break
        if got and not self._down_running() and not self._net_busy:
            self._update_status()

    def _net_call(self, label, fn, on_ok):
        """把一次在线请求丢到后台线程，结果回主线程。"""
        if self._closed:
            return
        self._net_gen += 1
        token = self._net_gen
        self._net_cbs[token] = (label, on_ok)
        self._net_busy = True
        self._update_status()

        def run(tok=token, lab=label, f=fn):
            try:
                out = f()
                self._net_results.put(("ok", tok, out, ""))
            except musicnet.NetError as e:
                self._net_results.put(("fail", tok, None, str(e)))
            except Exception as e:
                self._net_results.put(("fail", tok, None,
                                       "%s: %s" % (e.__class__.__name__, e)))
        threading.Thread(target=run, daemon=True).start()

    def _on_net_reply(self, token, payload, err):
        entry = self._net_cbs.pop(token, None)
        self._net_busy = bool(self._net_cbs)
        if entry is None:
            return
        label, cb = entry
        if err:
            self._hint("%s失败：%s" % (label, err[:100]), 6000)
            self._update_status()
            return
        try:
            cb(payload)
        except Exception as e:
            self._hint("%s处理异常：%s" % (label, e), 6000)
        self._update_status()

    def _on_stream_msg(self, msg):
        kind, meta = msg[0], msg[1]
        if meta[0] == "play":
            uid = meta[1]
            if kind == "early" and uid == self.current_uid:
                # 首块就绪即开播，不等整轨完成；与 lx-music-desktop 行为一致
                self._play_ready_file(uid, msg[2])
            elif kind == "done" and uid == self.current_uid:
                self._play_ready_file(uid, msg[2])
            elif kind == "progress":
                # 缓冲实时反馈：Streamer 每 0.2s 发一条，这里 0.5s 节流刷 hint
                if uid == self.current_uid and self._preparing:
                    got, total = msg[2], msg[3]
                    now = time.time()
                    if now - getattr(self, "_buf_hint_at", 0.0) >= 0.5:
                        self._buf_hint_at = now
                        if total > 0:
                            self._hint("缓冲中 %d%%（%.1f/%.1fMB）…" % (
                                got * 100 // total,
                                got / 1048576.0, total / 1048576.0), 8000)
                        else:
                            self._hint("已缓冲 %.1fMB…" % (got / 1048576.0), 8000)
            elif kind == "error":
                if uid == self.current_uid:
                    self._url_deadline = 0.0
                    self._preparing = False
                    self.playing = False
                    self._paint_play()
                    self._hint("缓冲失败：%s" % (msg[2] or "")[:100], 6000)
            elif kind == "cancel":
                pass
            return
        jid = meta[1]
        job = self._find_job(jid)
        if job is None:
            return
        if kind == "progress":
            job["got"], job["total"] = msg[2], msg[3]
            job["state"] = "run"
            job["detail"] = ""
            # progress 每 0.2s/任务一条：只更新数据，卡片整页重建交给 2s 的
            # _slow_tick 兜底，否则下载中每秒销毁重建 40 张卡片
            return
        elif kind == "done":
            self._down_finish(jid, msg[2])
            return
        elif kind == "error":
            job["state"] = "err"
            job["detail"] = msg[2] or "下载失败"
        elif kind == "cancel":
            self._down_jobs.remove(job)
        if self.view[0] == "down":
            self._render_down_cards()
        self._update_nav_counts()

    # ---- 显示刷新 ----
    def _paint_all(self):
        self._paint_now()
        self._paint_progress()
        self._paint_play()
        self._paint_volume()
        self._paint_mode()
        self._draw_lyrics()

    def _paint_now(self):
        track = self.current
        if track:
            title = track.get("title") or os.path.basename(track.get("path", ""))
            artist = track.get("artist") or "未知歌手"
            album = track.get("album") or ""
            line = artist if not album else "%s · %s" % (artist, album)
            self.title_lbl.configure(text=self._ellipsize(title, self._f_row, 182))
            self.artist_lbl.configure(text=self._ellipsize(line, self._f_row2, 182))
            fav = track["uid"] in self.fav_keys
            self._paint_fav_btn(fav)
            badge = []
            if track.get("kind") == "online":
                badge.append(SOURCE_LABEL.get(track.get("source"),
                                              track.get("source") or ""))
                if track.get("quality"):
                    badge.append(QUALITY_LABEL.get(track["quality"],
                                                   track["quality"]))
            else:
                badge.append((os.path.splitext(track.get("path", ""))[1] or ".")[1:]
                             .upper())
            self.badge.configure(text=" ".join(b for b in badge if b))
        else:
            self.title_lbl.configure(text="未在播放")
            self.artist_lbl.configure(text="导入文件夹或在线搜索后开始")
            self._paint_fav_btn(False)
            self.badge.configure(text="")
        self.mini_title.configure(text=self._ellipsize(
            (track.get("title") or "") if track else "未在播放", self._f_row, 200))
        if track and self.view[0] != "lyric":
            self.mini_artist.configure(text=self._ellipsize(
                track.get("artist") or "", self._f_row2, 200))
        self._paint_cover(track)
        self._update_window_title()

    def _paint_fav_btn(self, fav=None, hover_color=None):
        """收藏按钮：Feather 心形图标，收藏状态只用颜色区分。"""
        if fav is None:
            fav = self.current_uid in self.fav_keys
        color = hover_color or ("#d99a9a" if fav else M_MUTED)
        try:
            ph = _icon_photo("heart", 18, color)
            self.fav_btn.configure(image=ph)
            self.fav_btn._icon_photo = ph
            self._fav_on = fav
        except tk.TclError:
            pass

    def _paint_cover(self, track):
        url = (track or {}).get("pic") or ""
        size = 42
        photo = self.covers.ready(url, size) if url else None
        box_bg = _cover_tint((track or {}).get("title") or "") if track else M_SURF3
        if photo is None:
            self.cover_img.configure(image="", bg=box_bg)
            self.cover_img.place_forget()
            ph = _icon_photo("music", 20, M_ACC_H)
            self._cover_ph = ph
            self.cover_lbl.configure(bg=box_bg, image=ph, text="")
            self.cover_lbl.place(relx=0.5, rely=0.5, anchor="center")
            if track:
                # pic 为空也要解析：musicnet.pic 支持按源请求封面 URL
                # （此前 if url 守卫让无 pic 字段的歌永远停在文字占位）
                self._net_get_pic(track)
            return
        self.cover_lbl.configure(image="", text="")
        self.cover_img.configure(image=photo, bg=box_bg)
        self.cover_img.place(relx=0.5, rely=0.5, anchor="center")

    def _net_get_pic(self, track):
        uid = track["uid"]
        if uid in self._pic_requested:
            return
        self._pic_requested.add(uid)
        if len(self._pic_requested) > 400:
            self._pic_requested.clear()
        if track.get("kind") != "online":
            return

        def work(t=dict(track)):
            return musicnet.pic(t.get("source"), t)
        self._net_call("封面", work,
                       lambda url: self._net_results.put(("pic", uid, url or "")))

    def _on_pic_loaded(self, uid, url):
        track = self.tracks.get(uid)
        if track is None or not url:
            return
        track["pic"] = url
        if uid == self.current_uid:
            self._paint_cover(track)

    def _update_window_title(self):
        try:
            track = self.current
            if track and self.playing:
                self.win.title("%s - %s | 音乐播放器" % (
                    track.get("title"), track.get("artist") or ""))
            else:
                self.win.title("音乐播放器 - " + APP_NAME)
        except tk.TclError:
            pass

    def _paint_progress(self):
        dur = max(0.0, self.duration)
        frac = (self.position / dur) if dur > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        try:
            self.seek_slider.set(frac)
            self.cur_lbl.configure(text=_fmt_sec(self.position))
            self.dur_lbl.configure(text=_fmt_sec(dur))
            w = self.mini_prog.winfo_width() or 380
            self.mini_prog.coords(self._mini_fill, 0, 0, int(w * frac), 2)
        except tk.TclError:
            pass

    def _paint_play(self):
        try:
            if self._preparing:
                for btn in (self.play_btn, self.mini_play):
                    btn.set_icon(None)
                    btn.set_text("…")
            else:
                icon = "pause" if self.playing else "play"
                self.play_btn.set_icon(icon)
                self.mini_play.set_icon(icon)
        except tk.TclError:
            pass

    def _paint_volume(self):
        try:
            self.vol_slider.set(self.volume / 100.0)
            self.vol_lbl.configure(text="%d%%" % self.volume)
        except tk.TclError:
            pass

    def _paint_mode(self):
        try:
            self.mode_btn.configure(text=MODE_LABELS[self.mode],
                                    fg=M_ACC_H if self.mode else M_MUTED)
        except tk.TclError:
            pass

    def _refresh_marker(self, old_uid, new_uid):
        if self._pane_kind() != "list":
            return
        sel = -1
        for i, row in enumerate(self.rows):
            if row.get("uid") == self.current_uid:
                sel = i
                break
        try:
            self.list.sel = sel
            self.list.redraw()
        except tk.TclError:
            pass

    # ---- 歌词 ----
    def _load_lyrics(self, track):
        uid = track["uid"]
        if uid == self._lyric_for:
            self._draw_lyrics()
            return
        self._lyric_for = uid
        self._lyrics = []
        self._lyric_times = []
        self._lyric_idx = -1
        self._lyric_reset_scroll()
        cached = self._lyric_cache.get(uid)
        if cached is not None:
            # 缓存存的是 lrc 原文，必须先 parse——直接塞字符串会把每个
            # 字符当一行，row[1] 当场 IndexError
            self._apply_lyric_text(parse_lrc(cached) if isinstance(cached, str)
                                   else cached)
            self._draw_lyrics()
            return
        if track.get("kind") == "local":
            self._apply_lyric_text(load_lyrics(track.get("path")))
            self._draw_lyrics()
            return
        self._draw_lyrics()
        t = dict(track)

        def work():
            return musicnet.lyric(t.get("source"), t)
        self._net_call("歌词", work,
                       lambda text: self._net_results.put(("lyric", uid, text or "")))

    def _on_lyric_loaded(self, uid, text):
        if not text:
            return
        self._lyric_cache[uid] = text
        while len(self._lyric_cache) > 8:
            self._lyric_cache.popitem(last=False)
        if uid == self.current_uid:
            self._apply_lyric_text(parse_lrc(text))
            self._draw_lyrics()
            if self.view[0] == "lyric":
                self._paint_now()

    def _apply_lyric_text(self, rows):
        if isinstance(rows, str):           # 兜底：塞进来的是 lrc 原文就先解析
            rows = parse_lrc(rows)
        # 收敛成统一 (time, text, trans) 结构，脏数据（裸字符/缺字段）直接丢
        self._lyrics = [tuple(r) for r in (rows or [])
                        if isinstance(r, (tuple, list)) and len(r) >= 2]
        self._lyric_times = [r[0] for r in self._lyrics]
        self._lyric_idx = -1
        self._lyric_reset_scroll()

    def _lyric_reset_scroll(self):
        """换歌/换词：停掉动画，滚动归零。"""
        self._lyric_scroll = 0.0
        self._lyric_scroll_to = 0.0
        self._lyric_manual = 0.0
        self._lyric_drawn = None
        if self._lyric_anim_job is not None:
            try:
                self.lyric_canvas.after_cancel(self._lyric_anim_job)
            except (tk.TclError, AttributeError, RuntimeError):
                pass
            self._lyric_anim_job = None

    def _ellipsize(self, text, font, maxw):
        text = text or ""
        if font.measure(text) <= maxw:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + "…") <= maxw:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

    def _draw_lyrics(self):
        if self._closed:
            return
        c = self.lyric_canvas
        try:
            w = max(1, c.winfo_width())
            h = max(1, c.winfo_height())
        except tk.TclError:
            return
        # 脏检查：行号、滚动位置、尺寸都没变就不重画（歌词页常驻重绘是大头）
        state = (self._lyric_idx, round(self._lyric_scroll * 40), w, h)
        if state == self._lyric_drawn:
            return
        self._lyric_drawn = state
        try:
            c.delete("all")
        except tk.TclError:
            return
        track = self.current
        if not self._lyrics:
            hint = "暂无歌词"
            if track and track.get("kind") == "local":
                hint += "\n同目录放一个同名 .lrc 文件即可显示"
            elif track:
                hint += "（正在尝试从在线接口获取）"
            else:
                hint = "播放后这里显示歌词"
            try:
                c.create_text(w / 2, h / 2, text=hint, fill=M_DIM,
                              font=(core.FONT, 10), justify="center",
                              anchor="center", width=min(w - 24, 320))
                if track:
                    self._draw_big_cover(c, w, h, track)
            except tk.TclError:
                pass
            try:
                self.lyric_line.configure(text="")
            except tk.TclError:
                pass
            return
        # 底层：模糊压暗的封面铺满（LX 同款视觉），没有就保持纯色背景
        photo = self._lyric_blur_photo(track, w, h)
        if photo is not None:
            try:
                c.create_image(w / 2, h / 2, image=photo, anchor="center")
            except tk.TclError:
                pass
        self._draw_big_cover(c, w, h, track)
        # 连续滚动位置：当前行悬在 45% 高度锚点（对齐 LX 的视觉重心），逐行平滑插值
        active = max(0, self._lyric_idx)
        step = 30
        span = max(1, int((h * 0.42) / step) + 2)
        scroll = max(0.0, min(float(len(self._lyrics) - 1), self._lyric_scroll))
        base_y = h * 0.45
        start = max(0, int(scroll) - span)
        end = min(len(self._lyrics), int(scroll) + span + 2)
        for i in range(start, end):
            y = base_y + (i - scroll) * step
            if y < -step or y > h + step:
                continue
            font = self._font_lyric_on if i == active else self._font_lyric
            row = self._lyrics[i]
            text = row[1] or ""
            text = self._ellipsize(text, font, max(40, w - 40))
            fill = "#f2f5fb" if i == active else ("#8f98a8" if i < active else "#5f6879")
            try:
                c.create_text(w / 2, y, text=text, fill=fill, font=font,
                              anchor="center")
                # 翻译行：主行下方次级小字（双语 lrc 才有）
                if len(row) > 2 and row[2]:
                    tr = self._ellipsize(row[2], self._font_lyric_tr,
                                         max(40, w - 40))
                    c.create_text(w / 2, y + (19 if i == active else 15),
                                  text=tr,
                                  fill="#c3ccdc" if i == active else "#6a7383",
                                  font=self._font_lyric_tr, anchor="center")
            except tk.TclError:
                pass
        # 上下边缘渐隐（黑色 vignette，压在背景图上做暗角）
        try:
            fades = self._lyric_fade_imgs(h)
            if fades:
                c.create_image(0, 0, image=fades[0], anchor="nw")
                c.create_image(0, h, image=fades[1], anchor="sw")
        except (tk.TclError, AttributeError, IndexError):
            pass
        try:
            line = self._lyrics[active][1] or ""
            self.lyric_line.configure(text=self._ellipsize(line, self._f_row2, 260))
        except (tk.TclError, IndexError, AttributeError):
            pass

    def _lyric_blur_photo(self, track, w, h):
        """歌词页模糊背景 PhotoImage；图不够大时整数倍 zoom 铺满（缓存）。"""
        url = (track or {}).get("pic") or ""
        im = self.covers.blur_ready(url)
        if im is None:
            return None
        photo = self._blur_photos.get(url)
        if photo is not None:
            f = 1
            if photo.width() < w or photo.height() < h:
                f = max(2, -(-w // max(1, photo.width())),
                        -(-h // max(1, photo.height())))
                big = self._blur_photos.get("%s#z%d" % (url, f))
                if big is not None:
                    return big
                try:
                    from PIL import ImageTk
                    big = ImageTk.PhotoImage(im)
                    big = big.zoom(f, f)
                except Exception:
                    return photo
                self._blur_photos["%s#z%d" % (url, f)] = big
                while len(self._blur_photos) > 4:
                    self._blur_photos.popitem(last=False)
                return big
            return photo
        try:
            from PIL import ImageTk
            photo = ImageTk.PhotoImage(im)
        except Exception:
            return None
        self._blur_photos[url] = photo
        while len(self._blur_photos) > 4:
            self._blur_photos.popitem(last=False)
        return photo

    def _lyric_fade_imgs(self, h):
        """上下边缘渐隐条：黑色 0→55% 透明度竖向渐变（压在模糊背景上做暗角）。"""
        need_h = max(80, min(600, h))
        cached = self._lyric_fade_cache
        if cached and cached[2] == need_h:
            return cached[0], cached[1]
        try:
            from PIL import Image, ImageTk
        except Exception:
            return None
        fade_h = 56

        def strip(flipped):
            im = Image.new("RGBA", (4, fade_h))
            px = im.load()
            for y in range(fade_h):
                t = y / (fade_h - 1)
                a = int(140 * t)
                for x in range(4):
                    px[x, y] = (0, 0, 0, a)
            if flipped:
                im = im.transpose(Image.FLIP_TOP_BOTTOM)
            return ImageTk.PhotoImage(im)

        top, bottom = strip(False), strip(True)
        self._lyric_fade_cache = (top, bottom, need_h)
        return top, bottom

    def _lyric_anim(self):
        """滚动动画帧：指数逼近目标行，越近越慢（300ms 级观感）。"""
        self._lyric_anim_job = None
        if self._closed:
            return
        diff = self._lyric_scroll_to - self._lyric_scroll
        if abs(diff) < 0.01:
            self._lyric_scroll = self._lyric_scroll_to
            self._draw_lyrics()
            return
        self._lyric_scroll += diff * 0.26
        self._draw_lyrics()
        try:
            self._lyric_anim_job = self.lyric_canvas.after(16, self._lyric_anim)
        except tk.TclError:
            pass

    def _lyric_go(self, target):
        self._lyric_scroll_to = max(0.0, target)
        if self._lyric_anim_job is None:
            try:
                self._lyric_anim_job = self.lyric_canvas.after(16, self._lyric_anim)
            except tk.TclError:
                pass

    def _lyric_invalidate(self):
        self._lyric_drawn = None
        self._draw_lyrics()

    def _draw_big_cover(self, c, w, h, track):
        if not track:
            return
        # 尺寸同时受宽/高约束并保证不出画布：歌词页高度小时缩图并下沉，
        # 封面顶部不越界、底部不压歌词 45% 锚点区
        size = int(min(220, w - 32, max(110, h * 0.34), max(72, h * 0.32)))
        size = max(64, size)
        x = w / 2
        y = max(size / 2 + 8, min(h * 0.24, h - size / 2 - 8))
        photo = self.covers.ready((track or {}).get("pic") or "", size)
        if photo is not None:
            c.create_image(x, y, image=photo, anchor="center")
            return
        tint = _cover_tint((track or {}).get("title") or "")
        c.create_rectangle(x - size / 2, y - size / 2, x + size / 2, y + size / 2,
                           fill=tint, outline="")
        ph = _icon_photo("music", max(24, int(size / 2)), "#cdd7ea")
        c.create_image(x, y, image=ph)
        self._big_cover_photo = ph
        if track.get("kind") == "online":
            self._net_get_pic(track)    # pic 空的在线歌在这里触发封面解析

    def _sync_lyrics(self):
        if not self._lyrics:
            return
        ms = int(self.position * 1000)
        idx = bisect.bisect_right(self._lyric_times, ms) - 1
        idx = max(0, min(len(self._lyrics) - 1, idx))
        if idx != self._lyric_idx:
            self._lyric_idx = idx
            if time.time() >= self._lyric_manual:
                self._lyric_go(idx)
            return
        # 手动浏览期结束：平滑回到当前播放行
        if self._lyric_manual and time.time() >= self._lyric_manual \
                and not self._lyric_anim_job:
            self._lyric_manual = 0.0
            self._lyric_go(idx)

    def _lyric_press(self, e):
        self._lyric_press_y = e.y
        self._lyric_dragged = False

    def _lyric_motion(self, e):
        dy = e.y - self._lyric_press_y
        if not self._lyric_dragged and abs(dy) < 6:
            return
        self._lyric_dragged = True
        self._lyric_press_y = e.y
        self._lyric_manual = time.time() + 3.0
        limit = max(0, len(self._lyrics) - 1)
        target = max(0.0, min(float(limit), self._lyric_scroll_to - dy / 30.0))
        self._lyric_go(target)

    def _lyric_release(self, e):
        if self._lyrics and not self._lyric_dragged:
            self._on_lyric_click(e)
        self._lyric_dragged = False

    def _lyric_wheel(self, e):
        if not self._lyrics:
            return
        self._lyric_manual = time.time() + 3.0
        limit = max(0, len(self._lyrics) - 1)
        delta = -1 if e.delta > 0 else 1
        target = max(0.0, min(float(limit), self._lyric_scroll_to + delta * 2))
        self._lyric_go(target)

    def _on_lyric_click(self, e):
        if not self._lyrics:
            return
        c = self.lyric_canvas
        try:
            h = max(1, c.winfo_height())
        except tk.TclError:
            return
        step = 30
        scroll = max(0.0, min(float(len(self._lyrics) - 1), self._lyric_scroll))
        idx = int(round(scroll + (h * 0.45 - e.y) / step))
        if 0 <= idx < len(self._lyrics):
            self._seek_to(self._lyrics[idx][0] / 1000.0)

    # ---- 在线：搜索 / 榜单 / 歌单 ----
    def _do_search(self):
        text = self._search_text()
        if self.view[0] != "search":
            self._select_view(("search", ""))
        if not text:
            self._search_rows = []
            self._search_hint = ""
            self._search_page = 1
            self._search_all_page = 1
            self._refresh_rows()
            return
        self._search_page = 1
        self._run_search(append=False)

    def _run_search(self, append=False):
        text = self._search_text()
        source = self.search_source
        kind = self.search_kind
        page = self._search_page

        def work():
            return musicnet.search(source, text, page, kind)
        self._net_call("搜索", work,
                       lambda res: self._on_search_ok(res, text, append))
        self._hint("正在搜索 %s…" % text[:20], 8000)

    def _on_search_ok(self, res, text, append):
        if self.view[0] != "search" or self._search_text() != text:
            return
        self._search_hint = ""
        if self.search_kind == "songlist":
            rows = []
            for item in res["list"]:
                rows.append({"rk": "plist", "id": item["id"],
                             "source": item["source"], "lead": "list",
                             "name": item["name"],
                             "sub": "%s · %d 首 · %s" % (
                                 item.get("author") or SOURCE_LABEL.get(
                                     item["source"], item["source"]),
                                 item.get("count") or 0,
                                 _fmt_size_short(item.get("plays"))),
                             "state_text": SOURCE_LABEL.get(item["source"], "")})
            if append:
                self._plist_rows.extend(rows)
            else:
                self._plist_rows = rows
            del self._plist_rows[3000:]     # 翻页上限：对象列表无界增长没有意义
            self._search_rows = self._plist_rows
            self._search_all_page = res["allPage"]
            self._refresh_rows()
            self._hint("找到 %d 个歌单" % res["total"], 3000)
            return
        rows = []
        for info in res["list"]:
            rows.append(self._remember(info))
        self._trim_online_store()       # 批量后裁一次（原来每首裁一次 O(全库)）
        if append:
            self._search_rows.extend(rows)
        else:
            self._search_rows = rows
        del self._search_rows[3000:]    # 翻页上限
        self._search_all_page = res["allPage"]
        self._refresh_rows()
        self._hint("找到 %d 首（第 %d/%d 页）" % (
            res["total"], res["page"], res["allPage"]), 3000)

    def _remember(self, info):
        """把接口结果落成统一曲目并进总表（批量调用后请手动 _trim_online_store 一次）。"""
        track = make_online(info)
        track["kind"] = "online"
        self.tracks[track["uid"]] = track
        return track

    def _trim_online_store(self):
        keep = set(self.fav_uids) | set(self.recent_uids) | set(self.ctx_uids)
        for uid, track in list(self.tracks.items()):
            if track.get("kind") != "online":
                continue
            if uid in keep or uid == self.current_uid:
                continue
            if len(self.tracks) > MAX_TRACKS:
                self.tracks.pop(uid, None)

    def _open_board_list(self):
        self.view = ("board", "")
        self._list_title = ""
        self._paint_navs()
        self._show_pane()
        self._build_chips()
        self.chips.frame.pack(side="left")
        # 先给空列表立即返回，榜单数据走后台线程取（此前主线程同步请求会冻结整个界面）
        self._board_rows = []
        self._board_cur = None
        self.rows = []
        self.list.set_rows([], self._render_row)
        self.view_title.configure(text="榜单")
        self._update_status()
        self._hint("正在取榜单…", 8000)

        def work():
            return musicnet.boards(self.board_source)

        def ok(boards):
            if self._closed or self.view[0] != "board":
                return
            rows = [{"rk": "board", "id": b["id"], "raw": b["raw"],
                     "name": b["name"], "lead": "bar-chart-2", "source": self.board_source,
                     "sub": "%s · 点击看榜内歌曲" % SOURCE_LABEL.get(
                         self.board_source, self.board_source)}
                    for b in boards]
            self._board_rows = rows
            if not rows:
                self._hint("这个源暂未接榜单", 3000)
            self.view_title.configure(text="榜单")
            self.list.set_rows(rows, self._render_row)
            self.rows = rows
            self._update_status()

        self._net_call("榜单", work, ok)

    def _open_board_songs(self, row):
        self._board_cur = row
        self._list_title = row["name"]
        self._board_page = 1
        self.view_title.configure(text=row["name"])
        self.back_btn.pack(side="left")
        self.chips.frame.pack_forget()
        self._run_board_songs(row, append=False)

    def _run_board_songs(self, row, append=False):
        source = row.get("source") or self.board_source
        page = self._board_page

        def work():
            return musicnet.board_songs(source, row["id"], page)
        self._net_call("榜单", work,
                       lambda res: self._on_board_ok(res, row, append))
        self._hint("正在取榜单…", 8000)

    def _on_board_ok(self, res, row, append):
        # 视图守卫：快速切换榜单时，旧榜单的迟到回执不得覆盖当前列表
        if self._closed or self.view[0] != "board" or self._board_cur is not row:
            return
        rows = [self._remember(info) for info in res["list"]]
        self._trim_online_store()
        if append:
            self._board_rows.extend(rows)
        else:
            self._board_rows = rows
        del self._board_rows[3000:]     # 翻页上限
        self._board_all_page = res["allPage"]
        self.view = ("board", "")
        self._refresh_rows()
        self._hint("%s：%d 首" % (row["name"], res["total"]), 3000)

    def _open_plist_list(self):
        self.view = ("plist", "")
        self._list_title = ""
        self._paint_navs()
        self._show_pane()
        self._build_chips()
        self.chips.frame.pack(side="left")
        rows = [{"rk": "plist", "id": "__import__", "name": "粘贴歌单链接导入",
                 "sub": "支持酷我 / QQ音乐 / 网易云 的歌单链接", "lead": "plus"}]
        for item in self.my_plists:
            rows.append(self._plist_row(item))
        self._plist_rows = rows
        self.rows = rows
        self.view_title.configure(text="我的歌单")
        self.list.set_rows(rows, self._render_row)
        self._update_status()

    def _plist_row(self, item):
        return {"rk": "plist", "id": item["id"], "source": item["source"],
                "name": item["name"], "lead": "list",
                "sub": "%s · 歌单号 %s" % (SOURCE_LABEL.get(item["source"], ""),
                                           item["id"]),
                "state_text": SOURCE_LABEL.get(item["source"], "")}

    def _open_plist_songs(self, row, autoplay=False):
        if row.get("id") == "__import__":
            self._paste_playlist()
            return
        source = row.get("source") or self.plist_source
        self._plist_cur = row
        self._list_title = row["name"]
        self.view_title.configure(text=row["name"])
        self.back_btn.pack(side="left")
        self.chips.frame.pack_forget()

        def work():
            return musicnet.songlist_songs(source, row["id"])
        self._net_call("歌单", work,
                       lambda res: self._on_plist_ok(res, row, autoplay))
        self._hint("正在取歌单…", 10000)

    def _on_plist_ok(self, res, row, autoplay):
        rows = [self._remember(info) for info in res["list"]]
        self._trim_online_store()
        self._plist_rows = rows
        self.rows = rows
        self.view = ("plist", "")
        self.list.set_rows(rows, self._render_row)
        self._hint("%s：%d 首" % (row["name"], len(rows)), 3000)
        if autoplay and rows:
            self._set_context(rows)
            self._play_track(rows[0]["uid"], restart=True)
        if row not in self.my_plists:
            entry = {"source": row.get("source") or self.plist_source,
                     "id": row["id"], "name": row["name"]}
            self.my_plists.append(entry)
            self.mcfg["playlists"] = self.my_plists
            self._save_state_soon()

    def _paste_playlist(self):
        win = tk.Toplevel(self.win)
        win.title("导入歌单链接")
        win.configure(bg=M_BG)
        win.geometry("480x170")
        win.transient(self.win)
        tk.Label(win, text="粘贴歌单链接（酷我 / QQ音乐 / 网易云）",
                 bg=M_BG, fg=M_MUTED, font=(core.FONT, 9)).pack(anchor="w",
                                                                 padx=14, pady=(12, 4))
        entry = tk.Entry(win, bg=M_SURF2, fg=M_TEXT, insertbackground=M_TEXT,
                         relief="flat", font=(core.FONT, 9))
        entry.pack(fill="x", padx=14)
        entry.insert(0, "")
        entry.focus_set()
        tip = tk.Label(win, text="", bg=M_BG, fg=M_WARN, font=(core.FONT, 8),
                       wraplength=440, anchor="w")
        tip.pack(fill="x", padx=14, pady=(6, 0))

        def go(_e=None):
            url = entry.get().strip()
            source, pid = musicnet.parse_list_url(url)
            if not source or not pid:
                tip.configure(text="认不出这个链接里的歌单号")
                return
            if source == "wy":
                tip.configure(text="网易云歌单暂时只支持试听列表解析，先试试酷我或 QQ 的链接")
                return

            def work():
                return musicnet.songlist_songs(source, pid)
            row = {"rk": "plist", "id": pid, "source": source, "name": "歌单 %s" % pid}
            self._net_call("歌单解析", work,
                           lambda res: self._on_imported(res, row))
            tip.configure(text="解析中…")
            win.destroy()
            if self.view[0] != "plist":
                self._select_view(("plist", ""))
            self._open_plist_songs(row)

        entry.bind("<Return>", go)
        tk.Button(win, text="导入", command=go, bg=M_ACC, fg="#ffffff",
                  relief="flat", bd=0, padx=16, pady=4, font=(core.FONT, 9),
                  cursor="hand2").pack(anchor="e", padx=14, pady=(8, 0))

    def _on_imported(self, res, row):
        pass

    def _remove_plist(self, row):
        self.my_plists = [p for p in self.my_plists
                          if not (p.get("id") == row.get("id") and
                                  p.get("source") == row.get("source"))]
        self.mcfg["playlists"] = self.my_plists
        save_cfg(self.app.cfg)
        self._open_plist_list()

    # ---- 音源 ----
    DL_PAGES = {
        "node": "https://nodejs.org/zh-cn/download",
        "ffmpeg": "https://www.gyan.dev/ffmpeg/builds/",
    }

    def _open_dl_page(self, key):
        try:
            import webbrowser
            webbrowser.open(self.DL_PAGES[key])
        except Exception as e:
            self._hint("打开下载页失败：%s" % e, 5000)

    def _ask_open_download(self, key, title, msg):
        """外部依赖缺失引导：每个会话每个依赖只问一次，同意就开官方下载页。"""
        flag = "_asked_" + key
        if getattr(self, flag, False):
            return
        setattr(self, flag, True)
        try:
            if messagebox.askyesno(title, msg, parent=self.win):
                self._open_dl_page(key)
        except Exception:
            pass

    def _api_load_active(self):
        item = musicapi.active_item(self.sources)
        if item is None:
            self._api_state = "idle"
            self._paint_src_state()
            return
        if not self.api.node_path():
            self._api_state = "error"
            self._api_error = "没找到 node.exe"
            self._paint_src_state()
            self._ask_open_download(
                "node", "缺少音源运行环境 node.exe",
                "在线歌的音源脚本需要 node.exe 来运行（本地沙箱执行，脚本拿不到"
                "你的文件和系统权限）。\n\n"
                "程序已自动搜索 PATH 和常见安装目录，没有找到。\n"
                "安装后重启 VBHelper 即可；也可以在「音源管理」设置页手动指定路径。")
            return
        self._api_state = "loading"
        self._paint_src_state()
        self.api.load(item)

    def _pump_api(self):
        n = 0
        while True:
            try:
                event, payload = self._api_results.get_nowait()
            except queue.Empty:
                break
            n += 1
            if event == "load_start":
                self._api_state = "loading"
            elif event == "ready":
                self._api_state = "ready"
                self._api_error = ""
                srcs = ", ".join(SOURCE_LABEL.get(s, s)
                                 for s in (payload.get("sources") or {}))
                self._api_message = payload.get("message") or ""
                self._hint("音源就绪，可解析：%s" % (srcs or "（没声明任何源）"), 4000)
                uid = self._pending_play_uid
                self._pending_play_uid = ""
                if uid and uid == self.current_uid:
                    self._play_online(self.tracks[uid])
            elif event == "error":
                self._api_state = "error"
                self._api_error = payload.get("message") or "音源初始化失败"
                self._pending_play_uid = ""
                if self.playing and not self.player.opened:
                    self.playing = False
                    self._preparing = False
                    self._paint_play()
                self._hint("音源：%s" % self._api_error[:110], 8000)
            elif event == "res":
                self._on_api_res(payload)
            elif event == "alert":
                self._show_update_alert(payload)
            elif event == "log":
                dbg_log("音源日志：%s" % payload.get("text"))
            elif event == "exit":
                self._api_state = "idle"
                self._api_error = ""
                self._api_broken()
            elif event == "reaped":
                self._api_state = "idle"
            if n > 24:
                break
        self._paint_src_state()
        if n and self.view[0] == "source":
            self._render_source_cards()

    def _on_api_res(self, payload):
        key = payload.get("id")
        entry = self._req_map.pop(key, None)
        if entry is None:
            return
        uid, purpose, quality, jid = entry
        # 带 uid 守卫：旧歌的迟到回执不得清掉新歌的 _pending_key
        if purpose == "url" and self._pending_key == key \
                and uid == self.current_uid:
            self._pending_key = None
        self._url_deadline = 0.0
        if self._preparing and purpose == "url" and uid == self.current_uid:
            self._preparing = False
        if not payload.get("ok"):
            err = payload.get("error") or "解析失败"
            if purpose == "url":
                if uid == self.current_uid and self._preparing:
                    self._preparing = False
                    self._url_retry(err)
                elif uid == self.current_uid:
                    self._hint("直链解析失败：%s（已自动重试仍未成功）"
                               % err[:100], 12000)
                    self.playing = False
                    self._paint_play()
                # 旧歌迟到失败：静默丢弃，不顶掉当前提示
            else:
                job = self._find_job(jid)
                if job:
                    job["state"] = "err"
                    job["detail"] = err[:80]
            return
        data = ((payload.get("data") or {}).get("data")) or {}
        url = data.get("url") if isinstance(data, dict) else data
        if not url:
            if uid == self.current_uid:
                self._hint("音源没返回可用的直链", 5000)
            return
        if purpose == "dl":
            self._buffer_url(uid, url, quality, "dl", jid)
        else:
            self._buffer_url(uid, url, quality)

    def _show_update_alert(self, payload):
        url = payload.get("updateUrl")
        if messagebox.askyesno(APP_NAME, "音源更新提示\n\n%s" %
                               (payload.get("log") or "")[:400] +
                               ("\n\n现在打开更新地址吗？" if url else ""),
                               parent=self.win) and url:
            self._open_path(url)

    def _paint_src_state(self):
        if getattr(self, "src_state", None) is None:
            return
        item = musicapi.active_item(self.sources)
        name = (item or {}).get("name") or "未选择音源"
        if self._api_state == "ready":
            text = "音源：%s\n就绪 · 空闲 45 秒自动释放 node 进程" % name
            color = M_GOOD
        elif self._api_state == "loading":
            text = "音源：%s\n初始化中…" % name
            color = M_MUTED
        elif self._api_state == "error":
            text = "音源：%s\n%s" % (name, self._api_error[:60])
            color = M_BAD
        else:
            text = "音源：%s\n未运行" % name
            color = M_DIM
        # 脏检查：_pump 每 80ms 调用一次，状态未变化时不更新控件
        if getattr(self, "_src_paint", None) == (text, color):
            return
        self._src_paint = (text, color)
        try:
            self.src_state.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def _import_source_dialog(self):
        win = tk.Toplevel(self.win)
        win.title("导入音源")
        win.configure(bg=M_BG)
        win.geometry("520x250")
        win.transient(self.win)
        tk.Label(win, text="本地 .js 文件", bg=M_BG, fg=M_MUTED,
                 font=(core.FONT, 9)).pack(anchor="w", padx=16, pady=(14, 2))
        row = tk.Frame(win, bg=M_BG)
        row.pack(fill="x", padx=16)
        path_box = tk.Entry(row, bg=M_SURF2, fg=M_TEXT, insertbackground=M_TEXT,
                            relief="flat", font=(core.FONT, 9))
        path_box.pack(side="left", fill="x", expand=True, pady=4)

        def browse():
            p = filedialog.askopenfilename(parent=win, title="选择音源脚本",
                                           filetypes=[("音源脚本", "*.js"),
                                                      ("所有文件", "*.*")])
            if p:
                path_box.delete(0, "end")
                path_box.insert(0, p)
            self._import_from_file(p)

        _flat_button(row, "浏览…", browse).pack(side="left", padx=(8, 0))
        tk.Label(win, text="或粘贴链接", bg=M_BG, fg=M_MUTED,
                 font=(core.FONT, 9)).pack(anchor="w", padx=16, pady=(12, 2))
        row2 = tk.Frame(win, bg=M_BG)
        row2.pack(fill="x", padx=16)
        url_box = tk.Entry(row2, bg=M_SURF2, fg=M_TEXT, insertbackground=M_TEXT,
                           relief="flat", font=(core.FONT, 9))
        url_box.pack(side="left", fill="x", expand=True, pady=4)

        def fetch():
            self._import_from_url(url_box.get().strip(), win)

        _flat_button(row2, "拉取并导入", fetch, True).pack(side="left", padx=(8, 0))
        status = tk.Label(win, text="协议：lx-music-desktop 自定义源 v2.0.0（.js）",
                          bg=M_BG, fg=M_DIM, font=(core.FONT, 8), wraplength=480,
                          anchor="w", justify="left")
        status.pack(fill="x", padx=16, pady=(12, 14))

        def show(msg, bad=False):
            try:
                status.configure(text=msg[:300], fg=M_BAD if bad else M_DIM)
            except tk.TclError:
                pass

        self._import_win = win
        win.protocol("WM_DELETE_WINDOW", lambda: (
            setattr(self, "_import_win", None), win.destroy()))

    def _import_from_file(self, path):
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read(16 * 1024 * 1024)
        except Exception as e:
            self._report_import("读不了这个文件：%s" % e, True)
            return
        self._import_text(text)

    def _import_from_url(self, url, parent=None):
        if not url:
            self._report_import("链接是空的", True)
            return
        self._report_import("正在下载脚本…")

        def work():
            return musicapi.fetch_remote(url)
        self._net_call("拉取脚本", work, lambda text: self._import_text(text))

    def _report_import(self, msg, bad=False):
        win = self._import_win
        if win is not None and win.winfo_exists():
            for child in win.winfo_children():
                if isinstance(child, tk.Label) and child.cget("wraplength"):
                    child.configure(text=msg[:300], fg=M_BAD if bad else M_DIM)
                    break
        self._hint(msg, 5000)

    def _import_text(self, text):
        try:
            item = musicapi.import_script(self.sources, text)
        except ValueError as e:
            self._report_import(str(e), True)
            return
        musicapi.save_sources(self.sources)
        self._report_import("已导入：%s（%s）" % (item["name"], item["version"] or "无版本"))
        self._select_view(("source", ""))
        if self.sources["active"] == item["id"]:
            self._api_load_active()
        self._update_nav_counts()

    def _enable_source(self, sid):
        self.sources["active"] = sid
        musicapi.save_sources(self.sources)
        self.mcfg["api_active"] = sid
        self._select_view(("source", ""))
        self._api_load_active()

    def _delete_source(self, sid):
        item = next((i for i in self.sources["items"] if i.get("id") == sid), None)
        if item is None:
            return
        if not messagebox.askyesno(APP_NAME, "删除音源「%s」？" % item.get("name"),
                                   parent=self.win):
            return
        was_active = self.sources.get("active") == sid
        musicapi.remove_script(self.sources, sid)
        musicapi.save_sources(self.sources)
        if was_active:
            self.api.stop()
            self._api_state = "idle"
        self._select_view(("source", ""))
        if was_active and self.sources["items"]:
            self._api_load_active()
        self._paint_src_state()

    def _test_source(self, sid=None):
        sid = sid or self.sources.get("active")
        item = next((i for i in self.sources["items"] if i.get("id") == sid), None)
        if item is None:
            self._hint("先导入一个音源", 3000)
            return
        if self.api.current_id != sid or not self.api.alive:
            self.api.load(item)
            self._hint("测试中：音源初始化…", 8000)
            return
        source = next((s for s in self.api.sources), None)
        if not source:
            self._hint("这个音源没声明任何可用源，测不了", 5000)
            return
        quality = (self.api.qualitys(source) or ["128k"])[0]
        probe = make_online({"source": source, "songmid": "228908",
                             "name": "晴天", "singer": "周杰伦",
                             "qualitys": self.api.qualitys(source),
                             "dur": 269})
        key = "test|%s|%s" % (source, quality)
        self._req_map[key] = (probe["uid"], "test", quality, None)
        self.tracks[probe["uid"]] = probe
        self.api.request("musicUrl", source,
                         {"type": quality, "musicInfo": api_music_info(probe)}, key)
        self._hint("已发出解析请求，稍后看提示", 5000)

    def _render_source_cards(self):
        self._clear_cards()
        self.cards_title.configure(text="音源管理")
        self.cards_sub.configure(
            text="lx-music-desktop 自定义源协议 v2.0.0：本地 .js 或 http(s) 链接导入。"
                 "脚本只在 Node 子进程里跑，不接触文件与进程；空闲 45 秒自动销毁。")
        row = tk.Frame(self.cards_right, bg=M_BG)
        row.pack(side="left")
        _flat_button(row, "导入音源", self._import_source_dialog, True).pack(side="left")
        _flat_button(row, "自检宿主", lambda: self._selftest_host()).pack(
            side="left", padx=(6, 0))
        if not self.sources["items"]:
            tip = tk.Label(self.cards_scroll.inner,
                           text="还没有音源。\n点「导入音源」选一个 lx-music-desktop 的 .js 脚本，"
                                "在线歌就需要它来解析播放直链。",
                           bg=M_BG, fg=M_MUTED, font=(core.FONT, 10),
                           justify="left", wraplength=520)
            self.cards_scroll.add(tip)
            return
        for item in self.sources["items"]:
            self.cards_scroll.add(self._source_card(item))

    def _source_card(self, item):
        sid = item["id"]
        active = self.sources.get("active") == sid
        card = tk.Frame(self.cards_scroll.inner, bg="#181c23", bd=1, relief="flat",
                        highlightbackground=M_LINE, highlightthickness=1)
        inner = tk.Frame(card, bg="#181c23")
        inner.pack(fill="x", padx=14, pady=10)
        left = tk.Frame(inner, bg="#181c23")
        left.pack(side="left", fill="x", expand=True)
        title = "%s  %s" % (item.get("name") or "未命名",
                            item.get("version") or "")
        tk.Label(left, text=title, bg="#181c23", fg=M_TEXT,
                 font=(core.FONT, 10, "bold"), anchor="w").pack(anchor="w")
        state, color = self._source_state_of(item, active)
        tk.Label(left, text=state, bg="#181c23", fg=color, font=(core.FONT, 8),
                 anchor="w", justify="left", wraplength=520).pack(anchor="w",
                                                                 pady=(3, 0))
        right = tk.Frame(inner, bg="#181c23")
        right.pack(side="right")
        if not active:
            _text_button(right, "启用", lambda: self._enable_source(sid),
                         "#181c23", fg=M_MUTED, size=9).pack(side="left", padx=4)
        else:
            tk.Label(right, text="使用中", bg=M_ACC_BG, fg="#bcd0ff",
                     font=(core.FONT, 8), padx=7, pady=2).pack(side="left", padx=4)
        _text_button(right, "测试", lambda: self._test_source(sid), "#181c23",
                     fg=M_MUTED, size=9).pack(side="left", padx=4)
        _text_button(right, "删除", lambda: self._delete_source(sid), "#181c23",
                     fg=M_MUTED, size=9).pack(side="left", padx=4)
        if item.get("homepage"):
            _text_button(right, "主页", lambda: self._open_path(item["homepage"]),
                         "#181c23", fg=M_DIM, size=9).pack(side="left", padx=4)
        return card

    def _source_state_of(self, item, active):
        if not active:
            desc = item.get("description") or ""
            return ("%s%s" % (desc + "  " if desc else "", "未启用")), M_DIM
        if self._api_state == "ready":
            srcs = []
            for source, entry in (self.api.sources or {}).items():
                acts = "/".join(entry.get("actions") or [])
                quals = " ".join(entry.get("qualitys") or [])
                srcs.append("%s(%s%s)" % (SOURCE_LABEL.get(source, source),
                                          acts, (" " + quals) if quals else ""))
            return ("就绪 · %s" % ("、".join(srcs) or "没声明能力")), M_GOOD
        if self._api_state == "loading":
            return "初始化中…", M_MUTED
        if self._api_state == "error":
            return "初始化失败：%s" % self._api_error, M_BAD
        return "未运行（放在线歌时会自动启动）", M_WARN

    def _selftest_host(self):
        """用内置的自检脚本验宿主本身，不依赖第三方音源。"""
        if not self.api.node_path():
            self._hint("没找到 node.exe，去设置里指一下路径", 5000)
            return
        item = {"id": "selftest", "script": musicapi._pack(
            musicapi.SELFTEST_SCRIPT), "name": "宿主自检"}
        self._hint("宿主自检：正在启动 node…", 6000)
        self.api.load(item)

    # ---- 下载 ----
    def _download_track(self, uid):
        track = self.tracks.get(uid)
        if track is None:
            return
        if track.get("kind") != "online":
            self._hint("本地歌已经在磁盘上了", 3000)
            return
        if not self.api.inited:
            self._hint("音源还没就绪，等一下再试", 4000)
            return
        source = track.get("source")
        quality = self._effective_quality(track)
        if not quality:
            self._hint("当前音源解析不了这首（音质或源不支持）", 5000)
            return
        self._down_seq += 1
        jid = self._down_seq
        safe = re.sub(r'[\\/:*?"<>|]', " ",
                      "%s - %s" % (track.get("artist"), track.get("title")))
        name = safe.strip()[:120] + ONLINE_EXTS.get(quality, ".mp3")
        target = os.path.join(self.down_dir, name)
        if os.path.isfile(target):
            self._hint("这个文件已经在了：%s" % name[:60], 4000)
            return
        self._down_jobs.insert(0, {"jid": jid, "uid": uid, "name": name,
                                   "quality": quality, "got": 0, "total": 0,
                                   "state": "wait", "detail": "解析直链…",
                                   "path": target})
        if self.view[0] != "down":
            self._select_view(("down", ""))
        key = "d|%s|%s" % (uid, quality)
        self._req_map[key] = (uid, "dl", quality, jid)
        self.api.request("musicUrl", source,
                         {"type": quality, "musicInfo": api_music_info(track)}, key)

    def _find_job(self, jid):
        for job in self._down_jobs:
            if job.get("jid") == jid:
                return job
        return None

    def _down_finish(self, jid, target):
        job = self._find_job(jid)
        if job is None:
            return
        job["state"] = "done"
        job["detail"] = "已保存 · %s" % human_size(os.path.getsize(target)) \
            if os.path.isfile(target) else "已保存"
        try:
            job["got"] = job["total"] = os.path.getsize(target)
        except OSError:
            pass
        self._hint("下载完成：%s" % os.path.basename(target)[:60], 4000)
        self._render_down_cards() if self.view[0] == "down" else None
        self._update_nav_counts()

    def _down_running(self):
        return any(j["state"] in ("wait", "run") for j in self._down_jobs)

    def _cancel_download(self, jid):
        job = self._find_job(jid)
        if job is None:
            return
        self.streamer.cancel_meta(("dl", jid))
        job["state"] = "err"
        job["detail"] = "已取消"
        self._render_down_cards()

    def _render_down_cards(self):
        self._clear_cards()
        self.cards_title.configure(text="下载")
        self.cards_sub.configure(text="保存目录：%s" % self.down_dir)
        row = tk.Frame(self.cards_right, bg=M_BG)
        row.pack(side="left")
        _flat_button(row, "打开目录",
                     lambda: self._open_path(self.down_dir)).pack(side="left")
        _flat_button(row, "选目录", self._pick_down_dir).pack(side="left",
                                                             padx=(6, 0))
        if not self._down_jobs:
            self.cards_scroll.add(tk.Label(
                self.cards_scroll.inner,
                text="没有下载任务。\n在线搜索结果里右键一首歌 → 下载到本地。",
                bg=M_BG, fg=M_MUTED, font=(core.FONT, 10), justify="left",
                wraplength=520))
            return
        for job in self._down_jobs[:40]:
            self.cards_scroll.add(self._down_card(job))

    def _down_card(self, job):
        card = tk.Frame(self.cards_scroll.inner, bg="#181c23",
                        highlightbackground=M_LINE, highlightthickness=1)
        inner = tk.Frame(card, bg="#181c23")
        inner.pack(fill="x", padx=14, pady=9)
        left = tk.Frame(inner, bg="#181c23")
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="%s  %s" % (job["name"], job["quality"]),
                 bg="#181c23", fg=M_TEXT, font=(core.FONT, 9, "bold"),
                 anchor="w").pack(anchor="w")
        detail = job["detail"]
        if job["state"] in ("wait", "run") and job["total"]:
            detail = "%s / %s" % (human_size(job["got"]), human_size(job["total"]))
        tk.Label(left, text=detail, bg="#181c23", fg=M_MUTED,
                 font=(core.FONT, 8), anchor="w").pack(anchor="w", pady=(2, 0))
        bar = tk.Canvas(left, height=3, bg="#2a303b", highlightthickness=0, bd=0)
        bar.pack(fill="x", pady=(5, 0))
        ratio = (job["got"] / job["total"]) if job["total"] else (
            1.0 if job["state"] == "done" else 0.0)
        bar.create_rectangle(0, 0, max(1, int((bar.winfo_width() or 240) * ratio)),
                             3, fill=M_ACC, outline="")
        right = tk.Frame(inner, bg="#181c23")
        right.pack(side="right")
        if job["state"] == "done":
            _text_button(right, "打开位置",
                         lambda p=job["path"]: self._reveal(p), "#181c23",
                         fg=M_MUTED, size=9).pack(side="left")
        elif job["state"] in ("wait", "run"):
            _text_button(right, "取消",
                         lambda j=job["jid"]: self._cancel_download(j), "#181c23",
                         fg=M_MUTED, size=9).pack(side="left")
        _text_button(right, "移除",
                     lambda j=job: self._drop_job(j), "#181c23",
                     fg=M_DIM, size=9).pack(side="left", padx=(4, 0))
        return card

    def _drop_job(self, job):
        if job in self._down_jobs:
            self._down_jobs.remove(job)
            self._render_down_cards()

    def _pick_down_dir(self):
        path = filedialog.askdirectory(parent=self.win, title="选择下载目录",
                                       initialdir=self.down_dir)
        if not path:
            return
        self.down_dir = os.path.normpath(path)
        self.mcfg["down_dir"] = self.down_dir
        save_cfg(self.app.cfg)
        self._render_down_cards()

    def _clear_cards(self):
        self.cards_scroll.clear()
        for child in list(self.cards_right.winfo_children()):
            child.destroy()

    # ---- 设置 ----
    def _render_settings_cards(self):
        self._clear_cards()
        self.cards_title.configure(text="设置")
        self.cards_sub.configure(text="改动即时生效并写进配置")
        box = tk.Frame(self.cards_scroll.inner, bg="#181c23",
                       highlightbackground=M_LINE, highlightthickness=1)
        self.cards_scroll.add(box)
        r = 0
        tk.Label(box, text="默认音质", bg="#181c23", fg=M_MUTED,
                 font=(core.FONT, 9)).grid(row=r, column=0, sticky="w",
                                           padx=14, pady=8)
        qf = tk.Frame(box, bg="#181c23")
        qf.grid(row=r, column=1, sticky="w", padx=8)
        for q in ("128k", "320k", "flac", "flac24bit"):
            chip = tk.Label(qf, text=QUALITY_LABEL[q], font=(core.FONT, 8),
                            padx=9, pady=3, cursor="hand2",
                            bg=M_ACC_BG if q == self.quality else M_SURF2,
                            fg="#bcd0ff" if q == self.quality else M_MUTED)
            chip.pack(side="left", padx=(0, 5))
            chip.bind("<Button-1>", lambda e, v=q: self._set_quality(v))
        r += 1
        tk.Label(box, text="Node 路径", bg="#181c23", fg=M_MUTED,
                 font=(core.FONT, 9)).grid(row=r, column=0, sticky="w",
                                           padx=14, pady=8)
        nf = tk.Frame(box, bg="#181c23")
        nf.grid(row=r, column=1, sticky="ew", padx=8)
        node = self.api.node_path() or "没找到"
        tk.Label(nf, text=node, bg="#181c23",
                 fg=M_GOOD if node != "没找到" else M_BAD,
                 font=(core.FONT, 8)).pack(side="left")
        _text_button(nf, "选择…", self._pick_node, "#181c23", fg=M_MUTED,
                     size=8).pack(side="left", padx=(8, 0))
        _text_button(nf, "下载页", lambda: self._open_dl_page("node"),
                     "#181c23", fg=M_MUTED, size=8).pack(side="left", padx=(6, 0))
        r += 1
        tk.Label(box, text="下载目录", bg="#181c23", fg=M_MUTED,
                 font=(core.FONT, 9)).grid(row=r, column=0, sticky="w",
                                           padx=14, pady=8)
        df = tk.Frame(box, bg="#181c23")
        df.grid(row=r, column=1, sticky="ew", padx=8)
        tk.Label(df, text=self.down_dir, bg="#181c23", fg=M_TEXT,
                 font=(core.FONT, 8)).pack(side="left")
        _text_button(df, "更改…", self._pick_down_dir, "#181c23", fg=M_MUTED,
                     size=8).pack(side="left", padx=(8, 0))
        r += 1
        tk.Label(box, text="转码缓存", bg="#181c23", fg=M_MUTED,
                 font=(core.FONT, 9)).grid(row=r, column=0, sticky="w",
                                           padx=14, pady=8)
        cf = tk.Frame(box, bg="#181c23")
        cf.grid(row=r, column=1, sticky="w", padx=8)
        tk.Label(cf, text=self._cache_size_text(), bg="#181c23", fg=M_TEXT,
                 font=(core.FONT, 8)).pack(side="left")
        _text_button(cf, "清空", self._clear_cache, "#181c23", fg=M_MUTED,
                     size=8).pack(side="left", padx=(8, 0))
        r += 1
        tk.Label(box, text="窗口置顶", bg="#181c23", fg=M_MUTED,
                 font=(core.FONT, 9)).grid(row=r, column=0, sticky="w",
                                           padx=14, pady=8)
        tf = tk.Frame(box, bg="#181c23")
        tf.grid(row=r, column=1, sticky="w", padx=8)
        topmost = bool(self.mcfg.get("topmost", False))
        for label, value in (("开", True), ("关", False)):
            chip = tk.Label(tf, text=label, font=(core.FONT, 8),
                            padx=9, pady=3, cursor="hand2",
                            bg=M_ACC_BG if topmost == value else M_SURF2,
                            fg="#bcd0ff" if topmost == value else M_MUTED)
            chip.pack(side="left", padx=(0, 5))
            chip.bind("<Button-1>",
                      lambda e, v=value: self._toggle_topmost() if v != topmost else None)
        r += 1
        tk.Label(box, text="ffmpeg", bg="#181c23", fg=M_MUTED,
                 font=(core.FONT, 9)).grid(row=r, column=0, sticky="w",
                                           padx=14, pady=8)
        ff = self.prep.ffmpeg or self._ffprobe or ""
        ffbox = tk.Frame(box, bg="#181c23")
        ffbox.grid(row=r, column=1, sticky="ew", padx=8)
        tk.Label(ffbox, text=ff or "没找到（flac/m4a 需要它）", bg="#181c23",
                 fg=M_GOOD if ff else M_BAD, font=(core.FONT, 8)).pack(side="left")
        _text_button(ffbox, "下载页", lambda: self._open_dl_page("ffmpeg"),
                     "#181c23", fg=M_MUTED, size=8).pack(side="left", padx=(6, 0))
        box.grid_columnconfigure(1, weight=1)

        tips = tk.Label(self.cards_scroll.inner,
                        text="占用说明：\n"
                             "· 列表是虚拟化的，只渲染视口内的行，8000 首也只有一个画布。\n"
                             "· 封面缩略图最多留 24 张，歌词缓存最多 8 首。\n"
                             "· Node 音源进程只在解析在线直链时存在，空闲 45 秒自动销毁。\n"
                             "· 在线歌先整轨缓冲再播，所以 flac 能放、能拖进度；缓冲文件走 LRU 清理。",
                        bg=M_BG, fg=M_MUTED, font=(core.FONT, 8), justify="left",
                        wraplength=560)
        self.cards_scroll.add(tips)

    def _set_quality(self, value):
        self.quality = value
        self.mcfg["quality"] = value
        save_cfg(self.app.cfg)
        self._render_settings_cards()
        self._hint("默认音质：%s" % value, 2000)

    def _pick_node(self):
        path = filedialog.askopenfilename(parent=self.win, title="选择 node.exe",
                                          filetypes=[("node", "node.exe"),
                                                     ("所有文件", "*.*")])
        if not path:
            return
        self.mcfg["node_path"] = os.path.normpath(path)
        save_cfg(self.app.cfg)
        self.api.reset_node_cache()
        self._render_settings_cards()
        self._api_load_active()

    def _cache_size_text(self):
        try:
            total = 0
            for name in os.listdir(CACHE_DIR):
                try:
                    total += os.path.getsize(os.path.join(CACHE_DIR, name))
                except OSError:
                    pass
            return "%s / 上限 %s" % (human_size(total), human_size(CACHE_MAX_BYTES))
        except OSError:
            return "还没生成过缓存"

    def _clear_cache(self):
        if not messagebox.askyesno(APP_NAME, "清空转码与在线缓冲？\n（不影响已下载的音乐）",
                                   parent=self.win):
            return
        removed = 0
        try:
            for name in os.listdir(CACHE_DIR):
                if name.endswith(".part"):
                    continue
                path = os.path.join(CACHE_DIR, name)
                try:
                    if _normcase(path) == _normcase(self.player._path):
                        continue
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        except OSError:
            pass
        self._hint("清掉 %d 个缓冲文件" % removed, 3000)
        self._render_settings_cards()

    # ---- 收藏与最近 ----
    def _is_fav(self, uid):
        return bool(uid) and uid in self.fav_keys

    def _toggle_fav(self):
        if self.current_uid:
            self._toggle_fav_uid(self.current_uid)

    def _toggle_fav_uid(self, uid):
        if not uid:
            return
        if uid in self.fav_keys:
            self.fav_keys.discard(uid)
            self.fav_uids = [u for u in self.fav_uids if u != uid]
            added = False
        else:
            self.fav_keys.add(uid)
            self.fav_uids = [uid] + self.fav_uids
            added = True
        del self.fav_uids[1500:]
        self.mcfg["fav"] = self.fav_uids
        save_cfg(self.app.cfg)
        self._paint_now()
        self._refresh_rows()
        self._update_nav_counts()
        self._hint("已加入我喜欢" if added else "已移出我喜欢", 2200)

    def _push_recent(self, uid):
        if not uid:
            return
        self.recent_uids = [uid] + [u for u in self.recent_uids if u != uid]
        del self.recent_uids[120:]
        self.mcfg["recent"] = self.recent_uids
        if self.view[0] == "recent":
            self._refresh_rows()
        else:
            self._update_nav_counts()
        self._save_state_soon()

    # ---- 杂项 ----
    def _reveal(self, path):
        if not path:
            return
        try:
            if os.path.isdir(path):
                os.startfile(path)
            else:
                subprocess.Popen(["explorer", "/select," + path])
        except Exception:
            self._hint("无法打开资源管理器", 2500)

    def _open_path(self, target):
        try:
            os.startfile(target)
        except Exception:
            self._hint("打不开这个地址", 2500)

    def _copy(self, text):
        if not set_clipboard(text):
            try:
                self.win.clipboard_clear()
                self.win.clipboard_append(text)
            except tk.TclError:
                pass
        self._hint("已复制", 1800)

    def bring(self):
        if self._mini_shown:
            self._toggle_mini(False)
        try:
            self._apply_topmost()
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:
            pass

    def _apply_topmost(self):
        """窗口置顶跟随设置（music.topmost，默认不置顶）。"""
        try:
            self.win.attributes("-topmost", bool(self.mcfg.get("topmost", False)))
        except Exception:
            pass

    def _toggle_topmost(self):
        value = not bool(self.mcfg.get("topmost", False))
        self.mcfg["topmost"] = value
        save_cfg(self.app.cfg)
        self._apply_topmost()
        self._render_settings_cards()
        self._hint("窗口置顶：%s" % ("开" if value else "关"), 2000)

    def _bind_keys(self):
        self.win.bind("<KeyPress>", self._on_key)

    def _typing(self):
        try:
            w = self.win.focus_get()
        except (tk.TclError, KeyError):
            return False
        return isinstance(w, tk.Entry)

    def _on_key(self, e):
        if self._typing():
            if e.keysym == "Escape":
                self.search.delete(0, "end")
                self._search_placeholder()
                self.win.focus_set()
                return "break"
            return None
        key = e.keysym
        if key == "space":
            self._toggle_play()
            return "break"
        if key == "Left":
            self._prev()
            return "break"
        if key == "Right":
            self._next()
            return "break"
        if key == "Up":
            self._nudge_volume(5)
            return "break"
        if key == "Down":
            self._nudge_volume(-5)
            return "break"
        if key in ("m", "M"):
            self._toggle_mini()
            return "break"
        if key in ("F5",):
            self._scan_all()
            return "break"
        if key in ("slash",) or (e.state & 0x400 and key == "f"):
            self._select_view(("search", ""))
            self.search.focus_set()
            return "break"
        if key == "Escape":
            if self._mini_shown:
                self._toggle_mini(False)
            else:
                self.close()
            return "break"
        return None

    def _vol_change(self, frac):
        self.volume = max(0, min(100, int(round(frac * 100))))
        try:
            self.vol_lbl.configure(text="%d%%" % self.volume)
        except tk.TclError:
            pass

    def _vol_commit(self, frac):
        self._vol_change(frac)
        self.mcfg["volume"] = self.volume
        if self.player.opened and not self.player.set_volume(self.volume):
            self._hint("这个通道的音量由系统控制", 4500)
        self._save_state_soon()

    def _nudge_volume(self, delta):
        self._volume_before_mute = 0
        self.volume = max(0, min(100, self.volume + delta))
        self._paint_volume()
        if self.player.opened:
            self.player.set_volume(self.volume)
        self.mcfg["volume"] = self.volume
        self._save_state_soon()

    def _toggle_mute(self):
        """双击音量百分比：静音 / 恢复（记忆静音前的音量，对齐 LX）。"""
        if self.volume > 0:
            self._volume_before_mute = self.volume
            self.volume = 0
            self._hint("已静音（双击恢复）", 2500)
        else:
            self.volume = self._volume_before_mute or 50
            self._volume_before_mute = 0
        self._paint_volume()
        if self.player.opened:
            self.player.set_volume(self.volume)
        self.mcfg["volume"] = self.volume
        self._save_state_soon()

    def _cycle_mode(self):
        self.mode = (self.mode + 1) % 3
        self._paint_mode()
        self.mcfg["mode"] = self.mode
        self._save_state_soon()
        self._hint("播放模式：" + MODE_TITLES[self.mode], 2000)

    # ---- 保存与退出 ----
    def _save_state_soon(self):
        if self._closed:
            return
        if self._save_job is not None:
            try:
                self.win.after_cancel(self._save_job)
            except tk.TclError:
                pass
        self._save_job = self.win.after(900, self._save_state)

    def _save_state(self):
        self._save_job = None
        if self._closed:
            return
        try:
            self.mcfg["folders"] = self.folders
            self.mcfg["recent"] = self.recent_uids
            self.mcfg["fav"] = self.fav_uids
            self.mcfg["volume"] = self.volume
            self.mcfg["mode"] = self.mode
            self.mcfg["quality"] = self.quality
            self.mcfg["down_dir"] = self.down_dir
            self.mcfg["api_active"] = self.sources.get("active") or ""
            self.mcfg["last"] = self.current_uid
            self.mcfg["pos"] = int(self.position * 1000)
            self.mcfg["playlists"] = self.my_plists[:60]
            needed = set(self.fav_uids) | set(self.recent_uids) | set(self.ctx_uids)
            dump = []
            for uid in needed:
                track = self.tracks.get(uid)
                if track and track.get("kind") == "online":
                    dump.append(track)
            self.mcfg["online"] = dump[:600]
            try:
                self.mcfg["geom"] = self.win.geometry()
            except tk.TclError:
                pass
            if self._mini is not None:
                try:
                    self.mcfg["mini_pos"] = [self._mini.winfo_x(),
                                             self._mini.winfo_y()]
                except tk.TclError:
                    pass
            save_cfg(self.app.cfg)
        except Exception:
            pass

    def close(self):
        global _window
        if self._closed:
            return
        self._prep_gen += 1
        for closer in (lambda: self.prep.cancel(self._prep_gen),
                        self._save_state, self._flush_meta):
            try:
                closer()
            except Exception:
                pass
        self._closed = True
        for ev in (self._scan_stop, self._meta_stop):
            try:
                ev.set()
            except Exception:
                pass
        for obj in (self.prep, self.streamer, self.covers, self.api):
            try:
                obj.close() if hasattr(obj, "close") else obj.stop()
            except Exception:                pass
        try:
            self.stream_hub.stop()
        except Exception:
            pass
        try:
            self.player.close()
        except Exception:
            pass
        for job in (self._tick_job, self._pump_job, self._slow_job, self._save_job,
                    self._hint_job, self._meta_flush_job, self._cover_job,
                    getattr(self, "_search_job", None)):
            if job is None:
                continue
            try:
                self.win.after_cancel(job)
            except Exception:
                pass
        for frame in (self._mini, self.win):
            try:
                if frame is not None:
                    frame.destroy()
            except Exception:
                pass
        if _window is self:
            _window = None

    # ---- 迷你播放条 ----
    def _build_mini(self):
        m = tk.Toplevel(self.root)
        m.withdraw()
        m.overrideredirect(True)
        m.configure(bg=M_LINE)
        try:
            m.attributes("-topmost", True)
        except Exception:
            pass
        outer = tk.Frame(m, bg=M_LINE)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=M_SURF)
        inner.pack(fill="both", expand=True, padx=1, pady=(1, 3))

        self.mini_cover = tk.Frame(inner, bg=M_SURF3, width=40, height=40)
        self.mini_cover.pack(side="left", padx=(9, 8), pady=9)
        self.mini_cover.pack_propagate(False)
        self.mini_cover_lbl = tk.Label(self.mini_cover, bg=M_SURF3)
        self._mini_cover_ph = _icon_photo("music", 18, M_ACC_H)
        self.mini_cover_lbl.configure(image=self._mini_cover_ph)
        self.mini_cover_lbl.place(relx=0.5, rely=0.5, anchor="center")
        self.mini_cover_img = tk.Label(self.mini_cover, bg=M_SURF3, bd=0)

        info = tk.Frame(inner, bg=M_SURF)
        info.pack(side="left", fill="x", expand=True)
        self.mini_title = tk.Label(info, text="未在播放", bg=M_SURF, fg=M_TEXT,
                                   font=(core.FONT, 9, "bold"), anchor="w")
        self.mini_title.pack(fill="x", pady=(9, 0))
        self.mini_artist = tk.Label(info, text="", bg=M_SURF, fg=M_MUTED,
                                    font=(core.FONT, 8), anchor="w")
        self.mini_artist.pack(fill="x", pady=(2, 0))

        btns = tk.Frame(inner, bg=M_SURF)
        btns.pack(side="right", padx=(4, 7))
        _text_button(btns, "", self._prev, M_SURF, fg=M_TEXT, size=10,
                     padx=3, icon="skip-back").pack(side="left", padx=1)
        self.mini_play = _RoundButton(btns, "", self._toggle_play, M_SURF, 28)
        self.mini_play.set_icon("play")
        self.mini_play.canvas.pack(side="left", padx=2)
        _text_button(btns, "", self._next, M_SURF, fg=M_TEXT, size=10,
                     padx=3, icon="skip-forward").pack(side="left", padx=1)
        _text_button(btns, "", lambda: self._toggle_mini(False), M_SURF,
                     fg=M_DIM, size=10, padx=3,
                     icon="maximize").pack(side="left", padx=1)

        self.mini_prog = tk.Canvas(m, height=2, bg="#343a46",
                                   highlightthickness=0, bd=0)
        self.mini_prog.place(relx=0, rely=1.0, relwidth=1.0, height=2, anchor="sw")
        self._mini_fill = self.mini_prog.create_rectangle(0, 0, 0, 2, fill=M_ACC,
                                                          width=0)
        self._mini = m
        for widget in (inner, self.mini_cover, self.mini_cover_lbl, info,
                       self.mini_title, self.mini_artist):
            widget.bind("<Button-1>", self._mini_press)
            widget.bind("<B1-Motion>", self._mini_motion)
            widget.bind("<ButtonRelease-1>", self._mini_release)
            widget.bind("<Button-3>", self._mini_menu)
        m.protocol("WM_DELETE_WINDOW", self.close)
        m.bind("<Escape>", lambda e: self._toggle_mini(False))
        self._place_mini()

    def _place_mini(self):
        if self._mini is None:
            return
        L, T, R, B = core.work_area()
        w, h = 380, 64
        pos = self.mcfg.get("mini_pos")
        x = y = None
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                x, y = int(pos[0]), int(pos[1])
            except (TypeError, ValueError):
                x = y = None
        if x is None or not (L <= x <= R - w and T <= y <= B - h):
            x, y = R - w - 24, B - h - 24
        try:
            self._mini.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except tk.TclError:
            pass

    def _mini_press(self, e):
        try:
            self._mini_drag = (e.x_root - self._mini.winfo_x(),
                               e.y_root - self._mini.winfo_y())
        except tk.TclError:
            self._mini_drag = None

    def _mini_motion(self, e):
        if not self._mini_drag:
            return
        x = e.x_root - self._mini_drag[0]
        y = e.y_root - self._mini_drag[1]
        try:
            self._mini.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass

    def _mini_release(self, e):
        if not self._mini_drag:
            return
        self._mini_drag = None
        try:
            self.mcfg["mini_pos"] = [self._mini.winfo_x(), self._mini.winfo_y()]
            save_cfg(self.app.cfg)
        except tk.TclError:
            pass

    def _mini_menu(self, e):
        m = tk.Menu(self._mini, tearoff=0, bg=M_SURF, fg=M_TEXT,
                    activebackground=M_ACC, activeforeground="#ffffff",
                    bd=0, relief="flat", font=(core.FONT, 9))
        m.add_command(label="展开播放器", command=lambda: self._toggle_mini(False))
        m.add_separator()
        m.add_command(label="退出播放器", command=self.close)
        try:
            m.post(e.x_root, e.y_root)
        except tk.TclError:
            pass

    def _toggle_mini(self, show=None):
        if self._mini is None or self._closed:
            return
        on = (not self._mini_shown) if show is None else bool(show)
        if on == self._mini_shown:
            return
        try:
            if on:
                self._paint_now()
                self._paint_progress()
                self._paint_play()
                self.win.withdraw()
                self._mini.deiconify()
                self._mini.lift()
                self._mini.attributes("-topmost", True)
                self._mini.after(30, lambda: core.round_region(
                    self._mini.winfo_id(), 380, 64, 12))
                self._mini_shown = True
            else:
                self._mini.withdraw()
                self._apply_topmost()
                self.win.deiconify()
                self.win.lift()
                self.win.focus_force()
                self._mini_shown = False
        except tk.TclError:
            pass
