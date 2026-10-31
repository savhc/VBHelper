"""lx-music-desktop 自定义音源（userApi 协议 v2.0.0）运行时。

音源脚本是一段浏览器风格的 JS，只认宿主给的 window.lx。这里用 Node 子进程当
宿主，Python 侧通过 stdio 收发 JSON 行；子进程懒启动、空闲即销毁，不听在线歌
的时候不给系统留占用。

实现遵循 userApi v2.0.0 的能力表与 request/send/on 语义，
与 lx-music-desktop 的同名实现保持行为一致；来源声明见 NOTICE。
"""
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib

from . import core
from .core import APP_NAME, load_cfg

CREATE_NO_WINDOW = 0x08000000

# 脚本可声明的音源集合；超出范围的声明一律忽略
ALL_SOURCES = ("kw", "kg", "tx", "wy", "mg", "local")
SUPPORT_QUALITYS = {
    "kw": ["128k", "320k", "flac", "flac24bit"],
    "kg": ["128k", "320k", "flac", "flac24bit"],
    "tx": ["128k", "320k", "flac", "flac24bit"],
    "wy": ["128k", "320k", "flac", "flac24bit"],
    "mg": ["128k", "320k", "flac", "flac24bit"],
    "local": [],
}
SUPPORT_ACTIONS = {
    "kw": ["musicUrl"], "kg": ["musicUrl"], "tx": ["musicUrl"], "wy": ["musicUrl"],
    "mg": ["musicUrl"], "xm": ["musicUrl"], "local": ["musicUrl", "lyric", "pic"],
}

# 脚本元信息字段的长度上限，超出即截断
INFO_LIMITS = {"name": 24, "description": 36, "author": 56,
               "homepage": 1024, "version": 36}

DATA_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SOURCE_STORE = os.path.join(DATA_DIR, "music_sources.json")
HOST_JS = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME, "lx_host.js")

IDLE_KILL_S = 45.0        # 空闲这么久就销毁 node，下次用到再冷启
LOAD_TIMEOUT_S = 25.0     # 脚本初始化时限（脚本可能在 init 里发校验请求）

# ---------------------------------------------------------------- 宿主脚本

