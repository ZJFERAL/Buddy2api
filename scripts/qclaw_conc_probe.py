"""QClaw 上游并发/限流探针。

用途：诊断 qclaw 通道 `stream failed`（上游 429）到底是本地并发、上游限流
还是账号问题。直接打 aizone，不走 auth_manager，不写 DB，
因此**不会污染本服务的账号冷却状态**。

用法（在仓库根目录）：
    # 单次探测
    .venv/Scripts/python.exe scripts/qclaw_conc_probe.py

    # 并发 10 个长输出流式请求（复现限流的典型配置）
    PROBE_STREAM=1 PROBE_CONC=10 PROBE_MAXTOK=600 \
    PROBE_PROMPT="详细解释TCP三次握手" \
    .venv/Scripts/python.exe scripts/qclaw_conc_probe.py

    # 换模型
    PROBE_MODEL=pool-minimax-m3 .venv/Scripts/python.exe scripts/qclaw_conc_probe.py

环境变量：
    PROBE_MODEL   默认 pool-deepseek-v4-flash
    PROBE_STREAM  1=流式（默认 0）
    PROBE_CONC    并发数（默认 3）
    PROBE_MAXTOK  默认 8
    PROBE_PROMPT  默认 "hi"
    PROBE_ACCOUNT 指定 qclaw 账号 id（默认取第一个）

安全：密钥只在内存中使用，不打印、不落盘。仅打印长度与前缀。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import database as db  # noqa: E402
import credential_crypto  # noqa: E402

DB_PATH = Path(os.environ.get("CB_GATEWAY_DB") or (REPO / "codebuddy_gateway.db"))
db.DB_PATH = DB_PATH


def load_account():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        want = os.environ.get("PROBE_ACCOUNT", "").strip()
        if want:
            row = con.execute(
                "SELECT * FROM accounts WHERE provider='qclaw' AND id=?", (int(want),)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM accounts WHERE provider='qclaw' ORDER BY id LIMIT 1"
            ).fetchone()
    finally:
        con.close()
    if not row:
        print("no qclaw account in DB:", DB_PATH)
        raise SystemExit(1)
    return dict(row)


ACCT = load_account()
KEY = credential_crypto.decrypt_secret(ACCT.get("access_token"), DB_PATH)
JWT = credential_crypto.decrypt_secret(ACCT.get("refresh_token"), DB_PATH)
try:
    EXTRA = json.loads(ACCT.get("extra") or "{}")
except ValueError:
    EXTRA = {}

import httpx  # noqa: E402

from providers.qclaw.constants import AIZONE_BASE  # noqa: E402
from providers.qclaw.sign import aizone_headers  # noqa: E402

MODEL = os.environ.get("PROBE_MODEL", "pool-deepseek-v4-flash")
STREAM = os.environ.get("PROBE_STREAM", "0") == "1"
CONC = int(os.environ.get("PROBE_CONC", "3"))
MAXTOK = int(os.environ.get("PROBE_MAXTOK", "8"))
PROMPT = os.environ.get("PROBE_PROMPT", "hi")

results: list[tuple[int, int, int, str]] = []


async def call(client: httpx.AsyncClient, idx: int, gate: asyncio.Event):
    await gate.wait()
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": STREAM,
        "max_tokens": MAXTOK,
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = aizone_headers(
        api_key=KEY, jwt=JWT, guid=EXTRA.get("guid") or "", account=str(ACCT.get("uid") or "")
    )
    t0 = time.time()
    try:
        if STREAM:
            async with client.stream(
                "POST", f"{AIZONE_BASE}/chat/completions", headers=headers, content=raw
            ) as resp:
                txt = (await resp.aread()).decode("utf-8", errors="replace")
                code = resp.status_code
        else:
            resp = await client.post(
                f"{AIZONE_BASE}/chat/completions", headers=headers, content=raw
            )
            txt, code = resp.text, resp.status_code
        results.append((idx, code, int((time.time() - t0) * 1000), txt[:300]))
    except Exception as exc:  # noqa: BLE001
        results.append((idx, -1, int((time.time() - t0) * 1000), f"{type(exc).__name__}: {exc}"))


async def main():
    print("=== qclaw 上游探针（只读，不影响本服务冷却状态）===")
    print(f"  db        = {DB_PATH}")
    print(f"  account   = id={ACCT.get('id')} name={ACCT.get('name')!r} uid={ACCT.get('uid')}")
    print(f"  key       = len={len(KEY or '')} prefix={(KEY or '')[:8]}...")
    print(f"  base      = {AIZONE_BASE}")
    print(f"  model     = {MODEL}  stream={STREAM}  conc={CONC}  max_tokens={MAXTOK}")
    print()

    gate = asyncio.Event()
    async with httpx.AsyncClient(timeout=120.0) as client:
        tasks = [asyncio.create_task(call(client, i, gate)) for i in range(1, CONC + 1)]
        await asyncio.sleep(0.2)
        gate.set()
        await asyncio.gather(*tasks)

    results.sort()
    ok = sum(1 for r in results if r[1] == 200)
    print(f"  -> HTTP 200: {ok}/{len(results)}")
    for idx, code, ms, txt in results:
        print(f"  [{idx:>2}] HTTP {code}  {ms:>6}ms")
        if code != 200:
            print(f"       {txt!r}")

    bad = [r for r in results if r[1] != 200]
    if bad:
        print("\n  ⚠️ 出现非 200：上游确实在限流，请对比上表 status/time 判断")
        print("     若本次并发下出现 429 而串行全部 200 → 上游按并发限制")
        print("     若串行也 429 → 上游按速率/时段限制")
    else:
        print("\n  本次全部 200：当前时段上游正常，429 为时段性/条件性")


if __name__ == "__main__":
    asyncio.run(main())
