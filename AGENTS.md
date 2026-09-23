# Buddy2api — Agent Guide

> **Read this before working on the project.** It distills months of hands-on
> experience so you don't repeat mistakes that have already been made.

## 1. What this project is

Buddy2api is a **multi-channel LLM API gateway** written in Python (FastAPI).
It exposes a standard **OpenAI-compatible** API (`/v1/chat/completions`,
`/v1/responses`, `/v1/models`) and routes requests to multiple upstream
"channels" (providers), each backed by client-side credentials imported into a
local SQLite database.

- **Language**: Python 3.12+ (tested on 3.13)
- **Framework**: FastAPI + Uvicorn
- **Frontend**: Single-file Vue 3 SPA (`web/index.html`, served by the backend)
- **Database**: SQLite (`codebuddy_gateway.db`), credentials encrypted via
  `credential_crypto.py` (AES-256-GCM, DPAPI-derived key)
- **Deployment**: Local venv, not Docker (Dockerfile exists but is unused)

## 2. Channel / Provider architecture

Each channel is an independent package under `providers/<id>/` implementing the
`Provider` protocol defined in `providers/protocol.py`.

**Current channels (6)**:

| Channel | Upstream | Auth model | Key files |
|---|---|---|---|
| `workbuddy` | WorkBuddy backend | `.info` token files, server-side refresh | `providers/workbuddy/` |
| `qclaw` | QClaw gateway | JWT via `X-New-Token` rolling | `providers/qclaw/` |
| `qwenwork` | QwenWork / Qoder CN | COSY signature → OAuth Bearer (v1.1.0+) | `providers/qwenwork/` |
| `traework` | TraeWork | ECDSA device proof + refresh token | `providers/traework/` |
| `qoderwork` | Qoder CLI | `~/.qoder/.auth` decrypt → Bearer; server-side refresh | `providers/qoderwork/` |
| `zcode` | ZCode / Z.AI (GLM) | JWT (captcha) or API Key (no captcha) | `providers/zcode/` |

**Planned / in-progress**:
- `monkeycode` — MonkeyCode (长亭百智云), Cookie auth + WebSocket task streaming

### Provider Protocol contract

```python
# providers/protocol.py
class Provider(Protocol):
    id: ChannelId
    display_name: str
    checkin_supported: bool

    def list_models(self) -> list[dict]: ...
    def alias_map(self) -> dict[str, str]: ...
    def accepts_model(self, model: str) -> bool: ...
    def translate_model(self, model: str) -> str: ...
    def pick_account(self, ...) -> dict | None: ...
    def pick_account_with_fallback(self, ...) -> dict | None: ...
    def has_usable_account(self, model: str | None) -> bool: ...

    # Returns ("error", (status, detail)) | ("json", obj) | ("stream", async_gen)
    async def chat_completions(self, payload, api_key_info): ...

    # Optional: parse_credentials / discover / import_path / upsert_account
    # Optional: fetch_quota / test_chat / refresh / fetch_checkin / claim_checkin
```

### `chat_completions` return contract

Every provider's `chat_completions` must return one of:
- `("error", (status_code: int, detail: dict | str))` — error response
- `("json", response_dict)` — non-streaming success
- `("stream", async_generator_of_sse_bytes)` — streaming success

### Registering a new channel

1. Create `providers/<id>/` with at least `__init__.py`, `constants.py`,
   `chat.py`, `store.py`, `token.py`
2. In `providers/protocol.py`: add the id to `ChannelId` and
   `KNOWN_CHANNEL_IDS`
3. In `providers/__init__.py`: import the provider, add to
   `DEFAULT_PROVIDER_IDS` and `_LOADED`
4. Update test assertions in `tests/test_provider_schema.py` (channel count)
5. See `docs/maintenance/channel-integration-guide.md` for the full checklist

## 3. Key source files

