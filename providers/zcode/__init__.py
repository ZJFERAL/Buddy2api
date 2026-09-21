"""zcode provider —— 智谱 GLM 编码套餐（Z.AI / Bigmodel）通道。

上游协议 Anthropic Messages：
  - JWT 通道（zcode.z.ai zcode-plan）：身份头仿真 + 阿里云无痕验证码 + body 变换
  - API Key 回退通道（api.z.ai）与 Bigmodel 通道：免验证码最小头集

对外仍是 OpenAI / Responses 形态（router 统一桥接）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import httpx

import auth_manager
from providers.protocol import ChannelId, QuotaSnapshot
from providers.zcode import chat, claim, store
from providers.zcode.captcha import captcha_manager
from providers.zcode.constants import (
    BILLING_BALANCE_PATH,
    BILLING_BASE,
    BILLING_PREVIEW_PATH,
    MODEL_CATALOG,
    MODEL_NAME_MAP,
    RISK_CONTROL_HTTP_STATUSES,
    RISK_CONTROL_MARKERS,
)
from providers.zcode.identity import build_billing_headers
from providers.zcode.oauth import complete_flow, create_session, get_session, start_flow
from providers.zcode.token import account_mode

# billing/* 同账号最小查询间隔（秒）：连续查询易触发上游风控
QUOTA_CACHE_TTL = 30
CHANNEL_ID: ChannelId = "zcode"
DISPLAY_NAME = "ZCode"


class ZCodeProvider:
    id: ChannelId = "zcode"
    display_name = DISPLAY_NAME
    checkin_supported = True

    # ── 模型 ────────────────────────────────────────────────────────────────
    def list_models(self) -> list[dict]:
        import catalog

        rows = [{"id": item["id"], "name": item["name"]} for item in MODEL_CATALOG]
        return catalog.models_for(self.id, rows)

    def alias_map(self) -> dict[str, str]:
        import aliases

        return aliases.merged_map(self.id)

    def accepts_model(self, inner: str) -> bool:
        value = (inner or "").strip()
        if value in self.alias_map():
            return True
        ids = {item["id"] for item in MODEL_CATALOG}
        return value in ids or value.lower() in MODEL_NAME_MAP

    def translate_model(self, model: str) -> str:
        return chat.translate_model(model)

    # ── 账号 ────────────────────────────────────────────────────────────────
    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> Optional[dict]:
        # JWT 过期不自动刷新（zcode 无 refresh 流），直接交给 chat 层换号
        return self.pick_account(exclude_ids)

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    # ── 对话 ────────────────────────────────────────────────────────────────
    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        return await chat.chat_completions(payload, api_key_info)

    # ── 账号管理 ────────────────────────────────────────────────────────────
    def parse_credentials(self, body: dict) -> dict:
        return store.parse_credentials(body)

    def upsert_account(self, parsed: dict) -> dict:
        return store.upsert_account(parsed)

    def discover(self) -> dict:
        return store.discover()

    def import_path(self, path: str) -> dict:
        return store.import_discovered(path)

    # ── OAuth 免密登录（管理端接口）──────────────────────────────────────────
    async def oauth_start(self) -> dict:
        sid = create_session()
        await start_flow(sid)
        sess = get_session(sid)
        return {"session_id": sid, "authorize_url": sess["authorize_url"]}

    async def oauth_status(self, session_id: str) -> dict:
        sess = get_session(session_id)
        if sess is None:
            raise ValueError("会话不存在")
        return {"status": sess.get("status", "unknown")}

    async def oauth_complete(self, session_id: str) -> dict:
        return await complete_flow(session_id)

    def import_zcode2api(self, db_path: str) -> dict:
        return store.import_from_zcode2api(db_path)

    # ── 套餐领取（checkin）─────────────────────────────────────────────────
    async def fetch_checkin(self, account: dict, force: bool = False) -> dict:
        return await claim.fetch_checkin(account, force=force)

    async def claim_checkin(self, account: dict) -> dict:
        return await claim.claim_checkin(account)

    def diagnostics(self) -> dict:
        """渠道排障信息：验证码求解器可用性 + 最近一次失败原因。

        领取失败最容易被误判成「凭据问题」，实际多数是验证码求解环节：
        自带求解器只能拿到 failover 降级 param（无 securityToken），上游会回
        `400 code=3007 captcha verify failed`。这里把求解器清单、实际用到哪个、
        哪些被判为降级、最近错误一次性暴露出来。
        """
        return {
            "captcha": captcha_manager.diagnostics(),
            "billing_base": BILLING_BASE,
        }

    # ── 额度 / 套餐 ─────────────────────────────────────────────────────────
    async def fetch_quota(self, account: dict, force: bool = False) -> QuotaSnapshot:
        """拉取「生效套餐 + 额度窗口」并附带「可领取套餐」。

        契约（2026-09-21 真机 dump，zcode-api src/server/routes-quota.ts 交叉确认）：
          - GET {billing_base}/billing/balance → data.plans[]（生效套餐，
            entitlements[].grant_units / period / effective_at）
            + data.balances[]（额度窗口：show_name / total_units / used_units /
            remaining_units / unit_type / period_end） + data.server_time
          - GET {billing_base}/billing/preview → data.plans[]（可领取套餐）
          - /billing/current 与 /usage 实测不可用（405+code3012 / 404），已弃用
        """
        account_id = int(account.get("id") or 0)
        extra = account.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        provider = extra.get("provider") or "zai"
        if provider == "bigmodel":
            return QuotaSnapshot(
                ok=False, channel=self.id, account_id=account_id,
                unit="token", remaining=None, unsupported=True,
                message="bigmodel 额度端点未启用",
            )

        headers = build_billing_headers(account)

        # 错峰：billing/* 连续查询易触发风控，最小间隔内复用上次结果
        # force=True（用户在管理页主动刷新）时绕过缓存
        last_checked = float(extra.get("quota_checked_at") or 0)
        cached = extra.get("quota_cache")
        if (
            not force
            and isinstance(cached, dict)
            and last_checked
            and time.time() - last_checked < QUOTA_CACHE_TTL
        ):
            return _snapshot(self.id, account_id, cached, stale=True)

        async with httpx.AsyncClient(timeout=20) as client:
            balance_res, preview_res = await asyncio.gather(
                _safe_get(client, f"{BILLING_BASE}{BILLING_BALANCE_PATH}", headers),
                _safe_get(client, f"{BILLING_BASE}{BILLING_PREVIEW_PATH}", headers),
            )

        if balance_res is None:
            return QuotaSnapshot(
                ok=False, channel=self.id, account_id=account_id,
                unit="token", remaining=None, message="额度查询失败（网络错误）",
            )

        # 鉴权失效 → 账号置 invalid（验证码挑战属风控链路，不算鉴权问题）
        if balance_res.status_code in (401, 403):
            body = (balance_res.text or "").lower()
            if "captcha" not in body and "verify" not in body:
                store.update_status(account_id, "invalid", f"鉴权失败 HTTP {balance_res.status_code}")
                return QuotaSnapshot(
                    ok=False, channel=self.id, account_id=account_id,
                    unit="token", remaining=None, message="billing 鉴权失败",
                )

        balance_body = _safe_json(balance_res) or {}
        if balance_res.status_code in RISK_CONTROL_HTTP_STATUSES or _looks_like_risk(balance_res):
            return QuotaSnapshot(
                ok=False, channel=self.id, account_id=account_id,
                unit="token", remaining=None,
                message="上游风控拦截（unusual activity），请稍后再试",
            )

        code = _biz_code(balance_body)
        if code != 0:
            msg = str(balance_body.get("msg") or balance_body.get("message") or "").strip()
            if code == 3012:
                return QuotaSnapshot(
                    ok=False, channel=self.id, account_id=account_id,
                    unit="token", remaining=None,
                    message="上游风控拦截（unusual activity），请稍后再试",
                )
            return QuotaSnapshot(
                ok=False, channel=self.id, account_id=account_id,
                unit="token", remaining=None,
                message=f"上游返回 code={code}" + (f"：{msg}" if msg else ""),
            )

        data = balance_body.get("data") if isinstance(balance_body.get("data"), dict) else {}
        windows = _parse_windows(data.get("balances"))
        plans = _parse_plans(data.get("plans"))
        outcome: dict = {
            "balance": balance_body,
            "preview": _safe_json(preview_res) or {},
            "windows": windows,
            "plans": plans,
            "claimable": _parse_claimable(_safe_json(preview_res)),
            "server_time": data.get("server_time"),
            "plan_name": plans[0]["name"] if plans else "",
            "plan_ends_at": plans[0].get("ends_at") if plans else None,
        }
        outcome.update(_sum_token_windows(windows))
        outcome["packages"] = _to_packages(windows, outcome.get("plan_name") or "")

        # 账号状态：全部窗口耗尽且无已生效的一次性赠送池 → exhausted；否则恢复 active
        remaining = outcome.get("remaining")
        if remaining is not None and remaining <= 0 and not _has_active_bonus(plans):
            store.update_status(account_id, "exhausted", "额度已用完")
        elif extra.get("last_error"):
            store.update_status(account_id, "active")

        patch_extra = dict(extra)
        patch_extra["quota_cache"] = outcome
        patch_extra["quota_checked_at"] = time.time()
        import database as db_impl

        db_impl.update_account(account_id, {"extra": patch_extra})

        return _snapshot(self.id, account_id, outcome)


PROVIDER = ZCodeProvider()


# ── 工具 ───────────────────────────────────────────────────────────────────
def _biz_code(body) -> int:
    """上游业务码（HTTP 200 也可能承载错误码，必须单独判定）。"""
    if not isinstance(body, dict):
        return -1
    try:
        code = body.get("code")
        return int(code) if code is not None else 0
    except (TypeError, ValueError):
        return -1


def _num(value) -> Optional[float]:
    """宽松数值解析：None / 非数字 / bool → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _looks_like_risk(resp) -> bool:
    """响应体是否含风控标记（3012 / unusual activity）。"""
    try:
        text = (resp.text or "").lower()
    except Exception:  # noqa: BLE001 - 响应对象异常不应影响额度链路
        return False
    return any(marker in text for marker in RISK_CONTROL_MARKERS)


