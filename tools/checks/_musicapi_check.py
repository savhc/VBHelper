# -*- coding: utf-8 -*-
"""音乐在线能力的开发自检：python _musicapi_check.py

覆盖：node 定位、宿主落盘、脚本初始化、musicUrl/lyric/pic 三类动作、
能力表回填、取消、空闲销毁、导入解析与去重。
"""
import json
import os
import queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper import musicapi as M

fails = []
oks = []


def check(name, cond, detail=""):
    (oks if cond else fails).append("%s%s" % (name, "" if cond else "  <-- " + str(detail)))
    print(("  ok  " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail and not cond else ""))


print("== 1. 元信息解析 ==")
try:
    info = M.parse_script_info(M.SELFTEST_SCRIPT)
    check("解析 @name", info["name"] == "宿主自检", info)
    check("解析 @version", info["version"] == "selftest", info)
    check("解析 @author", info["author"] == "VBHelper", info)
except Exception as e:
    check("解析头部注释", False, e)

try:
    M.parse_script_info("window.lx = 1")
    check("无头注释应报错", False, "居然通过了")
except ValueError:
    check("无头注释应报错", True)

print("== 2. 导入与去重 ==")
store = {"active": "", "items": []}
item = M.import_script(store, M.SELFTEST_SCRIPT)
check("导入成功并置为激活", store["active"] == item["id"], store["active"])
check("id 形如 user_api_xxx_时间戳", item["id"].startswith("user_api_"), item["id"])
check("脚本压缩存储", (item["script"] or "").startswith("gz_"))
try:
    M.import_script(store, M.SELFTEST_SCRIPT)
    check("重复导入应拒绝", False, "居然通过了")
except ValueError as e:
    check("重复导入应拒绝", "相同" in str(e), e)
check("回取脚本内容一致", M._unpack(item["script"]) == M.SELFTEST_SCRIPT.strip())

print("== 3. node 定位 ==")
node = M.find_node()
check("找到 node.exe", bool(node), "未找到，请在设置里手动指定")
if not node:
    print("\n跳过运行时用例：%s" % "\n".join(fails))
    sys.exit(1)
print("     node = %s" % node)

print("== 4. 运行时全链路 ==")
results = queue.Queue()
rt = M.LxRuntime(results)
rt._node = node
check("宿主文件可落盘", rt._ensure_host())
loaded = rt.load({"id": item["id"], "script": item["script"], "name": item["name"]})
check("提交加载", loaded)


