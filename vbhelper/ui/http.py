# -*- coding: utf-8 -*-
"""HTTP 客户端：标准库实现 + SQLite 条件缓存 + SSRF 防护。

规则（计划文档 §3.8 / 架构 §7、§9）：
- 只用 urllib.request / http.client，不引入 requests/httpx；
- 默认 UA `VBHelper/<version> (+https://local)`；
- 超时、最大重定向（默认 5）、最大响应体（默认 5MB）；
- ETag/Last-Modified 条件请求走 vbhelper.store.cache（SQLite）；
  网络失败时回退缓存并标记 stale=True；
- TLS 默认校验（verify=False 可按请求关闭，有安全风险）；
- 默认阻止私网地址（SSRF）：解析出 IP 后拦
  127/8、10/8、172.16-31、192.168、169.254、::1、fc00::/7 等；
- 错误统一分类为 HttpError（kind: timeout/dns/tls/http/parse/size/network）。

用法::

    from vbhelper.ui.http import HttpClient, HttpError

    client = HttpClient()
    resp = client.get_text("https://example.com", cache_ttl=3600)
    print(resp.status, resp.text()[:80], resp.from_cache, resp.stale)

    data, resp = client.get_json("https://example.com/api.json")
    for event in client.stream_sse("https://example.com/sse"):
        print(event["data"])
"""

import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

from vbhelper.store.cache import HttpCache

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 5 * 1024 * 1024      # 5MB
CHUNK = 65536


def _app_version() -> str:
    """读应用版本号；读不到回退 1.0。"""
    try:
        from vbhelper import __version__
        v = str(__version__).strip()
        if v:
            return v
    except Exception:
        pass
    return "1.0"


