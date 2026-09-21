"""zcode 通道端到端测试（Mock 上游）：
1. JWT 通道（验证码头 + body 变换断言）
2. API Key 回退通道
3. 流式 SSE 转换
4. 账号解析 / 导入

用法：python test_zcode_e2e.py  （在 Buddy2api 项目根运行）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

# 环境先行：指向临时 DB 与 Mock 上游
_TMP = tempfile.mkdtemp(prefix="zcode_e2e_")
os.environ["CB_GATEWAY_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["ZAI_UPSTREAM_URL"] = "http://127.0.0.1:8599/api/v1/zcode-plan/anthropic/v1/messages"
os.environ["ZAI_FALLBACK_URL"] = "http://127.0.0.1:8599/api/anthropic/v1/messages"
os.environ["ZCODE_BILLING_BASE"] = "http://127.0.0.1:8599/api/v1/zcode-plan"
os.environ["ZCODE_OAUTH_BASE"] = "http://127.0.0.1:8599/api/v1"
os.environ["ZCODE_EXCHANGE_ORIGIN"] = "http://127.0.0.1:8599"
os.environ["ZCODE_MOCK_BODY_LOG"] = os.path.join(_TMP, "last_body.json")
# 已弃用端点（/billing/current）若被请求会写该文件，用于断言「未再请求」
os.environ["ZCODE_MOCK_CURRENT_HIT_LOG"] = os.path.join(_TMP, "current_hits.log")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import database  # noqa: E402
import providers.zcode.chat as zchat  # noqa: E402
from providers.zcode import PROVIDER, store, token as ztoken  # noqa: E402
from providers.zcode.captcha import captcha_manager  # noqa: E402
from mock_zcode_upstream import start as start_mock  # noqa: E402

OUT = []


def check(name, cond, detail=""):
    OUT.append(("OK  " if cond else "FAIL") + f" {name}" + (f" :: {detail}" if detail else ""))


def fake_jwt(user_id: str = "u_test_123") -> str:
    import base64

    def b64(o: dict) -> str:
        raw = json.dumps(o).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    import time

    payload = {"sub": user_id, "user_id": user_id, "exp": int(time.time()) + 86400}
    return f"{b64({'alg':'HS256','typ':'JWT'})}.{b64(payload)}.fakesig"


def add_jwt_account(user_id: str = "u_test_123") -> dict:
    parsed = store.parse_credentials({"token": fake_jwt(user_id), "provider": "zai", "mode": "jwt", "name": "mock-jwt"})
    store.upsert_account(parsed)
    return next(a for a in database.list_accounts(provider="zcode") if a.get("extra", {}).get("mode") == "jwt")


def add_apikey_account() -> dict:
    parsed = store.parse_credentials({"token": "mock-api-key-1.mock-secret-1", "provider": "zai", "mode": "apiKey", "name": "mock-apikey"})
    store.upsert_account(parsed)
    return next(a for a in database.list_accounts(provider="zcode") if a.get("extra", {}).get("mode") == "apiKey")


async def test_jwt_channel():
    account = add_jwt_account()
    # 绕过真实验证码求解（Mock 接受任意验证码头）
    async def _fake_verify_param():
        return "mock-verify-param", "cn"

    captcha_manager.get_verify_param = _fake_verify_param  # type: ignore

    result = await zchat.chat_completions(
        {"model": "glm-5.3", "messages": [{"role": "user", "content": "ping"}], "stream": False},
        None,
    )
    kind = result[0]
    check("JWT 非流式返回 json", kind == "json", f"kind={kind}")
    if kind == "json":
        body = result[1]
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        check("JWT 回复内容", "pong from mock upstream" in text, text[:60])
        check("JWT 模型回显", bool(body.get("model")), str(body.get("model")))  # 上游返回官方名，router 层会改写回 original

    # body 变换断言（mock 记录了收到请求体）
    log_path = os.environ["ZCODE_MOCK_BODY_LOG"]
    if os.path.exists(log_path):
        sent = json.load(open(log_path, encoding="utf-8"))
        system = sent.get("system") or []
        check("body system 官方块注入", len(system) >= 3, f"{len(system)} blocks")
        has_model_block = any("powered by the model named GLM-5.3" in (b.get("text") or "") for b in system if isinstance(b, dict))
        check("body currentModel 块", has_model_block)
        check("body metadata.user_id", (sent.get("metadata") or {}).get("user_id") == "u_test_123")
        last_msg = sent.get("messages", [])[-1]
        blocks = last_msg.get("content") or []
        has_cc = isinstance(blocks[-1], dict) and blocks[-1].get("cache_control") == {"type": "ephemeral"}
        check("body cache_control 注入", has_cc)
    else:
        check("body 变换断言（mock body 日志）", False, "未找到 body 日志")


async def test_apikey_channel():
    add_apikey_account()
    result = await zchat.chat_completions(
        {"model": "glm-5.3", "messages": [{"role": "user", "content": "ping"}], "stream": False},
        None,
    )
    kind = result[0]
    check("API Key 非流式返回 json", kind == "json", f"kind={kind}")
    if kind == "json":
        text = (result[1].get("choices") or [{}])[0].get("message", {}).get("content", "")
        check("API Key 回复内容", "pong from mock upstream" in text, text[:60])


async def test_stream():
    add_apikey_account()
    result = await zchat.chat_completions(
        {"model": "glm-5.3", "messages": [{"role": "user", "content": "ping"}], "stream": True},
        None,
    )
    check("流式返回 stream", result[0] == "stream")
    if result[0] == "stream":
        lines = []
        async for chunk in result[1]:
            lines.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
        joined = "".join(lines)
        check("流式含 role 首 chunk", '"role":"assistant"' in joined or '"role": "assistant"' in joined)
        check("流式含内容 chunk", "pong from mock upstream" in joined)
        check("流式含 [DONE]", "data: [DONE]" in joined)


def test_parse():
    p = store.parse_credentials({"token": fake_jwt("u_a"), "mode": "jwt"})
    check("JWT 解析 mode", p["extra"]["mode"] == "jwt")
    check("JWT 解析 uid", p["uid"] == "u_a")
    p2 = store.parse_credentials({"token": "k1.s1", "mode": "apiKey"})
    check("API Key 解析", p2["extra"]["mode"] == "apiKey" and p2["extra"].get("api_key") == "k1.s1")
    check("is_jwt 判定", ztoken.is_jwt(fake_jwt()) and not ztoken.is_jwt("k1.s1"))


async def _patch_captcha():
    """绕过真实 Node 求解器：claim 链路直接给固定验证码。"""
    async def _fake_verify_param():
        return "mock-verify-param", "cn"

    async def _fake_config():
        return {"region": "cn"}

    captcha_manager.get_verify_param = _fake_verify_param  # type: ignore
    captcha_manager.fetch_config = _fake_config  # type: ignore
    captcha_manager.invalidate = lambda: None  # type: ignore


async def test_fetch_checkin():
    add_jwt_account("u_checkin_1")
    await _patch_captcha()
    from providers.zcode import claim as zclaim

    result = await zclaim.fetch_checkin(next(a for a in database.list_accounts(provider="zcode") if a.get("extra", {}).get("mode") == "jwt"))
    check("claim 查询 ok", bool(result.get("ok")), result.get("message"))
    plans = result.get("plans") or []
    check("claim preview 套餐数", len(plans) == 2, f"{len(plans)}")
    check("claim preview 优先级排序", bool(plans) and plans[0]["plan_id"] == "wk-0918", (plans[0] if plans else {}).get("plan_id", "-"))
    check("claim preview 含资格明细", bool(plans[0].get("entitlements")), str(plans[0].get("entitlements"))[:80])


async def test_claim_ok():
    os.environ["ZCODE_MOCK_CLAIM_CODE"] = "0"
    os.environ["ZCODE_MOCK_CLAIM_LOG"] = os.path.join(_TMP, "last_claim.json")
    account = next(a for a in database.list_accounts(provider="zcode") if a.get("extra", {}).get("mode") == "jwt")
    from providers.zcode import claim as zclaim

    result = await zclaim.claim_checkin(account)
    check("claim 成功", bool(result.get("ok")), result.get("message"))
    check("claim claimed 标记", bool(result.get("claimed")))
    check("claim 领取最高优先级套餐", result.get("plan_id") == "wk-0918", str(result.get("plan_id")))
    # claim 请求头形态断言（Authorization + 验证码头 + 版本/平台头）
    log_path = os.environ["ZCODE_MOCK_CLAIM_LOG"]
    if os.path.exists(log_path):
        sent = json.load(open(log_path, encoding="utf-8"))
        check("claim 头带 Bearer", sent.get("auth", "").startswith("Bearer "), sent.get("auth", "")[:24])
        check("claim 头带验证码头", bool(sent.get("captcha")), str(sent.get("captcha"))[:20])
        check("claim 头带版本", bool(sent.get("app_ver")), str(sent.get("app_ver")))
        check("claim 头带平台", bool(sent.get("platform")), str(sent.get("platform")))
        check("claim body plan_id", (sent.get("body") or {}).get("plan_id") == "wk-0918", str(sent.get("body")))
    else:
        check("claim 头断言（请求日志）", False, "未找到 claim 日志")


async def test_claim_already():
    os.environ["ZCODE_MOCK_CLAIM_CODE"] = "1003"
    os.environ["ZCODE_MOCK_CLAIM_LOG"] = os.path.join(_TMP, "last_claim_1003.json")
    account = next(a for a in database.list_accounts(provider="zcode") if a.get("extra", {}).get("mode") == "jwt")
    from providers.zcode import claim as zclaim

    result = await zclaim.claim_checkin(account)
    check("claim 已领取标记", bool(result.get("already_claimed")), result.get("message"))
    check("claim 已领取不误报成功", not result.get("claimed"))


async def test_claim_apikey_unsupported():
    add_apikey_account()
    from providers.zcode import claim as zclaim

    account = next(a for a in database.list_accounts(provider="zcode") if a.get("extra", {}).get("mode") == "apiKey")
    result = await zclaim.claim_checkin(account)
    check("API Key 不支持领取", not result.get("ok"), result.get("message"))
    check("API Key 领取提示仅 JWT", "JWT" in result.get("message", ""))

    result2 = await zclaim.fetch_checkin(account)
    check("API Key 无领取状态", not result2.get("ok"))


def _reset_oauth_env(status: str = "ready"):
    os.environ["ZCODE_MOCK_OAUTH_STATUS"] = status
    for k in ("ZCODE_MOCK_OAUTH_JWT", "ZCODE_MOCK_OAUTH_OMIT_TOKEN"):
        os.environ.pop(k, None)


async def test_oauth_authorize_url_interstitial():
    """init 返回的 authorize_url 必须被覆盖为 /app/oauth/login 中转页。

    不覆盖的话浏览器授权完上游 poll 恒为 pending（真机症状）。
    """
    from providers.zcode import oauth as zoauth

    sid = zoauth.create_session()
    started = await zoauth.start_flow(sid)
    url = started["authorize_url"]
    import urllib.parse

    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    check("OAuth 保留原 query 参数", q.get("client_id") == "c" and q.get("state") == "s1", url[:90])
    check("OAuth redirect_uri 覆盖为中转页", q.get("redirect_uri", "").startswith("https://zcode.z.ai/app/oauth/login?"))
    check("OAuth 中转页带深链 redirect", "zcode%3A%2F%2Foauth%2Fcallback" in q.get("redirect_uri", ""))
    check("OAuth 不再使用上游 cli/callback", "cli%2Fcallback" not in url and "cli/callback" not in url)
    check("OAuth 轮询间隔已回填", zoauth.get_session(sid).get("poll_interval_sec") == 1)


async def test_oauth_pending_then_ready():
    """pending 不报错、可重试；ready 后按 data.token 入池 JWT + 兑换回退 Key。"""
    from providers.zcode import oauth as zoauth

    _reset_oauth_env("pending")
    saved_budget = zoauth.POLL_BUDGET_SEC
    zoauth.POLL_BUDGET_SEC = 1.2  # 加速：pending 分支只需确认"不报错且能重试"
    try:
        sid = zoauth.create_session()
        await zoauth.start_flow(sid)
        first = await zoauth.complete_flow(sid)
        check("OAuth pending 不报错", first.get("status") == "pending", str(first))
        check("OAuth pending 可重试（会话保留）", zoauth.get_session(sid).get("status") == "pending")
    finally:
        zoauth.POLL_BUDGET_SEC = saved_budget

    os.environ["ZCODE_MOCK_OAUTH_STATUS"] = "ready"
    done = await zoauth.complete_flow(sid)
    check("OAuth ready → done", done.get("status") == "done", str(done))
    check("OAuth 回退 Key 已兑换", done.get("fallback_key") is True)
    acc = next(a for a in database.list_accounts(provider="zcode") if a.get("uid") == "u_oauth_mock")
    check("OAuth JWT 取自 data.token", acc.get("extra", {}).get("mode") == "jwt")
    check("OAuth 账号名为 JWT 解析出的 uid", acc.get("name") == "u_oauth_mock", str(acc.get("name")))
    check("OAuth 回退 Key 落盘（{apiKey}.{secretKey}）",
          acc.get("extra", {}).get("api_key") == "mock-api-key.mock-secret-key",
          str(acc.get("extra", {}).get("api_key")))
    again = await zoauth.complete_flow(sid)
    check("OAuth 幂等：重复 complete 不重复入池", again.get("status") == "done" and again.get("account_id") == done.get("account_id"))


async def test_oauth_failed_and_expired():
    from providers.zcode import oauth as zoauth

    for state, expect in (("failed", "failed"), ("expired", "expired")):
        _reset_oauth_env(state)
        sid = zoauth.create_session()
        await zoauth.start_flow(sid)
        r = await zoauth.complete_flow(sid)
        check(f"OAuth 上游 {state} → {expect}", r.get("status") == expect, str(r))
        check(f"OAuth {state} 带可读提示", bool(r.get("message")))
    _reset_oauth_env("ready")


async def test_oauth_ready_without_jwt_rejected():
    """ready 但缺 data.token 时必须失败——绝不能把 zai.access_token 当 JWT 入池。"""
    from providers.zcode import oauth as zoauth

    _reset_oauth_env("ready")
    os.environ["ZCODE_MOCK_OAUTH_OMIT_TOKEN"] = "1"
    sid = zoauth.create_session()
    await zoauth.start_flow(sid)
    r = await zoauth.complete_flow(sid)
    check("OAuth ready 缺 JWT → failed", r.get("status") == "failed", str(r))
    check("OAuth 缺 JWT 提示指向 data.token", "token" in r.get("message", ""), str(r.get("message")))
    check("OAuth 缺 JWT 时不入池",
          not any(a.get("uid") == "u_oauth_mock" and a.get("extra", {}).get("api_key") == "mock-zai-access-token"
                  for a in database.list_accounts(provider="zcode")))
    _reset_oauth_env("ready")


def _clear_quota_cache(account: dict) -> dict:
    """清掉额度缓存，使下一次 fetch_quota 真正打上游。"""
    extra = dict(account.get("extra") or {})
    extra.pop("quota_cache", None)
    extra.pop("quota_checked_at", None)
    database.update_account(int(account["id"]), {"extra": extra})
    return database.get_account(int(account["id"]))


async def test_quota_snapshot():
    """额度/套餐：真机结构解析（窗口 / 生效套餐 / 可领取 / 汇总 / 前端明细）。"""
    account = add_jwt_account("u_quota_1")
    os.environ.pop("ZCODE_MOCK_BALANCE_MODE", None)
    account = _clear_quota_cache(account)
    snap = await PROVIDER.fetch_quota(account)
    extra = snap.extra or {}

    check("额度：ok", snap.ok is True, str(snap.message))
    check("额度：unit=token", snap.unit == "token")
    check("额度：剩余=窗口之和", snap.remaining == 7750000, f"remaining={snap.remaining}")
    check("额度：上限合计", extra.get("limit") == 8000000, f"limit={extra.get('limit')}")
    check("额度：已用合计", extra.get("used") == 250000, f"used={extra.get('used')}")
    check("额度：生效套餐名", extra.get("plan_name") == "ZCode Start Plan", str(extra.get("plan_name")))
    check("额度：窗口数 2", len(extra.get("windows") or []) == 2)

    plans = extra.get("plans") or []
    ents = (plans[0].get("entitlements") if plans else []) or []
    check("额度：套餐 entitlements 解析", len(ents) == 2, f"ents={len(ents)}")
    check("额度：entitlement 额度单位", bool(ents) and ents[0].get("units") == 3000000)

    claimable = extra.get("claimable") or []
    check("额度：可领取套餐非空", bool(claimable), json.dumps(claimable, ensure_ascii=False)[:160])

    pkgs = extra.get("packages") or []
    check("额度：前端明细 2 条", len(pkgs) == 2)
    check("额度：明细名=模型名", bool(pkgs) and pkgs[0].get("package_name") == "GLM-5.3")
    check("额度：明细含到期文本", bool(pkgs) and bool(pkgs[0].get("expire_time")), str(pkgs[0].get("expire_time")) if pkgs else "")
    check("额度：明细周期为字符串（前端 shortTime 需要）",
          bool(pkgs) and isinstance(pkgs[0].get("cycle_end"), str), type(pkgs[0].get("cycle_end")).__name__ if pkgs else "")

    # 已弃用端点不得再被请求（真机为 405+3012 / 404）
    hit_log = os.environ.get("ZCODE_MOCK_CURRENT_HIT_LOG", "")
    hits = ""
    if hit_log and os.path.exists(hit_log):
        with open(hit_log, encoding="utf-8") as fh:
            hits = fh.read().strip()
    check("额度：未再请求 /billing/current", hits == "", f"hits={hits[:80]}")


async def test_quota_risk_blocked():
    """风控拦截（HTTP 405 + code 3012）→ 明确提示，不误判为「额度用完」。"""
    account = add_jwt_account("u_quota_1")
    os.environ["ZCODE_MOCK_BALANCE_MODE"] = "risk"
    account = _clear_quota_cache(account)
    snap = await PROVIDER.fetch_quota(account)
    os.environ.pop("ZCODE_MOCK_BALANCE_MODE", None)
    check("风控：ok=False", snap.ok is False)
    check("风控：提示含 unusual activity", "unusual activity" in (snap.message or ""), str(snap.message))
    check("风控：remaining 为 None", snap.remaining is None)
    check("风控：不标记 exhausted",
          database.get_account(int(account["id"])).get("status") != "exhausted",
          str(database.get_account(int(account["id"])).get("status")))


async def test_quota_biz_error():
    """HTTP 200 但 body code!=0（上游业务错误藏在 200 里）→ 必须报错而非当成功。"""
    account = add_jwt_account("u_quota_1")
    os.environ["ZCODE_MOCK_BALANCE_MODE"] = "bizerror"
    account = _clear_quota_cache(account)
    snap = await PROVIDER.fetch_quota(account)
    os.environ.pop("ZCODE_MOCK_BALANCE_MODE", None)
    check("业务码：ok=False", snap.ok is False)
    check("业务码：提示含 code=1005", "1005" in (snap.message or ""), str(snap.message))


async def test_quota_unauth_marks_invalid():
    """401 → 账号置 invalid（凭据失效，不是额度问题）。"""
    account = add_jwt_account("u_quota_1")
    os.environ["ZCODE_MOCK_BALANCE_MODE"] = "unauth"
    account = _clear_quota_cache(account)
    snap = await PROVIDER.fetch_quota(account)
    os.environ.pop("ZCODE_MOCK_BALANCE_MODE", None)
    check("401：ok=False", snap.ok is False)
    check("401：账号标记 invalid",
          database.get_account(int(account["id"])).get("status") == "invalid",
          str(database.get_account(int(account["id"])).get("status")))
    store.update_status(int(account["id"]), "active")
    check("401：可恢复 active", database.get_account(int(account["id"])).get("status") == "active")


async def main():
    database.init_db()
    test_parse()
    await test_jwt_channel()
    await test_apikey_channel()
    await test_stream()
    await test_fetch_checkin()
    await test_claim_ok()
    await test_claim_already()
    await test_claim_apikey_unsupported()
    await test_oauth_authorize_url_interstitial()
    await test_oauth_pending_then_ready()
    await test_oauth_failed_and_expired()
    await test_oauth_ready_without_jwt_rejected()
    await test_quota_snapshot()
    await test_quota_risk_blocked()
    await test_quota_biz_error()
    await test_quota_unauth_marks_invalid()
    print("\n".join(OUT))


if __name__ == "__main__":
    server = start_mock(8599)
    try:
        asyncio.run(main())
    finally:
        server.shutdown()
        fails = [x for x in OUT if x.startswith("FAIL")]
        print(f"\n== {len(OUT) - len(fails)}/{len(OUT)} passed ==")
        if fails:
            sys.exit(1)