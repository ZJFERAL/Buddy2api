"""MonkeyCode 上游 HTTP 客户端。

统一负责：
  - Cookie 会话头构造
  - `{code, message, data}` 外壳解析（HTTP 200 也可能承载业务错误码）
  - 错误分类（auth / rate_limit / quota / busy / not_found / upstream）
    → 供上层判定是否换号（配合 auth_manager.mark_account_failure）

上游响应外壳实测（2026-09-21）：
  - `/users/subscription` → `{code:0, message:"success", data:{plan:...}}`
  - `/users/wallet`       → `{code:0, ..., data:{balance, daily_token_balance, ...}}`
  - `/users/models/available` → `{code:0, ..., data:[...]}`（data 是数组）
  - `/users/tasks` (POST) → `{code:0, ..., data:{id, model, image, ...}}`
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from providers.monkeycode.constants import (
    BASE,
    BUSY_CODES,
    EP_CHECKIN,
    EP_IMAGES,
    EP_MODELS_AVAILABLE,
    EP_SUBSCRIPTION,
    EP_TASKS,
    EP_WALLET,
    QUOTA_CODES,
    USER_AGENT,
)

# ── 错误类别（与 auth_manager 的判定语义对齐）──────────────────────────────
ERR_AUTH = "auth"            # Cookie 失效 → 换号 + 标记需重登
ERR_QUOTA = "quota"          # 额度耗尽 → 长冷却
ERR_RATE_LIMIT = "rate_limit"  # 429 → 短冷却
ERR_BUSY = "busy"            # 已有任务在跑 → 短冷却切号（瞬态）
ERR_NOT_FOUND = "not_found"  # 端点/模型不存在
ERR_UPSTREAM = "upstream"    # 其他 5xx / 未知


class MonkeyCodeError(RuntimeError):
    """上游错误，带 kind 供 failover 判定。"""

    def __init__(self, kind: str, message: str, code: int = 0, status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.status = status

    @property
    def retryable(self) -> bool:
        """是否值得换号重试（额度/忙/限流/上游故障类）。"""
        return self.kind in (ERR_QUOTA, ERR_BUSY, ERR_RATE_LIMIT, ERR_UPSTREAM)


def classify_biz_code(code: int) -> str:
    """上游业务码 → 错误类别。"""
    if code in BUSY_CODES:
        return ERR_BUSY
    if code in QUOTA_CODES:
        return ERR_QUOTA
    return ERR_UPSTREAM


def cookie_of(account: dict | None) -> str:
    """从账号记录取 Cookie 串（存于 access_token 字段）。"""
    if not account:
        return ""
    return str(account.get("access_token") or "").strip()


class MonkeyCodeClient:
    """单个（或空）Cookie 会话的上游客户端。

    `cookie` 为空时用于公开端点（如验证码 challenge/redeem）。
    """

    def __init__(self, cookie: str = "", timeout: float = 30.0):
        self.cookie = (cookie or "").strip()
        self.timeout = timeout

    # ── 头构造 ─────────────────────────────────────────────────────────────
    def headers(self, extra: dict | None = None) -> dict:
        head = {
            "User-Agent": USER_AGENT,
            "Referer": BASE + "/",
            "Origin": BASE,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self.cookie:
            head["Cookie"] = self.cookie
        if extra:
            head.update(extra)
        return head

    # ── 核心请求 ───────────────────────────────────────────────────────────
    async def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """发一次请求并解析外壳；失败抛 MonkeyCodeError。"""
        head = self.headers()
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            head["Content-Type"] = "application/json"

        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                resp = await client.request(
                    method, BASE + path, headers=head, content=payload
                )
        except httpx.HTTPError as exc:
            raise MonkeyCodeError(ERR_UPSTREAM, f"网络错误: {exc}") from exc

        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> Any:
        status = resp.status_code
        text = ""
        try:
            text = resp.text or ""
        except Exception:  # noqa: BLE001
            text = ""

        if status in (401, 403):
            raise MonkeyCodeError(ERR_AUTH, f"凭据失效 HTTP {status}", status=status)
        if status == 429:
            raise MonkeyCodeError(ERR_RATE_LIMIT, "上游限流 429", status=status)
        if status == 404:
            raise MonkeyCodeError(ERR_NOT_FOUND, f"端点不存在 {resp.url.path}", status=status)
        if status >= 500:
            raise MonkeyCodeError(
                ERR_UPSTREAM, f"上游 {status}: {text[:200]}", status=status
            )
        if status < 200 or status >= 300:
            raise MonkeyCodeError(
                ERR_UPSTREAM, f"上游异常 {status}: {text[:200]}", status=status
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise MonkeyCodeError(
                ERR_UPSTREAM, f"响应非 JSON: {text[:200]}", status=status
            ) from exc

        # 有 `code` 字段才是业务外壳；否则是裸对象/裸数组（pass-through）
        if isinstance(body, dict) and "code" in body:
            try:
                code = int(body.get("code") or 0)
            except (TypeError, ValueError):
                code = -1
            if code != 0:
                msg = str(body.get("message") or body.get("msg") or "").strip()
                kind = classify_biz_code(code)
                raise MonkeyCodeError(
                    kind, f"上游 code={code}" + (f" msg={msg}" if msg else ""),
                    code=code, status=status,
                )
            return body.get("data")
        return body

    # ── 便捷方法 ───────────────────────────────────────────────────────────
    async def get_json(self, path: str, *, timeout: float | None = None) -> Any:
        return await self.request("GET", path, None, timeout=timeout)

    async def post_json(self, path: str, body: Any = None, *,
                        timeout: float | None = None) -> Any:
        return await self.request("POST", path, body, timeout=timeout)

    async def get_subscription(self) -> dict:
        """会员档位（/users/me 已 404，改用此端点做身份校验）。"""
        data = await self.get_json(EP_SUBSCRIPTION)
        return data if isinstance(data, dict) else {}

    async def get_wallet(self) -> dict:
        """钱包 / 每日额度。"""
        data = await self.get_json(EP_WALLET)
        return data if isinstance(data, dict) else {}

    async def get_models_available(self) -> list:
        """平台可用模型目录（动态）。"""
        data = await self.get_json(EP_MODELS_AVAILABLE)
        return data if isinstance(data, list) else []

    async def get_images(self) -> list:
        """可用 devbox 镜像列表。"""
        data = await self.get_json(EP_IMAGES)
        if isinstance(data, dict) and isinstance(data.get("images"), list):
            return data["images"]
        return data if isinstance(data, list) else []

    async def get_checkin_status(self) -> dict:
        """签到状态（GET）。"""
        data = await self.get_json(EP_CHECKIN)
        return data if isinstance(data, dict) else {}

    async def post_checkin(self, captcha_token: str) -> dict:
        """执行签到（需 captcha_token）。"""
        data = await self.post_json(EP_CHECKIN, {"captcha_token": captcha_token})
        return data if isinstance(data, dict) else {}

    async def create_task(self, payload: dict) -> dict:
        """创建 Agent 任务，返回 data（含 id）。"""
        data = await self.post_json(EP_TASKS, payload)
        return data if isinstance(data, dict) else {}


def client_for(account: dict | None, timeout: float = 30.0) -> MonkeyCodeClient:
    """按账号构造客户端（无账号则用于公开端点）。"""
    return MonkeyCodeClient(cookie_of(account), timeout=timeout)