def _parse_windows(balances) -> list[dict]:
    """billing/balance 的 data.balances[] → 归一化额度窗口列表。

    同名窗口（同一模型可能有多个 bucket）合并，避免后写覆盖前写。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for item in balances or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("show_name") or item.get("model") or "额度").strip() or "额度"
        caps = item.get("capabilities")
        window = {
            "name": name,
            "unit": str(item.get("unit_type") or "token"),
            "total": _num(item.get("total_units")),
            "used": _num(item.get("used_units")),
            "remaining": _num(item.get("remaining_units")),
            "available": _num(item.get("available_units")),
            "period_start": _num(item.get("period_start")),
            "period_end": _num(item.get("period_end") if item.get("period_end") is not None else item.get("expires_at")),
            "plan_id": str(item.get("plan_id") or ""),
            "entitlement_id": str(item.get("entitlement_id") or ""),
            "capabilities": [str(c) for c in caps] if isinstance(caps, list) else [],
            "priority": _num(item.get("priority")) or 0,
        }
        prev = merged.get(name)
        if prev is None:
            merged[name] = window
            order.append(name)
            continue
        for key in ("total", "used", "remaining", "available"):
            if window[key] is None and prev[key] is None:
                continue
            prev[key] = (prev[key] or 0.0) + (window[key] or 0.0)
        ends = [v for v in (prev.get("period_end"), window.get("period_end")) if v]
        prev["period_end"] = max(ends) if ends else None
        if window["period_start"] and not prev["period_start"]:
            prev["period_start"] = window["period_start"]
    return [merged[name] for name in order]


def _parse_plans(plans) -> list[dict]:
    """billing/balance 的 data.plans[] → 生效套餐列表（含 entitlements）。"""
    out: list[dict] = []
    for raw in plans or []:
        if not isinstance(raw, dict):
            continue
        ents = []
        for ent in raw.get("entitlements") or []:
            if not isinstance(ent, dict):
                continue
            ents.append({
                "id": str(ent.get("entitlement_id") or ""),
                "name": str(ent.get("show_name") or ""),
                "units": _num(ent.get("grant_units")) or 0.0,
                "unit_type": str(ent.get("unit_type") or "token"),
                "period": str(ent.get("period") or ""),
                "meter": str(ent.get("meter") or ""),
                "effective_at": _num(ent.get("effective_at")) or 0.0,
                "expires_at": _num(ent.get("ends_at") if ent.get("ends_at") is not None else ent.get("expires_at")),
            })
        out.append({
            "plan_id": str(raw.get("plan_id") or raw.get("id") or ""),
            "name": str(raw.get("name") or raw.get("plan_id") or "套餐").strip(),
            "description": str(raw.get("description") or "").strip(),
            "status": str(raw.get("status") or "").strip(),
            "starts_at": _num(raw.get("starts_at")),
            "ends_at": _num(raw.get("ends_at")),
            "priority": _num(raw.get("priority")) or 0,
            "entitlements": ents,
        })
    out.sort(key=lambda p: p.get("priority") or 0, reverse=True)
    return out


def _parse_claimable(preview_body) -> list[dict]:
    """billing/preview → 可领取套餐（复用 claim 的解析，字段语义一致）。"""
    if not isinstance(preview_body, dict) or _biz_code(preview_body) != 0:
        return []
    data = preview_body.get("data") if isinstance(preview_body.get("data"), dict) else {}
    plans = data.get("plans")
    if not isinstance(plans, list):
        return []
    result = []
    for raw in plans:
        summary = claim._plan_summary(raw)  # noqa: SLF001 - 同一 provider 内部复用
        if summary:
            result.append(summary)
    result.sort(key=lambda p: p.get("priority") or 0, reverse=True)
    return result


def _sum_token_windows(windows: list[dict]) -> dict:
    """按 token 语义汇总额度（跨模型加总仅用于总量展示，不作路由判断）。"""
    total = used = remaining = 0.0
    has_total = has_used = has_remaining = False
    for window in windows:
        if window.get("unit") != "token":
            continue
        if window.get("total") is not None:
            total += window["total"]
            has_total = True
        if window.get("used") is not None:
            used += window["used"]
            has_used = True
        if window.get("remaining") is not None:
            remaining += window["remaining"]
            has_remaining = True
    return {
        "limit": total if has_total else None,
        "used": used if has_used else None,
        "remaining": remaining if has_remaining else None,
        "window_count": len(windows),
    }


def _has_active_bonus(plans: list[dict], now: Optional[float] = None) -> bool:
    """是否存在已生效的一次性赠送额度（balance 不含这类池，用于耗尽判定）。"""
    now = time.time() if now is None else now
    for plan in plans:
        for ent in plan.get("entitlements") or []:
            if ent.get("period") != "one_time":
                continue
            effective = ent.get("effective_at") or 0
            expires = ent.get("expires_at") or 0
            if effective and effective <= now and (not expires or now <= expires):
                return True
    return False


def _fmt_time(ts: Optional[float]) -> str:
    """epoch 秒 → 本地 'YYYY-MM-DD HH:MM'；无效值返回空串。"""
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (ValueError, OSError, OverflowError):
        return ""


def _to_packages(windows: list[dict], plan_name: str) -> list[dict]:
    """额度窗口 → 前端明细表条目（字段名对齐 web/index.html 已有列）。"""
    packages = []
    now = time.time()
    for window in windows:
        end = window.get("period_end")
        days = None
        if end:
            days = int((float(end) - now) // 86400)
        packages.append({
            "package_name": window.get("name") or "额度",
            "product_name": plan_name or "额度窗口",
            "resource_type": window.get("unit") or "token",
            "remaining_precise": window.get("remaining"),
            "cycle_used": window.get("used"),
            "cycle_size": window.get("total"),
            # 前端 shortTime() 按字符串切分展示，故这里直接给格式化文本
            "cycle_start": _fmt_time(window.get("period_start")),
            "cycle_end": _fmt_time(window.get("period_end")),
            "expire_time": _fmt_time(end),
            "days_to_expire": days,
            "expired": bool(end and float(end) < now),
        })
    return packages


def _snapshot(channel: str, account_id: int, outcome: dict, stale: bool = False) -> QuotaSnapshot:
    """outcome（含缓存旧格式）→ QuotaSnapshot。缺解析结果时现场补算。"""
    outcome = outcome if isinstance(outcome, dict) else {}
    balance = outcome.get("balance") if isinstance(outcome.get("balance"), dict) else {}
    data = balance.get("data") if isinstance(balance.get("data"), dict) else {}

    windows = outcome.get("windows")
    if not isinstance(windows, list) or not windows:
        windows = _parse_windows(data.get("balances"))
    plans = outcome.get("plans")
    if not isinstance(plans, list) or not plans:
        plans = _parse_plans(data.get("plans"))

    sums = _sum_token_windows(windows)
    remaining = outcome.get("remaining")
    if remaining is None:
        remaining = sums.get("remaining")
    used = outcome.get("used")
    if used is None:
        used = sums.get("used")
    limit = outcome.get("limit")
    if limit is None:
        limit = sums.get("limit")

    plan_name = outcome.get("plan_name") or (plans[0]["name"] if plans else "")
    packages = outcome.get("packages")
    if not isinstance(packages, list):
        packages = _to_packages(windows, plan_name)

    extra = dict(outcome)
    extra.update({
        "windows": windows,
        "plans": plans,
        "plan_name": plan_name,
        "packages": packages,
        "used": used,
        "limit": limit,
        "stale": bool(stale),
        "checked_at": time.time(),
    })
    return QuotaSnapshot(
        ok=True,
        channel=channel,
        account_id=account_id,
        unit="token",
        remaining=remaining,
        extra=extra,
    )


async def _safe_get(client: httpx.AsyncClient, url: str, headers: dict):
    try:
        return await client.get(url, headers=headers)
    except httpx.HTTPError:
        return None


def _safe_json(resp) -> dict | None:
    if resp is None:
        return None
    try:
        data = resp.json()
        return data if isinstance(data, dict) else None
    except ValueError:
        return None