NODE_HOST_JS = r'''"use strict";
// VBHelper 的 lx-music-desktop userApi 宿主：只实现协议，不给脚本 fs / child_process。
const vm = require("vm");
const http = require("http");
const https = require("https");
const tls = require("tls");
const zlib = require("zlib");
const crypto = require("crypto");
const readline = require("readline");

const ALL_SOURCES = ["kw", "kg", "tx", "wy", "mg", "local"];
const SUPPORT_QUALITYS = {
  kw: ["128k", "320k", "flac", "flac24bit"],
  kg: ["128k", "320k", "flac", "flac24bit"],
  tx: ["128k", "320k", "flac", "flac24bit"],
  wy: ["128k", "320k", "flac", "flac24bit"],
  mg: ["128k", "320k", "flac", "flac24bit"],
  local: [],
};
const SUPPORT_ACTIONS = {
  kw: ["musicUrl"], kg: ["musicUrl"], tx: ["musicUrl"], wy: ["musicUrl"],
  mg: ["musicUrl"], xm: ["musicUrl"], local: ["musicUrl", "lyric", "pic"],
};

const send = (obj) => { try { process.stdout.write(JSON.stringify(obj) + "\n"); } catch (_) {} };

// 网络代理（对齐 lx 设置里的「网络代理」）：load 消息带 proxy 字段 > 环境变量。
// Node 原生 http(s) 永远直连、不读代理环境变量，没有这层的话，
// 被污染/劫持的域名在本机 DNS 上直接 getaddrinfo 失败——LX 走代理能用而我们不能的根因。
let PROXY_CONF = "";

function pickProxy() {
  const raw = String(PROXY_CONF || process.env.https_proxy || process.env.HTTPS_PROXY
    || process.env.http_proxy || process.env.HTTP_PROXY || "").trim();
  if (!raw) return null;
  try {
    const u = new URL(raw);
    if (!u.hostname) return null;
    return { hostname: u.hostname, port: Number(u.port) || 80 };
  } catch (_) { return null; }
}

const lx = {
  EVENT_NAMES: { request: "request", inited: "inited", updateAlert: "updateAlert" },
  version: "2.0.0",
  env: "desktop",
  currentScriptInfo: { name: "", description: "", version: "", author: "", homepage: "", rawScript: "" },
  utils: {
    crypto: {
      aesEncrypt(buffer, mode, key, iv) {
        const cipher = crypto.createCipheriv(mode, key, iv);
        return Buffer.concat([cipher.update(buffer), cipher.final()]);
      },
      rsaEncrypt(buffer, key) {
        buffer = Buffer.concat([Buffer.alloc(128 - buffer.length), buffer]);
        return crypto.publicEncrypt({ key, padding: crypto.constants.RSA_NO_PADDING }, buffer);
      },
      randomBytes(size) { return crypto.randomBytes(size); },
      md5(str) { return crypto.createHash("md5").update(str).digest("hex"); },
      sha256(str) { return crypto.createHash("sha256").update(str).digest("hex"); },
    },
    buffer: {
      from(...args) { return Buffer.from(...args); },
      bufToString(buf, format) { return Buffer.from(buf, "binary").toString(format); },
    },
    zlib: {
      inflate(buf) { return Promise.resolve(zlib.inflateSync(buf)); },
      deflate(buf) { return Promise.resolve(zlib.deflateSync(buf)); },
    },
  },
};

let inited = false;
let reportedError = false;
let requestHandler = null;
const aborts = new Map();      // httpId -> abort
let httpSeq = 0;

// 初始化只报第一次错，对齐 preload.js 的 onError
function failInit(message) {
  if (reportedError || inited) return;
  reportedError = true;
  send({ t: "init_error", message: String(message || "unknown error").slice(0, 1024) });
}

function verifyUrl(value) {
  return typeof value == "string" && value.length > 0 && value.length <= 2048 && /^https?:/.test(value);
}

// HTTP CONNECT 隧道：经代理连 https 目标，等价 lx 用的 tunnel.httpsOverHttp
function connectViaProxy(proxy, host, port, cb) {
  const req = http.request({
    host: proxy.hostname, port: proxy.port,
    method: "CONNECT", path: host + ":" + port,
    headers: { host: host + ":" + port },
    timeout: 15000,
  });
  req.on("connect", (res, socket) => {
    if (res.statusCode === 200) return cb(null, socket);
    socket.destroy();
    cb(new Error("proxy CONNECT " + res.statusCode));
  });
  req.on("timeout", () => req.destroy(new Error("proxy connect timeout")));
  req.on("error", cb);
  req.end();
}

// needle 风格的 (err, resp, body)：raw 保留原始字节，body 尽量给解析后的 JSON
function httpRequest(url, options, done) {
  let target;
  try { target = new URL(url); } catch (e) { return done(new Error("invalid url"), null, null); }
  const opts = options || {};
  const lib = target.protocol === "https:" ? https : http;
  const method = String(opts.method || "get").toUpperCase();
  // 脚本经常给可选头塞 undefined（例如取一个协议里不存在的字段），
  // lx 用的 needle 会静默丢掉这种头，而 Node 原生 http 会直接抛
  // ERR_HTTP_INVALID_HEADER_VALUE，整条取链就死在这里。这里按 needle 的语义清洗。
  const headers = {};
  const dropped = [];
  for (const key of Object.keys(opts.headers || {})) {
    const value = opts.headers[key];
    if (value === undefined || value === null) { dropped.push(key); continue; }
    headers[key] = Array.isArray(value) ? value.map((x) => String(x)) : String(value);
  }
  if (dropped.length) send({ t: "log", text: "已丢弃空请求头: " + dropped.join(",") });
  let payload = null;
  if (opts.body != null) {
    payload = Buffer.from(typeof opts.body == "string" ? opts.body : JSON.stringify(opts.body), "utf8");
    if (!headers["content-type"]) headers["content-type"] = "application/json";
  } else if (opts.form != null) {
    payload = Buffer.from(new URLSearchParams(opts.form).toString(), "utf8");
    if (!headers["content-type"]) headers["content-type"] = "application/x-www-form-urlencoded";
  } else if (opts.formData != null) {
    payload = Buffer.from(new URLSearchParams(opts.formData).toString(), "utf8");
    if (!headers["content-type"]) headers["content-type"] = "application/x-www-form-urlencoded";
  }
  if (payload && !headers["content-length"]) headers["content-length"] = String(payload.length);
  const timeout = (typeof opts.timeout == "number" && opts.timeout > 0)
    ? Math.min(opts.timeout, 60000) : 60000;

  let settled = false;
  const hid = "h" + (++httpSeq);
  // GET/HEAD 带 form 时拼进 query，和 needle 的行为对齐（脚本大量依赖这点）
  const isGet = method == "GET" || method == "HEAD";
  if (payload && isGet && opts.body == null) {
    const extra = new URLSearchParams(opts.form != null ? opts.form : opts.formData).toString();
    if (extra) target.search = target.search ? target.search + "&" + extra : "?" + extra;
    payload = null;
    delete headers["content-length"];
  }
  const finish = (err, resp, body) => {
    if (settled) return;
    settled = true;
    aborts.delete(hid);
    if (err) {
      // 失败原文上报：很多脚本会把错误吞成一句 "error"，不落这条日志就没法查
      send({ t: "log", text: "[req] " + method + " " + url.slice(0, 100)
        + " -> " + String((err && err.message) || err).slice(0, 160) });
    }
    if (typeof done == "function") {
      try { done(err || null, resp || null, body == null ? null : body); }
      catch (e) { failInit(e && e.message); }
    }
  };
  const proxy = pickProxy();
  const issue = (socket) => {
    const reqOpts = {
      protocol: target.protocol, hostname: target.hostname, port: target.port,
      path: target.pathname + target.search, method, headers, timeout,
    };
    if (socket) {
      // https over CONNECT 隧道：在代理给的裸 socket 上做 TLS，校验保持严格
      reqOpts.agent = false;
      reqOpts.createConnection = (_o, cb) => {
        const tlsSock = tls.connect({
          socket, servername: target.hostname, rejectUnauthorized: true,
        }, () => cb(null, tlsSock));
        tlsSock.once("error", (e) => cb(e));
      };
    } else if (proxy && target.protocol === "http:") {
      // http 经代理：向代理发绝对 URL（等价 tunnel.httpOverHttp）
      reqOpts.hostname = proxy.hostname;
      reqOpts.port = proxy.port;
      reqOpts.path = target.href;
      reqOpts.headers = Object.assign({}, headers, { host: target.hostname });
    }
    const req = (socket ? https : lib).request(reqOpts, (resp) => {
      const chunks = [];
      let bytes = 0;
      resp.on("data", (c) => { chunks.push(c); bytes += c.length; });
      resp.on("end", () => {
        const raw = Buffer.concat(chunks);
        let data = raw;
        const enc = String(resp.headers["content-encoding"] || "").toLowerCase();
        try {
          if (enc == "gzip") data = zlib.gunzipSync(raw);
          else if (enc == "deflate") data = zlib.inflateSync(raw);
          else if (enc == "br") data = zlib.brotliDecompressSync(raw);
        } catch (_) { data = raw; }
        const headersOut = {};
        for (const k of Object.keys(resp.headers)) headersOut[k] = resp.headers[k];
        const text = data.toString("utf8");
        let parsed = text;
        try { parsed = JSON.parse(text); } catch (_) {}
        finish(null, {
          statusCode: resp.statusCode, statusMessage: resp.statusMessage,
          headers: headersOut, bytes, raw, body: parsed,
        }, parsed);
      });
      resp.on("error", (e) => finish(e, null, null));
    });
    req.on("timeout", () => { req.destroy(new Error("request timeout")); });
    req.on("error", (e) => finish(e, null, null));
    req.setTimeout(timeout);
    req.end(payload);
    aborts.set(hid, () => { try { req.destroy(); } catch (_) {} });
  };
  if (proxy && target.protocol === "https:") {
    connectViaProxy(proxy, target.hostname, Number(target.port) || 443,
      (err, socket) => {
        if (err) return finish(err, null, null);
        issue(socket);
      });
  } else {
    issue(null);
  }
}

// 校验并回填能力表，逻辑对齐 preload.js 的 handleInit
function handleInit(info) {
  if (!info) throw new Error("Missing required parameter init info");
  const sources = {};
  for (const source of ALL_SOURCES) {
    const user = info.sources && info.sources[source];
    if (!user || user.type !== "music") continue;
    const declaredActions = Array.isArray(user.actions) ? user.actions : [];
    const declaredQualitys = Array.isArray(user.qualitys) ? user.qualitys : [];
    sources[source] = {
      type: "music",
      actions: (SUPPORT_ACTIONS[source] || []).filter((a) => declaredActions.includes(a)),
      qualitys: (SUPPORT_QUALITYS[source] || []).filter((q) => declaredQualitys.includes(q)),
    };
  }
  send({
    t: "inited", sources,
    message: typeof info.message == "string" ? info.message.slice(0, 200) : "",
  });
}

function verifyLyricInfo(info) {
  if (typeof info != "object" || typeof info.lyric != "string") throw new Error("failed");
  if (info.lyric.length > 51200) throw new Error("failed");
  return {
    lyric: info.lyric,
    tlyric: (typeof info.tlyric == "string" && info.tlyric.length < 5120) ? info.tlyric : null,
    rlyric: (typeof info.rlyric == "string" && info.rlyric.length < 5120) ? info.rlyric : null,
    lxlyric: (typeof info.lxlyric == "string" && info.lxlyric.length < 8192) ? info.lxlyric : null,
  };
}

function reply(msg, ok, data, error) {
  send({
    t: "res", id: msg.id, ok, data: ok ? data : null,
    error: ok ? "" : String((error && error.message) || error || "failed").slice(0, 300),
  });
}

function handleRequest(msg) {
  if (!requestHandler) return reply(msg, false, null, new Error("Request event is not defined"));
  const action = msg.action;
  let out;
  try { out = requestHandler.call(context, { source: msg.source, action: action, info: msg.info || {} }); }
  catch (e) { return reply(msg, false, null, e); }
  if (!out || typeof out.then != "function") out = Promise.resolve(out);
  const timer = setTimeout(() => reply(msg, false, null, new Error("request timeout")), 58000);
  out.then((response) => {
    clearTimeout(timer);
    try {
      if (action == "musicUrl") {
        if (!verifyUrl(response)) return reply(msg, false, null, new Error("failed"));
        return reply(msg, true, {
          source: msg.source, action,
          data: { type: (msg.info && msg.info.type) || "", url: response },
        });
      }
      if (action == "pic") {
        if (!verifyUrl(response)) return reply(msg, false, null, new Error("failed"));
        return reply(msg, true, { source: msg.source, action, data: response });
      }
      if (action == "lyric") {
        return reply(msg, true, { source: msg.source, action, data: verifyLyricInfo(response) });
      }
      return reply(msg, true, { source: msg.source, action, data: response });
    } catch (e) { reply(msg, false, null, e); }
  }).catch((err) => { clearTimeout(timer); reply(msg, false, null, err); });
}

let context = null;

function loadScript(text, name, info) {
  inited = false;
  reportedError = false;
  requestHandler = null;
  for (const abort of aborts.values()) { try { abort(); } catch (_) {} }
  aborts.clear();
  Object.assign(lx, { currentScriptInfo: Object.assign({ rawScript: text }, info || {}) });
  lx.currentScriptInfo.name = lx.currentScriptInfo.name || name || "";
  // 每次 load 都用全新 vm context：脚本顶层 const/let 声明会进 context 的
  // 全局词法环境，复用同一个 context 会在二次加载时报
  // "Identifier 'xxx' has already been declared"。标准 lx 用
  // webFrame.executeJavaScript 每次都是独立作用域，这里用新建 context 对齐。
  const ctx = {};
  vm.createContext(ctx);
  Object.assign(ctx, {
    lx,
    console: {
      log: (...a) => send({ t: "log", text: a.map(String).join(" ").slice(0, 400) }),
      info: (...a) => send({ t: "log", text: a.map(String).join(" ").slice(0, 400) }),
      warn: (...a) => send({ t: "log", text: a.map(String).join(" ").slice(0, 400) }),
      error: (...a) => send({ t: "log", text: a.map(String).join(" ").slice(0, 400) }),
      debug: (...a) => send({ t: "log", text: a.map(String).join(" ").slice(0, 400) }),
      group: (...a) => send({ t: "log", text: a.map(String).join(" ").slice(0, 400) }),
      groupEnd: () => {},
    },
    setTimeout: (fn, ms, ...rest) => setTimeout(fn, Math.min(Number(ms) || 0, 120000), ...rest),
    clearTimeout,
    setInterval: (fn, ms, ...rest) => setInterval(fn, Math.max(Number(ms) || 0, 1), ...rest),
    clearInterval,
    Buffer, URL, URLSearchParams, TextEncoder, TextDecoder,
  });
  ctx.window = ctx;
  ctx.global = ctx;
  context = ctx;
  lx.request = (url, options, callback) => {
    httpRequest(url, options, callback);
    return () => { for (const abort of aborts.values()) { try { abort(); } catch (_) {} } };
  };
  lx.send = (eventName, data) => new Promise((resolve, reject) => {
    if (eventName == "inited") {
      if (inited) return reject(new Error("Script is inited"));
      try { handleInit(data); } catch (e) { return reject(e); }
      inited = true;
      return resolve();
    }
    if (eventName == "updateAlert") {
      if (!data || typeof data != "object" || typeof data.log != "string") {
        return reject(new Error("parameter format error."));
      }
      send({ t: "alert", log: String(data.log).slice(0, 1024), updateUrl: data.updateUrl || "" });
      return resolve();
    }
    reject(new Error("The event is not supported: " + eventName));
  });
  lx.on = (eventName, handler) => {
    if (eventName == "request") { requestHandler = handler; return Promise.resolve(); }
    return Promise.reject(new Error("The event is not supported: " + eventName));
  };
  try {
    vm.runInContext(text, ctx, { timeout: 20000, filename: "lx_source.js" });
  } catch (e) {
    failInit(e && e.message);
    return;
  }
  if (!/registerSource/.test(text)) return;
  setTimeout(() => {
    if (!inited && !reportedError) {
      failInit("脚本调用了 registerSource（lx-music-desktop 旧版协议），本播放器只支持 2.x 自定义源协议");
    }
  }, 100);
}

process.on("unhandledRejection", (reason) => {
  const msg = typeof reason == "string" ? reason : (reason && reason.message) || String(reason);
  failInit(String(msg).replace(/^Error:\s/, ""));
});

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  line = line.trim();
  if (!line) return;
  let msg;
  try { msg = JSON.parse(line); } catch (_) { return; }
  if (msg.t == "load") {
    PROXY_CONF = String(msg.proxy || "");
    loadScript(String(msg.script || ""), msg.name, msg.info);
  }
  else if (msg.t == "req") handleRequest(msg);
  else if (msg.t == "ping") send({ t: "pong", node: process.version });
});
rl.on("close", () => process.exit(0));
''';

