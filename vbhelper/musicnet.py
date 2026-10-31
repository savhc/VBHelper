"""在线音乐接口：酷我 / 酷狗 / QQ / 网易 / 咪咕 的搜索、榜单、歌单、歌词、封面。

只负责「找歌」，不负责「取播放直链」——后者由导入的音源脚本（musicapi.py）完成。
这个分工和 lx-music-desktop 一致：它的内置 musicSdk 也只做前者。

各接口的 URL、参数名与字段路径按平台现行协议实现；分页起点（0 起还是 1 起）
与签名要求在各函数注释里单独标注，改动前先核对目标接口。
"""
import html
import json
import math
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import music_boards
from .musiccrypto import aes_ecb_encrypt, md5

TIMEOUT = 12.0
RETRY = 2

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/69.0.3497.100 Safari/537.36")
UA_IE9 = "Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; WOW64; Trident/5.0)"
UA_LINUX = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36")
UA_MG = ("Mozilla/5.0 (Linux; U; Android 11.0.0; zh-cn; MI 11 Build/OPR1.170623.032) "
         "AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30")
UA_KG = "KuGou2012-9020-ExpandSearchManager"

SOURCES = ("kw", "kg", "tx", "wy", "mg")

# 酷我 N_MINFO 里的 bitrate 数字到档位的映射（kw/musicSearch.js:44-76）
KW_BITRATE = {"4000": "flac24bit", "2000": "flac", "320": "320k", "128": "128k"}
KW_MINFO = re.compile(r"level:(\w+),bitrate:(\d+),format:(\w+),size:([\w.]+)")
# 咪咕档位（mg/musicSearch.js:139-171，旧接口用 ZQ，新接口用 ZQ24）
MG_FORMAT = {"PQ": "128k", "HQ": "320k", "SQ": "flac", "ZQ24": "flac24bit", "ZQ": "flac"}

QUALITY_ORDER = ("flac24bit", "flac", "320k", "128k")

class NetError(Exception):
    """接口层统一异常，消息直接显示给用户。"""

# ---------------------------------------------------------------- 公共小件

def unescape(text):
    """等价 lx 的 renderer 版 decodeName（DOMParser 解 HTML 实体）。"""
    if not text:
        return ""
    try:
        return html.unescape(str(text))
    except Exception:
        return str(text)

def kw_singer(text):
    """酷我用裸 & 分隔多歌手（kw/util.js:47）。"""
    return unescape(text or "").replace("&", "、")

def join_names(items, key="name", sep="、"):
    if isinstance(items, str):
        return unescape(items)
    if not isinstance(items, (list, tuple)):
        return unescape(str(items or ""))
    names = []
    for it in items:
        if isinstance(it, dict):
            val = it.get(key)
            if val:
                names.append(str(val))
        elif it:
            names.append(str(it))
    return unescape(sep.join(names))

def size_formate(size):
    """字节数转可读大小：KiB/MiB 一档，保留两位小数。"""
    try:
        size = float(size)
    except (TypeError, ValueError):
        return "0 B"
    if not size:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    n = min(4, int(math.floor(math.log(size) / math.log(1024))))
    return "%.2f %s" % (size / (1024 ** n), units[n])

