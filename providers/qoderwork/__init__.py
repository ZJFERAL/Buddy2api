"""Qoder / Qoder CLI provider. OpenAI-compatible bearer auth, no COSY."""

from __future__ import annotations

from typing import Optional

import auth_manager
import database as db
from providers.protocol import ChannelId, QuotaSnapshot
from providers.qoderwork import chat, store
from providers.qoderwork.constants import CHANNEL_ID, DISPLAY_NAME
from providers.qoderwork.token import QoderAuthError, is_token_expired, refresh_account


class QoderWorkProvider:
    id: ChannelId = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = False

    def list_models(self) -> list[dict]:
        from providers.qoderwork.constants import MODEL_DISPLAY, STATIC_MODELS

        items = []
        for slug in STATIC_MODELS:
            display = MODEL_DISPLAY.get(slug, slug)
            items.append({"id": slug, "name": display})
            # Also expose the display name itself as a callable id, so
            # clients that pick ids straight from /v1/models can call
            # "Qwen3.8-Flash" and the alias map resolves it to the slug.
            alias = display.strip().lower()
            if alias and alias != slug:
                items.append({"id": display, "name": f"{display} (别名)"})
        return items

    def alias_map(self) -> dict[str, str]:
        from providers.qoderwork.constants import ALIASES

        return dict(ALIASES)

    def accepts_model(self, inner: str) -> bool:
        value = (inner or "").strip()
        if not value:
            return False
        if value in self.alias_map() or value.lower() in self.alias_map():
            return True
        ids = {str(item.get("id")) for item in self.list_models() if isinstance(item, dict)}
        return value in ids or value.lower() in ids

    def translate_model(self, model: str) -> str:
        return chat.translate_model(model)

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        exclude = exclude_ids or set()
        account = self.pick_account(exclude)
        if account:
            if is_token_expired(account):
                try:
                    return await refresh_account(account)
                except QoderAuthError:
                    pass
            else:
                return account
        expired = [
            row
            for row in db.list_accounts(provider=self.id)
            if row.get("status") == "expired" and row.get("id") not in exclude
        ]
        for row in expired:
            try:
                return await refresh_account(row)
            except QoderAuthError:
                continue
        return None

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        return await chat.chat_completions(payload, api_key_info)

    def parse_credentials(self, body: dict) -> dict:
        return store.parse_credentials(body)

    def discover(self) -> dict:
        return store.discover()

    def import_path(self, path: str) -> dict:
        return store.import_path(path)

    def upsert_account(self, parsed: dict) -> dict:
        return store.upsert_account(parsed)

    async def fetch_quota(self, account: dict) -> QuotaSnapshot:
        return QuotaSnapshot(
            ok=False,
            channel=self.id,
            account_id=int(account.get("id") or 0),
            unit="unknown",
            remaining=None,
            unsupported=True,
            message="quota not exposed by Qoder new endpoint",
        )

    async def test_chat(self, account: dict, model: str = "lite", prompt: str = "ping") -> dict:
        return await chat.test_chat(account, model, prompt)

    async def refresh(self, account: dict) -> dict:
        return await refresh_account(account)


PROVIDER = QoderWorkProvider()
