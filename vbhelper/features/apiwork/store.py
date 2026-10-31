"""API 工作台数据层（cc-switch 形态）：API 站点配置的加密存储与请求组装。

- 密钥在 Windows 上用 DPAPI（CryptProtectData，当前用户绑定）加密，落盘为
  ``dp1:base64``，换机器/换用户解不开；其它平台没有等价机制，退化为
  ``raw:`` 明文并依赖配置文件权限，模块不假装它是加密的。
- UI 展示一律走 :func:`mask_key` 打码，明文只在「应用/测试」那一刻解密。
- 连通测试打 ``{base}/v1/models``，余额查询走 one-api/new-api 系通用协议
  （subscription + usage，回退 credit_grants）。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import time
import urllib.parse
import uuid

from ... import platform as plat

HISTORY_LIMIT = 30
_DPAPI_ENTROPY = "VBHelper.ApiWorkbench.v1"

# ---------------------------------------------------------------- DPAPI 加密

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

def _blob(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

def dpapi_encrypt(text: str) -> str:
    """明文 -> 落盘形式。空值原样返回。

    Windows 返回 ``dp1:base64``（绑定当前用户）；其它平台返回 ``raw:`` 前缀明文。
    """
    if not text:
        return ""
    if not plat.HAS_DPAPI:
        return "raw:" + text
    raw = text.encode("utf-8")
    inp, out = _blob(raw), _BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(inp), _DPAPI_ENTROPY, None, None, None, 0,
            ctypes.byref(out)):
        raise OSError("DPAPI 加密失败")
    try:
        return "dp1:" + base64.b64encode(
            ctypes.string_at(out.pbData, out.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)

def dpapi_decrypt(token: str) -> str:
    """落盘形式 -> 明文；兼容 ``raw:`` 明文与旧版本未加密数据。"""
    if not token:
        return ""
    if token.startswith("raw:"):
        return token[4:]
    if not token.startswith("dp1:"):
        return token
    raw = base64.b64decode(token[4:])
    inp, out = _blob(raw), _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI 解密失败（换 Windows 用户或机器后无法还原）")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)

# ---------------------------------------------------------------- 打码

def mask_key(key: str) -> str:
    """密钥打码：sk-••••••••••••x1y2（前 5 后 4，其余用 •）。"""
    key = key or ""
    if len(key) <= 9:
        return "•" * len(key)
    return key[:5] + "•" * 12 + key[-4:]

# ---------------------------------------------------------------- 数据模型

def uid() -> str:
    return uuid.uuid4().hex[:8]

def new_provider(name: str = "", base_url: str = "", key_enc: str = "",
                 model: str = "", note: str = "") -> dict:
    return {"id": uid(), "name": name or "新站点", "base_url": (base_url or "").rstrip("/"),
            "key_enc": key_enc, "model": model or "", "note": note or "",
            "status": "unknown", "status_text": "", "balance": "",
            "created": time.strftime("%Y-%m-%d %H:%M")}

def default_data() -> dict:
    return {"version": 1, "providers": [], "active_id": ""}

def load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default_data()
    if not isinstance(data, dict) or "providers" not in data:
        return default_data()
    data.setdefault("providers", [])
    data.setdefault("active_id", "")
    return data

def save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

def get_provider(data: dict, pid: str) -> dict | None:
    return next((p for p in data.get("providers") or []
                 if p.get("id") == pid), None)

def key_of(provider: dict) -> str:
    """解出明文密钥（只有应用/测试时调用）。"""
    return dpapi_decrypt(str((provider or {}).get("key_enc") or ""))

# ---------------------------------------------------------------- 请求组装

def _trim_base(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")

def probe_url(provider: dict) -> str:
    """连通测试地址：{base}/v1/models。"""
    return _trim_base(provider.get("base_url")) + "/v1/models"

def build_probe(provider: dict) -> dict:
    """连通测试请求参数（/v1/models，带 Bearer）。"""
    return {"url": probe_url(provider),
            "headers": {"Authorization": "Bearer " + key_of(provider)},
            "method": "GET"}

def build_balance(provider: dict, today: str | None = None) -> list[dict]:
    """余额查询请求序列（one-api/new-api 通用协议，逐个尝试）。

    返回 [{label, url, pick}]：pick(body_json, headers) -> 展示文本或 None。
    """
    base = _trim_base(provider.get("base_url"))
    auth = {"Authorization": "Bearer " + key_of(provider)}
    day = today or time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-01")
    urlenc = urllib.parse.urlencode({"start_date": start + "T00:00:00",
                                     "end_date": day + "T23:59:59"})
    return [
        {"label": "订阅额度 - 已用",
         "urls": [base + "/v1/dashboard/billing/subscription",
                  base + "/dashboard/billing/subscription"],
         "headers": auth,
         "pick": lambda j, _h: _fmt_sub(j)},
        {"label": "已用金额",
         "urls": [base + "/v1/dashboard/billing/usage?" + urlenc,
                  base + "/dashboard/billing/usage?" + urlenc],
         "headers": auth,
         "pick": lambda j, _h: _fmt_usage(j)},
        {"label": "剩余额度",
         "urls": [base + "/v1/dashboard/billing/credit_grants",
                  base + "/dashboard/billing/credit_grants"],
         "headers": auth,
         "pick": lambda j, _h: _fmt_grants(j)},
    ]

def _fmt_sub(j) -> str | None:
    if not isinstance(j, dict):
        return None
    total = j.get("hard_limit_usd", j.get("system_hard_limit_usd"))
    if total is None:
        return None
    try:
        return "额度 $%.2f" % float(total)
    except (TypeError, ValueError):
        return None

def _fmt_usage(j) -> str | None:
    if not isinstance(j, dict) or j.get("total_usage") is None:
        return None
    try:
        return "已用 $%.2f" % (float(j["total_usage"]) / 100.0)
    except (TypeError, ValueError):
        return None

def _fmt_grants(j) -> str | None:
    if isinstance(j, dict):
        grants = j.get("grants") or []
        total = sum(float(g.get("total_available") or 0) for g in grants
                    if isinstance(g, dict))
        return "剩余 $%.2f" % total if grants else None
    if isinstance(j, list):
        total = sum(float(g.get("total_available") or 0) for g in j
                    if isinstance(g, dict))
        return "剩余 $%.2f" % total if j else None
    return None

# ---------------------------------------------------------------- Claude Code

def claude_settings_path() -> str:
    home = os.path.expanduser("~")
    return os.path.join(home, ".claude", "settings.json")

def claude_settings_exists() -> bool:
    return os.path.isfile(claude_settings_path())

def apply_to_claude(provider: dict) -> tuple[bool, str]:
    """把站点写进 Claude Code 设置（先备份原文件）。

    返回 (成功, 说明)。密钥以 ANTHROPIC_AUTH_TOKEN 写入 env。
    """
    path = claude_settings_path()
    key = key_of(provider)
    doc: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except ValueError as e:
            return False, "settings.json 不是合法 JSON：%s" % e
        backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            with open(path, "r", encoding="utf-8") as f:
                with open(backup, "w", encoding="utf-8") as g:
                    g.write(f.read())
        except OSError:
            pass
    env = doc.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = _trim_base(provider.get("base_url"))
    env["ANTHROPIC_AUTH_TOKEN"] = key
    if provider.get("model"):
        env["ANTHROPIC_MODEL"] = provider["model"]
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = provider["model"]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return False, "写入失败：%s" % e
    return True, "已写入（原文件已备份）。重启 Claude Code 会话后生效。"
