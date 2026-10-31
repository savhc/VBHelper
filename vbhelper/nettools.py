"""网络工具窗口：DNS 优选/一键切换、网络诊断、hosts 编辑。

这些操作都会改本机网络配置（改网卡 DNS、刷新缓存、保存 hosts），
所以入口在打开前会做管理员自检并提权。不涉及任何绕过网络管控的功能。
"""
import os
import queue
import socket
import ssl
import struct
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import messagebox, ttk

from . import core
from .core import *

CREATE_NO_WINDOW = 0x08000000

# 公共 DNS 候选表（名称, 主, 备）
DNS_LIST = [
    ("114 DNS", "114.114.114.114", "114.114.115.115"),
    ("阿里 DNS", "223.5.5.5", "223.6.6.6"),
    ("腾讯 DNS", "119.29.29.29", "182.254.116.116"),
    ("百度 DNS", "180.76.76.76", ""),
    ("360 DNS", "101.226.4.6", "218.30.118.6"),
    ("CNNIC", "1.2.4.8", "210.2.4.8"),
    ("Google DNS", "8.8.8.8", "8.8.4.4"),
    ("Cloudflare", "1.1.1.1", "1.0.0.1"),
    ("OpenDNS", "208.67.222.222", "208.67.220.220"),
]
TEST_DOMAINS = ("www.baidu.com", "www.qq.com", "github.com")
HOSTS_PATH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "drivers", "etc", "hosts")

# 加密 DNS 默认候选（名称, DoH 端点, 对应服务器地址）；DoT 固定用 853 端口。
# 这两份列表只是「默认值」，实际使用的那份可以在窗口里点「编辑列表」改，存到配置文件。
DOH_LIST = [
    ("阿里 DoH", "https://dns.alidns.com/dns-query", "223.5.5.5"),
    ("腾讯 DoH", "https://doh.pub/dns-query", "119.29.29.29"),
    ("360 DoH", "https://doh.360.cn/dns-query", "101.226.4.6"),
    ("Cloudflare（境外）", "https://cloudflare-dns.com/dns-query", "1.1.1.1"),
    ("Google（境外）", "https://dns.google/dns-query", "8.8.8.8"),
    ("Quad9（境外）", "https://dns.quad9.net/dns-query", "9.9.9.9"),
]
DOT_LIST = [
    ("阿里 DoT", "223.5.5.5", "dns.alidns.com"),
    ("腾讯 DoT", "119.29.29.29", "dot.pub"),
    ("Cloudflare（境外）", "1.1.1.1", "cloudflare-dns.com"),
    ("Google（境外）", "8.8.8.8", "dns.google"),
    ("Quad9（境外）", "9.9.9.9", "dns.quad9.net"),
]
QTYPE_A, QTYPE_TXT, QTYPE_HTTPS = 1, 16, 65
SVCB_ECH = 5           # SvcParamKey: ech（RFC 9460）
SVCB_ALPN = 1

