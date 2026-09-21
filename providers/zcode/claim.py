"""zcode 套餐领取（manual claim / checkin）支持。

协议来源：zcode-api（MIT, src/claim/client.ts）翻译为主，zcode2api（AGPL）仅作
协议事实验证；不拷贝 AGPL 源码，只按协议实现。

链路：
  1. GET  {billing_base}/billing/preview?app_version=&platform=  → data.plans[]
  2. POST {billing_base}/billing/claim  body {"plan_id": ...}
     头：Authorization / Content-Type / X-Aliyun-Captcha-Verify-Param [/Region] /
         X-ZCode-App-Version / X-Platform / [X-Device-Mid]

业务码（classifyClaimCode 同款）：
  0 成功；1001 not_found；1002 unavailable；1003 already_claimed；
  1004 ineligible；1005 quota_exhausted；3001 invalid_request；
  3007 captcha（换码重试一次）；401 login_required。

注：billing/claim 实测缺版本/平台头时即使验证码有效也回 3007；
    preview 在 campaign 期要求 UUID 格式 X-Device-Mid（build_billing_headers 已带）。
"""

from __future__ import annotations

import time

import httpx

from providers.zcode.captcha import captcha_manager
from providers.zcode.constants import (
    BILLING_APP_VERSION,
    BILLING_BASE,
    CAPTCHA_HEADER,
    CAPTCHA_REGION_HEADER,
    X_PLATFORM,
)
from providers.zcode.identity import build_billing_headers

# ── 业务码 → 语义 ─────────────────────────────────────────────────────────────
CLAIM_CODE_MESSAGES = {
    1001: "套餐不存在",
    1002: "活动已结束",
    1003: "今日已领取过该套餐",
    1004: "不符合领取条件",
    1005: "今日名额已用完",
    3001: "参数错误",
    3007: "验证码校验失败",
    401: "未登录或凭据已失效",
}


def _classify_code(code: int) -> str:
    return {
        1001: "not_found",
        1002: "unavailable",
        1003: "already_claimed",
        1004: "ineligible",
        1005: "quota_exhausted",
        3001: "invalid_request",
        3007: "captcha",
        401: "login_required",
    }.get(code, "unknown")


def _is_jwt_account(account: dict) -> bool:
    extra = account.get("extra") or {}
    mode = extra.get("mode") if isinstance(extra, dict) else None
    return mode == "jwt" and bool(account.get("access_token"))


def _biz_code(body: dict) -> int:
    try:
        code = body.get("code")
        return int(code) if code is not None else 0
    except (TypeError, ValueError):
        return -1


def _message_for(code: int, body: dict) -> str:
    msg = body.get("msg") or body.get("message")
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    return CLAIM_CODE_MESSAGES.get(code, f"领取失败（code={code}）")


def _plan_summary(raw: dict) -> dict | None:
    """preview 单条套餐 → 摘要 dict。无 plan_id 返回 None。"""
    if not isinstance(raw, dict):
        return None
    plan_id = str(raw.get("plan_id") or "").strip()
    if not plan_id:
        return None
    entitlements = []
    for ent in raw.get("entitlements") or []:
        if not isinstance(ent, dict):
            continue
        entitlements.append(
            {
                "id": str(ent.get("entitlement_id") or ""),
                "name": str(ent.get("show_name") or ""),
                "units": ent.get("grant_units", 0),
                "period": str(ent.get("period") or ""),
                "meter": str(ent.get("meter") or ""),
            }
        )
    plan = {
        "plan_id": plan_id,
        "name": str(raw.get("name") or plan_id).strip(),
        "description": str(raw.get("description") or "").strip(),
        "priority": raw.get("priority") or 0,
        "entitlements": entitlements,
    }
    if raw.get("starts_at") is not None:
        plan["starts_at"] = raw["starts_at"]
    if raw.get("ends_at") is not None:
        plan["ends_at"] = raw["ends_at"]
    return plan


def _checkin_payload(account: dict, **kw) -> dict:
    payload = {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": False,
        "claimed": False,
        "already_claimed": False,
        "status_code": 0,
        "message": "",
        "active": None,
        "today_checked_in": None,
    }
    payload.update(kw)
    return payload


async def _billing_request(method: str, path: str, **kwargs) -> httpx.Response:
    base = BILLING_BASE.strip("/")
    async with httpx.AsyncClient(timeout=25) as client:
        return await client.request(method, f"{base}{path}", **kwargs)


async def _fetch_previews(account: dict) -> list[dict]:
    """GET /billing/preview → 已解析套餐列表（按优先级降序）。"""
    query = f"app_version={BILLING_APP_VERSION}&platform={X_PLATFORM}"
    headers = {"Authorization": f"Bearer {account.get('access_token')}"}
    with_headers = build_billing_headers(account)
    if with_headers.get("X-Device-Mid"):
        headers["X-Device-Mid"] = with_headers["X-Device-Mid"]
    res = await _billing_request("GET", f"/billing/preview?{query}", headers=headers)
    try:
        body = res.json()
    except ValueError:
        body = {}
    if res.status_code >= 400:
        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")
    code = _biz_code(body)
    if code not in (0, -1):
        raise RuntimeError(f"code={code}: {_message_for(code, body)}")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    plans = [_plan_summary(p) for p in (data.get("plans") or [])]
    plans = [p for p in plans if p]
    plans.sort(key=lambda p: (-p["priority"], p["plan_id"]))
    return plans


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    """查询当前可领取套餐（checkin 状态）。

    返回结构对齐 auth_manager._checkin_result 语义 +
    plans 摘要用于管理页展示。仅 JWT 账号支持。
    """
    if not _is_jwt_account(account):
        return _checkin_payload(
            account,
            message="仅 Coding Plan (JWT) 账号支持领取套餐",
        )
    # 顺手启动验证码预热池（幂等）：管理页一读领取状态就把 token 备好，
    # 用户点「领取」时不必再等一次同步求解。
    captcha_manager.start()
    try:
        plans = await _fetch_previews(account)
    except RuntimeError as err:
        return _checkin_payload(account, status_code=0, message=str(err)[:240])
    except httpx.HTTPError as err:
        return _checkin_payload(account, status_code=0, message=f"上游网络错误: {str(err)[:200]}")
    return _checkin_payload(
        account,
        ok=True,
        active=True if plans else False,
        today_checked_in=False if plans else True,
        status_code=0,
        message=f"可领取套餐 {len(plans)} 个" if plans else "当前无可领取套餐",
        plans=plans,
    )