def play_time(sec):
    """common/utils/common.ts:62-66，秒转 mm:ss。"""
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return "--/--"
    return "%02d:%02d" % (sec // 60, sec % 60)

def _order_qualitys(sizes):
    return [q for q in QUALITY_ORDER if q in sizes]

def _req(url, method="get", headers=None, data=None, timeout=TIMEOUT):
    hdr = {"User-Agent": UA_PC}
    if headers:
        for k, v in headers.items():
            hdr[k] = v
    req = urllib.request.Request(url, headers=hdr, data=data,
                                 method=method.upper())
    # 统一 opener：certifi CA + 与音源宿主同源代理（被污染域名直连会 getaddrinfo 失败）
    from . import musicapi
    with musicapi.music_opener().open(req, timeout=timeout) as resp:
        raw = resp.read(16 * 1024 * 1024)
        ctype = (resp.headers.get("Content-Type") or "").lower()
    return raw, ctype

def _call_bytes(url, method="get", headers=None, data=None):
    """要拿原始字节（酷我歌词是 gzip 二进制）必须走这条，文本解码会毁掉它。"""
    err = None
    for _ in range(RETRY + 1):
        try:
            raw, ctype = _req(url, method, headers, data)
            enc = ""
            if "gzip" in ctype or raw[:2] == b"\x1f\x8b":
                try:
                    raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
                except zlib.error:
                    pass
            return raw
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
            if e.code in (404, 403):
                break
        except Exception as e:
            err = str(e) or e.__class__.__name__
        time.sleep(0.25)
    raise NetError(err or "请求失败")

def _call(url, method="get", headers=None, data=None):
    """带重试的请求，返回 (文本, content-type)。一律按 UTF-8 解（对齐 request.js:50）。"""
    raw = _call_bytes(url, method, headers, data)
    return raw.decode("utf-8", "replace"), ""

def _json_of(text):
    """lx 行为：一律 utf-8 文本再 JSON.parse，失败就返回字符串（request.js:48-57）。"""
    try:
        return json.loads(text)
    except ValueError:
        return text

def get_json(url, headers=None):
    text, _ = _call(url, "get", headers)
    out = _json_of(text)
    if isinstance(out, str):
        out = _obj_str2json(out)
    if out is None or out == "" or (isinstance(out, str) and not out):
        raise NetError("响应是空的")
    return out

def post_json(url, obj, headers=None):
    """application/json；发送的字节必须与签名用的是同一份。"""
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    text, _ = _call(url, "post", hdr, body)
    out = _json_of(text)
    if isinstance(out, str) and out.lstrip().startswith("{"):
        out = _obj_str2json(out)
    return out

def post_form(url, form, headers=None):
    body = urllib.parse.urlencode(form).encode("utf-8")
    hdr = {"Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        hdr.update(headers)
    text, _ = _call(url, "post", hdr, body)
    out = _json_of(text)
    if isinstance(out, str) and out.lstrip().startswith("{"):
        out = _obj_str2json(out)
    return out

def _obj_str2json(text):
    """酷我部分接口返回单引号伪 JSON（kw/util.js:42-44 的 objStr2JSON）。

    源码用了变宽后视断言，Python 的 re 直接报错，所以按同样的判定规则手写扫描：
    引号前一个非空白字符是 { : , 或引号后一个非空白字符是 : , } 时，它才是字符串定界符。
    """
    guard = text.replace("\\'", "\x01")
    out = []
    for i, ch in enumerate(guard):
        if ch == "'":
            prev = guard[:i].rstrip()
            nxt = guard[i + 1:].lstrip()
            prev_c = prev[-1] if prev else ""
            next_c = nxt[0] if nxt else ""
            if prev_c in "{:,[" or next_c in ":,}]" or not prev_c or not next_c:
                out.append('"')
                continue
        out.append(ch)
    try:
        return json.loads("".join(out).replace("\x01", "'"))
    except ValueError:
        return text

def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _track(source, songmid, name, singer, album="", albumid="", seconds=0,
           sizes=None, pic="", **extra):
    sizes = {k: v for k, v in (sizes or {}).items() if v is not None}
    out = {
        "source": source, "songmid": str(songmid or ""),
        "name": name, "singer": singer, "albumName": album, "albumid": str(albumid or ""),
        "dur": int(seconds or 0), "interval": play_time(seconds),
        "qualitys": _order_qualitys(sizes), "sizes": sizes, "pic": pic or "",
    }
    out.update(extra)
    return out

# ---------------------------------------------------------------- kw 酷我

def kw_search(keyword, page=1, limit=30):
    """kw/musicSearch.js:17-20。page 从 1 开始，传给服务器的 pn 从 0 开始。"""
    url = ("http://search.kuwo.cn/r.s?client=kt&all=%s&pn=%d&rn=%d&uid=794762570"
           "&ver=kwplayer_ar_9.2.2.1&vipver=1&show_copyright_off=1&newver=1"
           "&ft=music&cluster=0&strategy=2012&encoding=utf8&rformat=json"
           "&vermerge=1&mobi=1&issubtitle=1" % (urllib.parse.quote(keyword),
                                               page - 1, limit))
    last = None
    for _ in range(RETRY + 1):
        body = get_json(url)
        if not isinstance(body, dict):
            last = "响应格式不对"
            continue
        total = str(body.get("TOTAL", ""))
        show = str(body.get("SHOW", ""))
        if total != "0" and show == "0":
            last = "这一页没有结果"
            continue
        rows = _kw_filter_list(body.get("abslist"))
        if rows is None:
            last = "响应里缺少音质字段"
            continue
        total = int(_num(total, 0))
        return {"list": rows, "total": total,
                "allPage": max(1, -(-total // limit)), "page": page}
    raise NetError(last or "搜索失败")

def _kw_filter_list(raw):
    """kw/musicSearch.js:27-101：N_MINFO 里按 bitrate 分档。

    缺 N_MINFO 就整体失败让上层重试（源码 return null），半截结果会让音质列莫名缺失。
    """
    rows = []
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict):
            return None
        nminfo = item.get("N_MINFO")
        if not nminfo:
            return None
        sizes = {}
        for part in str(nminfo).split(";"):
            m = KW_MINFO.search(part)
            if not m:
                continue
            quality = KW_BITRATE.get(m.group(2))
            if quality:
                sizes[quality] = m.group(4).upper()
        songmid = str(item.get("MUSICRID", "")).replace("MUSIC_", "")
        rows.append(_track(
            "kw", songmid,
            unescape(item.get("SONGNAME") or item.get("NAME")),
            kw_singer(item.get("ARTIST")),
            unescape(item.get("ALBUM")), unescape(item.get("ALBUMID")),
            _num(item.get("DURATION")), sizes))
    return rows

def _wbd_build(data):
    """kw/util.js:193-225 wbdCrypto.buildParam：AES-128-ECB + MD5 大写签名。"""
    from .musiccrypto import aes_ecb_encrypt as enc
    key = bytes([112, 87, 39, 61, 199, 250, 41, 191, 57, 68, 45, 114, 221, 94, 140, 228])
    app_id = "y67sprxhhpws"
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = enc(text.encode("utf-8"), key)
    import base64
    b64 = base64.b64encode(encoded).decode("ascii")
    ts = int(time.time() * 1000)
    sign = md5("%s%s%d" % (app_id, b64, ts)).upper()
    return "data=%s&time=%d&appId=%s&sign=%s" % (
        urllib.parse.quote(b64), ts, app_id, sign)

def _wbd_decode(text):
    import base64
    key = bytes([112, 87, 39, 61, 199, 250, 41, 191, 57, 68, 45, 114, 221, 94, 140, 228])
    from .musiccrypto import aes_ecb_decrypt
    body = _json_of(text)
    if isinstance(body, str):
        body = body.strip()
    else:
        body = json.dumps(body)
    data = base64.b64decode(urllib.parse.unquote(body).strip().strip('"'))
    return json.loads(aes_ecb_decrypt(data, key).decode("utf-8", "replace"))

def kw_board_songs(bangid, page=1, limit=100):
    """酷我榜单。

    先用明文接口 kbangserver.kuwo.cn/ksong.s（源码里被注释掉的那条）：一次就够、
    不用解密；它失效再回落到 wbd 的 AES 接口——纯 Python 解一份榜单要 50 秒，
    当兜底可以，当主路不行。
    """
    try:
        return _kw_board_plain(bangid, page, limit)
    except Exception:
        return _kw_board_wbd(bangid, page, limit)

def _kw_board_plain(bangid, page=1, limit=100):
    url = ("http://kbangserver.kuwo.cn/ksong.s?from=pc&fmt=json&pn=%d&rn=%d"
           "&type=bang&data=content&show_copyright_off=0&pcmp4=1&isbang=1&id=%s"
           % (page - 1, limit, bangid))
    body = get_json(url)
    if not isinstance(body, dict) or not body.get("musiclist"):
        raise NetError("榜单是空的")
    rows = []
    for item in body.get("musiclist") or []:
        seconds = _num(item.get("song_duration")) or _num(item.get("duration"))
        rows.append(_track("kw", item.get("id"), unescape(item.get("name")),
                           kw_singer(item.get("artist")),
                           unescape(item.get("album")), item.get("albumid"),
                           seconds, {}))
    total = int(_num(body.get("num"), len(rows)))
    return {"list": rows, "total": total, "allPage": 1, "page": page,
            "info": {"name": unescape(body.get("name"))}}

def _kw_board_wbd(bangid, page=1, limit=100):
    """kw/leaderboard.js:200-224 + kw/util.js:193-225。"""
    payload = {"uid": "", "devId": "", "sFrom": "kuwo_sdk", "user_type": "AP",
               "carSource": "kwplayercar_ar_6.0.1.0_apk_keluze.apk",
               "id": str(bangid), "pn": page - 1, "rn": limit}
    url = "https://wbd.kuwo.cn/api/bd/bang/bang_info?" + _wbd_build(payload)
    text, _ = _call(url)
    try:
        raw = _wbd_decode(text)
    except Exception as e:
        raise NetError("榜单解密失败：%s" % e)
    if not isinstance(raw, dict) or raw.get("code") != 200:
        raise NetError("榜单返回异常")
    data = raw.get("data") or {}
    rows = []
    for item in data.get("musiclist") or []:
        sizes = {}
        for part in str(item.get("n_minfo") or "").split(";"):
            m = KW_MINFO.search(part)
            if m and KW_BITRATE.get(m.group(2)):
                sizes[KW_BITRATE[m.group(2)]] = m.group(4).upper()
        rows.append(_track("kw", item.get("id"), unescape(item.get("name")),
                           kw_singer(item.get("artist")), unescape(item.get("album")),
                           item.get("albumId"), _num(item.get("duration")), sizes,
                           pic=item.get("pic") or ""))
    total = int(_num(data.get("total"), len(rows)))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

def kw_songlists(tag_id="", tag_type="", page=1, limit=36):
    """kw/songList.js:31-38。pn 这里从 1 开始（不 -1）。"""
    if not tag_id:
        url = ("http://wapi.kuwo.cn/api/pc/classify/playlist/getRcmPlayList"
               "?loginUid=0&loginSid=0&appUid=76039576&&pn=%d&rn=%d&order=%s"
               % (page, limit, tag_type or "hot"))
    elif str(tag_type) == "43":
        url = ("http://mobileinterfaces.kuwo.cn/er.s?type=get_pc_qz_data&f=web"
               "&id=%s&prod=pc" % tag_id)
    else:
        url = ("http://wapi.kuwo.cn/api/pc/classify/playlist/getTagPlayList"
               "?loginUid=0&loginSid=0&appUid=76039576&pn=%d&id=%s&rn=%d"
               % (page, tag_id, limit))
    body = get_json(url)
    rows = []
    total = 0
    if str(tag_type) == "43" and isinstance(body, list):
        for group in body:
            for item in (group.get("list") or []):
                if item.get("type") not in ("songlist", "list", "album"):
                    continue
                rows.append(_plist(item, "kw"))
        total = len(rows)
    else:
        data = (body or {}).get("data") or {}
        for item in data.get("data") or []:
            rows.append(_plist(item, "kw"))
        total = int(_num(data.get("total"), len(rows)))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

def _plist(item, source):
    return {
        "source": source, "id": str(item.get("id") or item.get("playlistid") or ""),
        "name": unescape(item.get("name") or item.get("nickname")),
        "author": unescape(item.get("uname") or item.get("creator") or ""),
        "count": int(_num(item.get("songnum") or item.get("song_total") or
                          item.get("total"))),
        "plays": int(_num(item.get("listencnt") or item.get("playcnt"))),
        "img": item.get("img") if str(item.get("img", "")).startswith("http") else "",
        "desc": unescape(item.get("desc") or item.get("intro") or ""),
    }

def kw_songlist_search(text, page=1, limit=30):
    """kw/songList.js:462-486，响应是单引号伪 JSON。"""
    url = ("http://search.kuwo.cn/r.s?all=%s&pn=%d&rn=%d&rformat=json&encoding=utf8"
           "&ver=mbox&vipver=MUSIC_8.7.7.0_BCS37&plat=pc&devid=28156413"
           "&ft=playlist&pay=0&needliveshow=0"
           % (urllib.parse.quote(text), page - 1, limit))
    body = get_json(url)
    if not isinstance(body, dict):
        raise NetError("歌单搜索响应格式不对")
    rows = [_plist(x, "kw") for x in body.get("abslist") or []]
    total = int(_num(body.get("TOTAL")))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

def kw_songlist_detail(pid, page=1, limit=1000):
    """kw/songList.js:39-43。"""
    url = ("http://nplserver.kuwo.cn/pl.svc?op=getlistinfo&pid=%s&pn=%d&rn=%d"
           "&encode=utf8&keyset=pl2012&identity=kuwo&pcmp4=1"
           "&vipver=MUSIC_9.0.5.0_W1&newver=1" % (pid, page - 1, limit))
    body = get_json(url)
    if not isinstance(body, dict) or body.get("result") != "ok":
        raise NetError("歌单取不到")
    rows = []
    for item in body.get("musiclist") or []:
        sizes = {}
        for part in str(item.get("N_MINFO") or "").split(";"):
            m = KW_MINFO.search(part)
            if m and KW_BITRATE.get(m.group(2)):
                sizes[KW_BITRATE[m.group(2)]] = m.group(4).upper()
        rows.append(_track("kw", item.get("id"), unescape(item.get("name")),
                           kw_singer(item.get("artist")), unescape(item.get("album")),
                           item.get("albumid"), _num(item.get("duration")), sizes))
    total = int(_num(body.get("total"), len(rows)))
    return {"list": rows, "total": total, "allPage": 1, "page": 1,
            "info": {"name": unescape(body.get("title")),
                     "img": body.get("pic") or "",
                     "author": unescape(body.get("uname")),
                     "desc": unescape(body.get("info"))}}

def kw_songlist_id(url):
    """从酷我歌单链接里抠 pid。"""
    m = re.search(r"[?&]pid=(\d+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"/(\d{5,})", url or "")
    return m.group(1) if m else ""

def kw_lyric(songmid, word=False):
    """kw/lyric.js:228-261 + kw/util.js:21-40：inflate 之后还要 base64 + XOR 'yeelion'。

    实测 lrcx=0 也回的是编码过的 base64 文本（源码在非逐字分支直接 toString 返回，
    这里按实际响应统一解码），不这样处理歌词就是一串乱码。
    """
    url = ("http://mlyric.kuwo.cn/mobi.s?f=web&type=lyric&lrcx=%d&rid=%s&encode=utf8"
           % (1 if word else 0, songmid))
    raw = _call_bytes(url)
    if raw[:10].decode("ascii", "replace").lower() != "tp=content":
        return ""
    idx = raw.find(b"\r\n\r\n")
    if idx < 0:
        return ""
    try:
        body = zlib.decompress(raw[idx + 4:])
    except zlib.error:
        try:
            body = zlib.decompressobj(-15).decompress(raw[idx + 4:])
        except zlib.error:
            return ""
    text = body.decode("utf-8", "replace")
    if "[" in text[:1] or text.lstrip().startswith("["):
        return text
    import base64
    try:
        data = base64.b64decode(text)
    except Exception:
        return text
    key = b"yeelion"
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i % len(key)]
    return bytes(out).decode("utf-8", "replace")

def kw_pic(songmid):
    """kw/pic.js:4-8，返回纯文本 URL。"""
    text, _ = _call("http://artistpicserver.kuwo.cn/pic.web?corp=kuwo&type=rid_pic"
                    "&pictype=500&size=500&rid=%s" % songmid)
    text = (text or "").strip()
    return text if text.startswith("http") else ""

def kw_tip(keyword):
    body = get_json("https://tips.kuwo.cn/t.s?corp=kuwo&newver=3&p2p=1&notrace=0"
                    "&c=mbox&w=%s&encoding=utf8&rformat=json"
                    % urllib.parse.quote(keyword))
    if isinstance(body, dict):
        return [unescape(x.get("RELWORD")) for x in body.get("WORDITEMS") or []
                if x.get("RELWORD")]
    return []

# ---------------------------------------------------------------- kg 酷狗

def kg_search(keyword, page=1, limit=30):
    """kg/musicSearch.js:11-14，此接口无签名。Grp 子项要一并展开。"""
    url = ("http://songsearch.kugou.com/song_search_v2?platform=AndroidFilter"
           "&iscorrection=1&keyword=%s&hifiquality=0&pagesize=%d"
           "&PrivilegeFilter=0&page=%d" % (urllib.parse.quote(keyword), limit, page))
    body = get_json(url)
    if not isinstance(body, dict) or body.get("error_code") != 0:
        raise NetError("搜索失败：%s" % (body or {}).get("error_msg", "响应异常"))
    data = body.get("data") or {}
    items = []
    for item in data.get("lists") or []:
        items.append(item)
        for grp in item.get("Grp") or []:
            items.append(grp)
    seen = set()
    rows = []
    for item in items:
        key = (item.get("Audioid"), item.get("FileHash"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(_kg_track(item))
    total = int(_num(data.get("total"), len(rows)))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

def _kg_track(item):
    """kg/musicSearch.js:15-68：四档取各自 hash。"""
    sizes = {}
    if _num(item.get("FileSize")):
        sizes["128k"] = size_formate(item.get("FileSize"))
    if _num(item.get("HQFileSize")):
        sizes["320k"] = size_formate(item.get("HQFileSize"))
    if _num(item.get("SQFileSize")):
        sizes["flac"] = size_formate(item.get("SQFileSize"))
    if _num(item.get("ResFileSize")):
        sizes["flac24bit"] = size_formate(item.get("ResFileSize"))
    suffix = item.get("Suffix") or ""
    name = unescape("%s%s" % (item.get("OriSongName") or item.get("SongName") or "",
                              (" " + suffix) if suffix else ""))
    seconds = _num(item.get("Duration"))
    return _track("kg", item.get("Audioid"), name,
                  join_names(item.get("Singers"), "name"),
                  unescape(item.get("AlbumName")), item.get("AlbumID"), seconds, sizes,
                  hash=item.get("FileHash") or "",
                  albumAudioId=item.get("MixSongID") or "")

def kg_board_songs(rankid, page=1, limit=100):
    """kg/leaderboard.js:66-68，无签名。page 从 1 开始。"""
    url = ("http://mobilecdnbj.kugou.com/api/v3/rank/song?version=9108&ranktype=1"
           "&plat=0&pagesize=%d&area_code=1&page=%d&rankid=%s&with_res_tag=0"
           "&show_portrait_mv=1" % (limit, page, rankid))
    body = get_json(url)
    if not isinstance(body, dict) or body.get("errcode") != 0:
        raise NetError("榜单取不到")
    data = body.get("data") or {}
    rows = []
    for item in data.get("info") or []:
        sizes = {}
        if _num(item.get("filesize")):
            sizes["128k"] = size_formate(item.get("filesize"))
        if _num(item.get("320filesize")):
            sizes["320k"] = size_formate(item.get("320filesize"))
        if _num(item.get("sqfilesize")):
            sizes["flac"] = size_formate(item.get("sqfilesize"))
        if _num(item.get("filesize_high")):
            sizes["flac24bit"] = size_formate(item.get("filesize_high"))
        rows.append(_track("kg", item.get("audio_id") or item.get("hash"),
                           unescape(item.get("songname")),
                           join_names(item.get("authors"), "author_name"),
                           unescape(item.get("remark")), item.get("album_id"),
                           _num(item.get("duration")), sizes,
                           hash=item.get("hash") or "",
                           albumAudioId=item.get("album_audio_id") or ""))
    total = int(_num(data.get("total"), len(rows)))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

KG_LRC_HEADERS = {"KG-RC": "1",
                  "KG-THash": "expand_search_manager.cpp:852736169:451",
                  "User-Agent": UA_KG}

def kg_lyric(songmid, info):
    """kg/lyric.js：先搜候选再下载。lrc 直接 base64，krc 走 XOR16+inflate。"""
    keyword = "%s %s" % (_tget(info, "singer", "artist"), _tget(info, "name", "title"))
    url = ("http://lyrics.kugou.com/search?ver=1&man=yes&client=pc&keyword=%s"
           "&hash=%s&timelength=%d&lrctxt=1"
           % (urllib.parse.quote(keyword.strip()), info.get("hash") or songmid,
              int(_num(info.get("dur")))))
    body = get_json(url, KG_LRC_HEADERS)
    cand = ((body or {}).get("candidates") or [{}])[0]
    cid, access = cand.get("id"), cand.get("accesskey")
    if not cid or not access:
        return ""
    fmt = "lrc" if cand.get("contenttype") in (1, "1", None) and \
        cand.get("krctype") != 1 else "krc"
    for use in ([fmt, "lrc" if fmt == "krc" else "krc"]):
        got = _kg_download(cid, access, use)
        if got:
            return got
    return ""

def _kg_download(cid, access, fmt):
    url = ("http://lyrics.kugou.com/download?ver=1&client=pc&id=%s&accesskey=%s"
           "&fmt=%s&charset=utf8" % (cid, access, fmt))
    try:
        body = get_json(url, KG_LRC_HEADERS)
    except NetError:
        return ""
    content = (body or {}).get("content") or ""
    if not content:
        return ""
    import base64
    raw = base64.b64decode(content)
    if fmt == "lrc":
        return raw.decode("utf-8", "replace")
    key = bytes([0x40, 0x47, 0x61, 0x77, 0x5e, 0x32, 0x74, 0x47,
                 0x51, 0x36, 0x31, 0x2d, 0xce, 0xd2, 0x6e, 0x69])
    buf = bytearray(raw[4:])
    for i in range(len(buf)):
        buf[i] ^= key[i % 16]
    try:
        text = zlib.decompress(bytes(buf)).decode("utf-8", "replace")
    except zlib.error:
        return ""
    return _krc_to_lrc(text)

def _krc_to_lrc(text):
    """KRC 的 [ms,dur] 行时间转成普通 LRC；逐字 () 标记丢掉，行级歌词够用。"""
    out = []
    for line in (text or "").splitlines():
        if line.startswith("[id:$") or not line.startswith("["):
            continue
        m = re.match(r"^\[(\d+),(\d+)\](.*)$", line)
        if not m:
            out.append(line)
            continue
        ms = int(m.group(1))
        body = re.sub(r"\(-?\d+,-?\d+\)", "", m.group(3))
        out.append("[%02d:%02d.%02d]%s" % (ms // 60000, (ms // 1000) % 60,
                                           (ms % 1000) // 10, unescape(body)))
    return "\n".join(out)

def kg_pic(info):
    """kg/pic.js:4-46，POST 特殊体。"""
    songmid = str(info.get("songmid") or "")
    audio_id = songmid.split("_")[0] if len(songmid) == 32 else songmid
    body = post_json("http://media.store.kugou.com/v1/get_res_privilege", {
        "appid": 1001, "area_code": "1", "behavior": "play", "clientver": "9020",
        "need_hash_offset": 1, "relate": 1,
        "resource": [{"album_audio_id": info.get("albumAudioId") or audio_id,
                      "album_id": info.get("albumid") or "",
                      "hash": info.get("hash") or "", "id": 0,
                      "name": "%s - %s.mp3" % (_tget(info, "singer", "artist"),
                                               _tget(info, "name", "title")),
                      "type": "audio"}],
        "token": "", "userid": 2626431536, "vip": 1,
    }, KG_LRC_HEADERS)
    try:
        item = body["data"][0]["info"]
    except (KeyError, IndexError, TypeError):
        return ""
    image = item.get("image") or ""
    if item.get("imgsize"):
        image = image.replace("{size}", str(item["imgsize"][0]))
    return image if str(image).startswith("http") else ""

# ---------------------------------------------------------------- tx QQ音乐

def _zzc_sign(text):
    """tx/utils/crypto.js:3-27 zzcSign：sha1 后按下标取字符 + 异或字节 base64。"""
    import hashlib
    p1_idx = [23, 14, 6, 36, 16, 40, 7, 19]
    p2_idx = [16, 1, 32, 12, 19, 27, 8, 5]
    scr = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64,
           242, 133, 143, 161, 121, 179]
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()
    # 源码下标里有 40，而 sha1 hex 只有 40 位（0~39）；JS 越界取到 undefined，
    # Array.join 对越界下标返回空串，此处保持一致，不抛异常
    part1 = "".join(h[i] for i in p1_idx if i < len(h))
    part2 = "".join(h[i] for i in p2_idx if i < len(h))
    raw = bytes([v ^ int(h[i * 2:i * 2 + 2], 16) for i, v in enumerate(scr)])
    import base64
    b64 = re.sub(r"[\\/+=]", "", base64.b64encode(raw).decode("ascii"))
    return ("zzc%s%s%s" % (part1, b64, part2)).lower()

def _search_id():
    return "".join("%x" % random.randrange(16) for _ in range(32)).upper() + \
        str(random.randrange(100000)).zfill(5)

def tx_search(keyword, page=1, limit=50):
    """tx/musicSearch.js:13-51，musics.fcg 需要 zzcSign。"""
    data = {
        "comm": {
            "_channelid": "0", "_os_version": "6.2.9200-2", "ct": "19", "cv": "2151",
            "guid": "1F70E520B2EAA7D25E11760783C53CA9", "patch": "118",
            "psrf_access_token_expiresAt": 0, "psrf_qqaccess_token": "",
            "psrf_qqopenid": "", "psrf_qqunionid": "", "tmeAppID": "qqmusic",
            "tmeLoginType": 0, "uin": "0", "wid": "7223299733393904640",
        },
        "music.search.SearchCgiService": {
            "module": "music.search.SearchCgiService",
            "method": "DoSearchForQQMusicDesktop",
            "param": {"grp": 1, "num_per_page": limit, "page_num": page,
                      "query": keyword, "remoteplace": "txt.newclient.top",
                      "search_type": 0, "searchid": _search_id()},
        },
    }
    body_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    url = "https://u.y.qq.com/cgi-bin/musics.fcg?sign=" + _zzc_sign(body_bytes)
    out = post_json(url, data, {"User-Agent": "QQMusic 14090508(android 12)"})
    service = (out or {}).get("music.search.SearchCgiService") or {}
    if service.get("code") not in (0, None) or (service.get("data") or {}).get("code") \
            not in (0, None):
        raise NetError("搜索被拒（可能需要换源）")
    inner = service.get("data") or {}
    meta = inner.get("meta") or {}
    songlist = ((inner.get("body") or {}).get("song") or {}).get("list") or []
    rows = [_tx_track(x) for x in songlist]
    total = int(_num(meta.get("sum"), len(rows)))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

def _tx_track(item):
    """tx/musicSearch.js:62-129。"""
    fileinfo = item.get("file") or {}
    sizes = {}
    for key, quality in (("size_128mp3", "128k"), ("size_320mp3", "320k"),
                         ("size_flac", "flac"), ("size_hires", "flac24bit")):
        if _num(fileinfo.get(key)):
            sizes[quality] = size_formate(fileinfo.get(key))
    album = item.get("album") or {}
    album_id = album.get("mid") or ""
    singers = item.get("singer") or []
    if album_id:
        pic = "https://y.gtimg.cn/music/photo_new/T002R500x500M000%s.jpg" % album_id
    elif singers:
        pic = "https://y.gtimg.cn/music/photo_new/T001R500x500M000%s.jpg" % \
            (singers[0].get("mid") or "")
    else:
        pic = ""
    return _track("tx", item.get("mid"), unescape(item.get("title")),
                  join_names(singers), unescape(album.get("name")), album_id,
                  _num(item.get("interval")), sizes, pic=pic,
                  songId=item.get("id"), strMediaMid=fileinfo.get("media_mid") or "",
                  albumMid=album_id)

def tx_board_songs(topid, page=1, limit=300, period=""):
    """tx/leaderboard.js:66-91，musicu.fcg 无 sign，一次取满不分页。"""
    body = post_json("https://u.y.qq.com/cgi-bin/musicu.fcg", {
        "toplist": {"module": "musicToplist.ToplistInfoServer",
                    "method": "GetDetail",
                    "param": {"topid": int(topid), "num": limit, "period": period}},
        "comm": {"uin": 0, "format": "json", "ct": 20, "cv": 1859},
    }, {"User-Agent": UA_IE9})
    songs = (((body or {}).get("toplist") or {}).get("data") or {}).get("songInfoList") or []
    rows = [_tx_track(x) for x in songs]
    return {"list": rows, "total": len(rows), "allPage": 1, "page": 1}

def tx_songlist_search(text, page=1, limit=30):
    """tx/songList.js:399-430，page_no 从 0 开始。"""
    url = ("http://c.y.qq.com/soso/fcgi-bin/client_music_search_songlist?page_no=%d"
           "&num_per_page=%d&format=json&query=%s&remoteplace=txt.yqq.playlist"
           "&inCharset=utf8&outCharset=utf-8"
           % (page - 1, limit, urllib.parse.quote(text)))
    body = get_json(url, {"User-Agent": UA_IE9,
                          "Referer": "http://y.qq.com/portal/search.html"})
    data = (body or {}).get("data") or {}
    rows = []
    for item in data.get("list") or []:
        rows.append({"source": "tx", "id": str(item.get("dissid") or ""),
                     "name": unescape(item.get("dissname")),
                     "author": unescape(((item.get("creator") or {}).get("name"))),
                     "count": int(_num(item.get("song_count"))),
                     "plays": int(_num(item.get("listennum"))),
                     "img": item.get("imgurl") if str(item.get("imgurl", "")).startswith("http") else "",
                     "desc": unescape(item.get("introduction"))})
    total = int(_num(data.get("sum"), len(rows)))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

def tx_songlist_detail(disstid, page=1):
    """tx/songList.js:63-65。一次取完不分页。"""
    url = ("https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?type=1"
           "&json=1&utf8=1&onlysong=0&new_format=1&disstid=%s&loginUin=0&hostUin=0"
           "&format=json&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json"
           "&needNewCode=0" % disstid)
    body = get_json(url, {"Origin": "https://y.qq.com",
                          "Referer": "https://y.qq.com/n/yqq/playsquare/%s.html" % disstid})
    cdlist = (body or {}).get("cdlist") or []
    if not cdlist:
        raise NetError("歌单不存在或已被删除")
    cd = cdlist[0]
    rows = [_tx_track(x) for x in cd.get("songlist") or []]
    desc = unescape(cd.get("desc") or "").replace("<br>", "\n")
    return {"list": rows, "total": int(_num(cd.get("totalnum"), len(rows))),
            "allPage": 1, "page": 1,
            "info": {"name": unescape(cd.get("dissname")), "img": cd.get("logo") or "",
                     "author": unescape(cd.get("nickname")), "desc": desc}}

def tx_songlist_id(url):
    m = re.search(r"[?&](disstid|id)=(\d+)", url or "")
    if m:
        return m.group(2)
    m = re.search(r"/(\d{6,})", url or "")
    return m.group(1) if m else ""

# ---------------------------------------------------------------- wy 网易云

EAPI_KEY = b"e82ckenh8dichen8"
WY_HEADERS = {"User-Agent": UA_LINUX, "origin": "https://music.163.com"}

def _eapi(inner_url, obj):
    """wy/utils/crypto.js:42-50：nobody{url}use{text}md5forencrypt。"""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    digest = md5("nobody%suse%smd5forencrypt" % (inner_url, text))
    data = "%s-36cd479b6b5-%s-36cd479b6b5-%s" % (inner_url, text, digest)
    return {"params": aes_ecb_encrypt(data.encode("utf-8"), EAPI_KEY).hex().upper()}

def wy_search(keyword, page=1, limit=30):
    """wy/musicSearch.js:20-29。offset 语义，page 从 1。"""
    form = _eapi("/api/search/song/list/page", {
        "keyword": keyword, "needCorrect": "1", "channel": "typing",
        "offset": limit * (page - 1), "scene": "normal",
        "total": page == 1, "limit": limit})
    body = post_form("http://interface.music.163.com/eapi/batch", form, WY_HEADERS)
    data = (body or {}).get("data") or {}
    rows = []
    for res in data.get("resources") or []:
        item = (res or {}).get("baseInfo") or {}
        rows.append(_wy_track(item.get("simpleSongData") or {}))
    rows = [r for r in rows if r["songmid"]]
    total = int(_num(data.get("totalCount"), len(rows)))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

def _wy_track(item):
    """wy/musicSearch.js:47-92，注意 maxbr 的 switch 是故意落穿的。"""
    al = item.get("al") or {}
    ar = item.get("ar") or []
    priv = item.get("privilege") or {}
    sizes = {}
    if priv.get("maxBrLevel") == "hires" and (item.get("hr") or {}).get("size"):
        sizes["flac24bit"] = size_formate(item["hr"].get("size"))
    maxbr = _num(priv.get("maxbr"))
    if maxbr >= 999000 and (item.get("sq") or {}).get("size"):
        sizes["flac"] = size_formate(item["sq"].get("size"))
    if maxbr >= 320000 and (item.get("h") or {}).get("size"):
        sizes["320k"] = size_formate(item["h"].get("size"))
    if (item.get("l") or {}).get("size"):
        sizes["128k"] = size_formate(item["l"].get("size"))
    return _track("wy", item.get("id"), unescape(item.get("name")),
                  join_names(ar), unescape(al.get("name")), al.get("id"),
                  _num(item.get("dt")) / 1000.0, sizes, pic=al.get("picUrl") or "")

def wy_lyric(songmid):
    """wy/lyric.js:37-55 + 270-292：外层路径与内层 url 必须都是 /api/song/lyric/v1。"""
    form = _eapi("/api/song/lyric/v1", {"id": str(songmid), "cp": False, "tv": 0,
                                        "lv": 0, "rv": 0, "kv": 0, "yv": 0,
                                        "ytv": 0, "yrv": 0})
    body = post_form("https://interface3.music.163.com/eapi/song/lyric/v1",
                     form, WY_HEADERS)
    if not isinstance(body, dict) or body.get("code") != 200:
        return ""
    lyric = ((body.get("lrc") or {}).get("lyric")) or ""
    if not lyric:
        return ""
    return re.sub(r"\[(\d{1,3}):(\d{2}):(\d{2})\]", r"[\1:\2.\3]", lyric)

# ---------------------------------------------------------------- mg 咪咕

MG_DEVICE_ID = "963B7AA0D21511ED807EE5846EC87D20"
MG_SIGN_KEY = "6cdc72a439cef99a3418d2a78aa28c73"
MG_APP_KEY = "yyapp2d16148780a1dcc7408e06336b98cfd50"

def _mg_sign(text, ts):
    """mg/musicSearch.js:5-10。"""
    return md5("%s%s%s%s%s" % (text, MG_SIGN_KEY, MG_APP_KEY, MG_DEVICE_ID, ts))

def mg_search(keyword, page=1, limit=20):
    ts = str(int(time.time() * 1000))
    url = ("https://jadeite.migu.cn/music_search/v3/search/searchAll?isCorrect=0"
           "&isCopyright=1&searchSwitch=%%7B%%22song%%22%%3A1%%2C%%22album%%22%%3A0"
           "%%2C%%22singer%%22%%3A0%%2C%%22tagSong%%22%%3A1%%2C%%22mvSong%%22%%3A0"
           "%%2C%%22bestShow%%22%%3A1%%2C%%22songlist%%22%%3A0%%2C%%22lyricSong%%22%%3A0"
           "%%7D&pageSize=%d&text=%s&pageNo=%d&sort=0&sid=USS"
           % (limit, urllib.parse.quote(keyword), page))
    body = get_json(url, {"uiVersion": "A_music_3.6.1", "deviceId": MG_DEVICE_ID,
                          "timestamp": ts, "sign": _mg_sign(keyword, ts),
                          "channel": "0146921", "User-Agent": UA_MG})
    # 响应是平铺的：{code, songResultData:{totalCount, resultList:[[...]}}
    # （源码里的 result.code 是它自己的 createHttpFetch 剥过一层壳）
    if not isinstance(body, dict) or body.get("code") != "000000":
        raise NetError("咪咕搜索返回 %s" % (body or {}).get("code"))
    song = body.get("songResultData") or {}
    seen = set()
    rows = []
    for group in song.get("resultList") or []:
        items = group if isinstance(group, list) else [group]
        for item in items:
            cid = item.get("copyrightId")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            rows.append(_mg_track(item))
    total = int(_num(song.get("totalCount"), len(rows)))
    return {"list": rows, "total": total,
            "allPage": max(1, -(-total // limit)), "page": page}

def _mg_track(item):
    sizes = {}
    for fmt in item.get("audioFormats") or []:
        quality = MG_FORMAT.get(fmt.get("formatType"))
        if not quality:
            continue
        size = fmt.get("asize") or fmt.get("isize")
        if size:
            sizes[quality] = size_formate(size)
    img = item.get("img3") or item.get("img2") or item.get("img1") or ""
    if img and not str(img).startswith("http"):
        img = "http://d.musicapp.migu.cn" + str(img)
    return _track("mg", item.get("songId"), unescape(item.get("name")),
                  join_names(item.get("singerList")), unescape(item.get("album")),
                  item.get("albumId"), _num(item.get("duration")), sizes, pic=img,
                  copyrightId=item.get("copyrightId") or "",
                  lrcUrl=item.get("lrcUrl") or item.get("lyricUrl") or
                         item.get("lrcurl") or "",
                  mrcUrl=item.get("mrcUrl") or item.get("mrcurl") or "",
                  trcUrl=item.get("trcUrl") or item.get("trcurl") or "")

def _mg_resource(item):
    """mg/musicInfo.js:22-88 filterMusicInfoList：榜单/歌单里的资源结构。

    和搜索结果不是一个形状：songName / artists[] / length "hh:mm:ss" /
    newRateFormats[].size / albumImgs[0].img。
    """
    sizes = {}
    for fmt in item.get("newRateFormats") or item.get("audioFormats") or []:
        quality = MG_FORMAT.get(fmt.get("formatType"))
        if not quality:
            continue
        size = fmt.get("size") or fmt.get("asize") or fmt.get("isize")
        if size:
            sizes[quality] = size_formate(size)
    seconds = 0
    m = re.search(r"(\d+):(\d+):(\d+)", str(item.get("length") or ""))
    if m:
        seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    elif str(item.get("length") or "").isdigit():
        seconds = int(item["length"])
    imgs = item.get("albumImgs") or []
    pic = (imgs[0].get("img") if imgs and isinstance(imgs[0], dict) else "") or ""
    if pic and not str(pic).startswith("http"):
        pic = "http://d.musicapp.migu.cn" + str(pic)
    return _track("mg", item.get("songId"), unescape(item.get("songName") or
                                                     item.get("name")),
                  join_names(item.get("artists") or item.get("singerList")),
                  unescape(item.get("album")), item.get("albumId"),
                  seconds, sizes, pic=pic,
                  copyrightId=item.get("copyrightId") or "",
                  lrcUrl=item.get("lrcUrl") or item.get("lyricUrl") or "",
                  mrcUrl=item.get("mrcUrl") or "",
                  trcUrl=item.get("trcUrl") or "")

def mg_board_songs(column_id, page=1):
    """mg/leaderboard.js:139-141，整榜返回不分页（page 参数只是接口层签名一致）。"""
    url = ("https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/querycontentbyId.do"
           "?columnId=%s&needAll=0" % column_id)
    body = get_json(url, {"User-Agent": UA_MG, "channel": "0146921",
                          "Referer": "https://m.music.migu.cn/"})
    if not isinstance(body, dict) or body.get("code") != "000000":
        raise NetError("咪咕榜单取不到")
    column = body.get("columnInfo") or {}
    rows = []
    for content in column.get("contents") or []:
        item = content.get("objectInfo") or {}
        if item.get("copyrightId") or item.get("songId"):
            rows.append(_mg_resource(item))
    return {"list": rows, "total": len(rows), "allPage": 1, "page": 1,
            "info": {"name": unescape(column.get("columnTitle")),
                     "img": column.get("columnPicUrl") or "",
                     "desc": unescape(column.get("columnDes") or "")}}

MG_LRC_HEADERS = {"Referer": "https://app.c.nf.migu.cn/", "User-Agent": UA_MG,
                  "channel": "0146921"}

def mg_lyric(info):
    """mg/lyric.js:76-95：mrcUrl 需要 TEA 解密，这里只取明文 lrcUrl / trcUrl。"""
    for url in (info.get("lrcUrl"), info.get("trcUrl")):
        if url:
            try:
                text = _call(url, headers=MG_LRC_HEADERS)[0]
            except NetError:
                continue
            if "[0" in text or "[" in text[:40]:
                return text
    return ""

# ---------------------------------------------------------------- 统一入口

def search(source, keyword, page=1, kind="music"):
    if kind == "songlist":
        if source == "kw":
            return kw_songlist_search(keyword, page)
        if source == "tx":
            return tx_songlist_search(keyword, page)
        raise NetError("%s 暂不支持歌单搜索" % SOURCE_LABEL.get(source, source))
    return {"kw": kw_search, "kg": kg_search, "tx": tx_search,
            "wy": wy_search, "mg": mg_search}[source](keyword, page)

SOURCE_LABEL = {"kw": "酷我", "kg": "酷狗", "tx": "QQ音乐", "wy": "网易云",
                "mg": "咪咕", "local": "本地"}

BOARDS = {"kw": music_boards.kw_BOARDS, "kg": music_boards.kg_BOARDS,
          "tx": music_boards.tx_BOARDS, "wy": music_boards.wy_BOARDS,
          "mg": music_boards.mg_BOARDS}

def boards(source):
    """返回 [{id,name}]。wy 的取榜歌要 weapi(RSA)，v1 未接。"""
    if source == "wy":
        return []
    return [{"id": bid, "name": name, "raw": raw}
            for bid, name, raw in BOARDS.get(source, [])]

def board_songs(source, board_id, page=1):
    raw = board_id.split("__")[-1]
    if source == "kw":
        return kw_board_songs(raw, page)
    if source == "kg":
        return kg_board_songs(raw, page)
    if source == "tx":
        return tx_board_songs(raw, page)
    if source == "mg":
        return mg_board_songs(raw, page)
    raise NetError("%s 暂不支持榜单" % SOURCE_LABEL.get(source, source))

def songlists(source, page=1, tag="", tag_type=""):
    if source == "kw":
        return kw_songlists(tag, tag_type, page)
    if source == "tx":
        return tx_songlist_search("", page) if not tag else tx_songlist_search(tag, page)
    raise NetError("%s 暂不支持歌单浏览，可以直接粘贴歌单链接"
                   % SOURCE_LABEL.get(source, source))

def songlist_songs(source, list_id, page=1):
    if source == "kw":
        return kw_songlist_detail(list_id, page)
    if source == "tx":
        return tx_songlist_detail(list_id, page)
    raise NetError("%s 暂不支持歌单取歌" % SOURCE_LABEL.get(source, source))

def parse_list_url(url):
    """粘贴链接解析：返回 (source, id)。"""
    url = (url or "").strip()
    if "kuwo" in url:
        return "kw", kw_songlist_id(url)
    if "y.qq.com" in url:
        return "tx", tx_songlist_id(url)
    if "music.163.com" in url:
        m = re.search(r"[?&/](?:playlist|id)[=/](\d+)", url)
        return "wy", m.group(1) if m else ""
    if "kugou" in url:
        m = re.search(r"specialid[=:](\d+)|/album/(\d+)", url)
        return "kg", (m.group(1) or m.group(2)) if m else ""
    return "", ""

def _tget(track, *keys):
    """接口层用 name/singer，播放器层用 title/artist，取第一个有值的。"""
    for key in keys:
        val = track.get(key)
        if val:
            return val
    return ""

def lyric(source, track):
    """在线歌词。tx 的 QRC 是非标准 DES（未接），统一回落酷我按歌名+歌手匹配。"""
    handlers = {
        "kw": lambda t: kw_lyric(_tget(t, "songmid")),
        "kg": lambda t: kg_lyric(_tget(t, "songmid"), t),
        "wy": lambda t: wy_lyric(_tget(t, "songmid")),
        "mg": lambda t: mg_lyric(t),
    }
    fn = handlers.get(source)
    text = ""
    if fn:
        try:
            text = fn(track) or ""
        except Exception:
            text = ""
    if text.strip():
        return text
    if source == "kw":
        return ""
    return kw_lyric_fallback(track)

def kw_lyric_fallback(track):
    """用酷我搜索按「歌手 歌名」找同一首，再取它的歌词。"""
    name = _tget(track, "title", "name")
    singer = _tget(track, "artist", "singer")
    keyword = ("%s %s" % (singer, name)).strip()
    if not keyword:
        return ""
    try:
        rows = kw_search(keyword, 1, 5)["list"]
    except NetError:
        return ""
    want = name.lower()
    for row in rows:
        if want and want in (row.get("name") or "").lower():
            try:
                return kw_lyric(row["songmid"])
            except Exception:
                return ""
    if rows:
        try:
            return kw_lyric(rows[0]["songmid"])
        except Exception:
            return ""
    return ""

def pic(source, track):
    """封面：结果里带的直接用，酷我再多走一次查询。"""
    if track.get("pic"):
        return track["pic"]
    try:
        if source == "kw":
            return kw_pic(track.get("songmid"))
        if source == "kg":
            return kg_pic(track)
        if source == "tx":
            album = track.get("albumMid") or track.get("albumid") or ""
            if album:
                return "https://y.gtimg.cn/music/photo_new/T002R500x500M000%s.jpg" % album
    except Exception:
        return ""
    return ""

def hot_search(source):
    """热搜，供搜索页空态提示。"""
    try:
        if source == "kw":
            body = get_json("http://hotword.kuwo.cn/hotword.s?prod=kwplayer_ar_9.3.0.1"
                            "&corp=kuwo&newver=2&vipver=9.3.0.1&source="
                            "kwplayer_ar_9.3.0.1_40.apk&p2p=1&notrace=0&uid=0&plat="
                            "kwplayer_ar&rformat=json&encoding=utf8&tabid=1",
                            {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9;)"})
            return [unescape(x.get("key")) for x in (body or {}).get("tagvalue") or []
                    if x.get("key")][:12]
    except Exception:
        return []
    return []