def wait_for(what, timeout=30.0):
    """按事件名取结果，顺带把过程中的 log 打出来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event, payload = results.get(timeout=0.2)
        except queue.Empty:
            rt.poll(now=time.time())
            continue
        if event == "log":
            print("     [脚本] %s" % payload["text"])
        if event == what:
            return payload
        if event == "error":
            if what in ("res", "alert"):
                return payload
            print("     [error] %s" % payload.get("message"))
    return None


ready = wait_for("ready")
check("脚本完成 inited", ready is not None, "没等到 ready")
if ready:
    src = (ready.get("sources") or {}).get("kw") or {}
    check("能力表回填 actions", src.get("actions") == ["musicUrl"], src)
    check("能力表回填 qualitys 被白名单裁剪",
          src.get("qualitys") == ["128k", "320k"], src)
    check("supports(kw, musicUrl, 320k)", rt.supports("kw", "musicUrl", "320k"))
    check("supports(kw, musicUrl, flac) 应为 False", not rt.supports("kw", "musicUrl", "flac"))
    check("supports(tx) 未声明应为 False", not rt.supports("tx"))

    key = rt.request("musicUrl", "kw",
                     {"type": "320k", "musicInfo": {"songmid": "12345", "source": "kw"}})
    check("发出 musicUrl 请求", bool(key), key)
    res = wait_for("res")
    url = (((res or {}).get("data") or {}).get("data") or {}).get("url") if res else ""
    check("musicUrl 返回 https 直链", bool(url) and url.startswith("https://"), res)
    check("md5 在宿主里算得出来", bool(url) and len(url.split("/")[-1].split(".")[0]) == 32, url)

    key2 = rt.request("lyric", "kw", {"musicInfo": {"songmid": "12345"}})
    res2 = wait_for("res")
    lyric = (((res2 or {}).get("data") or {}).get("data") or {}).get("lyric") if res2 else ""
    check("lyric 动作走通", lyric == "[00:00.00]宿主自检歌词", res2)

    key3 = rt.request("pic", "kw", {"musicInfo": {"songmid": "12345"}})
    res3 = wait_for("res")
    pic = ((res3 or {}).get("data") or {}).get("data") if res3 else ""
    check("pic 动作走通", pic == "https://example.com/cover.jpg", res3)

    bad = rt.request("bogus", "kw", {"musicInfo": {}})
    resbad = wait_for("res")
    check("脚本 reject 时错误能回传",
          resbad is not None and not resbad.get("ok") and "unsupported" in (resbad.get("error") or ""),
          resbad)

    cancelled = rt.request("musicUrl", "kw", {"type": "128k", "musicInfo": {"songmid": "9"}})
    rt.cancel(cancelled)
    r2 = rt.request("musicUrl", "kw", {"type": "128k", "musicInfo": {"songmid": "8"}})
    res2b = wait_for("res")
    check("被取消的请求回执被丢弃", (res2b or {}).get("id") == r2, res2b)

print("== 5. 空闲销毁 ==")
rt._last_use = time.time() - 100
rt.poll(now=time.time())
ev = None
try:
    ev = results.get(timeout=0.5)
except queue.Empty:
    pass
check("超时空闲后发 reaped 并杀掉进程", (ev or [None])[0] == "reaped", ev)
check("进程确实没了", not rt.alive)
check("重新用到会自动拉起并加载", rt.start_if_needed())
ready2 = wait_for("ready", timeout=30)
check("销毁后能重建", ready2 is not None)
rt.stop()
check("stop 后进程退出", not rt.alive)

print("== 6. 真实联网解析（脚本自己发 http） ==")
NET_SCRIPT = r'''/*!
 * @name 联网自检
 * @description 用宿主发一次真实请求
 * @version 1.0
 */
(function () {
  const lx = window.lx
  lx.on('request', ({ action, info }) => new Promise((resolve, reject) => {
    if (action !== 'musicUrl') return reject(new Error('skip'))
    // 参数不全时酷我会退化成单引号伪 JSON，这里用源码里的完整 URL
    lx.request('http://search.kuwo.cn/r.s?client=kt&all=' + encodeURIComponent('周杰伦') +
      '&pn=0&rn=3&uid=794762570&ver=kwplayer_ar_9.2.2.1&vipver=1&show_copyright_off=1' +
      '&newver=1&ft=music&cluster=0&strategy=2012&encoding=utf8&rformat=json&vermerge=1&mobi=1&issubtitle=1', {
      method: 'get',
      headers: { 'User-Agent': 'Mozilla/5.0' },
      timeout: 15000,
    }, (err, resp, body) => {
      if (err) return reject(err)
      if (!resp || resp.statusCode !== 200) return reject(new Error('http ' + (resp && resp.statusCode)))
      if (typeof body !== 'object') return reject(new Error('响应不是标准 JSON'))
      if (!body.TOTAL || !Array.isArray(body.abslist)) return reject(new Error('响应里没有 TOTAL/abslist'))
      const first = body.abslist[0] || {}
      if (!first.MUSICRID) return reject(new Error('abslist 缺 MUSICRID'))
      resolve('https://example.com/' + first.MUSICRID.replace('MUSIC_', '') + '-' + body.TOTAL + '.mp3')
    })
  }))
  lx.send('inited', { sources: { kw: { type: 'music', actions: ['musicUrl'], qualitys: ['128k', '320k'] } } })
})()
'''
results2 = queue.Queue()
rt2 = M.LxRuntime(results2)
rt2._node = node
item2 = {"id": "netcheck", "script": M._pack(NET_SCRIPT), "name": "联网自检"}
rt2.load(item2)
ready3 = wait_for2 = None
deadline = time.time() + 30
while time.time() < deadline and ready3 is None:
    try:
        e, p = results2.get(timeout=0.2)
    except queue.Empty:
        continue
    if e == "ready":
        ready3 = p
    elif e == "error":
        print("     [error] %s" % p.get("message"))
        break
check("联网脚本初始化", ready3 is not None)
if ready3:
    rt2.request("musicUrl", "kw", {"type": "128k", "musicInfo": {"songmid": "1"}})
    got = None
    deadline = time.time() + 30
    while time.time() < deadline and got is None:
        try:
            e, p = results2.get(timeout=0.2)
        except queue.Empty:
            continue
        if e == "res":
            got = p
    url = (((got or {}).get("data") or {}).get("data") or {}).get("url") if got else ""
    check("宿主内发出的真实请求拿到结果", bool(url), got)
    if url:
        print("     -> %s" % url)
rt2.stop()

print("== 7. 回归：脚本给请求头传 undefined ==")
HDR_SCRIPT = r'''/*!
 * @name 空请求头回归
 * @description needle 会丢掉 undefined 头，宿主也必须丢掉而不是报错
 * @version 1.0
 */
(function () {
  const lx = window.lx
  lx.on('request', ({ action }) => new Promise((resolve, reject) => {
    if (action !== 'musicUrl') return reject(new Error('skip'))
    // 故意把一个不存在的字段塞进请求头，复现真实音源里的写法
    const info = {}
    lx.request('http://search.kuwo.cn/r.s?client=kt&all=test&pn=0&rn=1&uid=794762570'
      + '&ver=kwplayer_ar_9.2.2.1&vipver=1&show_copyright_off=1&newver=1&ft=music'
      + '&cluster=0&strategy=2012&encoding=utf8&rformat=json&vermerge=1&mobi=1&issubtitle=1',
      { method: 'get',
        headers: { 'User-Agent': 'lx-music/desktop', ver: '2.0.0',
                   'source-ver': info.version } },
      (err, resp, body) => {
        if (err) return reject(err)
        if (!resp || resp.statusCode !== 200) return reject(new Error('http ' + (resp && resp.statusCode)))
        if (!body || typeof body !== 'object' || body.TOTAL === undefined) {
          return reject(new Error('响应里没有 TOTAL'))
        }
        resolve('https://example.com/ok.mp3')
      })
  }))
  lx.send('inited', { sources: { kw: { type: 'music', actions: ['musicUrl'],
                                      qualitys: ['128k'] } } })
})()
'''
res3 = queue.Queue()
rt3 = M.LxRuntime(res3)
rt3._node = node
rt3.load({"id": "hdr", "script": M._pack(HDR_SCRIPT), "name": "空请求头回归"})
ok3 = None
deadline = time.time() + 30
while time.time() < deadline and ok3 is None:
    try:
        e, p = res3.get(timeout=0.2)
    except queue.Empty:
        continue
    if e == "ready":
        ok3 = True
    elif e == "error":
        print("     [error] %s" % p.get("message"))
        ok3 = False
check("带 undefined 头的脚本能初始化", bool(ok3))
if ok3:
    rt3.request("musicUrl", "kw", {"type": "128k", "musicInfo": {"songmid": "1"}})
    got3 = None
    deadline = time.time() + 30
    while time.time() < deadline and got3 is None:
        try:
            e, p = res3.get(timeout=0.2)
        except queue.Empty:
            continue
        if e == "res":
            got3 = p
    url3 = (((got3 or {}).get("data") or {}).get("data") or {}).get("url") if got3 else ""
    check("undefined 头被丢掉后请求成功", bool(url3),
          (got3 or {}).get("error") or str(got3)[:160])
rt3.stop()

print("\n" + "=" * 52)
print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
for f in fails:
    print("  - " + f)
sys.exit(1 if fails else 0)
