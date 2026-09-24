# Upstream Protocol Notes

> Verified facts about each upstream channel's API, gathered from real
> debugging sessions. These are not guesses — each item was confirmed by
> hitting the real upstream or reading verified client code.

## Table of Contents

1. [workbuddy](#1-workbuddy)
2. [qclaw](#2-qclaw)
3. [qwenwork](#3-qwenwork)
4. [traework](#4-traework)
5. [qoderwork](#5-qoderwork)
6. [zcode](#6-zcode)
7. [monkeycode (planned)](#7-monkeycode-planned)

---

## 1. workbuddy

**Auth model**: Server-side token refresh via `POST /v2/plugin/auth/token/refresh`.
Tokens stored as `.info` files in `CB_AUTH_DIR` directory.

**Key facts**:
- Multi-account: one `.info` file per account in the auth directory.
- `auth_manager.pick_account` is **sticky** — same provider sticks to the
  current account until failure/cooldown.
- Token expiry check: 60 seconds early (vs 5 minutes for other channels).
- No COSY, no captcha — straightforward Bearer auth.
- Checkin: `GET /v2/billing/meter/checkin-activity-status` (workbuddy backend).

---

## 2. qclaw

**Auth model**: JWT with rolling token via response header `X-New-Token`.
`qprx.apply_new_token` writes the new token back to the account.

**Key facts**:
- Single official auth directory.
- Additional accounts via "paste JSON" import.
- JWT refresh is transparent — the server sends a new token in the response
  header, and the client writes it back.

---

## 3. qwenwork

**Auth model**: COSY signature (RSA + AES + MD5 →
`Bearer COSY.{payload}.{sig}`) on the legacy gateway, unchanged.

**Status (2026-09-24, v2.1.15)**: the legacy HTTP+SSE chat path is **live**.
The 2026-09-21 `503 Model catalog unavailable` outage was a body-field bug, not
a dead transport: the 1.0.4 gateway resolves the model catalog from
`business.product` / `business.type` in the request body. Sending only the
`Cosy-Business-*` headers left login, quota and model list working while chat
still 503'd. v2.1.15 made the body and the headers share one source
(`qoder_work` / `agent`), and moved chat infer to the official `Encode=1`
WASM packing (`providers/qwenwork/encode.py`, needs `wasmtime`).

| Aspect | Value |
|---|---|
| Auth | `Authorization: Bearer COSY.{payload}.{sig}` |
| Chat endpoint | `/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=..&AgentId=..` on `GATEWAY`, plus `&Encode=1` after WASM packing |
| Request body | WASM-encoded by `prepareInferRequest` (not plain JSON) |
| Response | Plain SSE |
| Catalog | `/api/v2/model/list` (COSY) |

`OPENAPI_BASE` / `REALM_GATEWAY` / `MODEL_SERVER_PATH` are CN hosts recorded
from the client's endpoint cache for a future protocol iteration. They are
diagnostic-only (used by `scripts/qwenwork_probe.py`); the live chat path stays
on `GATEWAY`, and the `gateway.qwenwork.cn` token is scoped to that realm.

**Error pattern**: HTTP 200 with embedded error in SSE envelope
(`statusCodeValue` or `body.code`). The `envelope_status()` function in
`providers/qwenwork/chat.py` extracts the real status code, so retryable
upstream states (e.g. 503) keep their `RETRYABLE_STATUS` handling instead of
being flattened to 400.

**Identity constants** (current, frozen):
- `IDE_VERSION = 1.0.4`
- `RELEASE_VERSION = 1.0.4-26090412`
- `COSY_VERSION = 1.1.32` (qoderclicn 1.0.4 `mm`)
- `USER_AGENT = qoderwork/1.0.4`
- `MACHINE_TYPE = 5`, `DATA_POLICY = disagree`

**Probe**: `python scripts/qwenwork_probe.py` (read-only, 4 layers: catalog,
  credential domain, old chat transport, new protocol surface).

---

## 4. traework

**Auth model**: `ExchangeToken` — refresh_token + locally stored ECDSA device
private key signs a `DeviceProof`.

**Key facts**:
- Has `fetch_checkin` / `claim_checkin` in `providers/traework/quota.py`.
- Quota unit: "credit".
- `control_plane` dispatches checkin to provider-specific methods when
  available, falls back to `auth_manager` for workbuddy.

---

## 5. qoderwork

### Auth & endpoints

**Credential source**: `~/.qoder/.auth/{user, machine_id}` (AES-128-CBC,
key = `machine_id[:16]`, IV = key, PKCS#7 padding).

**Two routing paths**:

| Path | Endpoint | Auth | Models |
|---|---|---|---|
| OpenAI compatible | `https://api2-v2.qoder.sh/model/v1/chat/completions` | `Bearer {security_oauth_token}` | 4 built-in: `lite`, `auto`, `performance`, `ultimate` |
| Native COSY | `https://api2.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation` | `Bearer COSY.{payloadB64}.{md5sig}` | 13 advanced: `qfmodel`, `qmodel_38max`, `gfmodel`, `dfmodel`, `kmodel`, etc. |

**COSY signature**: `md5("{payloadB64}\n{cosyKey}\n{unixSeconds}\n{encodedBody}\n{signedPath}")`
where `signedPath` = path with `/algo` prefix stripped.

**Body encoding** (COSY): Custom Base64 alphabet + `$` padding + outer-third
swap (`[last 3 chars][middle][first 3 chars]`).

**System prompt requirement**: Native requests must put the system prompt in
**both** the top-level `system` field and `messages[0]` (role=system).
Missing either causes the upstream to silently drop it.

### Server-side token refresh

```
POST https://openapi.qoder.sh/api/v1/deviceToken/refresh
Body: {"refresh_token": "..."}
Headers: User-Agent: qoder/1.1.16
```

Returns: `device_token`, `refresh_token` (rotated!), `expires_at` (RFC3339),
`refresh_token_expires_at` (~360 days).

**Critical**: `refresh_token` rotates on every call. The new value MUST be
written back to `accounts.refresh_token` via `db.update_account()`, or the
next refresh will fail (using the old, now-invalid refresh_token).

**COSY fields are decoupled from access token**: After refresh, old
`cosy_key` / `encrypt_user_info` / `machine_id` still work with the new
token for native COSY endpoints.

### Multi-account

- `CB_QODER_AUTH_DIRS` env var (semicolon-separated) for multiple auth dirs.
- `start-oneclick.bat` auto-discovers snapshots in
  `%USERPROFILE%\.qoder\snapshots\*\.auth`.
- `store.session_to_account(sess, source, auth_dir=None)` — `auth_dir` must
  be the actual import path, not a global default.
- `scripts/snapshot_qoder_auth.py <name>` — captures current CLI login to
  a snapshot directory.
- `scripts/qoder_campaign_watch.py` — checks and claims daily 100-credit
  campaigns (requires `Cosy-ClientType: 10` + UMID machine headers).

### Model directory

17 models in `chat` group (from `~/.qoder/.models/<uid>/catalog-v6`):
- `qfmodel` = Qwen3.8-Flash
- `qmodel_38max` = Qwen3.8-Max
- `gfmodel` = GLM-5.3-Flash
- `dfmodel` = DeepSeek-Flash
- `kmodel` = Kimi
- `lite`, `auto`, `performance`, `ultimate` = built-in (OpenAI endpoint)
- `MODEL_DISPLAY` dict in `constants.py` maps slug → real name

### Known issues

- `Cannot find model` (400) from native COSY endpoint is **upstream-side,
  account-level, temporary** — not a local bug. It self-heals within ~1 hour.
- `Billing daily count exceeded` (400) = daily quota exhausted for that
  account. Triggers `status=expired` + `quota_blocked_until`.
- Native COSY endpoint does **not** validate model key — any string works
  at the protocol level. Model resolution happens upstream.

---

## 6. zcode

### Two auth paths

| Path | Endpoint | Auth | Captcha | Models |
|---|---|---|---|---|
| JWT Plan | `https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages` | Identity headers + captcha header | Yes (Alibaba Cloud) | GLM-5.3-Flash, GLM-5.3, GLM-5.2, GLM-5-Turbo, GLM-5.1, GLM-4.7 |
| API Key fallback | `https://api.z.ai/api/anthropic/v1/messages` | `x-api-key` | No | Same |
| bigmodel | `https://open.bigmodel.cn/api/anthropic/v1/messages` | — | — | — |

**Model names are case-sensitive** (upstream).

### Risk control signals

| Signal | HTTP | Code | Action |
|---|---|---|---|
| Unusual activity | 405 | 3012 | Disable account, exponential backoff |
| Captcha expired | 403 | 3007 | Refresh captcha, retry once |
| Quota exhausted | 402 | — | Switch account |
| Insufficient balance | 429 | 1113 | Switch account |

**Business errors can hide in HTTP 200** — always check `code != 0` in the
response body.

### Identity headers (JWT channel)

Required headers for WAF fingerprinting:
- `X-ZCode-*` family (app version, platform, device mid, etc.)
- `X-Platform: darwin-arm64`
- `X-Device-Mid` (persisted per account)
- Tracing headers (start-plan three headers)
- **Do NOT send** `x-query-id` / `x-session-id` on JWT channel (triggers 3012)

### Body transformation

System identity block injection (anti-3012), `cache_control`,
`metadata.user_id` — see `providers/zcode/body_transform.py` +
`zcode_system.json`.

### Captcha (JWT channel only)

- SDK URL: `https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js`
  (no version number — versioned URLs are 404)
- SDK API: `mode: popup|embed`, `getInstance` callback, lowercase camelCase
  params, requires `window.AliyunCaptchaConfig` + real `#cap`/`#btn` mount
  points
- **Quality gate** (`is_valid_verify_param`): Must be `base64(JSON)` with
  `securityToken ≥ 50` chars (~280 total). 84-char failover params (only
  `certifyId/sceneId/isSign`) are rejected by upstream with code 3007.
- Solver candidates (priority order):
  1. `ZCODE_CAPTCHA_SOLVER_JS` env var
  2. `providers/zcode/captcha_node/solver.js` (built-in: heavy-duty happy-dom
     solver ported from Zcode2Api3, AS_IS license — constant Chrome/127 Linux
     fingerprint, CDN disk+memory cache, pe VM patch, per-request header
     injection, cookie priming, ~40 polyfills, mouse-glide emulation, stall
     detection. Passes Aliyun risk as T001.)
- `_ordered_solvers()` learns from past success/failure, avoids wasting
  time on known-bad solvers.
- Pre-warm pool: `captcha_manager.start()` is called idempotently in
  `get_verify_param()` and `fetch_checkin()`.
- Failure cooldown: when a solve attempt yields no valid token,
  `_last_fail_at` is set and background refill / on-demand solve back off for
  `ZCODE_CAPTCHA_REFILL_COOLDOWN` (default 30s) before retrying — avoids
  hammering the Aliyun risk endpoints when the solver is persistently failing
  (e.g. risk engine rejecting the headless env). Exposed as
  `refill_cooldown_remaining` in diagnostics.

### OAuth flow

1. `GET /admin/zcode/oauth/start` → upstream `init` → returns `authorize_url`
2. **Must override `redirect_uri`** to interstitial page:
   `https://zcode.z.ai/app/oauth/login?redirect=zcode://oauth/callback&app_version=3.12.3`
   (otherwise upstream never records authorization → poll stays pending)
3. User authorizes in browser
4. Frontend auto-polls `GET /admin/zcode/oauth/complete` (3s × 60)
5. Poll upstream `cli/poll` — four states: `pending`/`ready`/`failed`/`expired`
6. On `ready`: JWT is in `data.token` (NOT `data.access_token` — that's for
   API Key exchange); `data.zai.access_token` is for fallback key exchange

### Billing

- `GET /billing/balance` — single response with `data.plans[]` (active plans +
  `entitlements`) and `data.balances[]` (quota windows: `show_name`,
  `total_units`, `used_units`, `remaining_units`, `unit_type`, `period_end`)
  + `server_time`
- `GET /billing/preview` — claimable plans (`period=one_time`)
- **`/billing/current` → HTTP 405 + code 3012 (risk control) — DO NOT CALL**
- **`/usage` → HTTP 404 — DO NOT CALL**
- Quota cache: `fetch_quota(account, force=False)` — 30s default, `force=True`
  bypasses cache (for user-initiated refresh)

### Claim/checkin

1. `fetch_checkin`: `GET /billing/preview?app_version=&platform=` → plan list
   (priority descending) + eligibility details
2. `claim_checkin`: pick highest priority → `captcha_manager.get_verify_param`
   → `POST /billing/claim` (headers = base billing headers + captcha header
   + `X-ZCode-App-Version: 3.11.2` + `X-Platform: darwin-arm64`, body =
   `plan_id`)
3. 3007 → refresh captcha, retry once
4. Business codes: 1001-1005/3001/3007/401 all translated;
   1003 = already_claimed (idempotent success)
5. Only JWT accounts (`extra.mode=jwt` with `access_token`) support claim

### Probe & diagnostics

```bash
# Full claim chain probe
.venv/Scripts/python.exe scripts/zcode_claim_probe.py --solve
.venv/Scripts/python.exe scripts/zcode_claim_probe.py --claim
.venv/Scripts/python.exe scripts/zcode_claim_probe.py --param <verifyParam>

# Solver diagnostics
curl http://127.0.0.1:8787/admin/providers/zcode/diagnostics
```

### License note

Protocol translated from MIT-licensed `zcode-api` (TypeScript). Do NOT copy
AGPL-licensed `zcode2api` source code into this repo. The `solver.js` is
self-authored based on the zcode-api mechanism. The happy-dom solver was
ported as a design port from `Zcode2Api3` (AS_IS license, see
`providers/zcode/captcha_node/solver.js` header) — not copied from the
AGPL `zcode2api`/`zcode2api-plus` variants.

---

## 7. monkeycode (planned)

> Phase 0-3 completed in development memory; channel registration pending
> in this repo copy.

### Auth

- **Cookie-based**: session cookie `monkeycode_ai_session` (UUID format)
- **NOT** `nebula_session` (Go version had wrong constant)
- Cookie stored in `accounts.access_token` (encrypted via `credential_crypto`)
- uid = session value (no better identifier available — `/users/me` is 404,
  `wallet.id` is all zeros)

### Endpoints (verified)

| Endpoint | Status | Purpose |
|---|---|---|
| `GET /api/v1/users/me` | **404** (Go version used it — broken) | ~~Identity check~~ |
| `GET /api/v1/users/subscription` | 200 `{"plan":"basic"}` | Identity check |
| `GET /api/v1/users/wallet` | 200 | Quota: `daily_token_limit`, `daily_token_balance` |
| `GET /api/v1/users/models/available` | 200 (26 models / 14 visible) | Dynamic model catalog |
| `GET /api/v1/users/wallet/checkin` | 200 `{"checked_in": true}` | Checkin status |
| `POST /api/v1/users/tasks` | 200 `{"data":{"id":"<task_id>"}}` | Create task |
| `DELETE /api/v1/users/tasks/{id}` | 200 | Stop/clean task |
| `GET /api/v1/users/tasks/{id}` | 200 | Task details (contains real `user_id`) |
| `GET /api/v1/users/tasks/rounds?id=` | 200 | Historical frames (WS recovery) |

### WebSocket event frame syntax (verified, 51 frames)

**Connection**: `wss://monkeycode-ai.com/api/v1/users/tasks/stream?id=<task_id>&mode=develop`
(`mode=develop` is fixed, even for `task_type="chat"`)

**Envelope** (outer is plaintext JSON, `data` is base64):
```json
{
  "type": "task-running",
  "data": "eyJzZXNzaW9uSWQiOiJzZXNf...",  // base64(JSON) or null
  "kind": "acp_event",
  "seq": 47,
  "timestamp": 1790001297012
}
```

**Frame types**:

| `type` | Count | `data` | Action |
|---|---|---|---|
| `task-running` | 47 | base64 (see below) | Parse for text |
| `user-input` | 1 | base64 `{content, attachments}` | Ignore (echo) |
| `task-started` | 1 | `null` | Agent started (~9s after creation) |
| `ping` | 1 | `null` | **Must ignore** |
| `task-ended` | 1 | base64 `{exit_code, message}` | **End of stream** |

**Text extraction** (from `task-running`):
```
base64 decode data → JSON
  → payload.update.sessionUpdate
    == "agent_message_chunk" → output update.content.text (FINAL TEXT)
    == "agent_thought_chunk" → DISCARD (reasoning chain)
    == "available_commands_update" → ignore
    == "usage_update" → ignore (or collect usage)
```

**End detection**: `type == "task-ended"` (NOT `done/finish/completed/error/abort`
— Go version was wrong)

### Task creation

```json
POST /api/v1/users/tasks
{
  "content": "<user message>",
  "cli_name": "opencode",
  "model_id": "<UUID>",
  "image_id": "2e214f06-79ba-4535-9ac1-89adc2d9c6cc",
  "host_id": "public_host",
  "resource": {"core": 2, "memory": 8589934592, "life": 7200},
  "task_type": "chat"
}
```

`task_type: "chat"` does NOT create a sandbox (preferred). `model_id` must
be a UUID, not a name (use `/users/models/available` to resolve).

### Error codes

| Code | Meaning | Action |
|---|---|---|
| 10811 | Busy (task already running) | Short cooldown, retry (SLOT_BUSY_RETRY=3) |
| 4002 | Quota/upgrade required | Long cooldown, switch account |
| 401 | Auth failure | Mark account invalid |

### Performance

- First task: ~12-18s (Agent cold start ~9s)
- Subsequent tasks: faster but still slow vs. direct LLM chat
- Client timeout must be ≥60s
- Single account = single concurrent task (physical limit)

### Captcha (Cap.js PoW)

- `GET /api/v1/public/captcha/challenge` → c sub-challenges
- Each: FNV-1a seed → xorshift32 → SHA-256 nonce mining
- `POST /api/v1/public/captcha/redeem` → `captcha_token`
- Parallel solving via `ThreadPoolExecutor(16)`
- Pure standard library, ~80 lines

### Quota

- `GET /api/v1/users/wallet` → `daily_token_limit = 10000000` (10M, free)
- `daily_token_balance` = remaining for today
- Resets daily (UTC)
- `100000` = "balance" (points/credits, separate from token quota)
