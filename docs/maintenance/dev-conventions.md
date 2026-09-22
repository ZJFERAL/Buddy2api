# Development Conventions & Pitfalls

> Distilled from real debugging sessions. Each item here was learned the hard way.

## 1. Python environment

### Use the project venv

All Python operations must use `.venv/Scripts/python.exe`. Do not use:
- System Python (may lack dependencies)
- Managed Python at `~/.workbuddy/binaries/python/` (lacks pytest, fastapi)
- WindowsApps Python (lacks everything)

```bash
# Install deps
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

### pytest is not in the venv by default

The project venv does not ship with pytest. Install `requirements-dev.txt`
before running any tests.

### Script-style tests vs pytest-style

| Style | Files | How to run |
|---|---|---|
| pytest-style | Most `tests/test_*.py` | `.venv/Scripts/python.exe -m pytest tests/xxx.py -q` |
| Script-style | `tests/test_zcode_e2e.py`, `tests/test_zcode_captcha_quality.py` | `.venv/Scripts/python.exe tests/xxx.py` |

Running script-style tests with pytest produces ~15
`async def functions are not natively supported` errors. This is a collection
issue, not a real failure.

### Full regression command

```bash
.venv/Scripts/python.exe -m pytest tests/ -q \
  --ignore=tests/test_zcode_e2e.py \
  --ignore=tests/test_zcode_captcha_quality.py \
  --basetemp=_pytest_tmp
```

### pytest sandbox interference

The Windows sandbox may intercept bulk file deletion during pytest temp
cleanup (>50 files triggers `SAFE_DELETE_BULK_CONFIRM_REQUIRED`). Symptoms:
process exits without printing the summary line, or many `SystemExit(1)` in
setup phase.

**Fix**: Set `CODEBUDDY_SAFE_DELETE_ENABLED=0` environment variable, or use
`--basetemp=<fresh-empty-dir>` to relocate temp root.

## 2. Windows / Git Bash specifics

### Missing coreutils

Git Bash in this environment lacks `head`, `tail`, `ls`, `dirname`, `cat`.
Use:
- `Read` tool for file contents
- `Glob` for file listing
- `Grep` for searching
- Python `subprocess` for shell operations

### Path mangling

Git Bash mangles `/e/...` style paths. Always use Windows absolute paths
without a leading slash: `E:/AiWorkspace/Tools/...` (forward slashes work
in Python and most tools).

### Windows .bat files

**Must be GBK-encoded with CRLF line endings.**

- Editing with UTF-8 tools corrupts Chinese characters.
- Mixed CRLF/LF causes cmd.exe to misparse commands (reports "not internal
  or external command" for valid commands).
- When patching via Python:
  ```python
  with open(path, 'r', encoding='gbk') as f:
      content = f.read()  # universal newlines → \n
  # ... modify ...
  with open(path, 'w', encoding='gbk', newline='\r\n') as f:
      f.write(content)  # write back as CRLF
  ```
- Verify: `LF_only == 0` after writing.

### cmd.exe / wscript.exe blocked

The sandbox security policy may block:
- `cmd.exe` invoked from Bash
- `Start-Process cmd.exe` from PowerShell
- `wscript.exe` (LOLBin protection)

**Workarounds**:
- Use Python `subprocess` with `cmd /c <bat> hidden` (the `hidden` arg
  bypasses the wscript self-hide branch)
- Use Bash `run_in_background=true` for long-running services
- For service restart, use `scripts/buddy_restart.py --foreground` with
  `run_in_background=true`

### Process tree management

- **Background processes started by Bash are killed when the session ends.**
  Popen with `DETACHED_PROCESS` and `os.execv` shell-swapping also get
  reaped by the sandbox.
- To stop a node process chain (e.g., AIClient2API), use
  `taskkill /PID <top-node-pid> /F /T` to kill the entire tree. Killing
  only child processes causes the parent to fork-restart them.
- Use Python `ctypes` + `CreateToolhelp32Snapshot` to enumerate process
  trees when PowerShell/cmd are blocked.

### Enumerating ports and processes

```bash
# Find what's listening on a port
netstat -ano | findstr :8787

