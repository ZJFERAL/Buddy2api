"""zcode 通道端到端测试的 Mock 上游（本地 HTTP 服务）。

模拟 zcode.z.ai 的关键端点：
  - POST /api/v1/zcode-plan/anthropic/v1/messages  JWT 通道（验证码头校验）
  - POST /api/anthropic/v1/messages                API Key 回退通道
  - GET  /api/v1/client/configs                    验证码配置
  - GET  /api/v1/zcode-plan/billing/balance              额度+生效套餐（真机结构）
  - GET  /api/v1/zcode-plan/billing/preview              可领取套餐
  - GET  /api/v1/zcode-plan/billing/current              真机为 405+3012（风控），客户端不应请求
  - POST /api/v1/oauth/cli/init                    OAuth 发起
  - GET  /api/v1/oauth/cli/poll/{flow_id}          OAuth 轮询

用法：ZCODE_MOCK_PORT=8599 python mock_upstream_server.py
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY_JSON = {"type": "message", "id": "msg_mock01", "role": "assistant", "model": "GLM-5.3",
             "content": [{"type": "text", "text": "pong from mock upstream"}],
             "stop_reason": "end_turn",
             "usage": {"input_tokens": 12, "output_tokens": 6}}

BODY_LOG = os.environ.get("ZCODE_MOCK_BODY_LOG", "zcode_mock_last_body.json")

# claim 测试控制：返回业务码（0 成功 / 1003 已领取 / 3007 验证码失败）
# 注意：必须在 handler 内动态读取，测试中途改 env 才能生效

CAPTCHA_CFG = {"data": {"configs": {"captcha": {"enabled": True, "sceneId": "11xygtvd", "region": "cn", "prefix": "no8xfe"}}}}

# billing/balance 真机结构（2026-09-21 从 zcode.z.ai 实际 dump 复刻）：
# 同一响应同时携带生效套餐 plans[] 与额度窗口 balances[]。
# 注意：/billing/current 与 /usage 真机不可用（405+3012 / 404），不再提供正常响应。
BALANCE_PLANS = [{
    "user_plan_id": "upl_mock_1",
    "plan_id": "zcode-v3-start-plan-0817",
    "name": "ZCode Start Plan",
    "description": "免费 GLM 旗舰模型体验",
    "priority": 90,
    "status": "active",
    "starts_at": 1789986315,
    "ends_at": 1790351999,
    "entitlements": [
        {"entitlement_id": "ent_glm_5p3", "show_name": "GLM-5.3", "meter": "model_usage",
         "unit_type": "token", "capabilities": ["model:glm-5.3"], "grant_units": 3000000,
         "period": "daily", "priority": 110, "effective_at": 0},
        {"entitlement_id": "ent_glm_5p3f", "show_name": "GLM-5.3-Flash", "meter": "model_usage",
         "unit_type": "token", "capabilities": ["model:glm-5.3-flash"], "grant_units": 5000000,
         "period": "daily", "priority": 80, "effective_at": 0},
    ],
}]
BALANCE_WINDOWS = [
    {"bucket_id": "b_glm_5p3", "user_plan_id": "upl_mock_1", "plan_id": "zcode-v3-start-plan-0817",
     "entitlement_id": "ent_glm_5p3", "show_name": "GLM-5.3", "unit_type": "token",
     "capabilities": ["model:glm-5.3"], "priority": 110, "total_units": 3000000, "used_units": 0,
     "remaining_units": 3000000, "available_units": 3000000,
     "period_start": 1789920000, "period_end": 1790006399, "expires_at": 1790006399},
    {"bucket_id": "b_glm_5p3f", "user_plan_id": "upl_mock_1", "plan_id": "zcode-v3-start-plan-0817",
     "entitlement_id": "ent_glm_5p3f", "show_name": "GLM-5.3-Flash", "unit_type": "token",
     "capabilities": ["model:glm-5.3-flash"], "priority": 80, "total_units": 5000000, "used_units": 250000,
     "remaining_units": 4750000, "available_units": 4750000,
     "period_start": 1789920000, "period_end": 1790006399, "expires_at": 1790006399},
]


def balance_response(mode: str):
    """按模式返回 (body, http_status)。mode 由 env 在请求内动态读取（勿缓存到模块级）。"""
    if mode == "risk":
        return {"code": 3012, "msg": "request has been blocked due to unusual activity."}, 405
    if mode == "bizerror":
        return {"code": 1005, "msg": "quota exhausted"}, 200
    if mode == "unauth":
        return {"code": 401, "msg": "login required"}, 401
    return {
        "code": 0,
        "msg": "",
        "data": {"server_time": 1789986349, "plans": BALANCE_PLANS, "balances": BALANCE_WINDOWS},
    }, 200


# billing/preview：套餐列表（首项 wk-0918 优先级高，claim 应选它）
PREVIEW = {"data": {"plans": [
    {"plan_id": "wk-0918", "name": "GLM-5.3 周末套餐", "description": "mock weekend plan",
     "priority": 10, "starts_at": 0, "ends_at": 9999999999,
     "entitlements": [{"entitlement_id": "e1", "show_name": "GLM-5.3", "meter": "model_usage",
                       "unit_type": "token", "grant_units": 500000, "period": "weekend",
                       "priority": 100, "effective_at": 0}]},
    {"plan_id": "start-plan", "name": "GLM 5.3", "description": "mock start plan",
     "priority": 1,
     "entitlements": [{"entitlement_id": "e2", "show_name": "GLM-5.3", "grant_units": 100000, "period": "trial"}]},
]}}
# claim 成功响应（带 data.plan）
CLAIM_OK = {"code": 0, "data": {"plan": {"plan_id": "wk-0918", "name": "GLM-5.3 周末套餐",
                                          "starts_at": 0, "ends_at": 9999999999}}}


def _b64(obj) -> str:
    import base64

    raw = json.dumps(obj, ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mock_oauth_jwt(user_id: str = "u_oauth_mock") -> str:
    """构造三段 JWT（store.jwt_user_id / jwt_expiry 可解析）。"""
    import time

    payload = {"sub": user_id, "user_id": user_id, "exp": int(time.time()) + 86400}
    return f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64(payload)}.mocksig"


# OAuth init 返回：authorize_url 故意带 zcode 自身的 cli 回调 redirect_uri，
# 以验证客户端必须把它覆盖成 /app/oauth/login 中转页（见 providers/zcode/oauth.py）。
OAUTH_INIT = {"code": 0, "data": {
    "flow_id": "mock-flow-1",
    "poll_token": "server-poll-token",
    "authorize_url": ("https://chat.z.ai/api/oauth/authorize?client_id=c"
                      "&redirect_uri=https%3A%2F%2Fzcode.z.ai%2Fapi%2Fv1%2Foauth%2Fcli%2Fcallback%2Fzai"
                      "&state=s1"),
    "expires_at": 9999999999,
    "poll_interval_sec": 1,
}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, status: int = 200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def log_message(self, *args):  # 静默
        pass

    def do_GET(self):
        if self.path.startswith("/api/v1/client/configs"):
            return self._json(CAPTCHA_CFG)
        if self.path.startswith("/api/v1/zcode-plan/billing/current"):
            # 真机实测：HTTP 405 + code 3012（风控拦截）。客户端已弃用该端点；
            # 若被请求则记录痕迹，供测试断言「未再请求」。
            hit_log = os.environ.get("ZCODE_MOCK_CURRENT_HIT_LOG", "")
            if hit_log:
                with open(hit_log, "a", encoding="utf-8") as fh:
                    fh.write(self.path + "\n")
            return self._json({"code": 3012, "msg": "request has been blocked due to unusual activity."}, 405)
        if self.path.startswith("/api/v1/zcode-plan/billing/balance"):
            body, status = balance_response(os.environ.get("ZCODE_MOCK_BALANCE_MODE", "normal"))
            return self._json(body, status)
        if self.path.startswith("/api/v1/zcode-plan/billing/preview"):
            return self._json(PREVIEW)
        if self.path.startswith("/api/v1/zcode-plan/usage") or self.path.startswith("/usage"):
            # 真机为 404；客户端已弃用
            return self._json("404 page not found", 404)
        if self.path.startswith("/api/v1/oauth/cli/poll/"):
            # 契约（zcode-api src/auth/oauth.ts）：
            #   pending → 继续；ready → {token: JWT, user, zai:{access_token}}
            #   failed / expired → 终止
            # 状态由 env 动态控制，便于测试状态机各分支
            state = os.environ.get("ZCODE_MOCK_OAUTH_STATUS", "ready")
            if state != "ready":
                return self._json({"code": 0, "data": {"status": state}})
            data = {
                "status": "ready",
                "token": os.environ.get("ZCODE_MOCK_OAUTH_JWT") or mock_oauth_jwt(),
                "user": {"user_id": os.environ.get("ZCODE_MOCK_OAUTH_UID", "u_oauth_mock")},
                "zai": {"access_token": os.environ.get("ZCODE_MOCK_OAUTH_ACCESS", "mock-zai-access-token")},
            }
            if os.environ.get("ZCODE_MOCK_OAUTH_OMIT_TOKEN") == "1":
                data.pop("token")  # ready 但缺 JWT → 客户端必须拒绝，不得把 access_token 当 JWT
            return self._json({"code": 0, "data": data})
        if self.path.startswith("/api/biz/customer/getCustomerInfo"):
            return self._json({"data": {"organizations": [{
                "organizationId": "org-1", "organizationName": "默认机构",
                "projects": [{"projectId": "proj-1", "projectName": "默认项目"}],
            }]}})
        if "/api_keys/copy/" in self.path:
            return self._json({"data": {"secretKey": "mock-secret-key"}})
        if "/api_keys" in self.path:
            return self._json({"data": [{"name": "zcode-api-key", "apiKey": "mock-api-key"}]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_body()
        if self.path.startswith("/api/v1/zcode-plan/anthropic/v1/messages"):
            # JWT 通道：必须带验证码头 + Authorization: Bearer
            captcha_header = self.headers.get("X-Aliyun-Captcha-Verify-Param", "")
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not captcha_header:
                return self._json({"error": {"message": "captcha verify failed", "code": 3007}}, 403)
            # 记录收到的 body（供测试断言 body 变换）
            with open(BODY_LOG, "w", encoding="utf-8") as fh:
                json.dump(body, fh, ensure_ascii=False, indent=1)
            return self._json(BODY_JSON)
        if self.path.startswith("/api/anthropic/v1/messages"):
            # API Key 回退通道：x-api-key 头校验
            api_key = self.headers.get("x-api-key", "")
            if not api_key:
                return self._json({"error": {"message": "authentication required"}}, 401)
            return self._json(BODY_JSON)
        if self.path.startswith("/api/v1/oauth/cli/init"):
            return self._json(OAUTH_INIT)
        if self.path.startswith("/api/auth/z/login"):
            # OAuth access_token → 业务 token（兑换链第一跳）
            return self._json({"data": {"access_token": "mock-biz-token"}})
        if self.path.startswith("/api/v1/zcode-plan/billing/claim"):
            # 校验：需 Authorization: Bearer + 验证码头 + 版本/平台头
            auth = self.headers.get("Authorization", "")
            captcha_header = self.headers.get("X-Aliyun-Captcha-Verify-Param", "")
            app_ver = self.headers.get("X-ZCode-App-Version", "")
            platform = self.headers.get("X-Platform", "")
            claim_log = os.environ.get("ZCODE_MOCK_CLAIM_LOG", "zcode_mock_last_claim.json")
            with open(claim_log, "w", encoding="utf-8") as fh:
                json.dump({"auth": auth, "captcha": captcha_header, "app_ver": app_ver,
                           "platform": platform, "body": body}, fh, ensure_ascii=False, indent=1)
            if not auth.startswith("Bearer ") or not captcha_header:
                return self._json({"code": 3007, "msg": "captcha verify failed"}, 403)
            if not app_ver or not platform:
                return self._json({"code": 3007, "msg": "missing version headers"}, 200)
            code = int(os.environ.get("ZCODE_MOCK_CLAIM_CODE", "0"))
            if code == 0:
                return self._json(CLAIM_OK)
            return self._json({"code": code, "msg": {1003: "already claimed", 3007: "captcha failed"}.get(code, "fail")})
        return self._json({"error": "not found"}, 404)


def start(port: int = 8599):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    port = int(os.environ.get("ZCODE_MOCK_PORT", "8599"))
    start(port)
    print(f"mock upstream on 127.0.0.1:{port}", flush=True)
    threading.Event().wait()