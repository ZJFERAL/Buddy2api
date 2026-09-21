"""QwenWork channel health probe (read-only).

Reproduces the layered diagnosis used during the 2026-09-21 upstream migration:
which layer is failing — credential realm, catalog list, or the chat transport.

    python scripts/qwenwork_probe.py

Nothing is written: no account mutation, no token refresh, one tiny chat probe
per model key. Never prints token material.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import database as db
from providers.qwenwork import cosy
from providers.qwenwork.chat import (
    build_body,
    envelope_error,
    envelope_status,
    static_headers,
)
from providers.qwenwork.constants import (
    ACCOUNT_CONTEXT_PATH,
    CHANNEL_ID,
    GATEWAY,
    MODELS_PATH,
    OPENAPI_BASE,
    REALM_GATEWAY,
)

PROBE_MODELS = ("pro", "flash", "qwork-advanced", "qwork-lite")
CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
CHAT_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common"


def _identity(account: dict) -> dict:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    return {
        "token": str(account.get("access_token") or ""),
        "uid": str(account.get("uid") or extra.get("uid") or ""),
        "name": str(account.get("nickname") or account.get("name") or extra.get("name") or ""),
        "email": str(extra.get("email") or ""),
        "machine_id": str(extra.get("login_device_id") or ""),
    }


async def probe_models(ident: dict) -> None:
    print("A. catalog list  GET " + MODELS_PATH + "  (COSY)")
    url = f"{GATEWAY}{MODELS_PATH}"
    rid = uuid.uuid4().hex
    headers = static_headers("pro", rid, ident["machine_id"])
    headers["Accept"] = "application/json"
    headers.update(
        cosy.auth_headers(
            uid=ident["uid"], name=ident["name"], email=ident["email"],
            access_token=ident["token"], url=url, body="", timestamp=int(time.time()),
            request_id=rid,
        )
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
    print(f"   HTTP {response.status_code}")
    if response.status_code >= 400:
        print(f"   {response.text[:200]}")
        return
    try:
        payload = response.json()
    except ValueError:
        print("   non-JSON body")
        return
    rows = payload.get("qwork") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        keys = [str(row.get("key")) for row in rows if isinstance(row, dict) and row.get("enable") is not False]
        print(f"   enabled keys: {keys}")


async def probe_account_context(ident: dict, base: str) -> None:
    url = f"{base}{ACCOUNT_CONTEXT_PATH}?include=user,quota"
    headers = {
        "Accept": "application/json",
        "User-Agent": "qoderwork/1.1.0",
        "X-Request-Id": str(uuid.uuid4()),
        "Authorization": f"Bearer {ident['token']}",
    }
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        print(f"   {base}  EXC {type(exc).__name__}")
        return
    note = ""
    if response.status_code < 400:
        try:
            data = response.json()
            user = (data.get("data") or {}).get("user") or {}
            note = f"user={user.get('name')}"
        except ValueError:
            note = "non-JSON"
    else:
        note = response.text[:120].replace("\n", " ")
    print(f"   {base}  HTTP {response.status_code}  {note}")


async def probe_chat(ident: dict, model: str) -> None:
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "stream": False, "max_tokens": 16}
    body, raw, _ = build_body(payload)
    body["model_config"]["key"] = model
    body["chat_context"]["extra"]["modelConfig"]["key"] = model
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    url = f"{GATEWAY}{CHAT_PATH}?{CHAT_QUERY}"
    headers = static_headers(model, str(body["request_id"]), ident["machine_id"])
    headers.update(
        cosy.auth_headers(
            uid=ident["uid"], name=ident["name"], email=ident["email"],
            access_token=ident["token"], url=url, body=raw, timestamp=int(time.time()),
            request_id=uuid.uuid4().hex,
        )
    )
    verdict = "?"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            async with client.stream("POST", url, headers=headers, content=raw) as response:
                if response.status_code >= 400:
                    verdict = f"HTTP {response.status_code} {(await response.aread()).decode('utf-8', 'replace')[:100]}"
                else:
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        err = envelope_error(data)
                        if err:
                            verdict = f"envelope {envelope_status(data)} :: {err}"
                        else:
                            verdict = "OK (content received)"
                        break
    except httpx.HTTPError as exc:
        verdict = f"EXC {type(exc).__name__}: {str(exc)[:80]}"
    print(f"   chat model={model:20} -> {verdict}")


async def probe_new_surface(ident: dict) -> None:
    headers = {
        "Accept": "application/json",
        "User-Agent": "qoderwork/1.1.0",
        "X-Request-Id": str(uuid.uuid4()),
        "Authorization": f"Bearer {ident['token']}",
    }
    for base, path in (
        (OPENAPI_BASE, "/algo/api/v3/service/region/endpoints"),
        (REALM_GATEWAY, "/api/v1/userinfo"),
    ):
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                response = await client.get(base + path, headers=headers)
            body = response.text[:60].replace("\n", " ")
            enc = " (encrypted body)" if response.status_code == 200 and not body.startswith("{") else ""
            print(f"   {base + path}  HTTP {response.status_code}{enc}  {body}")
        except httpx.HTTPError as exc:
            print(f"   {base + path}  EXC {type(exc).__name__}")


async def main() -> int:
    db.init_db()
    accounts = [row for row in db.list_accounts(provider=CHANNEL_ID)]
    if not accounts:
        print("No qwenwork account configured.")
        return 2
    account = accounts[0]
    ident = _identity(account)
    print(f"account: {account.get('name')}  status={account.get('status')}  token_len={len(ident['token'])}")
    print()

    await probe_models(ident)
    print()
    print("B. credential realm  GET account-context  (Bearer)")
    for base in (GATEWAY, REALM_GATEWAY, OPENAPI_BASE):
        await probe_account_context(ident, base)
    print()
    print("C. legacy chat transport  POST agent_chat_generation  (COSY)")
    for model in PROBE_MODELS:
        await probe_chat(ident, model)
        await asyncio.sleep(0.3)
    print()
    print("D. new protocol surface  (Bearer, informational)")
    await probe_new_surface(ident)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