async def _claim_headers(verify_param: str, region: str | None) -> dict:
    headers = {
        "Content-Type": "application/json",
        CAPTCHA_HEADER: verify_param,
        "X-ZCode-App-Version": BILLING_APP_VERSION,
        "X-Platform": X_PLATFORM,
    }
    if region:
        headers[CAPTCHA_REGION_HEADER] = region
    return headers


async def _post_claim(account: dict, plan_id: str, verify_param: str, region: str | None) -> dict:
    """POST /billing/claim → 返回 {ok, already_claimed, code, message, plan}。"""
    base_headers = build_billing_headers(account)
    headers = await _claim_headers(verify_param, region)
    # 基座头（Authorization / X-Device-Mid / x-request-id）并入，验证码头优先级最高
    merged = {**base_headers, **headers}
    res = await _billing_request(
        "POST", "/billing/claim", headers=merged, json={"plan_id": plan_id}
    )
    try:
        body = res.json()
    except ValueError:
        body = {}
    code = _biz_code(body)
    if res.status_code >= 400 and code == -1:
        code = res.status_code
    if code == 0:
        plan = _plan_summary((body.get("data") or {}).get("plan")) if isinstance(body.get("data"), dict) else None
        return {"ok": True, "already_claimed": False, "code": 0, "message": "领取成功", "plan": plan}
    if code == 1003:
        return {"ok": False, "already_claimed": True, "code": 1003, "message": _message_for(1003, body), "plan": None}
    return {"ok": False, "already_claimed": False, "code": code, "message": _message_for(code, body), "plan": None}


async def _raw_claim(
    account: dict, plan_id: str, verify_param: str, region: str | None
) -> dict:
    """POST /billing/claim 的原始响应（诊断用：不做业务码翻译，保留 HTTP 码与 body）。"""
    merged = {**build_billing_headers(account), **await _claim_headers(verify_param, region)}
    res = await _billing_request(
        "POST", "/billing/claim", headers=merged, json={"plan_id": plan_id}
    )
    try:
        body = res.json()
    except ValueError:
        body = {"_raw_text": res.text[:600]}
    return {"http_status": res.status_code, "body": body}


async def claim_checkin(account: dict) -> dict:
    """领取优先级最高的可领套餐。验证码被拒（3007）换码重试一次。"""
    if not _is_jwt_account(account):
        return _checkin_payload(
            account,
            message="仅 Coding Plan (JWT) 账号支持领取套餐",
        )
    try:
        plans = await _fetch_previews(account)
    except RuntimeError as err:
        return _checkin_payload(account, message=str(err)[:240])
    except httpx.HTTPError as err:
        return _checkin_payload(account, message=f"上游网络错误: {str(err)[:200]}")
    if not plans:
        # 「没有可领取套餐」不是失败：今日已领过 / 活动已结束都属于这一态。
        # 早先按 ok=False 返回，管理页会把它渲染成红色「领取失败」，误导排查。
        return _checkin_payload(
            account,
            ok=True,
            already_claimed=True,
            today_checked_in=True,
            message="当前没有可领取的套餐（今日已领或活动已结束）",
        )
    target = plans[0]
    plan_id: str = target["plan_id"]

    last_message = "领取失败"
    for attempt in (1, 2):
        try:
            verify_param, verify_region = await captcha_manager.get_verify_param()
        except RuntimeError as err:
            return _checkin_payload(account, message=f"无法获取验证码: {str(err)[:160]}")
        if not verify_param:
            return _checkin_payload(account, message="验证码求解失败，请稍后重试")
        config = await captcha_manager.fetch_config()
        region = verify_region or (config or {}).get("region")
        try:
            result = await _post_claim(account, plan_id, verify_param, region)
        except httpx.HTTPError as err:
            return _checkin_payload(account, message=f"上游网络错误: {str(err)[:200]}")
        if result["ok"]:
            payload = _checkin_payload(
                account,
                ok=True,
                claimed=True,
                status_code=0,
                message=f"已领取 {target['name'] or plan_id}",
                plan_id=plan_id,
                plan_name=target["name"] or plan_id,
            )
            return payload
        if result["already_claimed"]:
            return _checkin_payload(
                account,
                ok=True,
                already_claimed=True,
                status_code=0,
                message=result["message"],
                plan_id=plan_id,
            )
        last_message = result["message"]
        if result["code"] == 3007 and attempt == 1:
            captcha_manager.invalidate()
            continue
        break
    return _checkin_payload(account, message=last_message)