| File | Role |
|---|---|
| `server.py` | FastAPI app, all HTTP routes, admin endpoints |
| `proxy.py` / `router.py` | Request routing, channel binding, model dispatch |
| `auth_manager.py` | Account selection (sticky), failure marking, failover, checkin |
| `database.py` | SQLite layer, accounts/api_keys/logs/settings tables |
| `credential_crypto.py` | AES-256-GCM encryption for stored credentials |
| `control_plane.py` | Admin operations: discover/import/checkin |
| `catalog.py` / `aliases.py` | Model catalog snapshot and alias resolution |
| `fingerprint.py` | Per-account device fingerprinting |
| `model_capacity.py` / `model_reasoning.py` | Model metadata |
| `web/index.html` | Vue 3 SPA (management UI) |
| `preflight-buddy.py` | Pre-startup DB health check + dependency sync |

## 4. Development environment

### Setting up

```bash
# venv is at .venv/ (Windows)
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

### Running tests

```bash
# Standard pytest tests (most files)
.venv/Scripts/python.exe -m pytest tests/ -q \
  --ignore=tests/test_zcode_e2e.py \
  --ignore=tests/test_zcode_captcha_quality.py

# Script-style tests (have their own runner, do NOT use pytest)
.venv/Scripts/python.exe tests/test_zcode_e2e.py
.venv/Scripts/python.exe tests/test_zcode_captcha_quality.py
```

> **Critical**: The venv may not have `pytest` installed. Always install
> `requirements-dev.txt` first. Managed Python (`.workbuddy`) also lacks pytest.

### Running the server

```bash
.venv/Scripts/python.exe server.py --port 8787 --host 127.0.0.1
```

For production-style startup, use `start-oneclick.bat` (reads port/token from
itself, runs preflight, starts silently).

## 5. Critical conventions & pitfalls

### Testing

- **Two test styles**: Most tests are pytest-style. But `test_zcode_e2e.py`
  and `test_zcode_captcha_quality.py` are **script-style** (run them directly
  with `python tests/xxx.py`, NOT pytest — pytest will fail with
  `async def functions are not natively supported`).
- **pytest `--basetemp`**: If pytest appears to hang or crash at the end, set
  `--basetemp=<workspace-dir>` to avoid sandbox delete-bulk confirmation
  triggers on temp cleanup.
- **`CODEBUDDY_SAFE_DELETE_ENABLED=0`**: Set this env var to prevent sandbox
  safe-delete from interfering with pytest temp file cleanup.

### Environment (Windows / Git Bash)

- **Use `.venv/Scripts/python.exe`** for all Python operations. Do not rely on
  system Python or managed Python.
- **Git Bash lacks** `head`, `tail`, `ls`, `dirname` — use Python or the
  dedicated tools (Read, Glob, Grep) instead.
- **Windows .bat files** must be **GBK + CRLF**. When editing via Python, use
  `io.open(encoding='gbk')` and `newline='\r\n'`. Mixed line endings cause
  cmd.exe to misparse commands.
- **`cmd.exe` / `wscript.exe`** may be blocked by sandbox security policy.
  Use Python `subprocess` or Bash `run_in_background=true` as alternatives.
- **Background processes started by Bash are killed when the session ends.**
  For long-running services, use `start-oneclick.bat` or
  `scripts/buddy_restart.py --foreground` with `run_in_background=true`.

### Upstream error handling

- **HTTP 200 does not mean success.** Many upstream channels embed errors
  inside HTTP 200 responses (SSE envelope `statusCodeValue`, `body.code`, or
  `event:error` frames). Always parse the response body, not just the status
  code.
- **`RETRYABLE_STATUS`** = `{408, 409, 425, 429, 500, 502, 503, 504}`.
  Status codes outside this set do not trigger automatic account failover.
- **Quota/model errors (400/402)**: `auth_manager.classify_failover()`
  categorizes these. Quota errors → `status=expired` + `quota_blocked_until`.
  Model errors → block specific `(provider, account, model)` for 30 min.

### Frontend (`web/index.html`)

- **`server.py::_render_index_html()` reads `web/index.html` from disk on
  every request** with `Cache-Control: no-store`. Changes take effect on F5
  refresh — no server restart needed.
- **Vue `return{...}` trap**: When adding new refs, functions, or `@click`
  handlers to a Vue component, you MUST add them to the component's
  `setup() return{...}`. Missing exports cause the entire component to
  crash (white screen). Run `scripts/web_binding_check.py` to verify.

### Credential security

- Credentials are encrypted with AES-256-GCM via DPAPI-derived key.
- The key file is `codebuddy_gateway.db.credentials.key`.
- **When testing with a DB copy**, set `CB_GATEWAY_CREDENTIAL_KEY_FILE` to
  point to the real key file, or decryption will fail with `InvalidToken`.
- `.env` and credential files are gitignored and protected.

## 6. Service startup & restart

```bash
# Check status
python scripts/buddy_restart.py --status

