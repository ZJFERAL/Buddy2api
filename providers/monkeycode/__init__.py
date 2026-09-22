"""MonkeyCode / 长亭百智云 provider。

对话通道是 **Agent 任务**（非标准 LLM chat）：
  POST /api/v1/users/tasks → WebSocket /api/v1/users/tasks/stream 拉正文。
  首次响应约 12s（Agent 冷启动），客户端超时须 ≥60s。

认证 = 同源 Cookie（`monkeycode_ai_session`），存 `accounts.access_token`。

数据结构约定：
  - accounts.uid            = 会话 Cookie 值（`/users/me` 已 404，无更优标识）
  - accounts.access_token   = Cookie 原文（database 层自动加密）
  - accounts.extra          = {session_value, plan, wallet, wallet_checked_at, ...}

WS 帧语法见 `monkeycode_ws_events.md`。
"""

from __future__ import annotations

import time
from typing import Optional

import auth_manager
import database as db
from providers.protocol import ChannelId, QuotaSnapshot
from providers.monkeycode import store
from providers.monkeycode.client import ERR_AUTH, MonkeyCodeError, client_for
from providers.monkeycode.constants import (
    CHANNEL_ID,
    DISPLAY_NAME,
    MODEL_DISPLAY,
    STATIC_MODEL_UUIDS,
    TOKEN_QUOTA_FREE_PER_DAY,
)
from providers.monkeycode import models

CHANNEL: ChannelId = CHANNEL_ID
PROVIDER_PREFIX = f"{CHANNEL_ID}/"


