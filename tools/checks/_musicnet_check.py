# -*- coding: utf-8 -*-
"""在线接口层的真实联网质检：python _musicnet_check.py

每条都打真接口，任何一条挂了都能立刻看出来是签名错、字段路径错还是编码解错。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper import musicnet as N

fails = []
oks = []


def check(name, fn, verify):
    """verify(result) -> (bool, 摘要)"""
    try:
        res = fn()
    except Exception as e:
        fails.append(name)
        print("  FAIL %-26s %s: %s" % (name, type(e).__name__, e))
        return None
    ok, detail = verify(res)
    (oks if ok else fails).append(name)
    print(("  ok   %-26s %s" if ok else "  FAIL %-26s %s") % (name, detail))
    return res


def show_track(t):
    return "%s / %s / %ss / %s / %s" % (
        t.get("name"), t.get("singer"), t.get("dur"),
        ",".join(t.get("qualitys") or []) or "无档位",
        (t.get("sizes") or {}).get("128k") or "")


def search_ok(res):
    rows = res["list"]
    if not rows:
        return False, "0 条"
    bad = [r for r in rows if not r.get("songmid") or not r.get("name")]
    return (len(rows) >= 3 and not bad), ("%d 条 / 共 %d / %s" % (
        len(rows), res["total"], show_track(rows[0]))) + (
        "" if not bad else "  <-- %d 条缺 id 或歌名" % len(bad))


def plist_ok(r):
    if not r["list"]:
        return False, "0 个"
    first = r["list"][0]
    return True, "%d 个 / %s / %s" % (len(r["list"]), first["name"],
                                      first.get("count", ""))


def songs_ok(r):
    return len(r["list"]) > 5, "%d 首 / 歌单名 %s" % (
        len(r["list"]), (r.get("info") or {}).get("name"))


print("== 搜索（五源） ==")
for src in ("kw", "kg", "tx", "wy", "mg"):
    check("%s 搜索" % src, lambda s=src: N.search(s, "周杰伦", 1), search_ok)

print("== 歌单搜索 ==")
check("kw 歌单搜索", lambda: N.search("kw", "华语", 1, kind="songlist"), plist_ok)
check("tx 歌单搜索", lambda: N.search("tx", "华语", 1, kind="songlist"), plist_ok)

print("== 歌单浏览与取歌 ==")
pl = check("kw 歌单推荐", lambda: N.songlists("kw", 1), plist_ok)
if pl and pl["list"]:
    check("kw 歌单取歌", lambda: N.songlist_songs("kw", pl["list"][0]["id"]), songs_ok)
check("tx 歌单取歌", lambda: N.songlist_songs(
    "tx", N.search("tx", "华语", 1, kind="songlist")["list"][0]["id"]), songs_ok)

print("== 榜单 ==")
for src in ("kw", "kg", "tx", "mg"):
    boards = N.boards(src)
    if not boards:
        print("  SKIP %-26s 没有可用榜单" % (src + " 榜单"))
        continue
    bid = boards[0]["id"]
    check("%s 榜单(%s)" % (src, boards[0]["name"][:8]),
          lambda s=src, b=bid: N.board_songs(s, b, 1),
          lambda r: (len(r["list"]) >= 5, "%d 首 / %s" % (
              len(r["list"]), show_track(r["list"][0]) if r["list"] else "")))

print("== 歌词 ==")
kwrow = N.search("kw", "晴天", 1)["list"][0]
check("kw 歌词", lambda: N.lyric("kw", kwrow),
      lambda t: (bool(t and "[" in t), "%d 字 / 首行 %s" % (
          len(t or ""), (t or "").splitlines()[0][:34] if t else "")))
check("kw 逐字歌词", lambda: N.kw_lyric(kwrow["songmid"], True),
      lambda t: (bool(t and "[" in t), "%d 字" % len(t or "")))
kgrow = N.search("kg", "晴天", 1)["list"][0]
check("kg 歌词(KRC)", lambda: N.lyric("kg", kgrow),
      lambda t: (bool(t and "[" in t), "%d 字 / 首行 %s" % (
          len(t or ""), (t or "").splitlines()[0][:34] if t else "")))
wyrow = N.search("wy", "晴天", 1)["list"][0]
check("wy 歌词", lambda: N.lyric("wy", wyrow),
      lambda t: (bool(t and "[" in t), "%d 字" % len(t or "")))
mgres = N.search("mg", "晴天", 1)["list"]
mgrow = next((r for r in mgres if r.get("lrcUrl")), mgres[0] if mgres else None)
if mgrow:
    check("mg 歌词", lambda: N.lyric("mg", mgrow),
          lambda t: (bool(t and "[" in t), "%d 字" % len(t or "")))
txrow = N.search("tx", "晴天", 1)["list"][0]
check("tx 歌词回落酷我", lambda: N.lyric("tx", txrow),
      lambda t: (bool(t and "[" in t), "%d 字（走回落）" % len(t or "")))

print("== 封面 ==")
for src, row in (("kw", kwrow), ("kg", kgrow), ("tx", txrow), ("wy", wyrow)):
    check("%s 封面" % src, lambda s=src, r=row: N.pic(s, r),
          lambda u: (bool(u) and str(u).startswith("http"), str(u)[:70]))

print("== 链接解析 ==")
cases = [("http://www.kuwo.cn/playlist_detail/12345678", "kw"),
         ("https://y.qq.com/n/ryqq/playlist/8802438904.html", "tx"),
         ("https://music.163.com/#/playlist?id=7415552145", "wy")]
for url, want in cases:
    got = N.parse_list_url(url)
    ok = got[0] == want and got[1]
    (oks if ok else fails).append("解析 " + url[:24])
    print(("  ok   " if ok else "  FAIL ") + "解析 %s -> %s" % (want, got))

print("== 联想与热搜 ==")
check("kw 联想", lambda: N.kw_tip("周杰"),
      lambda v: (bool(v), "%s" % v[:4]))
check("kw 热搜", lambda: N.hot_search("kw"),
      lambda v: (bool(v), "%s" % v[:4]))

print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
for f in fails:
    print("  - " + f)
sys.exit(1 if fails else 0)