class HttpError(Exception):
    """统一 HTTP 错误：kind 分类 + 可读信息。

    kind 取值：timeout / dns / tls / http / parse / size / network / ssrf。
    status 仅 kind="http" 时有意义。
    """

    def __init__(self, kind: str, message: str,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status

    def __str__(self) -> str:             # UI 直接可显示
        if self.kind == "http" and self.status is not None:
            return "HTTP %s：%s" % (self.status, self.args[0])
        return "[%s] %s" % (self.kind, self.args[0])


@dataclass
class HttpResponse:
    """一次响应：状态、头、体与缓存标记。

    - from_cache=True 表示体来自本地缓存（304 命中或 TTL 内）；
    - stale=True 表示网络失败后回退的旧缓存（可能已过期）。
    """

    status: int
    url: str
    headers: dict[str, str]
    body: bytes
    stale: bool = False
    from_cache: bool = False
    content_type: str = ""
    etag: str | None = None
    last_modified: str | None = None

    def text(self, encoding: str | None = None) -> str:
        """解码响应体；编码优先参数，其次 Content-Type charset，最后 utf-8。"""
        if encoding is None:
            encoding = "utf-8"
            ct = (self.content_type or "").lower()
            if "charset=" in ct:
                encoding = ct.split("charset=", 1)[1].split(";")[0].strip()
        return self.body.decode(encoding, errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """关闭 urllib 自动重定向：3xx 原样返回，由我们自己跟。

    注意：只把 redirect_request 返回 None 没用——error() 会兜底抛
    HTTPError，所以这里直接重写 http_error_30x 把响应原样交回。
    """

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: D401
        return fp

    def http_error_302(self, req, fp, code, msg, headers):
        return fp

    def http_error_303(self, req, fp, code, msg, headers):
        return fp

    def http_error_307(self, req, fp, code, msg, headers):
        return fp

    def http_error_308(self, req, fp, code, msg, headers):
        return fp


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """私网/环回/链路本地/保留地址一律视为不可访问。"""
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


class HttpClient:
    """标准库 HTTP 客户端：GET/POST/JSON/SSE/下载 + SQLite 条件缓存。

    用法::

        client = HttpClient()
        resp = client.get_text("https://example.com", cache_ttl=600)
        data, resp = client.get_json("https://example.com/a.json")
        path = client.download("https://example.com/a.zip", r"D:\\tmp")
        client.close()
    """

    def __init__(self, *, cache: HttpCache | None = None,
                 user_agent: str | None = None,
                 max_redirects: int = DEFAULT_MAX_REDIRECTS,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._cache = cache if cache is not None else HttpCache()
        self._ua = user_agent or (
            "VBHelper/%s (+https://local)" % _app_version())
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.timeout = timeout
        ctx = ssl.create_default_context()
        self._opener_tls = urllib.request.build_opener(_NoRedirect,
                                                       urllib.request.HTTPSHandler(context=ctx))
        self._opener_plain = urllib.request.build_opener(_NoRedirect)

    # ---------------- 错误分类 ----------------

    def _classify(self, exc: BaseException) -> HttpError:
        """把底层异常翻译成 HttpError（超时/DNS/TLS/HTTP/连接）。"""
        if isinstance(exc, HttpError):
            return exc
        if isinstance(exc, urllib.error.HTTPError):
            return HttpError("http", exc.reason or "HTTP 错误", status=exc.code)
        if isinstance(exc, ssl.SSLCertVerificationError):
            return HttpError("tls", "TLS 证书校验失败：%s" % exc)
        if isinstance(exc, ssl.SSLError):
            return HttpError("tls", "TLS 错误：%s" % exc)
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return HttpError("timeout", "请求超时")
        if isinstance(exc, socket.gaierror):
            return HttpError("dns", "域名解析失败：%s" % exc)
        if isinstance(exc, ConnectionError):
            return HttpError("network", "连接失败：%s" % exc)
        if isinstance(exc, urllib.error.URLError):
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return HttpError("timeout", "请求超时")
            if isinstance(reason, ssl.SSLError):
                return HttpError("tls", "TLS 错误：%s" % reason)
            if isinstance(reason, socket.gaierror):
                return HttpError("dns", "域名解析失败：%s" % reason)
            if isinstance(reason, ConnectionError):
                return HttpError("network", "连接失败：%s" % reason)
            return HttpError("network", "网络错误：%s" % reason)
        if isinstance(exc, ValueError):
            return HttpError("parse", "解析失败：%s" % exc)
        return HttpError("network", "未知网络错误：%s" % exc)

    # ---------------- SSRF 防护 ----------------

    def _guard_host(self, url: str, allow_private: bool) -> None:
        """默认阻止私网地址：解析出全部 IP 后逐一检查。"""
        if allow_private:
            return
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise HttpError("network", "不支持的协议：%s" % parts.scheme)
        host = parts.hostname
        if not host:
            raise HttpError("network", "URL 缺少主机名")
        # 已是 IP 字面量：直接检查
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if _is_private_ip(ip):
                raise HttpError(
                    "ssrf", "已阻止访问私网地址 %s（allow_private=True 可放行）" % ip)
            return
        # 域名：解析后逐个检查
        try:
            infos = socket.getaddrinfo(host, parts.port or 80,
                                       proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise HttpError("dns", "域名解析失败：%s" % exc) from exc
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if _is_private_ip(ip):
                raise HttpError(
                    "ssrf",
                    "已阻止访问私网地址 %s -> %s（allow_private=True 可放行）"
                    % (host, ip))

    # ---------------- 底层请求 ----------------

    def _open(self, url: str, *, method: str = "GET",
              headers: dict[str, str] | None = None,
              body: bytes | None = None,
              timeout: float | None = None,
              verify: bool = True) -> urllib.response.addinfourl:
        """发起单次请求（不跟随重定向）。"""
        hdrs = {"User-Agent": self._ua}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=body, headers=hdrs,
                                     method=method)
        opener = self._opener_tls if verify else self._opener_plain
        return opener.open(req, timeout=timeout if timeout is not None
                           else self.timeout)

    def _follow(self, url: str, *, method: str = "GET",
                headers: dict[str, str] | None = None,
                body: bytes | None = None, timeout: float | None = None,
                allow_private: bool = False,
                verify: bool = True) -> tuple[Any, str]:
        """手动跟随重定向（每跳都重新做 SSRF 检查），返回 (响应, 最终URL)。"""
        current = url
        for _ in range(self.max_redirects + 1):
            self._guard_host(current, allow_private)
            try:
                resp = self._open(current, method=method, headers=headers,
                                  body=body, timeout=timeout, verify=verify)
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return exc, current     # 条件命中；urllib 把 304 当错误抛
                raise
            status = resp.status if hasattr(resp, "status") else resp.getcode()
            if status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                resp.close()
                if not loc:
                    raise HttpError("http", "重定向缺少 Location",
                                    status=status)
                current = urllib.parse.urljoin(current, loc)
                continue
            return resp, current
        raise HttpError("http", "重定向次数超过上限 %d" % self.max_redirects)

    def _read_body(self, resp: Any, max_bytes: int | None = None) -> bytes:
        """限量读体：超过 max_bytes 抛 size 错误，防止内存被打爆。"""
        limit = self.max_bytes if max_bytes is None else max_bytes
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HttpError("size", "响应体超过上限 %d 字节" % limit)
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _headers_dict(resp: Any) -> dict[str, str]:
        """把响应头压平成小写键 dict。"""
        out: dict[str, str] = {}
        try:
            for k, v in resp.headers.items():
                lk = k.lower()
                if lk in out:
                    out[lk] += ", " + v
                else:
                    out[lk] = v
        except Exception:
            pass
        return out

    # ---------------- 公共 API ----------------

    def get_text(self, url: str, *, headers: dict[str, str] | None = None,
                 timeout: float | None = None, cache_ttl: float = 0,
                 allow_private: bool = False,
                 verify: bool = True) -> HttpResponse:
        """GET 文本资源，返回 HttpResponse（用 .text() 解码）。

        cache_ttl>0 时启用缓存：TTL 内直接回缓存；过期则发
        If-None-Match/If-Modified-Since 条件请求，304 命中回缓存体；
        网络失败时回退任何已有缓存并标记 stale=True。
        """
        return self._get(url, headers=headers, timeout=timeout,
                         cache_ttl=cache_ttl, allow_private=allow_private,
                         verify=verify)

    def get_json(self, url: str, *, headers: dict[str, str] | None = None,
                 timeout: float | None = None, cache_ttl: float = 0,
                 allow_private: bool = False,
                 verify: bool = True) -> tuple[dict, HttpResponse]:
        """GET JSON，返回 (解析后的 dict, HttpResponse)。解析失败抛 parse 错。"""
        resp = self._get(url, headers=headers, timeout=timeout,
                         cache_ttl=cache_ttl, allow_private=allow_private,
                         verify=verify)
        try:
            data = json.loads(resp.text())
        except ValueError as exc:
            raise HttpError("parse", "JSON 解析失败：%s" % exc) from exc
        if not isinstance(data, dict):
            raise HttpError("parse", "JSON 顶层不是对象")
        return data, resp

    def post_json(self, url: str, payload: dict | list | None = None,
                  *, headers: dict[str, str] | None = None,
                  timeout: float | None = None,
                  allow_private: bool = False,
                  verify: bool = True) -> tuple[Any, HttpResponse]:
        """POST JSON，返回 (解析后的响应 JSON, HttpResponse)。不做缓存。"""
        body = json.dumps(payload if payload is not None else {},
                          ensure_ascii=False).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        try:
            resp, final_url = self._follow(
                url, method="POST", headers=hdrs, body=body,
                timeout=timeout, allow_private=allow_private, verify=verify)
        except Exception as exc:
            raise self._classify(exc) from exc
        with resp:
            status = resp.status if hasattr(resp, "status") else resp.getcode()
            raw = self._read_body(resp)
            hd = self._headers_dict(resp)
        out = HttpResponse(status=status, url=final_url, headers=hd, body=raw,
                           content_type=hd.get("content-type", ""))
        try:
            data = json.loads(out.text())
        except ValueError as exc:
            raise HttpError("parse", "JSON 解析失败：%s" % exc) from exc
        return data, out

    def stream_sse(self, url: str, *,
                   headers: dict[str, str] | None = None,
                   timeout: float | None = None,
                   allow_private: bool = False,
                   verify: bool = True) -> Iterator[dict]:
        """SSE 流：逐行解析，按事件 yield dict(event/data/id)。

        生成器内做网络 IO：urlopen 的 timeout 覆盖每次 socket 读；
        生成器被 close()/耗尽时在 finally 里关闭连接。
        网络错误会被翻译成 HttpError 从生成器抛出。
        """
        try:
            resp, final_url = self._follow(
                url, headers=headers, timeout=timeout,
                allow_private=allow_private, verify=verify)
        except Exception as exc:
            raise self._classify(exc) from exc
        try:
            event_name: str | None = None
            data_lines: list[str] = []
            last_id: str | None = None
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        yield {"event": event_name or "message",
                               "data": "\n".join(data_lines),
                               "id": last_id}
                    event_name, data_lines = None, []
                    continue
                if line.startswith(":"):
                    continue              # 注释行
                field, _, value = line.partition(":")
                value = value.lstrip(" ")
                if field == "data":
                    data_lines.append(value)
                elif field == "event":
                    event_name = value
                elif field == "id":
                    last_id = value
            if data_lines:                # 流结束时没有空行也要冲刷
                yield {"event": event_name or "message",
                       "data": "\n".join(data_lines), "id": last_id}
        except Exception as exc:
            raise self._classify(exc) from exc
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def download(self, url: str, dest: str, *,
                 headers: dict[str, str] | None = None,
                 timeout: float | None = None,
                 allow_private: bool = False, verify: bool = True,
                 max_bytes: int | None = None) -> str:
        """下载到本地文件，返回写入的完整路径。

        dest 可以是目录（文件名取 URL 末段）或完整文件路径；
        写入限制在 max_bytes（默认客户端上限）内。
        """
        import os
        try:
            resp, final_url = self._follow(
                url, headers=headers, timeout=timeout,
                allow_private=allow_private, verify=verify)
        except Exception as exc:
            raise self._classify(exc) from exc
        limit = self.max_bytes if max_bytes is None else max_bytes
        if os.path.isdir(dest):
            name = urllib.parse.urlsplit(final_url).path.rstrip("/").rsplit("/", 1)[-1]
            if not name:
                name = "download.bin"
            path = os.path.join(dest, name)
        else:
            path = dest
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        total = 0
        try:
            with resp, open(path, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise HttpError("size", "下载超过上限 %d 字节" % limit)
                    f.write(chunk)
        except Exception as exc:
            raise self._classify(exc) from exc
        return path

    def close(self) -> None:
        """关闭底层缓存连接（当前线程）。"""
        self._cache.close()

    def request(self, url: str, *, method: str = "GET",
                headers: dict[str, str] | None = None,
                body: bytes | None = None,
                timeout: float | None = None,
                follow: bool = True,
                verify: bool = True,
                allow_private: bool = False,
                max_bytes: int | None = None) -> HttpResponse:
        """通用请求（API 工作台引擎）：任意方法/头/体，不走缓存。

        - 4xx/5xx 按正常响应返回（调试器必须能看到 404/500 的响应体）；
        - follow=True 时每跳重定向都重做 SSRF 检查；verify=False 关 TLS 校验；
        - 超时/连接/DNS/大小上限统一翻译成 HttpError（kind 可读）。
        """
        method = method.upper()
        status_code: int | None = None
        try:
            if follow:
                try:
                    resp, final_url = self._follow(
                        url, method=method, headers=headers, body=body,
                        timeout=timeout, allow_private=allow_private,
                        verify=verify)
                except urllib.error.HTTPError as exc:
                    if exc.code == 304:
                        status_code, resp, final_url = 304, exc, url
                    else:
                        raise
            else:
                self._guard_host(url, allow_private)
                try:
                    resp = self._open(url, method=method, headers=headers,
                                      body=body, timeout=timeout, verify=verify)
                except urllib.error.HTTPError as exc:
                    if exc.code in (301, 302, 303, 307, 308):
                        raise HttpError("http", "重定向 %d（未跟随）" % exc.code,
                                        status=exc.code) from exc
                    status_code, resp, final_url = int(exc.code), exc, url
        except urllib.error.HTTPError as exc:
            # 4xx/5xx/304：HTTPError 本身就是一次完整响应（有 headers/read）。
            # 注意 except as 结束会删掉 exc 名字，状态码要在块内存下来。
            status_code, resp, final_url = int(exc.code), exc, url
        except HttpError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc

        if isinstance(resp, urllib.error.HTTPError):
            status = status_code or int(resp.code)
        else:
            status = resp.status if hasattr(resp, "status") else resp.getcode()
        hd = self._headers_dict(resp)
        data = self._read_body(resp, max_bytes)
        try:
            resp.close()
        except Exception:
            pass
        return HttpResponse(status=status, url=final_url, headers=hd,
                            body=data, content_type=hd.get("content-type", ""))

    # ---------------- 内部：带缓存的 GET ----------------

    def _get(self, url: str, *, headers: dict[str, str] | None,
             timeout: float | None, cache_ttl: float,
             allow_private: bool, verify: bool) -> HttpResponse:
        """get_text/get_json 共用：条件请求 + 缓存回退。"""
        entry = self._cache.get(url) if cache_ttl > 0 else None
        if entry is not None and not HttpCache.entry_expired(entry):
            return self._response_from_entry(entry, stale=False)

        hdrs = dict(headers or {})
        if entry is not None:
            if entry.get("etag"):
                hdrs.setdefault("If-None-Match", str(entry["etag"]))
            if entry.get("last_modified"):
                hdrs.setdefault("If-Modified-Since", str(entry["last_modified"]))

        try:
            resp, final_url = self._follow(
                url, headers=hdrs, timeout=timeout,
                allow_private=allow_private, verify=verify)
        except HttpError:
            if entry is not None:
                return self._response_from_entry(entry, stale=True)
            raise
        except Exception as exc:
            err = self._classify(exc)
            if entry is not None:
                return self._response_from_entry(entry, stale=True)
            raise err from exc

        with resp:
            status = resp.status if hasattr(resp, "status") else resp.getcode()
            hd = self._headers_dict(resp)
            if status == 304 and entry is not None:
                # 条件命中：刷新时间戳，回缓存体
                self._cache.touch(url, ttl=cache_ttl)
                return self._response_from_entry(entry, stale=False)
            body = self._read_body(resp)

        if cache_ttl > 0:
            self._cache.put(url, etag=hd.get("etag"),
                            last_modified=hd.get("last-modified"),
                            status=status, headers=hd, body=body,
                            ttl=cache_ttl)
        return HttpResponse(status=status, url=final_url, headers=hd,
                            body=body, content_type=hd.get("content-type", ""))

    def _response_from_entry(self, entry: dict, *, stale: bool) -> HttpResponse:
        """把缓存行包装成 HttpResponse。"""
        return HttpResponse(status=int(entry.get("status") or 200),
                            url=str(entry.get("key") or ""),
                            headers=dict(entry.get("headers") or {}),
                            body=entry.get("body") or b"",
                            stale=stale, from_cache=True,
                            content_type=str((entry.get("headers") or {})
                                             .get("content-type", "")),
                            etag=entry.get("etag"),      # type: ignore[arg-type]
                            last_modified=entry.get("last_modified"))