SELFTEST_SCRIPT = r'''/*!
 * @name 宿主自检
 * @description 校验 request / crypto / zlib / buffer 与 musicUrl 全链路
 * @version selftest
 * @author VBHelper
 */
(function () {
  const lx = window.lx
  lx.on('request', ({ action, info }) => new Promise((resolve, reject) => {
    if (action === 'musicUrl') {
      const sum = lx.utils.crypto.md5(info.musicInfo.songmid + '|selftest')
      if (!/^[0-9a-f]{32}$/.test(sum)) return reject(new Error('md5 failed'))
      const buf = lx.utils.buffer.from('128k', 'utf-8')
      return lx.utils.zlib.deflate(buf).then((z) => {
        if (z.length === 0) return reject(new Error('zlib failed'))
        resolve('https://example.com/' + sum + '.mp3')
      })
    }
    if (action === 'lyric') return resolve({ lyric: '[00:00.00]宿主自检歌词' })
    if (action === 'pic') return resolve('https://example.com/cover.jpg')
    reject(new Error('unsupported action'))
  }))
  lx.send('inited', {
    message: '自检就绪',
    sources: { kw: { type: 'music', actions: ['musicUrl', 'lyric', 'pic'], qualitys: ['128k', '320k'] } },
  }).catch(() => {})
})()
''';