# Restart (reads port/token from start-oneclick.bat)
python scripts/buddy_restart.py --foreground  # use with run_in_background=true
```

- Startup takes **~25 seconds** to reach health 200 (preflight + scanning).
  Don't use short timeouts.
- **Never hand-type the admin token** — read it from `start-oneclick.bat` to
  avoid mismatch with the browser's logged-in session.
- Code changes require **process restart** to take effect. Restart also
  clears in-process state (`_sticky_account_id`, `_account_failures`).

## 7. Channel-specific quick reference

### zcode
- **Captcha dependency**: JWT channel requires Alibaba Cloud captcha solving
  (Node.js + `providers/zcode/captcha_node/solver.js`, a heavy-duty happy-dom
  solver ported from Zcode2Api3, AS_IS license). Dependencies:
  `happy-dom@^20.14.0` + `undici@^8.10.2` (install via
  `providers/zcode/captcha_node/install_deps.sh`). Quality gate:
  `is_valid_verify_param()` checks `base64(JSON)` + `securityToken ≥ 50`.
  **jsdom solver is rejected** — Aliyun risk engine returns F001
  (`VerifyResult:false`) for its headless fingerprint; the happy-dom build
  passes (T001). First mint warms a CDN disk cache + zcode cookies; occasional
  exit-2 stalls are normal and recovered by the existing retry loop.
- **Diagnostics**: `GET /admin/providers/zcode/diagnostics`
- **Probe**: `python scripts/zcode_claim_probe.py`
- **License**: Protocol translated from MIT-licensed `zcode-api` (TypeScript).
  Do NOT copy AGPL `zcode2api` source code into this repo.

### qoderwork
- **Multi-account**: Uses `~/.qoder/.auth` snapshots. Set
  `CB_QODER_AUTH_DIRS` env var for multiple auth directories.
- **Server-side refresh**: `POST openapi.qoder.sh/api/v1/deviceToken/refresh`
  (refresh_token rotates — must write new value back to DB).
- **Snapshot tool**: `python scripts/snapshot_qoder_auth.py <name>`
- **Campaign watch**: `python scripts/qoder_campaign_watch.py`

### qwenwork
- **Protocol changed in v1.1.0**: Old COSY chat endpoint deprecated.
  New version uses `@qwen-work/gateway-sdk` long connection + JWT.
  Current HTTP+SSE path may return 503 "Model catalog unavailable".
- **Only `flash` model reliably works** for the tested account.
- **Probe**: `python scripts/qwenwork_probe.py`

### monkeycode (planned)
- **Cookie auth**: Session cookie `monkeycode_ai_session` (not `nebula_session`).
- **WebSocket streaming**: `wss://monkeycode-ai.com/api/v1/users/tasks/stream`
  with base64-encoded `data` field. Only `agent_message_chunk` frames are
  output text; `agent_thought_chunk` is reasoning chain (discard).
- **Single task per account**: 10811 busy → short cooldown + retry.
- **Captcha**: Cap.js PoW (FNV-1a + xorshift32 + SHA-256).
- See `docs/maintenance/upstream-protocol-notes.md` for full WS event syntax.

## 8. Where to learn more

| Topic | Document |
|---|---|
| Detailed dev conventions & pitfalls | `docs/maintenance/dev-conventions.md` |
| How to add a new channel | `docs/maintenance/channel-integration-guide.md` |
| Upstream protocol facts per channel | `docs/maintenance/upstream-protocol-notes.md` |
| Multi-channel v2 design | `docs/design/multi-channel-v2.md` |
| Release workflow | `docs/maintenance/release_workflow_zh.md` |