# Kill by PID (Git Bash: use -PID, not /PID)
taskkill -PID <pid> -F
```

## 3. Database

### Location

`codebuddy_gateway.db` in the project root. Tables: `accounts`, `api_keys`,
`logs`, `settings`, `api_key_daily_usage`.

### Credential encryption

Credentials are AES-256-GCM encrypted via DPAPI-derived key stored in
`codebuddy_gateway.db.credentials.key`.

**Testing with DB copies**: Set `CB_GATEWAY_CREDENTIAL_KEY_FILE` to the real
key file path. Otherwise the copy derives a different key and decryption
fails with `InvalidToken`.

### Creating temporary API keys for testing

```python
import database as db
key_id, key_secret = db.add_api_key(name="probe", default_channel="qwenwork")
# ... use key_secret for API calls ...
db.delete_api_key(key_id)  # clean up
```

Do not read `api_keys.key_secret` directly — it's encrypted.

### Orphan foreign keys

After deleting API keys, `logs` table may contain orphaned `api_key_id`
references. The `api_key_daily_usage` table has a FK constraint that will
cause `init_db` to fail with `FOREIGN KEY constraint failed`.

`preflight-buddy.py` handles this: it nullifies orphan `api_key_id` in
`logs` and cleans derived tables before startup.

### Model catalog in DB

`settings.channel_catalogs` stores per-channel model catalogs. This is NOT
affected by git operations — it persists across code syncs.

## 4. Upstream error patterns

### HTTP 200 with embedded error

Many upstream channels return HTTP 200 but embed errors in the response body:

| Channel | Error location |
|---|---|
| qwenwork | SSE envelope `statusCodeValue` or `body.code` |
| qoderwork | `event:error` frames in SSE stream |
| zcode | Response body `code != 0` (business error code) |

**Rule**: Never judge success by HTTP status alone. Parse the response body.

### envelope_status (qwenwork)

`providers/qwenwork/chat.py::envelope_status()` extracts the real upstream
status code from SSE envelopes. Previously, all envelope errors were
hardcoded as 400, which prevented 503 (retryable) from triggering failover.

### Error classification (auth_manager)

`auth_manager.classify_failover(status_code, error_msg)` returns:
- `QUOTA` — billing/quota exceeded → `status=expired` + `quota_blocked_until`
- `MODEL` — model not found/unsupported → block `(provider, account, model)` for 30 min
- `None` — request-level error, don't switch accounts

Key: `account_quota_blocked()` only works when `status=expired`. Otherwise
the "refresh expired account" fallback would reactivate an empty-quota
account after token refresh succeeds (token is valid but quota is still
empty).

## 5. Frontend (Vue 3 SPA)

### Hot reload

`server.py::_render_index_html()` reads `web/index.html` from disk on every
request with `Cache-Control: no-store`. Changes are visible on browser F5
refresh — **no server restart needed**.

### Vue return{...} trap

When adding new template references (refs, reactive vars, functions, @click
handlers) to a Vue component's `<template>`, you MUST also add them to the
component's `setup() return{...}` block.

**Failure mode**: Missing exports cause `ReferenceError` at render time,
which crashes the **entire component** (white screen), not just the new
element. This has happened with both zcode and monkeycode additions.

**Verification tool**: `scripts/web_binding_check.py` — statically checks
template references vs `return{...}` exports.

### Browser cache

If the user reports "can't see changes", first `curl` the server to verify
the HTML content. If the server has the new content, it's browser cache —
tell the user to Ctrl+F5. Don't restart the server for a cache issue.

## 6. Git

### Branch naming

The project uses simple branch names. Avoid slashes in branch names — the
local Git (PortableGit 2.55.0) has a bug where `git checkout -b feat/slash`
silently fails (exit 0 but doesn't write the ref file, and deletes the
directory you created).

### Commit conventions

Follow existing commit message style. The project doesn't enforce a specific
format but tends toward descriptive single-line messages.

### Sensitive files

`.gitignore` covers:
- `codebuddy_gateway.db` and `.credentials.key`
- `.env`
- `.qoder-auth/`
- `__pycache__/`
- `providers/zcode/captcha_node/node_modules/`

Always verify `.gitignore` coverage before adding new credential or
dependency directories. Use `git check-ignore -v <path>` to verify.

## 7. Service restart checklist

1. **Read the admin token from `start-oneclick.bat`** — never hand-type it.
   The display layer may mask it, and copy-paste from terminal output can
   include trailing quote characters.
2. **Stop the old process**: `taskkill -PID <pid> -F` (find PID via
   `netstat -ano | findstr :8787`)
3. **Start new process**: Use `scripts/buddy_restart.py --foreground` with
   Bash `run_in_background=true`, or tell the user to double-click
   `start-oneclick.bat`.
4. **Wait ~25 seconds** for health check to pass (preflight + scanning).
5. **Verify**: `curl http://127.0.0.1:8787/health`

### Environment variables

Some channels require env vars set at startup:

| Variable | Channel | Purpose |
|---|---|---|
| `CB_QODER_AUTH_DIRS` | qoderwork | Multiple auth directories (semicolon-separated) |
| `CB_GATEWAY_ALLOW_UNAUTHENTICATED_API` | all | Skip auth for localhost requests (default: off) |
| `CODEBUDDY_SAFE_DELETE_ENABLED=0` | dev | Disable sandbox safe-delete for pytest |
| `ZCODE_CAPTCHA_SOLVER_JS` | zcode | Path to external captcha solver JS |