# ---------------------------------------------------------------- 音源库存储

def _clip(text, limit):
    text = text or ""
    return text[:limit] + "..." if len(text) > limit else text

def parse_script_info(script):
    """读脚本头部块注释里的 @name/@description/@author/@homepage/@version。

    没有 /* */ 头就判定无效，规则与 utils.ts 的 parseScriptInfo 一致。
    """
    m = re.search(r"/\*[\S\s]+?\*/", script[:8192])
    if not m:
        raise ValueError("无效的自定义源文件：开头没有 /* */ 信息注释")
    rxp = re.compile(r"^\s?\*\s?@(\w+)\s(.+)$")
    found = {}
    for line in m.group(0).splitlines():
        r = rxp.match(line)
        if r and r.group(1) in INFO_LIMITS:
            found[r.group(1)] = r.group(2).strip()
    info = {k: _clip(found.get(k, ""), v) for k, v in INFO_LIMITS.items()}
    info["name"] = info["name"] or time.strftime("user_api_%Y%m%d%H%M%S")
    return info

def _pack(text):
    return "gz_" + base64.b64encode(zlib.compress(text.encode("utf-8"))).decode("ascii")

def _unpack(packed):
    if not isinstance(packed, str):
        return ""
    if not packed.startswith("gz_"):
        return packed
    try:
        return zlib.decompress(base64.b64decode(packed[3:])).decode("utf-8", "replace")
    except Exception:
        return ""

