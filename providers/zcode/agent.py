"""上游请求构建与失败分类。

build_request 根据账号凭证选择端点、组装请求头、应用 body 变换；
实际发送与流式透传在 chat.py。失败分类函数供 chat.py 的账号循环使用。
"""

from __future__ import annotations

import json

from . import body_transform, constants
from .identity import build_identity_headers, build_trace_headers
from . import token as ztoken

# 透传客户端 header 时需剔除的字段（身份/追踪头由本服务仿真生成）
_DROP_HEADERS = {
    "host",
    "content-length",
    "x-api-key",
    "authorization",
    "user-agent",
    "http-referer",
    "accept-encoding",
    "connection",
    "x-device-mid",
    "x-request-id",
    "x-zcode-trace-id",
    "x-zcode-session-type",
    "x-query-id",
    "x-session-id",
    "x-title",
    "x-platform",
    "x-release-channel",
    "x-client-language",
    "x-client-timezone",
    "x-os-category",
    "x-os-version",
    "anthropic-version",
}
# 前缀剔除：本服务仿真的头族 + SDK 特征头族
_DROP_HEADER_PREFIXES = ("x-zcode", "x-stainless")


def build_request(
    account: dict,
    body: dict,
    verify_param: str | None = None,
    incoming_headers: dict | None = None,
    verify_region: str | None = None,
) -> tuple[str, dict, bytes]:
    """返回 (目标 URL, 请求头, 序列化请求体)。

    body 变换（system 身份块 / cache_control / metadata.user_id）统一在此应用；
    验证码重试时用同一 body 重建请求（变换幂等，重复调用安全）。
    """
    extra = account.get("extra") or {}
    mode = str(extra.get("mode") or ztoken.account_mode(account))
    provider = str(extra.get("provider") or "zai")
    jwt_token = account.get("access_token") or ""
    api_key = str(extra.get("api_key") or "")

    target_url: str
    auth: dict
    if provider == "bigmodel":
        target_url = constants.MESSAGES_URLS["bigmodel"]
        if not api_key:
            raise RuntimeError("BigModel 账号缺少 API Key")
        auth = {"x-api-key": api_key}
        mode = "apiKey"
    elif mode == "jwt" and jwt_token:
        target_url = constants.MESSAGES_URLS["zai"]
        auth = {"Authorization": f"Bearer {jwt_token}"}
    elif api_key:
        target_url = constants.MESSAGES_URLS["zai_fallback"]
        auth = {"x-api-key": api_key}
        mode = "apiKey"
    else:
        raise RuntimeError("账号缺少有效凭证（JWT 或 API Key）")

    if mode == "jwt":
        # JWT 通道：全量身份头 + 追踪头（start-plan）+ body 变换
        user_id = body_transform.jwt_user_id(jwt_token)
        model = body.get("model") if isinstance(body.get("model"), str) else None
        body = body_transform.transform_body(body, user_id, model)
        headers: dict[str, str] = {
            "content-type": "application/json",
            **auth,
            "anthropic-version": constants.ANTHROPIC_VERSION,
            **build_identity_headers(account),
            **build_trace_headers("start-plan"),
        }
    else:
        # API Key 通道（回退 / bigmodel）：保持最小头集
        headers = {
            "content-type": "application/json",
            **auth,
            "anthropic-version": constants.ANTHROPIC_VERSION,
            "User-Agent": constants.USER_AGENT,
            "X-ZCode-App-Version": constants.X_ZCODE_APP_VERSION,
            "X-ZCode-Agent": constants.X_ZCODE_AGENT,
            "HTTP-Referer": constants.HTTP_REFERER,
        }
    if verify_param:
        headers[constants.CAPTCHA_HEADER] = verify_param
    if verify_region:
        headers[constants.CAPTCHA_REGION_HEADER] = verify_region

    for key, value in (incoming_headers or {}).items():
        lower = str(key).lower()
        if lower in _DROP_HEADERS or lower.startswith(_DROP_HEADER_PREFIXES):
            continue
        headers[key] = value

    return target_url, headers, json.dumps(body, ensure_ascii=False).encode("utf-8")


# ── 失败分类 ────────────────────────────────────────────────────────────────
def is_captcha_error(text: str) -> bool:
    low = (text or "").lower()
    return "captcha" in low or "verify token" in low or "verify failed" in low


def detect_captcha_challenge(status_code: int, headers: dict, text: str | None) -> str | None:
    """验证码挑战三形态：
      1. 响应头 X-Aliyun-Captcha-Verify-Param 存在
      2. HTTP 400/403 + body {"code":3007}
      3. HTTP 403 + 文案 captcha/verify
    """
    header_val = (headers or {}).get("x-aliyun-captcha-verify-param", "")
    if header_val and str(header_val).strip():
        return "header"
    if not text:
        return None
    if status_code in (400, 403) and any(m in text for m in constants.CAPTCHA_BODY_MARKERS):
        return "in-body-3007"
    if status_code == 403 and is_captcha_error(text):
        return "text"
    return None


def is_exhausted(status_code: int, text: str) -> bool:
    if status_code in constants.EXHAUST_HTTP_STATUSES:
        return True
    low = (text or "").lower()
    return any(k.lower() in low for k in constants.EXHAUST_KEYWORDS)


def is_risk_control(status_code: int, text: str) -> bool:
    """3012「unusual activity」/ messages 端点 405：账号级风控（禁用而非回传）。"""
    if status_code in constants.RISK_CONTROL_HTTP_STATUSES:
        return True
    low = (text or "").lower()
    return any(m.lower() in low for m in constants.RISK_CONTROL_MARKERS)


def parse_retry_after(value: str | None, wait_max: int = 120) -> int | None:
    if not value:
        return None
    try:
        secs = int(float(str(value).strip()))
    except (TypeError, ValueError, AttributeError):
        return None
    return min(secs, wait_max) if secs > 0 else None