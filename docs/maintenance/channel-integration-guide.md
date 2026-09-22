# Channel Integration Guide

> Step-by-step guide for adding a new upstream provider channel to Buddy2api.
> Distilled from the integration experiences of qoderwork, zcode, and monkeycode.

## Overview

Each channel is a self-contained package under `providers/<id>/` that
implements the `Provider` protocol from `providers/protocol.py`. The gateway
handles routing, auth, accounts, and the management UI — the provider only
needs to implement upstream-specific logic.

## 1. Assess reusability before writing code

Buddy2api already provides:

| Capability | Location | What you don't need to rewrite |
|---|---|---|
| Account storage | `database.py` accounts table | CRUD, encryption, dedup |
| Account selection (sticky) | `auth_manager.pick_account` | Priority/weight routing |
| Failure marking | `auth_manager.mark_account_failure` | Cooldown, expiry, model block |
| Credential encryption | `credential_crypto.py` | AES-256-GCM automatic |
| Provider interface | `providers/protocol.py` | Contract definition |
| Channel registration | `providers/__init__.py` | Import + `_LOADED` dict |
| Web management UI | `web/index.html` + `server.py` | Account list, key management |
| Checkin framework | `auth_manager` + `control_plane.py` | Daily claim flow |

Only write code for what's truly upstream-specific:
- Upstream HTTP/WS client
- Auth/token refresh logic
- Request/response format conversion (if upstream isn't OpenAI-compatible)
- Model catalog (static or dynamic)
- Credential parsing/import
- Captcha solving (if needed)

## 2. Create the provider package

```
providers/<id>/
├── __init__.py        # Provider class implementing the protocol
├── constants.py       # Endpoints, model tables, error codes, env vars
├── chat.py            # chat_completions implementation
├── store.py           # Credential parsing, discover, upsert
├── token.py           # Token validation, refresh logic
└── ...                # Additional modules as needed
```

### Minimal `__init__.py` template

```python
from __future__ import annotations
from providers.protocol import ChannelId, Provider

class MyProvider:
    id: ChannelId = "mychannel"
    display_name = "My Channel"
    checkin_supported = False

    def list_models(self) -> list[dict]:
        from .constants import STATIC_MODELS
        return [{"id": m} for m in STATIC_MODELS]

    def alias_map(self) -> dict[str, str]:
        return {}

    def accepts_model(self, model: str) -> bool:
        return any(m["id"] == model for m in self.list_models())

    def translate_model(self, model: str) -> str:
        return self.alias_map().get(model, model)

    def pick_account(self, *, model=None, **kw):
        import auth_manager
        return auth_manager.pick_account(provider=self.id, model=model, **kw)

    def pick_account_with_fallback(self, *, model=None, **kw):
        import auth_manager
        return auth_manager.pick_account_with_fallback(provider=self.id, model=model, **kw)

    def has_usable_account(self, model: str | None = None) -> bool:
        return self.pick_account_with_fallback(model=model) is not None

    async def chat_completions(self, payload, api_key_info):
        # Implement: pick account → call upstream → return ("error"|"json"|"stream", ...)
        ...

PROVIDER = MyProvider()
```

## 3. Register the channel

### `providers/protocol.py`

```python
ChannelId = Literal[..., "mychannel"]
KNOWN_CHANNEL_IDS: tuple[ChannelId, ...] = (..., "mychannel")
```

### `providers/__init__.py`

```python
from providers.mychannel import PROVIDER as MYCHANNEL_PROVIDER

DEFAULT_PROVIDER_IDS: tuple[str, ...] = (..., "mychannel")
_LOADED: dict[str, Provider] = {
    ...,
    "mychannel": MYCHANNEL_PROVIDER,
}
```

## 4. Update test assertions

Several tests hardcode the channel count. Update them:

| Test file | What to update |
|---|---|
| `tests/test_provider_schema.py` | Channel count and id list |
| `tests/test_qclaw.py` | Channel count (if it checks registry) |
| `tests/test_qwenwork.py` | Channel count |
| `tests/test_traework.py` | Channel count |

## 5. Connect to the management UI

### Frontend (`web/index.html`)

1. **Add fallback channel entry** in `FALLBACK_CH` (search for the constant
   in both `keys` and `mdls` components). This ensures the channel appears
   even if the `/admin/channels` API fails.

2. **Add account import form** in the "Advanced manual add" section. Follow
   the pattern of existing channels (e.g., qoderwork or zcode).

3. **If adding OAuth flow**: Add admin endpoints in `server.py` and wire up
   the frontend polling logic (see zcode OAuth for reference).

### Server endpoints (if needed)

Add admin endpoints in `server.py` for channel-specific operations:

```python
@app.post("/admin/mychannel/import")
async def admin_mychannel_import(body: dict):
    ...
```

Use `providers.get_provider("mychannel")` to dispatch.

### `control_plane.py`

If the channel needs discover/import path support, ensure:
- `control_plane.discover(channel, auth_dir)` passes `auth_dir` to the
  provider's `discover()` (uses `inspect` to detect parameter support).
- `control_plane.import_channel()` dispatches to the provider's
  `import_path()` or `upsert_account()`.

## 6. Verify the frontend

**Run `scripts/web_binding_check.py`** to statically verify that all template
references have corresponding `return{...}` exports.

```bash
.venv/Scripts/python.exe scripts/web_binding_check.py
```

This catches the most common frontend bug: adding a `@click` handler or
ref to the template but forgetting to export it from `setup()`.

## 7. Write tests

### pytest-style

```python
# tests/test_mychannel.py
def test_provider_registered():
    import providers
    assert "mychannel" in providers._LOADED

def test_parse_credentials():
    from providers.mychannel.store import parse_credentials
    result = parse_credentials({"token": "test-123"})
    assert result["uid"] == "test-123"
```

### Mock upstream

Create `tests/mock_mychannel_upstream.py` that simulates the upstream API
for end-to-end testing without real credentials.

### Script-style (for async e2e)

If tests need `asyncio` and can't use pytest-asyncio, make them script-style:

```python
# tests/test_mychannel_e2e.py
if __name__ == "__main__":
    # Run async test steps directly
    ...
```

Run with `.venv/Scripts/python.exe tests/test_mychannel_e2e.py` (NOT pytest).

## 8. Common pitfalls

### Mock anti-pattern

**Never write a mock that mirrors your (possibly wrong) implementation.**
Mocks must reflect the **real upstream contract**, verified by:
1. Reading the upstream's official client code (not just API docs)
2. Dumping real responses with a probe script
3. Cross-checking against reference implementations

If your mock mirrors a wrong implementation, tests will be "all green" but
the real upstream will reject every request. This happened with zcode OAuth:
the mock's poll response was written to match the (incorrect) implementation,
so 17 assertions passed while the real flow was completely broken.

### Upstream contract verification

Before coding, verify:
- Real request/response format (use a probe script to dump raw responses)
- Correct field names and paths (e.g., `data.token` vs `data.access_token`)
- Status codes and error codes
- Header requirements

### HTTP 200 ≠ success

Always check the response body for business-level errors. Many upstream
channels embed errors in HTTP 200 responses.

### Token refresh must persist

`refresh_account()` must call `db.update_account()` to write the new token
to the database. If it only updates the in-memory dict, the token is lost
on restart.

### `RETRYABLE_STATUS` and failover

Default `RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}`.
If upstream uses 400/402 for quota errors (like qoderwork), add those to
`RETRYABLE_STATUS` in the provider's `constants.py`, and use
`auth_manager.classify_failover()` to categorize them.

### License compliance

When porting from reference implementations:
- MIT-licensed projects: translate freely (algorithm ≠ copyright)
- AGPL-licensed projects: do NOT copy source code into this MIT repo;
  translate from a permissively-licensed reference instead, or run the
  AGPL code as an external runtime dependency

## 9. Channel-specific lessons learned

### From qoderwork integration
- Multi-account support requires per-account `auth_dir` (not a global constant)
- `store.session_to_account()` must write the actual import path to
  `extra.auth_dir`, not a global default — otherwise refresh reads the wrong
  account's token (serial number confusion)
- `is_token_expired` must read the correct field (`expires_at`, not
  `expire_time`) — a field name mismatch caused all accounts to appear
  permanently expired (1970 timestamp after `/1000` division)
- COSY signature protocol may not be needed if upstream switched to plain
  Bearer auth (qoderwork v2 simplified from COSY to Bearer)

### From zcode integration
- OAuth flow must override `redirect_uri` to an interstitial page, or the
  upstream server never records the authorization → poll stays pending forever
- Captcha quality gate is essential: 84-char failover params (without
  `securityToken`) will be rejected by upstream with code 3007
- Alibaba Cloud captcha SDK URL must not include version numbers
  (`/captcha-web/2.1.9/...` returns 404)
- `/billing/current` and `/usage` endpoints trigger risk control (HTTP 405 +
  code 3012) — don't call them; use `/billing/balance` instead

### From monkeycode integration
- Cookie name is `monkeycode_ai_session`, not `nebula_session` (Go version
  had the wrong constant)
- WebSocket `data` field is base64-encoded JSON — the Go implementation
  didn't decode it and could never extract text
- Must filter by `sessionUpdate == "agent_message_chunk"` —
  `agent_thought_chunk` is reasoning chain, not output
- End flag is `task-ended`, not `done/finish/completed/error/abort`
- Single account = single concurrent task (10811 busy); use optimistic
  creation with retry instead of pessimistic slot waiting
- `/users/me` endpoint is 404; use `/users/subscription` + `/users/wallet`