def load_sources():
    try:
        with open(SOURCE_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            return {"active": data.get("active") or "",
                    "items": [i for i in items
                              if isinstance(i, dict) and i.get("id")]}
    except Exception:
        pass
    return {"active": "", "items": []}

def save_sources(store):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SOURCE_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, SOURCE_STORE)
    except Exception:
        pass

def active_item(store):
    for item in store["items"]:
        if item.get("id") == store.get("active"):
            return item
    return None

def import_script(store, text):
    """导入脚本；内容与已有源相同直接拒绝（对齐 utils.ts 的 importApi）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("脚本内容是空的")
    info = parse_script_info(text)
    packed = _pack(text)
    for item in store["items"]:
        if item.get("script") == packed:
            raise ValueError("导入失败，脚本内容与已有的源「%s」相同"
                             % item.get("name"))
    item = {"id": "user_api_%d_%d" % (random.randint(100, 999), int(time.time() * 1000)),
            "script": packed}
    item.update(info)
    store["items"].append(item)
    if not store.get("active"):
        store["active"] = item["id"]
    return item

def remove_script(store, sid):
    store["items"] = [i for i in store["items"] if i.get("id") != sid]
    if store.get("active") == sid:
        store["active"] = store["items"][0]["id"] if store["items"] else ""

def fetch_remote(url, timeout=20):
    """从 http(s) 链接拉脚本正文。只做协议与大小限制，内容不做任何改写。"""
    if not re.match(r"^https?://", url or "", re.I):
        raise ValueError("只支持 http(s) 链接")
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with music_opener().open(req, timeout=timeout) as resp:
        raw = resp.read(8 * 1024 * 1024)
    return _detect_decode(raw)

def _detect_decode(raw):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", "replace")

def music_opener():
    """音乐请求统一 opener：certifi CA bundle + 与 Node 宿主同源的代理。

    搜索/歌词/封面/直链下载全走它，保证脚本内请求与 Python 端行为一致。
    """
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    proxy = (music_proxy() or "").strip()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(
            handler, urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

def music_proxy():
    """音源网络代理：config.json 的 music.proxy 优先，其次代理环境变量。

    Node 宿主不读代理环境变量、永远直连，被污染的域名会 getaddrinfo 失败；
    lx-music 桌面版靠设置里的网络代理走 tunnel 所以能用。这个函数和宿主的
    pickProxy 同源，保证脚本内请求与 Python 下载端走同一条代理。
    """
    try:
        cfg = load_cfg()
        p = ((cfg.get("music") or {}).get("proxy") or "").strip()
    except Exception:
        p = ""
    if p:
        return p
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        v = os.environ.get(key)
        if v:
            return v
    return ""

def find_node():
    """定位 node.exe：设置里手动指定 > PATH > 常见安装位置。"""
    try:
        cfg = load_cfg()
        custom = ((cfg.get("music") or {}).get("node_path") or "")
    except Exception:
        custom = ""
    if custom and os.path.isfile(custom):
        return custom
    found = shutil.which("node")
    if found:
        return found
    bases = [os.path.dirname(os.path.abspath(sys.executable))]
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "SystemDrive"):
        base = os.environ.get(key)
        if base:
            bases.append(os.path.join(base, "nodejs"))
            bases.append(base)
    for base in bases:
        for cand in (os.path.join(base, "node.exe"),
                     os.path.join(base, "nodejs", "node.exe"),
                     os.path.join(base, "NODE", "node.exe")):
            if os.path.isfile(cand):
                return cand
    return ""

# ---------------------------------------------------------------- 运行时

class LxRuntime:
    """一个 node 子进程 + 一份已加载的音源脚本。全异步，结果统一丢进队列。

    事件：load_start / ready / error / res / alert / log / exit / reaped
    """

    def __init__(self, results):
        self._results = results
        self._proc = None
        self._lock = threading.Lock()
        self._seq = 0
        self._sid = ""
        self._script = ""
        self._meta = {}
        self._pending_load = False
        self._inited = False
        self._sources = {}
        self._load_deadline = 0.0
        self._last_use = 0.0
        self._node = ""
        self._dropped = set()

    # ---- 状态 ----
    @property
    def inited(self):
        return self._inited

    @property
    def loading(self):
        return self._pending_load

    @property
    def sources(self):
        return self._sources

    @property
    def alive(self):
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def current_id(self):
        return self._sid

    def supports(self, source, action=None, quality=None):
        entry = self._sources.get(source) if self._sources else None
        if not entry:
            return False
        if action and action not in (entry.get("actions") or []):
            return False
        if quality and quality not in (entry.get("qualitys") or []):
            return False
        return True

    def qualitys(self, source):
        entry = self._sources.get(source) or {}
        return list(entry.get("qualitys") or [])

    def node_path(self):
        if not self._node:
            self._node = find_node()
        return self._node

    def reset_node_cache(self):
        self._node = ""

    # ---- 生命周期 ----
    def load(self, item):
        """加载一条音源；进程不在就冷启一个。"""
        if not item:
            return False
        script = _unpack(item.get("script") or "")
        if not script:
            self._emit("error", {"message": "这条音源的脚本内容是空的"})
            return False
        self._sid = item.get("id") or ""
        self._script = script
        self._meta = {k: item.get(k, "") for k in INFO_LIMITS}
        self._inited = False
        self._sources = {}
        self._dropped.clear()
        if not self._spawn():
            return False
        self._pending_load = True
        self._load_deadline = time.time() + LOAD_TIMEOUT_S
        self._last_use = time.time()
        self._emit("load_start", {"id": self._sid})
        return self._send({"t": "load", "script": script,
                           "name": self._sid, "info": self._meta,
                           "proxy": music_proxy()})

    def reload(self):
        if not self._script:
            return False
        return self.load({"id": self._sid, "script": _pack(self._script)})

    def start_if_needed(self):
        """被 reap 掉之后再次用到：把原脚本重新灌回去。"""
        if self.alive and self._inited:
            return True
        if not self._script:
            return False
        return self.load({"id": self._sid, "script": _pack(self._script)})

    def _spawn(self):
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            return True
        node = self.node_path()
        if not node:
            self._emit("error", {"message": "没有找到 node.exe，无法运行音源脚本；"
                                            "在设置里指一下 node 的位置再试"})
            return False
        if not self._ensure_host():
            return False
        env = dict(os.environ)
        env["NODE_NO_WARNINGS"] = "1"
        try:
            proc = subprocess.Popen(
                [node, HOST_JS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=DATA_DIR, env=env, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
                creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self._emit("error", {"message": "启动 node 失败：%s" % e})
            return False
        with self._lock:
            self._proc = proc
        threading.Thread(target=self._read_loop, args=(proc,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        return True

    def _ensure_host(self):
        """宿主脚本落盘一份给 node 当入口；内容变了就整体替换。"""
        try:
            os.makedirs(os.path.dirname(HOST_JS), exist_ok=True)
            old = ""
            if os.path.isfile(HOST_JS):
                with open(HOST_JS, "r", encoding="utf-8") as f:
                    old = f.read()
            if old != NODE_HOST_JS:
                tmp = HOST_JS + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(NODE_HOST_JS)
                os.replace(tmp, HOST_JS)
            return True
        except Exception as e:
            self._emit("error", {"message": "写入音源宿主失败：%s" % e})
            return False

    def _emit(self, event, payload):
        try:
            self._results.put_nowait((event, payload))
        except Exception:
            pass

    def _send(self, obj):
        try:
            blob = json.dumps(obj, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as e:
            self._emit("res", {"id": obj.get("id"), "ok": False, "data": None,
                               "error": "参数无法序列化：%s" % e})
            return False
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            try:
                proc.stdin.write(blob)
                proc.stdin.flush()
            except Exception:
                return False
        return True

    def _read_loop(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self._handle(msg)
        except Exception:
            pass
        code = None
        try:
            code = proc.wait(timeout=2)
        except Exception:
            pass
        with self._lock:
            if self._proc is proc:
                self._proc = None
        self._inited = False
        self._pending_load = False
        self._sources = {}
        self._emit("exit", {"code": code})

    @staticmethod
    def _drain_stderr(proc):
        try:
            for line in proc.stderr:
                text = line.strip()
                if text:
                    dbg = getattr(core, "dbg_log", None)
                    if callable(dbg):
                        dbg("lx_host: %s" % text[:400])
        except Exception:
            pass

    def _handle(self, msg):
        kind = msg.get("t")
        if kind == "inited":
            self._pending_load = False
            self._inited = True
            self._sources = msg.get("sources") or {}
            self._emit("ready", {"id": self._sid, "sources": self._sources,
                                 "message": msg.get("message") or ""})
        elif kind == "init_error":
            self._pending_load = False
            self._inited = False
            self._emit("error", {"id": self._sid,
                                 "message": msg.get("message") or "音源初始化失败"})
        elif kind == "res":
            rid = msg.get("id")
            if rid in self._dropped:
                self._dropped.discard(rid)
                return
            self._last_use = time.time()
            self._emit("res", {"id": rid, "ok": bool(msg.get("ok")),
                               "data": msg.get("data"),
                               "error": msg.get("error") or ""})
        elif kind == "alert":
            self._emit("alert", {"log": msg.get("log") or "",
                                 "updateUrl": msg.get("updateUrl") or ""})
        elif kind == "log":
            self._emit("log", {"text": msg.get("text") or ""})

    # ---- 调用 ----
    def request(self, action, source, info, key=None):
        """发一次请求。key 由调用方给，回来按 key 对号；False 表示没跑起来。"""
        if not self._inited:
            return False
        if key is None:
            self._seq += 1
            key = "r%d" % self._seq
        self._last_use = time.time()
        if self._send({"t": "req", "id": key, "action": action,
                       "source": source, "info": info}):
            return key
        self._emit("res", {"id": key, "ok": False, "data": None,
                           "error": "音源进程已退出"})
        return False

    def cancel(self, key):
        """只丢回执，不假装能掐断脚本里的 socket（lx 自己也是这么做的）。"""
        if key:
            self._dropped.add(key)

    def stop(self):
        with self._lock:
            proc = self._proc
            self._proc = None
        self._inited = False
        self._pending_load = False
        self._sources = {}
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass

    def poll(self, busy=False, now=None):
        """主循环里定期调：初始化超时判定 + 空闲销毁。"""
        now = now if now is not None else time.time()
        if self._pending_load and not self._inited and now > self._load_deadline:
            self._pending_load = False
            self._emit("error", {"id": self._sid,
                                 "message": "音源初始化超时（%.0fs 没等到 inited）"
                                            % LOAD_TIMEOUT_S})
            self.stop()
            return
        if busy or not self.alive or self._pending_load:
            return
        if now - self._last_use >= IDLE_KILL_S:
            self.stop()
            self._emit("reaped", {"id": self._sid})
