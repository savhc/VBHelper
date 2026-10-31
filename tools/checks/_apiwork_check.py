# -*- coding: utf-8 -*-
"""API 工作台自检：python _apiwork_check.py（无 GUI，测数据层与引擎）。

覆盖：DPAPI 加密往返、打码、持久化、连通/余额组装与本地回环真请求、
Claude Code 设置应用（临时 HOME）。
"""
import http.server
import json
import os
import sys
import threading
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vbhelper.features.apiwork import store

fails = []
oks = []


def check(name, cond, detail=""):
    (oks if cond else fails).append(name)
    print(("  ok  " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail and not cond else ""))


print("== 1. DPAPI 加密往返 ==")
enc = store.dpapi_encrypt("sk-test-abcdef123456")
check("密文带 dp1 前缀", enc.startswith("dp1:"), enc[:20])
check("密文不含明文", "sk-test" not in enc)
dec = store.dpapi_decrypt(enc)
check("解密还原", dec == "sk-test-abcdef123456")
check("空值透传", store.dpapi_encrypt("") == "" and store.dpapi_decrypt("") == "")

print("== 2. 打码 ==")
masked = store.mask_key("sk-abcdef1234567890xyz")
check("前 5 后 4 打码", masked.startswith("sk-ab") and masked.endswith("0xyz")
      and "•" in masked and "cdef" not in masked, masked)
check("短密钥全打码", store.mask_key("sk-1") == "••••", store.mask_key("sk-1"))

print("== 3. 持久化 ==")
tmp = os.path.join(tempfile.gettempdir(), "apiwork_check.json")
data = store.default_data()
p = store.new_provider("中转站", "https://api.example.com", store.dpapi_encrypt("sk-abc"), "gpt-4o")
data["providers"].append(p)
data["active_id"] = p["id"]
store.save(tmp, data)
data2 = store.load(tmp)
check("往返一致", data2["providers"][0]["base_url"] == "https://api.example.com")
check("密文落盘（不落明文）", "sk-abc" not in open(tmp, encoding="utf-8").read())
check("解密可用", store.key_of(data2["providers"][0]) == "sk-abc")
os.remove(tmp)

print("== 4. 请求组装 ==")
probe = store.build_probe(p)
check("连通打 /v1/models", probe["url"] == "https://api.example.com/v1/models", probe["url"])
check("Bearer 头", probe["headers"]["Authorization"] == "Bearer sk-abc")
queries = store.build_balance(p)
check("余额查询序列", len(queries) == 3 and
      queries[0]["urls"][0].endswith("/v1/dashboard/billing/subscription"), queries[0]["urls"])
check("余额解析 subscription",
      queries[0]["pick"]({"hard_limit_usd": 20.0}, {}) == "额度 $20.00")
check("余额解析 usage", queries[1]["pick"]({"total_usage": 560}, {}) == "已用 $5.60")
check("余额解析 grants",
      (queries[2]["pick"]({"grants": [{"total_available": 3.5}]}, {})) == "剩余 $3.50")

print("== 5. Claude Code 应用（临时 HOME） ==")
fake_home = tempfile.mkdtemp()
os.makedirs(os.path.join(fake_home, ".claude"), exist_ok=True)
cc = os.path.join(fake_home, ".claude", "settings.json")
with open(cc, "w", encoding="utf-8") as f:
    json.dump({"env": {"OLD": "1"}, "theme": "dark"}, f)
real_expand = os.path.expanduser
os.path.expanduser = lambda p: p.replace("~", fake_home) if p.startswith("~") else p
try:
    ok, msg = store.apply_to_claude(p)
    check("应用成功", ok, msg)
    doc = json.load(open(cc, encoding="utf-8"))
    check("BASE_URL 写入", doc["env"].get("ANTHROPIC_BASE_URL") == "https://api.example.com", doc["env"])
    check("AUTH_TOKEN 写入", doc["env"].get("ANTHROPIC_AUTH_TOKEN") == "sk-abc")
    check("原有键保留", doc["env"].get("OLD") == "1" and doc.get("theme") == "dark")
    backups = [f for f in os.listdir(os.path.dirname(cc)) if f.startswith("settings.json.bak-")]
    check("自动备份", len(backups) == 1, backups)
finally:
    os.path.expanduser = real_expand

print("== 6. HttpClient 连通/余额（本地回环假站） ==")
import time as _time
import urllib.parse
from vbhelper.ui.http import HttpClient
# 环境代理会劫持回环请求，测试里清掉（HttpClient 构造时读环境）
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)

def handler(hs):
    class H(http.server.BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer sk-abc":
                return self._json({"error": "bad key"}, 401)
            if self.path.startswith("/v1/models"):
                return self._json({"data": [{"id": "gpt-4o"}, {"id": "claude-3"}]})
            if "billing/subscription" in self.path:
                return self._json({"hard_limit_usd": 20.0})
            if "billing/usage" in self.path:
                return self._json({"total_usage": 560})
            if "credit_grants" in self.path:
                return self._json({"grants": [{"total_available": 14.4}]})
            self._json({"error": "nf"}, 404)

        def log_message(self, *a):
            pass
    return H

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler(None))
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]
client = HttpClient()
site = store.new_provider("本地假站", base, store.dpapi_encrypt("sk-abc"))
probe = store.build_probe(site)
resp = client.request(probe["url"], headers=probe["headers"], timeout=10,
                      allow_private=True, max_bytes=256 * 1024)
check("连通 200 + 2 个模型", resp.status == 200 and
      len(json.loads(resp.text()).get("data") or []) == 2, resp.status)
models_ok = True
for q in store.build_balance(site):
    got = None
    for url in q["urls"]:
        try:
            r = client.request(url, headers=q["headers"], timeout=10,
                               allow_private=True, max_bytes=256 * 1024)
        except Exception:
            continue
        if r.status == 200:
            try:
                got = q["pick"](json.loads(r.text()), r.headers)
            except ValueError:
                got = None
            if got:
                break
    check("余额步骤：%s" % q["label"], bool(got), got)
client.close()
srv.shutdown()

print("\n" + "=" * 52)
print("通过 %d 项，失败 %d 项" % (len(oks), len(fails)))
for f in fails:
    print("  - " + f)
sys.exit(1 if fails else 0)