class MonkeyCodeProvider:
    id: ChannelId = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = True

    # ── 模型 ───────────────────────────────────────────────────────────────
    def list_models(self) -> list[dict]:
        rows = models.CATALOG.list_visible()
        if not rows:
            # 目录尚未预热（服务刚启动）→ 用 Phase 0 实测的静态兜底表
            rows = [{"id": name, "name": name} for name in sorted(STATIC_MODEL_UUIDS)]
        out = []
        for row in rows:
            slug = row["id"]
            display = MODEL_DISPLAY.get(slug, slug)
            out.append({"id": slug, "name": display})
        return out

    def alias_map(self) -> dict[str, str]:
        # 展示名（小写）也可直接调用，便于客户端从 /v1/models 里挑 id
        mapping: dict[str, str] = {}
        for slug in list(STATIC_MODEL_UUIDS) + models.CATALOG.visible_names():
            display = MODEL_DISPLAY.get(slug)
            if display and display.lower() != slug:
                mapping[display.lower()] = slug
        return mapping

    def accepts_model(self, inner: str) -> bool:
        value = self._strip_prefix(inner)
        if not value:
            return False
        if value in self.alias_map():
            return True
        return models.CATALOG.accepts(value)

    def translate_model(self, model: str) -> str:
        """外部调用名 → 内部 slug（剥离渠道前缀；展示名/别名归一）。"""
        value = self._strip_prefix(model)
        if not value:
            return ""
        aliases = self.alias_map()
        if value.lower() in aliases:
            return aliases[value.lower()]
        return value

    def resolve_model_uuid(self, model: str) -> str:
        """内部 slug → 平台 model UUID（创建任务必须传 UUID）。"""
        slug = self.translate_model(model)
        uuid, _found = models.CATALOG.resolve(slug)
        return uuid

    @staticmethod
    def _strip_prefix(model: str) -> str:
        value = (model or "").strip()
        if value.lower().startswith(PROVIDER_PREFIX):
            return value[len(PROVIDER_PREFIX):]
        return value

    # ── 账号 ───────────────────────────────────────────────────────────────
    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> Optional[dict]:
        # Cookie 无 refresh 流（重新登录才换 session）→ 不做自动刷新
        return self.pick_account(exclude_ids)

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    # ── 对话（Phase 2 实现 task.py + chat.py）──────────────────────────────
    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        from providers.monkeycode import chat

        return await chat.chat_completions(payload, api_key_info)

    async def test_chat(self, account: dict, model: str = "", prompt: str = "ping") -> dict:
        from providers.monkeycode import chat

        return await chat.test_chat(account, model, prompt)

    # ── 账号管理 ───────────────────────────────────────────────────────────
    def parse_credentials(self, body: dict) -> dict:
        return store.parse_credentials(body)

    def upsert_account(self, parsed: dict) -> dict:
        return store.upsert_account(parsed)

    def discover(self) -> dict:
        return store.discover()

    def import_path(self, path: str) -> dict:
        return store.import_path(path)

    async def refresh(self, account: dict) -> dict:
        """Cookie 无法刷新；重新校验一次以确认有效性。"""
        try:
            cookies = await self.fetch_quota(account, force=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"账号校验失败: {exc}") from exc
        if not cookies.ok:
            raise ValueError(cookies.message or "账号不可用")
        return account

    # ── 额度 ───────────────────────────────────────────────────────────────
    async def fetch_quota(self, account: dict, force: bool = False) -> QuotaSnapshot:
        """拉取每日额度（真实来源：GET /api/v1/users/wallet）。

        `daily_token_limit > 0` 即「额度机制已生效（到账）」。
        """
        account_id = int(account.get("id") or 0)
        client = client_for(account)
        try:
            wallet = await client.get_wallet()
            subscription = await client.get_subscription()
        except MonkeyCodeError as exc:
            if exc.kind == ERR_AUTH:
                store.update_status(account_id, "expired", "Cookie 失效，请重新登录")
                return QuotaSnapshot(
                    ok=False, channel=self.id, account_id=account_id,
                    unit="token", remaining=None,
                    message="Cookie 失效，请重新登录 MonkeyCode 后重新添加账号",
                )
            return QuotaSnapshot(
                ok=False, channel=self.id, account_id=account_id,
                unit="token", remaining=None, message=str(exc),
            )

        plan = str(subscription.get("plan") or "")
        limit = _num(wallet.get("daily_token_limit"))
        remaining = _num(wallet.get("daily_token_balance"))
        balance = _num(wallet.get("balance"))

        store.save_wallet_snapshot(account_id, wallet, plan)

        # 全量消耗 → 标记 exhausted（额度每日 0 点重置，故用短隔离）
        if limit is not None and remaining is not None and remaining <= 0 and (balance or 0) <= 0:
            store.update_status(account_id, "exhausted", "每日额度已用完，等待次日重置")

        return QuotaSnapshot(
            ok=True,
            channel=self.id,
            account_id=account_id,
            unit="token",
            remaining=remaining,
            extra={
                "plan": plan,
                "limit": limit,
                "remaining": remaining,
                "balance": balance,
                "free_daily_quota": TOKEN_QUOTA_FREE_PER_DAY,
                "checked_at": int(time.time()),
                "packages": [{
                    "package_name": "每日免费额度",
                    "product_name": f"MonkeyCode {plan or 'basic'}",
                    "resource_type": "token",
                    "remaining_precise": remaining,
                    "cycle_size": limit,
                    "cycle_used": (limit - remaining) if (limit is not None and remaining is not None) else None,
                    "expire_time": "每日 00:00 重置",
                }] if limit else [],
            },
        )

    # ── 签到 ───────────────────────────────────────────────────────────────
    async def fetch_checkin(self, account: dict, force: bool = False) -> dict:
        account_id = int(account.get("id") or 0)
        client = client_for(account)
        try:
            status = await client.get_checkin_status()
        except MonkeyCodeError as exc:
            return {
                "account_id": account_id,
                "account_name": account.get("nickname") or account.get("name") or str(account_id),
                "ok": False,
                "message": str(exc),
                "today_checked_in": None,
            }
        checked = bool(status.get("checked_in"))
        return {
            "account_id": account_id,
            "account_name": account.get("nickname") or account.get("name") or str(account_id),
            "ok": True,
            "already_claimed": checked,
            "today_checked_in": checked,
            "streak_days": status.get("streak_days"),
            "message": "今日已签到" if checked else "今日未签到",
        }

    async def claim_checkin(self, account: dict) -> dict:
        """签到：先解 Cap.js PoW 验证码，再提交 captcha_token。幂等（已签跳过）。"""
        from providers.monkeycode import captcha

        account_id = int(account.get("id") or 0)
        name = account.get("nickname") or account.get("name") or str(account_id)
        client = client_for(account)

        try:
            status = await client.get_checkin_status()
        except MonkeyCodeError as exc:
            return {"account_id": account_id, "account_name": name, "ok": False, "message": str(exc)}

        if status.get("checked_in"):
            return {
                "account_id": account_id, "account_name": name, "ok": True,
                "already_claimed": True, "today_checked_in": True,
                "message": "今日已签到（跳过，不重复领取）",
            }

        try:
            cap_token = await captcha.solve_captcha(client)
        except captcha.CaptchaSolveError as exc:
            return {
                "account_id": account_id, "account_name": name, "ok": False,
                "message": f"验证码求解失败: {exc}",
            }

        try:
            result = await client.post_checkin(cap_token)
        except MonkeyCodeError as exc:
            return {"account_id": account_id, "account_name": name, "ok": False, "message": str(exc)}

        return {
            "account_id": account_id,
            "account_name": name,
            "ok": True,
            "claimed": True,
            "today_checked_in": True,
            "credit": result.get("credit") or 0,
            "message": "签到成功",
        }

    # ── 诊断 ───────────────────────────────────────────────────────────────
    def diagnostics(self) -> dict:
        return {
            "channel": self.id,
            "catalog_size": len(models.CATALOG),
            "catalog_stale": models.CATALOG.stale,
            "visible_models": models.CATALOG.visible_names(),
            "session_cookie_name": "monkeycode_ai_session",
        }


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


PROVIDER = MonkeyCodeProvider()