# ---------------- 系统命令 ----------------
def _run(cmd, timeout=25):
    """跑一条命令（隐藏控制台），返回 (成功?, 输出)"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, text=True,
                           encoding="gbk", errors="replace")
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return False, "执行失败：%s" % e

def _ps(script, timeout=25):
    return _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", script], timeout)

def dns_latency(server, domain, timeout=1.2, tries=1):
    """直接向指定 DNS 服务器发一次 A 查询，返回耗时（毫秒）；失败返回 None。

    这一步只做“问一句答一句”，不改动任何系统设置。
    """
    tid = int(time.time() * 1000) & 0xFFFF
    q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for part in domain.split("."):
        b = part.encode("ascii", "ignore")
        q += bytes([len(b)]) + b
    q += b"\x00" + struct.pack(">HH", 1, 1)
    best = None
    for _ in range(tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.perf_counter()
        try:
            s.sendto(q, (server, 53))
            data, _addr = s.recvfrom(512)
            dt = (time.perf_counter() - t0) * 1000
            if data[:2] == struct.pack(">H", tid):
                best = dt if best is None else min(best, dt)
        except Exception:
            pass
        finally:
            s.close()
    return best

def list_interfaces():
    """一次查询拿到 [(网卡名, 当前DNS), ...]：PowerShell 启动慢，少调一次是一次"""
    ok, out = _ps("Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { "
                  "$n = $_.Name; "
                  "$d = (Get-DnsClientServerAddress -InterfaceAlias $n "
                  "-AddressFamily IPv4).ServerAddresses -join ', '; "
                  '"$n|$d" }')
    rows = []
    if ok:
        for line in out.splitlines():
            line = line.strip()
            if "|" in line:
                name, _, dns = line.partition("|")
                rows.append((name.strip(), dns.strip()))
    return rows

def current_dns(alias):
    ok, out = _ps("(Get-DnsClientServerAddress -InterfaceAlias '%s' "
                  "-AddressFamily IPv4).ServerAddresses -join ', '" % alias)
    return out if ok else ""

def set_dns(alias, servers):
    arr = ",".join("'%s'" % s for s in servers if s)
    return _ps("Set-DnsClientServerAddress -InterfaceAlias '%s' "
               "-ServerAddresses (%s)" % (alias, arr))

def reset_dns(alias):
    return _ps("Set-DnsClientServerAddress -InterfaceAlias '%s' "
               "-ResetServerAddresses" % alias)

def flush_dns():
    return _run(["ipconfig", "/flushdns"])

# ---------------- 列表读写（可在窗口里编辑，存到配置文件） ----------------
def load_doh_list(app=None):
    """从配置读 DoH 列表；没配过就用默认表。传 app 走内存快照，避免被主程序覆盖"""
    cfg = app.cfg if app is not None else load_cfg()
    rows = cfg.get("doh_list")
    out = []
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("url"):
                out.append((str(r.get("name") or r["url"]), str(r["url"]),
                            str(r.get("ip") or "")))
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                out.append((str(r[0]), str(r[1]),
                            str(r[2]) if len(r) > 2 else ""))
    return out or [tuple(x) for x in DOH_LIST]

def load_dot_list(app=None):
    """从配置读 DoT 列表；没配过就用默认表。传 app 走内存快照，避免被主程序覆盖"""
    cfg = app.cfg if app is not None else load_cfg()
    rows = cfg.get("dot_list")
    out = []
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("ip"):
                out.append((str(r.get("name") or r["ip"]), str(r["ip"]),
                            str(r.get("sni") or "")))
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                out.append((str(r[0]), str(r[1]),
                            str(r[2]) if len(r) > 2 else ""))
    return out or [tuple(x) for x in DOT_LIST]

def save_lists(doh, dot, app=None):
    """把编辑后的两份列表写进配置文件；传 app 走内存快照，避免被主程序覆盖"""
    cfg = app.cfg if app is not None else load_cfg()
    cfg["doh_list"] = [{"name": n, "url": u, "ip": ip} for n, u, ip in doh]
    cfg["dot_list"] = [{"name": n, "ip": ip, "sni": sni} for n, ip, sni in dot]
    save_cfg(cfg)

def friendly_err(e):
    """把晦涩的网络异常翻译成人话"""
    s = str(e)
    low = s.lower()
    if "10054" in s or "10053" in s or "reset" in low:
        return "连接被重置（请求被中途打断，通常是线路上的设备干的）"
    if "timed out" in low or "timeout" in low or "10060" in s:
        return "连接超时（线路不通或对端不通）"
    if "certificate" in low or "ssl" in low:
        return "TLS / 证书校验失败"
    if "getaddrinfo" in low or "11001" in s or "name or service" in low:
        return "域名无法解析"
    if "refused" in low or "10061" in s:
        return "连接被拒绝"
    return s[:80]

# ---------------- DNS 报文（构造/解析，DoH、DoT、ECH 共用） ----------------
def build_query(domain, qtype=QTYPE_A, tid=None, edns=True):
    """按 RFC 1035 构造查询报文，附带 EDNS0（HTTPS 记录较大，需要它）"""
    if tid is None:
        tid = int(time.time() * 1000) & 0xFFFF
    pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 1 if edns else 0)
    for part in domain.rstrip(".").split("."):
        b = part.encode("ascii", "ignore")
        pkt += bytes([len(b)]) + b
    pkt += b"\x00" + struct.pack(">HH", qtype, 1)
    if edns:
        pkt += b"\x00" + struct.pack(">HHIH", 41, 1232, 0, 0)
    return pkt, tid

def _read_name(data, off):
    """读域名（支持压缩指针），返回 (名字, 指针之后的偏移)"""
    labels, jumped, end = [], False, off
    for _ in range(128):
        if off >= len(data):
            break
        n = data[off]
        if n == 0:
            if not jumped:
                end = off + 1
            break
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                break
            ptr = ((n & 0x3F) << 8) | data[off + 1]
            if not jumped:
                end = off + 2
            jumped = True
            off = ptr
            continue
        labels.append(data[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
        if not jumped:
            end = off
    return ".".join(labels), end

def parse_dns(data):
    """极简响应解析：{rcode, records: [(name, type, ttl, rdata)]}"""
    if len(data) < 12:
        raise ValueError("响应过短")
    _tid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    off = 12
    for _ in range(qd):
        _name, off = _read_name(data, off)
        off += 4
    records = []
    for _ in range(an + ns + ar):
        name, off = _read_name(data, off)
        if off + 10 > len(data):
            break
        rtype, _cls, ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        records.append((name, rtype, ttl, data[off:off + rdlen]))
        off += rdlen
    return {"rcode": flags & 0x0F, "records": records}

def parse_https_rr(rdata):
    """解析 HTTPS/SVCB 记录：返回 (优先级, 目标名, {参数键: 原始值})"""
    if len(rdata) < 3:
        return None
    pri = struct.unpack(">H", rdata[:2])[0]
    target, off = _read_name(rdata, 2)
    params = {}
    while off + 4 <= len(rdata):
        key, ln = struct.unpack(">HH", rdata[off:off + 4])
        off += 4
        params[key] = rdata[off:off + ln]
        off += ln
    return pri, target, params

def _alpn_names(raw):
    out, off = [], 0
    while off < len(raw):
        n = raw[off]
        out.append(raw[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
    return ", ".join(x for x in out if x)

# ---------------- DoH / DoT ----------------
def doh_request(url, domain, qtype=QTYPE_A, timeout=6):
    """按 RFC 8484 发一次 DoH 查询（POST wire format），返回响应报文"""
    pkt, _tid = build_query(domain, qtype)
    req = urllib.request.Request(url, data=pkt, method="POST", headers={
        "Content-Type": "application/dns-message",
        "Accept": "application/dns-message",
        "User-Agent": "VBHelper/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def doh_test(url, domain="www.baidu.com", timeout=6):
    """测 DoH 往返延迟并核对解析结果，返回 (毫秒, 说明) 或 (None, 错误)"""
    t0 = time.perf_counter()
    try:
        data = doh_request(url, domain, QTYPE_A, timeout)
    except Exception as e:
        return None, friendly_err(e)
    rtt = (time.perf_counter() - t0) * 1000
    try:
        info = parse_dns(data)
    except Exception as e:
        return None, "解析失败：%s" % e
    ips = [".".join(str(b) for b in rdata)
           for _n, t, _ttl, rdata in info["records"]
           if t == QTYPE_A and len(rdata) == 4]
    if not ips:
        return None, "没有解析结果（rcode=%d）" % info["rcode"]
    return rtt, ips[0] + (" 等" if len(ips) > 1 else "")

def dot_test(server, sni, domain="www.baidu.com", timeout=6):
    """DoT：TLS 握手后按 RFC 7858 查询，返回 (毫秒, 证书说明) 或 (None, 错误)"""
    ctx = ssl.create_default_context()
    t0 = time.perf_counter()
    try:
        with socket.create_connection((server, 853), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=sni) as s:
                cert = s.getpeercert() or {}
                pkt, _tid = build_query(domain, QTYPE_A)
                s.sendall(struct.pack(">H", len(pkt)) + pkt)
                head = s.recv(2)
                if len(head) < 2:
                    return None, "对端未返回数据"
                need = struct.unpack(">H", head)[0]
                buf = b""
                while len(buf) < need:
                    chunk = s.recv(need - len(buf))
                    if not chunk:
                        break
                    buf += chunk
        rtt = (time.perf_counter() - t0) * 1000
        if parse_dns(buf)["rcode"] != 0:
            return None, "查询返回非 0 状态码"
    except Exception as e:
        return None, friendly_err(e)
    issuer = ""
    for part in cert.get("issuer", ()):
        for k, v in part:
            if k == "organizationName":
                issuer = v
    return rtt, "证书已校验" + ("（%s 签发）" % issuer if issuer else "")

# ---------------- ECH 检测 ----------------
def ech_check(domain, timeout=6, doh_list=None):
    """查目标域名的 HTTPS 记录，看服务端是否提供 ECH 配置（RFC 9460）。

    ECH 是取代旧 ESNI 的标准化机制；能否实际用上取决于客户端（浏览器）是否支持。
    这里只做“服务端是否提供配置”的检测；doh_list 不传就用当前配置的那份。
    """
    out = {"domain": domain, "error": None, "https": [], "ech": None,
           "alpn": "", "esni": None}
    sources = doh_list or load_doh_list()
    data, last_err = None, None
    for _name, url, _ip in sources:
        try:
            data = doh_request(url, domain, QTYPE_HTTPS, timeout)
            break
        except Exception as e:
            last_err = e
    if data is None:
        out["error"] = "所有 DoH 源都不可达（%s）" % friendly_err(last_err)
    else:
        try:
            info = parse_dns(data)
            for _n, rtype, _ttl, rdata in info["records"]:
                if rtype != QTYPE_HTTPS:
                    continue
                parsed = parse_https_rr(rdata)
                if not parsed:
                    continue
                pri, target, params = parsed
                row = {"priority": pri, "target": target or ".",
                       "ech": SVCB_ECH in params}
                if row["ech"]:
                    out["ech"] = len(params[SVCB_ECH])
                if SVCB_ALPN in params:
                    out["alpn"] = _alpn_names(params[SVCB_ALPN])
                out["https"].append(row)
        except Exception as e:
            out["error"] = "解析失败：%s" % e
    # 旧机制 ESNI 的 _esni.<domain> TXT，仅作参考
    try:
        for _name, url, _ip in sources:
            data = doh_request(url, "_esni." + domain, QTYPE_TXT, timeout=4)
            txt = ""
            for _n, rtype, _ttl, rdata in parse_dns(data)["records"]:
                if rtype == QTYPE_TXT and rdata:
                    txt += rdata[1:1 + rdata[0]].decode("ascii", "replace")
            if txt:
                out["esni"] = len(txt)
                break
    except Exception:
        pass
    return out

# ---------------- Windows 原生 DoH 配置 ----------------
def system_doh_list():
    """系统里已登记的 DoH 服务器（Windows 11）"""
    ok, out = _ps("Get-DnsClientDohServerAddress | ForEach-Object { "
                  '"$($_.ServerAddress)|$($_.DohTemplate)|$($_.AutoUpgrade)" }')
    rows = []
    if ok:
        for line in out.splitlines():
            line = line.strip()
            if "|" in line:
                addr, _, rest = line.partition("|")
                tpl, _, auto = rest.partition("|")
                rows.append((addr.strip(), tpl.strip(), auto.strip()))
    return rows

def enable_system_doh(server, template):
    """登记 DoH 模板并开启自动升级（Windows 11 原生 DoH）"""
    ps = ("$ErrorActionPreference='SilentlyContinue'; "
          "Set-DnsClientDohServerAddress -ServerAddress '%s' -DohTemplate '%s' "
          "-AllowFallbackToUdp $false -AutoUpgrade $true; "
          "if (-not $?) { Add-DnsClientDohServerAddress -ServerAddress '%s' "
          "-DohTemplate '%s' -AllowFallbackToUdp $false -AutoUpgrade $true }"
          % (server, template, server, template))
    return _ps(ps)

def disable_system_doh(server):
    return _ps("Remove-DnsClientDohServerAddress -ServerAddress '%s' "
               "-ErrorAction SilentlyContinue; 'done'" % server)

# ---------------- 窗口 ----------------
class NetToolsWindow:
    def __init__(self, app):
        self.app = app
        self.bench_q = queue.Queue()
        self.diag_q = queue.Queue()
        self.msg_q = queue.Queue()     # 后台线程 -> UI 的唯一通道（线程不碰 tk）
        self.crypto_q = queue.Queue()
        self.ech_q = queue.Queue()
        self.iface_dns = {}
        self.doh_list = load_doh_list(app)
        self.dot_list = load_dot_list(app)
        self.busy = False

        win = tk.Toplevel(app.root)
        self.win = win
        win.title("网络工具 - " + APP_NAME)
        win.configure(bg=C_BG)
        win.geometry("780x580")
        win.minsize(640, 460)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.bind("<Escape>", lambda e: self._try_close())
        win.protocol("WM_DELETE_WINDOW", self._try_close)

        self._init_style()
        self._build_head()
        self._build_tabs()
        self._build_dns_tab()
        self._build_crypto_tab()
        self._build_ech_tab()
        self._build_diag_tab()
        self._build_hosts_tab()
        self._build_status()

        self.show_tab("dns")
        self._center()
        self.refresh_interfaces()
        self.reload_hosts()
        self.win.after(150, self._pump_msgs)

    def _pump_msgs(self):
        """主线程轮询后台消息，统一在这里改界面"""
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "ifaces":
                    self._set_interfaces(payload)
                elif kind == "status":
                    self.say(payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.win.after(150, self._pump_msgs)
        except tk.TclError:
            pass

    # ---- 外壳 ----
    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Net.Treeview", background=C_PANEL, foreground=C_TEXT,
                     fieldbackground=C_PANEL, borderwidth=0, rowheight=24,
                     font=(core.FONT, 9))
        st.map("Net.Treeview", background=[("selected", C_SELECT)],
               foreground=[("selected", "#ffffff")])
        st.configure("Net.Vertical.TScrollbar", background="#3a3d46",
                     troughcolor=C_PANEL, bordercolor=C_PANEL, arrowcolor=C_MUTED,
                     borderwidth=0, width=12)

    def _center(self):
        """摆位置交给主类统一处理：它会连标题栏一起算，免得窗口探出屏幕把底部按钮顶出去"""
        self.app._center_on_panel(self.win)

    def _build_head(self):
        head = tk.Frame(self.win, bg=C_BG)
        head.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(head, text="网络工具", bg=C_BG, fg=C_TEXT,
                 font=(core.FONT, 12, "bold")).pack(side="left")
        admin = "管理员" if self._is_admin() else "普通用户"
        color = "#7ee787" if self._is_admin() else "#ffb86b"
        tk.Label(head, text="当前权限：" + admin, bg=C_BG, fg=color,
                 font=(core.FONT, 9)).pack(side="right")

    def _is_admin(self):
        from . import elevate
        return elevate.is_admin()

    def _build_tabs(self):
        row = tk.Frame(self.win, bg=C_BG)
        row.pack(fill="x", padx=16, pady=(2, 0))
        self.tab_btns = {}
        for key, label in (("dns", "DNS 优选"), ("crypto", "加密 DNS"),
                           ("ech", "ECH 检测"), ("diag", "网络诊断"),
                           ("hosts", "hosts 编辑")):
            b = tk.Label(row, text=label, bg=C_BG, fg=C_MUTED, font=(core.FONT, 10),
                         padx=14, pady=6, cursor="hand2")
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda e, k=key: self.show_tab(k))
            self.tab_btns[key] = b
        tk.Frame(self.win, bg=C_BORDER, height=1).pack(fill="x", padx=16, pady=(6, 0))

    def _build_status(self):
        tk.Frame(self.win, bg=C_BORDER, height=1).pack(side="bottom", fill="x")
        self.status = tk.Label(self.win, text="就绪", bg=C_BG, fg=C_MUTED,
                               font=(core.FONT, 8), anchor="w")
        self.status.pack(side="bottom", fill="x", padx=16, pady=4)

    def say(self, msg):
        try:
            self.status.configure(text=msg)
        except tk.TclError:
            pass

    def show_tab(self, key):
        for k, b in self.tab_btns.items():
            if k == key:
                b.configure(bg=C_HOVER, fg=C_TEXT)
            else:
                b.configure(bg=C_BG, fg=C_MUTED)
        for k, f in (("dns", self.tab_dns), ("crypto", self.tab_crypto),
                     ("ech", self.tab_ech), ("diag", self.tab_diag),
                     ("hosts", self.tab_hosts)):
            if k == key:
                f.pack(fill="both", expand=True, padx=16, pady=12)
            else:
                f.pack_forget()

    def _btn(self, parent, text, cmd, accent=False):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=C_ACCENT if accent else C_PANEL, fg="white" if accent else C_TEXT,
                      activebackground=C_ACCENT_H if accent else C_HOVER,
                      activeforeground="white", relief="flat", bd=0, padx=14, pady=5,
                      font=(core.FONT, 9), cursor="hand2")
        return b

    # ---- DNS 优选 ----
    def _build_dns_tab(self):
        f = tk.Frame(self.win, bg=C_BG)
        self.tab_dns = f

        top = tk.Frame(f, bg=C_BG)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(top, text="网卡", bg=C_BG, fg=C_MUTED, font=(core.FONT, 9)).pack(side="left")
        self.iface_var = tk.StringVar(value="")
        self.iface_menu = tk.OptionMenu(top, self.iface_var, "")
        self.iface_menu.configure(bg=C_PANEL, fg=C_TEXT, activebackground=C_HOVER,
                                  relief="flat", bd=0, highlightthickness=0,
                                  font=(core.FONT, 9), padx=8)
        self.iface_menu["menu"].configure(bg=C_PANEL, fg=C_TEXT,
                                          activebackground=C_ACCENT)
        self.iface_menu.pack(side="left", padx=(6, 6))
        self._btn(top, "刷新网卡", self.refresh_interfaces).pack(side="left")
        self._btn(top, "开始测速", lambda: self.start_bench(), accent=True).pack(side="right")
        self._btn(top, "恢复自动获取", self.use_auto_dns).pack(side="right", padx=6)

        cols = ("name", "pri", "sec", "ms")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", style="Net.Treeview")
        for c, t, w in (("name", "DNS 名称", 140), ("pri", "主 DNS", 160),
                        ("sec", "备用 DNS", 160), ("ms", "延迟 / 可用性", 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c != "ms" else "center")
        self.tree.tag_configure("best", foreground="#7ee787")
        self.tree.tag_configure("bad", foreground="#ff7b72")
        vsb = ttk.Scrollbar(f, style="Net.Vertical.TScrollbar", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-Button-1>", lambda e: self.apply_dns())

        btns = tk.Frame(f, bg=C_BG)
        btns.pack(fill="x", pady=(8, 0))
        self._btn(btns, "应用到选中的 DNS", self.apply_dns, accent=True).pack(side="left")
        self._btn(btns, "刷新 DNS 缓存", lambda: self.do_flush()).pack(side="left", padx=6)
        tk.Label(btns, text="测速原理：直接向各 DNS 发域名查询比响应时间，不改动系统设置；"
                            "国内 DNS 通常比国外快很多。",
                 bg=C_BG, fg=C_MUTED, font=(core.FONT, 8)).pack(side="left", padx=10)

    def refresh_interfaces(self):
        self.say("正在读取网卡列表…")
        def work():
            names = list_interfaces()
            self.msg_q.put(("ifaces", names))
        threading.Thread(target=work, daemon=True).start()

    def _set_interfaces(self, rows):
        try:
            menu = self.iface_menu["menu"]
            menu.delete(0, "end")
            self.iface_dns = {}
            if not rows:
                self.say("没有找到已连接的网络接口，点“刷新网卡”重试")
                return
            for name, dns in rows:
                self.iface_dns[name] = dns
                menu.add_command(label=name, command=lambda v=name: self._pick_iface(v))
            self._pick_iface(rows[0][0])
        except tk.TclError:
            pass

    def _pick_iface(self, name):
        self.iface_var.set(name)
        cur = self.iface_dns.get(name, "")
        self.say("网卡 %s 当前 DNS：%s" % (name, cur or "自动获取"))

    def start_bench(self):
        if self.busy:
            self.say("正在测速，请稍候…")
            return
        self.busy = True
        for i in self.tree.get_children():
            self.tree.delete(i)
        for name, pri, sec in DNS_LIST:
            self.tree.insert("", "end", values=(name, pri, sec or "-", "排队中…"))
        self.say("测速中…（对每个 DNS 问 3 个域名，取平均）")

        tasks = queue.Queue()
        for item in DNS_LIST:
            tasks.put(item)

        def worker():
            while True:
                try:
                    name, pri, sec = tasks.get_nowait()
                except queue.Empty:
                    return
                vals = []
                for dom in TEST_DOMAINS:
                    v = dns_latency(pri, dom)
                    if v is not None:
                        vals.append(v)
                avg = sum(vals) / len(vals) if vals else None
                self.bench_q.put((name, avg, len(vals)))

        for _ in range(6):
            threading.Thread(target=worker, daemon=True).start()
        self.win.after(150, self._poll_bench)

    def _poll_bench(self):
        done = 0
        try:
            while True:
                name, avg, ok_n = self.bench_q.get_nowait()
                done += 1
                for iid in self.tree.get_children():
                    v = self.tree.item(iid, "values")
                    if v and v[0] == name:
                        if avg is None:
                            self.tree.item(iid, values=(v[0], v[1], v[2], "不可达"),
                                           tags=("bad",))
                        else:
                            self.tree.item(iid, values=(v[0], v[1], v[2],
                                                        "%.0f ms（%d/3 可用）" % (avg, ok_n)))
                        break
        except queue.Empty:
            pass
        if done:
            self._sort_and_mark()
        if self.busy and self._bench_remaining() == 0:
            self.busy = False
            self.say("测速完成：延迟越低越适合当前网络，可双击一行应用")
        if self.busy:
            self.win.after(150, self._poll_bench)

    def _bench_remaining(self):
        n = 0
        for iid in self.tree.get_children():
            v = self.tree.item(iid, "values")
            if v and ("ms" not in v[3] and v[3] != "不可达"):
                n += 1
        return n

    def _sort_and_mark(self):
        rows = []
        for iid in self.tree.get_children():
            v = self.tree.item(iid, "values")
            try:
                ms = float(str(v[3]).split()[0])
            except Exception:
                ms = None
            rows.append((ms, iid, v))
        rows.sort(key=lambda r: (r[0] is None, r[0] if r[0] is not None else 0))
        for idx, (ms, iid, v) in enumerate(rows):
            self.tree.move(iid, "", idx)
            if idx == 0 and ms is not None:
                self.tree.item(iid, tags=("best",))

    def apply_dns(self):
        sel = self.tree.selection()
        if not sel:
            self.say("先在列表里选中一行")
            return
        alias = self.iface_var.get()
        if not alias:
            self.say("没有选中网卡")
            return
        v = self.tree.item(sel[0], "values")
        pri, sec = v[1], "" if v[2] == "-" else v[2]
        self._do_set_dns(alias, pri, sec)

    def _do_set_dns(self, alias, pri, sec):
        self.say("正在设置 %s 的 DNS 为 %s …" % (alias, pri))
        def work():
            ok = set_dns(alias, [pri, sec])
            self.msg_q.put(("status", ("已把 %s 的 DNS 设为 %s" % (alias, pri)) if ok
                            else "设置失败：请确认程序以管理员身份运行"))
        threading.Thread(target=work, daemon=True).start()

    def use_auto_dns(self):
        alias = self.iface_var.get()
        if not alias:
            self.say("没有选中网卡")
            return
        self.say("正在把 %s 恢复为自动获取 DNS…" % alias)
        def work():
            ok = reset_dns(alias)
            self.msg_q.put(("status", "已恢复自动获取" if ok
                            else "恢复失败：需要管理员权限"))
        threading.Thread(target=work, daemon=True).start()

    def do_flush(self):
        def work():
            ok, out = flush_dns()
            self.msg_q.put(("status", "DNS 缓存已刷新" if ok else out))
        threading.Thread(target=work, daemon=True).start()

    # ---- 加密 DNS（DoH / DoT） ----
    def _build_crypto_tab(self):
        f = tk.Frame(self.win, bg=C_BG)
        self.tab_crypto = f
        tk.Label(f, text="DoH（DNS over HTTPS）和 DoT（DNS over TLS）会把 DNS 查询加密后再发出去。"
                         "「系统 DoH」按钮走 Windows 11 原生配置（需要管理员）。",
                 bg=C_BG, fg=C_MUTED, font=(core.FONT, 8), justify="left",
                 wraplength=720).pack(anchor="w", pady=(0, 8))
        row = tk.Frame(f, bg=C_BG)
        row.pack(fill="x", pady=(0, 6))
        self._btn(row, "测速 DoH", lambda: self.bench_crypto("doh"), accent=True).pack(side="left")
        self._btn(row, "测速 DoT", lambda: self.bench_crypto("dot")).pack(side="left", padx=6)
        self._btn(row, "启用选中为系统 DoH", self.apply_system_doh).pack(side="left")
        self._btn(row, "移除选中 DoH", self.remove_system_doh).pack(side="left", padx=6)
        self._btn(row, "编辑列表…", self.edit_lists).pack(side="left", padx=6)
        self._btn(row, "查看系统 DoH", self.show_system_doh).pack(side="right")

        cols = ("kind", "name", "addr", "ms")
        self.crypto_tree = ttk.Treeview(f, columns=cols, show="headings",
                                        style="Net.Treeview")
        for c, t, w in (("kind", "类型", 60), ("name", "名称", 120),
                        ("addr", "端点 / 服务器", 300), ("ms", "延迟 / 状态", 230)):
            self.crypto_tree.heading(c, text=t)
            self.crypto_tree.column(c, width=w,
                                    anchor="w" if c != "ms" else "center")
        self.crypto_tree.tag_configure("ok", foreground="#7ee787")
        self.crypto_tree.tag_configure("bad", foreground="#ff7b72")
        vsb = ttk.Scrollbar(f, style="Net.Vertical.TScrollbar",
                            command=self.crypto_tree.yview)
        self.crypto_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.crypto_tree.pack(fill="both", expand=True)

    def bench_crypto(self, kind):
        """分别测 DoH / DoT：每个目标一个线程，结果回主线程渲染"""
        tree = self.crypto_tree
        for i in tree.get_children():
            tree.delete(i)
        if kind == "doh":
            tasks = [("DoH", n, u, "") for n, u, _ip in self.doh_list]
        else:
            tasks = [("DoT", n, ip, sni) for n, ip, sni in self.dot_list]
        for k, n, a, sni in tasks:
            tree.insert("", "end", values=(k, n, a + (":" + sni if sni else ""), "排队中…"))
        self.say("正在测 %s…（加密握手比明文 DNS 慢一些是正常的）" % kind.upper())

        def worker(k, n, a, sni):
            if k == "DoH":
                ms, note = doh_test(a)
            else:
                ms, note = dot_test(a, sni)
            text = ("%.0f ms" % ms) if ms is not None else "不可达"
            self.crypto_q.put((k, n, text, note))

        for k, n, a, sni in tasks:
            threading.Thread(target=worker, args=(k, n, a, sni), daemon=True).start()
        self.win.after(200, self._poll_crypto)

    def _poll_crypto(self):
        got = 0
        try:
            while True:
                k, name, text, note = self.crypto_q.get_nowait()
                got += 1
                for iid in self.crypto_tree.get_children():
                    v = self.crypto_tree.item(iid, "values")
                    if v and v[0] == k and v[1] == name:
                        tail = (" — " + note[:58]) if note else ""
                        self.crypto_tree.item(
                            iid, values=(v[0], v[1], v[2], text + tail),
                            tags=("ok" if text.endswith("ms") else "bad",))
                        break
        except queue.Empty:
            pass
        except tk.TclError:
            return
        pending = any("排队中" in str(self.crypto_tree.item(i, "values")[3])
                      for i in self.crypto_tree.get_children())
        if got or pending:
            self.win.after(200, self._poll_crypto)

    def _sel_doh(self):
        """取选中的 DoH 行 -> (服务器IP, 端点)"""
        sel = self.crypto_tree.selection()
        if not sel:
            return None, None
        v = self.crypto_tree.item(sel[0], "values")
        if not v or v[0] != "DoH":
            return None, None
        for _n, url, ip in self.doh_list:
            if url == v[2]:
                return ip, url
        return None, None

    def apply_system_doh(self):
        ip, url = self._sel_doh()
        if not ip:
            self.say("先在列表里选中一个 DoH 条目")
            return
        self.say("正在登记系统 DoH：%s → %s" % (ip, url))

        def work():
            ok = enable_system_doh(ip, url)
            self.msg_q.put(("status", ("已登记系统 DoH：%s，把网卡 DNS 设为 %s 后即走加密解析"
                                       % (ip, ip)) if ok
                            else "登记失败：需要管理员权限，且系统需为 Windows 11"))
        threading.Thread(target=work, daemon=True).start()

    def remove_system_doh(self):
        ip, _url = self._sel_doh()
        if not ip:
            self.say("先选中一个 DoH 条目")
            return

        def work():
            disable_system_doh(ip)
            self.msg_q.put(("status", "已移除系统 DoH 配置：%s" % ip))
        threading.Thread(target=work, daemon=True).start()

    def show_system_doh(self):
        def work():
            rows = system_doh_list()
            txt = ("；".join("%s → %s（自动升级 %s）" % r for r in rows) if rows
                   else "系统里还没有登记 DoH 服务器（该功能需要 Windows 11）")
            self.msg_q.put(("status", txt))
        threading.Thread(target=work, daemon=True).start()

    def edit_lists(self):
        """编辑 DoH / DoT 候选列表并保存到配置文件，改完可立刻测速验证"""
        w = tk.Toplevel(self.win)
        w.title("编辑加密 DNS 列表 - " + APP_NAME)
        w.configure(bg=C_BG)
        w.geometry("720x600")
        w.minsize(560, 420)
        try:
            w.attributes("-topmost", True)
        except Exception:
            pass
        w.bind("<Escape>", lambda e: w.destroy())

        bar = tk.Frame(w, bg=C_BG)
        bar.pack(side="bottom", fill="x", padx=14, pady=(0, 12))

        tk.Label(w, text="每行一个，用竖线分隔：DoH 填「名称 | 完整 URL | 服务器IP(可留空)」；"
                         "DoT 填「名称 | 服务器IP | SNI 域名」。# 开头的行会被忽略，"
                         "留空的行自动跳过。保存后回到列表点“测速”即可验证。",
                 bg=C_BG, fg=C_MUTED, font=(core.FONT, 8), justify="left",
                 wraplength=680).pack(anchor="w", padx=14, pady=(12, 6))

        tk.Label(w, text="DoH 列表", bg=C_BG, fg=C_TEXT,
                 font=(core.FONT, 9, "bold")).pack(anchor="w", padx=14)
        doh_txt = tk.Text(w, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT, bd=0,
                          relief="flat", highlightthickness=0, height=8,
                          font=("Consolas", 9), padx=8, pady=6)
        doh_txt.pack(fill="both", expand=True, padx=14, pady=(2, 10))
        doh_txt.insert("1.0", "\n".join("%s | %s | %s" % (n, u, ip)
                                        for n, u, ip in self.doh_list))

        tk.Label(w, text="DoT 列表", bg=C_BG, fg=C_TEXT,
                 font=(core.FONT, 9, "bold")).pack(anchor="w", padx=14)
        dot_txt = tk.Text(w, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT, bd=0,
                          relief="flat", highlightthickness=0, height=6,
                          font=("Consolas", 9), padx=8, pady=6)
        dot_txt.pack(fill="both", expand=True, padx=14, pady=(2, 10))
        dot_txt.insert("1.0", "\n".join("%s | %s | %s" % (n, ip, sni)
                                        for n, ip, sni in self.dot_list))

        def parse(txt):
            rows = []
            for line in txt.get("1.0", "end").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")] + ["", "", ""]
                name, a, b = parts[0], parts[1], parts[2]
                if not a:
                    continue
                rows.append((name or a, a, b))
            return rows

        def save():
            doh = [(n, u if u.lower().startswith("http") else "https://" + u, ip)
                   for n, u, ip in parse(doh_txt)]
            dot = [(n, ip, sni) for n, ip, sni in parse(dot_txt)]
            if not doh and not dot:
                messagebox.showwarning(APP_NAME, "两份列表不能都是空的。", parent=w)
                return
            self.doh_list, self.dot_list = doh, dot
            save_lists(doh, dot, self.app)
            for i in self.crypto_tree.get_children():
                self.crypto_tree.delete(i)
            self.say("列表已保存：DoH %d 个，DoT %d 个（点“测速”验证可用性）"
                     % (len(doh), len(dot)))
            w.destroy()

        def restore():
            doh_txt.delete("1.0", "end")
            doh_txt.insert("1.0", "\n".join("%s | %s | %s" % (n, u, ip)
                                            for n, u, ip in DOH_LIST))
            dot_txt.delete("1.0", "end")
            dot_txt.insert("1.0", "\n".join("%s | %s | %s" % (n, ip, sni)
                                            for n, ip, sni in DOT_LIST))

        self._btn(bar, "保存", save, accent=True).pack(side="right")
        self._btn(bar, "取消", w.destroy).pack(side="right", padx=6)
        self._btn(bar, "填入默认列表", restore).pack(side="left")

    # ---- ECH 检测 ----
    def _build_ech_tab(self):
        f = tk.Frame(self.win, bg=C_BG)
        self.tab_ech = f
        tk.Label(f, text="ECH（Encrypted Client Hello）是取代旧 ESNI 的标准化机制，用于加密 TLS 握手"
                         "里的服务器名。这里检测“服务端是否已提供 ECH 配置”（查 HTTPS 记录中的 ech 参数），"
                         "能否用上还取决于浏览器是否支持；多个域名用逗号分隔。",
                 bg=C_BG, fg=C_MUTED, font=(core.FONT, 8), justify="left",
                 wraplength=720).pack(anchor="w", pady=(0, 8))
        row = tk.Frame(f, bg=C_BG)
        row.pack(fill="x", pady=(0, 8))
        tk.Label(row, text="域名", bg=C_BG, fg=C_MUTED,
                 font=(core.FONT, 9)).pack(side="left")
        self.ech_var = tk.StringVar(value="cloudflare.com, www.wikipedia.org, www.mozilla.org")
        tk.Entry(row, textvariable=self.ech_var, bg=C_PANEL, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat", bd=0, font=(core.FONT, 9),
                 width=52).pack(side="left", padx=8, ipady=4)
        self._btn(row, "开始检测", self.run_ech, accent=True).pack(side="left")

        self.ech_out = tk.Text(f, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT, bd=0,
                               relief="flat", highlightthickness=0, wrap="word",
                               font=(core.FONT, 9), padx=10, pady=8)
        esb = ttk.Scrollbar(f, style="Net.Vertical.TScrollbar", command=self.ech_out.yview)
        self.ech_out.configure(yscrollcommand=esb.set)
        esb.pack(side="right", fill="y")
        self.ech_out.pack(fill="both", expand=True)

    def run_ech(self):
        domains = [d.strip() for d in self.ech_var.get().replace("，", ",").split(",")
                   if d.strip()]
        if not domains:
            self.say("先填一个域名")
            return
        self.ech_out.delete("1.0", "end")
        self.ech_out.insert("end", "检测中…（通过 DoH 查询 HTTPS 记录）\n")
        self.say("ECH 检测中…")

        def work():
            for d in domains:
                self.ech_q.put(("one", d, ech_check(d, doh_list=self.doh_list)))
            self.ech_q.put(("done", "", None))

        threading.Thread(target=work, daemon=True).start()
        self.win.after(200, self._poll_ech)

    def _poll_ech(self):
        done = False
        try:
            while True:
                kind, d, info = self.ech_q.get_nowait()
                if kind == "done":
                    done = True
                    break
                self._render_ech(d, info)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if done:
            self.say("ECH 检测完成")
        else:
            self.win.after(200, self._poll_ech)

    def _render_ech(self, domain, info):
        t = self.ech_out
        t.insert("end", "\n【%s】\n" % domain)
        if info.get("error"):
            t.insert("end", "  [失败] %s\n" % info["error"])
        else:
            hs = info.get("https") or []
            if hs:
                for r in hs:
                    t.insert("end", "  HTTPS 记录：优先级 %s，目标 %s\n"
                             % (r["priority"], r["target"]))
            else:
                t.insert("end", "  没有 HTTPS 记录\n")
            if info.get("ech"):
                t.insert("end", "  [OK] 提供 ECH 配置（ECHConfigList %d 字节）\n" % info["ech"])
            else:
                t.insert("end", "  [失败] 未提供 ECH 配置\n")
            if info.get("alpn"):
                t.insert("end", "  ALPN：%s\n" % info["alpn"])
            if info.get("esni"):
                t.insert("end", "  旧机制 ESNI 记录：有（%d 字节，已被 ECH 取代）\n"
                         % info["esni"])
        t.see("end")

    # ---- 网络诊断 ----
    def _build_diag_tab(self):
        f = tk.Frame(self.win, bg=C_BG)
        self.tab_diag = f
        row = tk.Frame(f, bg=C_BG)
        row.pack(fill="x", pady=(0, 8))
        tk.Label(row, text="目标", bg=C_BG, fg=C_MUTED, font=(core.FONT, 9)).pack(side="left")
        self.host_var = tk.StringVar(value="www.baidu.com")
        tk.Entry(row, textvariable=self.host_var, bg=C_PANEL, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat", bd=0, font=(core.FONT, 9),
                 width=28).pack(side="left", padx=8, ipady=4)
        self._btn(row, "Ping", lambda: self._diag("ping")).pack(side="left", padx=2)
        self._btn(row, "路由跟踪", lambda: self._diag("tracert")).pack(side="left", padx=2)
        self._btn(row, "本机网络信息", lambda: self._diag("ipconfig")).pack(side="left", padx=2)
        self._btn(row, "刷新 DNS 缓存", lambda: self._diag("flush")).pack(side="left", padx=2)
        self._btn(row, "清空", self._clear_out).pack(side="right")

        self.out = tk.Text(f, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT, bd=0,
                           relief="flat", highlightthickness=0, wrap="none",
                           font=("Consolas", 9), padx=10, pady=8)
        osb = ttk.Scrollbar(f, style="Net.Vertical.TScrollbar", command=self.out.yview)
        self.out.configure(yscrollcommand=osb.set)
        osb.pack(side="right", fill="y")
        self.out.pack(fill="both", expand=True)

    def _clear_out(self):
        self.out.delete("1.0", "end")

    def _diag(self, kind):
        host = self.host_var.get().strip() or "www.baidu.com"
        if kind == "flush":
            self._stream(["ipconfig", "/flushdns"], "刷新 DNS 缓存")
        elif kind == "ipconfig":
            self._stream(["ipconfig", "/all"], "本机网络信息")
        elif kind == "ping":
            self._stream(["ping", "-n", "4", host], "Ping " + host)
        else:
            self._stream(["tracert", "-d", "-h", "15", host], "路由跟踪 " + host)

    def _stream(self, cmd, title):
        self.out.insert("end", "\n=== %s ===\n" % title)
        self.out.see("end")
        self.diag_q = queue.Queue()

        def work():
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT,
                                     creationflags=CREATE_NO_WINDOW, text=True,
                                     encoding="gbk", errors="replace")
                for line in p.stdout:
                    self.diag_q.put(line.rstrip("\n"))
                self.diag_q.put(None)
            except Exception as e:
                self.diag_q.put("执行失败：%s" % e)
                self.diag_q.put(None)

        threading.Thread(target=work, daemon=True).start()
        self._poll_diag()

    def _poll_diag(self):
        got_end = False
        try:
            while True:
                line = self.diag_q.get_nowait()
                if line is None:
                    got_end = True
                    break
                self.out.insert("end", line + "\n")
                self.out.see("end")
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if not got_end:
            self.win.after(120, self._poll_diag)

    # ---- hosts 编辑 ----
    def _build_hosts_tab(self):
        f = tk.Frame(self.win, bg=C_BG)
        self.tab_hosts = f
        row = tk.Frame(f, bg=C_BG)
        row.pack(fill="x", pady=(0, 8))
        self.hosts_btn = {}
        self.hosts_btn["reload"] = self._btn(row, "重新加载", self.reload_hosts)
        self.hosts_btn["reload"].pack(side="left")
        self.hosts_btn["save"] = self._btn(row, "保存", self.save_hosts, accent=True)
        self.hosts_btn["save"].pack(side="left", padx=6)
        self.hosts_btn["backup"] = self._btn(row, "备份一份", self.backup_hosts)
        self.hosts_btn["backup"].pack(side="left")
        self._btn(row, "打开所在文件夹",
                  lambda: os.startfile(os.path.dirname(HOSTS_PATH))).pack(side="left", padx=6)
        tk.Label(row, text="保存 hosts 需要管理员权限；改完建议先点“备份一份”。",
                 bg=C_BG, fg=C_MUTED, font=(core.FONT, 8)).pack(side="left", padx=10)

        self.hosts_txt = tk.Text(f, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                                 bd=0, relief="flat", highlightthickness=0, wrap="none",
                                 font=("Consolas", 9), padx=10, pady=8)
        hsb = ttk.Scrollbar(f, style="Net.Vertical.TScrollbar", command=self.hosts_txt.yview)
        self.hosts_txt.configure(yscrollcommand=hsb.set)
        hsb.pack(side="right", fill="y")
        self.hosts_txt.pack(fill="both", expand=True)

    def _try_close(self):
        """关闭前检查 hosts 是否有未保存的修改"""
        try:
            current = self.hosts_txt.get("1.0", "end").rstrip("\n")
            original = self._hosts_original.rstrip("\n")
        except Exception:
            self.win.destroy()
            return
        if current != original:
            answer = messagebox.askyesnocancel(
                APP_NAME, "hosts 有未保存的修改，保存吗？", parent=self.win)
            if answer is None:
                return
            if answer:
                self.save_hosts()
        self.win.destroy()

    def reload_hosts(self):
        self._hosts_enc = "utf-8"
        self._hosts_original = ""
        try:
            raw = open(HOSTS_PATH, "rb").read()
            for enc in ("utf-8-sig", "gbk", "utf-16", "latin-1"):
                try:
                    data = raw.decode(enc)
                    self._hosts_enc = "utf-8" if enc == "latin-1" else enc
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            else:
                data = raw.decode("utf-8", errors="replace")
        except Exception as e:
            data = "# 读取失败：%s" % e
        self._hosts_original = data
        try:
            self.hosts_txt.delete("1.0", "end")
            self.hosts_txt.insert("1.0", data)
            self.say("已加载 %s（编码 %s）" % (HOSTS_PATH, self._hosts_enc))
        except tk.TclError:
            pass

    def save_hosts(self):
        data = self.hosts_txt.get("1.0", "end")
        enc = getattr(self, "_hosts_enc", "utf-8")
        try:
            # 先备份磁盘上的原文件，再原子替换，避免写一半留下残文件
            self._backup_hosts_file()
            tmp = HOSTS_PATH + ".vbhelper_tmp"
            with open(tmp, "wb") as fh:
                fh.write(data.encode(enc, errors="replace"))
            os.replace(tmp, HOSTS_PATH)
            self.say("hosts 已保存（%s，可能需要刷新 DNS 缓存生效）" % enc)
        except PermissionError:
            self.say("保存失败：没有权限，请确认以管理员身份运行")
            messagebox.showerror(APP_NAME, "保存 hosts 需要管理员权限。", parent=self.win)
        except Exception as e:
            self.say("保存失败：%s" % e)

    def _backup_hosts_file(self):
        """备份磁盘上的 hosts 原文件（不是编辑区草稿）"""
        try:
            dst = HOSTS_PATH + ".backup_" + time.strftime("%Y%m%d_%H%M%S")
            with open(HOSTS_PATH, "rb") as src, open(dst, "wb") as out:
                out.write(src.read())
        except Exception:
            pass  # 备份失败不阻断保存

    def backup_hosts(self):
        try:
            dst = HOSTS_PATH + ".backup_" + time.strftime("%Y%m%d_%H%M%S")
            with open(HOSTS_PATH, "rb") as src, open(dst, "wb") as out:
                out.write(src.read())
            self.say("已备份到 %s" % dst)
        except Exception as e:
            self.say("备份失败：%s" % e)

def open_window(app):
    """打开网络工具窗口；已经存在就拉到前面"""
    old = getattr(app, "_nettools_win", None)
    if old is not None:
        try:
            if old.win.winfo_exists():
                old.win.deiconify()
                old.win.lift()
                old.win.focus_force()
                return old
        except tk.TclError:
            pass
    win = NetToolsWindow(app)
    app._nettools_win = win
    return win
