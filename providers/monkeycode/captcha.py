"""Cap.js / 长亭自研 PoW 验证码求解器（移植自 monkeycode2api/internal/upstream/captcha.go）。

流程（与 SPA 内嵌 cap.js widget 一致，Go 版已实机验证签到成功）：
  1. POST /api/v1/public/captcha/challenge → {challenge:{c,s,d}, token, expires}
  2. 生成 c 个子挑战：
       salt_i   = prng(token + i,       s)   # FNV-1a 播种 + xorshift32，hex 输出
       target_i = prng(token + i + "d", d)
  3. 对每个子挑战求 nonce：sha256(salt_i + str(nonce)) 的 hex 前缀 == target_i
  4. POST /api/v1/public/captcha/redeem {token, solutions} → {success, token}
     返回的 token 即签到用的 captcha_token。

难度 d 为 target 的 hex 字符数（线上常见 d=3 → 12bit，期望 ~2k 次/子挑战）。
子挑战之间并行求解。
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

from providers.monkeycode.constants import (
    EP_CAPTCHA_CHALLENGE,
    EP_CAPTCHA_REDEEM,
)

# ── FNV-1a 32bit（与 cap.js 实现一致）──────────────────────────────────────
_FNV_OFFSET_32 = 2166136261
_FNV_MASK = 0xFFFFFFFF

# 单个子挑战的 nonce 搜索上限。线上 d=3 时远用不到；
# 上限设 2^26（约 6700 万），d 升到 6 也够用。
MAX_NONCE_PER_SUB = 1 << 26

# 并行度（Go 版 semaphore 即为 16）
PARALLELISM = 16


class CaptchaSolveError(RuntimeError):
    """验证码求解失败。继承 RuntimeError 以便被既有 except RuntimeError 捕获。"""


def fnv1a(s: str) -> int:
    """FNV-1a 32bit，与 cap.js 实现一致。"""
    h = _FNV_OFFSET_32
    for byte in s.encode("utf-8"):
        h ^= byte
        h = (h + (h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24)) & _FNV_MASK
    return h


def prng(seed: str, length: int) -> str:
    """确定性 PRNG：FNV-1a 播种 + xorshift32，输出 length 长度的 hex 字符串。

    必须与 Cap.js widget 的 PRNG 完全一致。
    """
    state = fnv1a(seed)
    out: list[str] = []
    total = 0
    while total < length:
        state ^= (state << 13) & _FNV_MASK
        state ^= state >> 17
        state ^= (state << 5) & _FNV_MASK
        state &= _FNV_MASK
        chunk = f"{state:08x}"
        out.append(chunk)
        total += len(chunk)
    return "".join(out)[:length]


def solve_sub_challenge(token: str, idx: int, s_len: int, d_len: int) -> int:
    """求单个子挑战的 nonce；找不到抛 CaptchaSolveError。"""
    salt = prng(f"{token}{idx}", s_len)
    target = prng(f"{token}{idx}d", d_len)
    prefix = salt.encode("utf-8")
    for n in range(MAX_NONCE_PER_SUB):
        digest = hashlib.sha256(prefix + str(n).encode("utf-8")).hexdigest()
        if digest[:d_len] == target:
            return n
    raise CaptchaSolveError(
        f"sub-challenge {idx} unsolved in {MAX_NONCE_PER_SUB} attempts (d={d_len})"
    )


def solve_solutions(token: str, count: int, s_len: int, d_len: int) -> list[int]:
    """并行求解全部子挑战，返回按 idx 顺序排列的 nonce 列表。"""
    solutions: list[int] = [0] * count
    with ThreadPoolExecutor(max_workers=min(PARALLELISM, max(1, count))) as pool:
        futures = {
            pool.submit(solve_sub_challenge, token, idx, s_len, d_len): idx
            for idx in range(1, count + 1)
        }
        for fut, idx in futures.items():
            solutions[idx - 1] = fut.result()
    return solutions


async def solve_captcha(client, challenge_path: str = EP_CAPTCHA_CHALLENGE,
                        redeem_path: str = EP_CAPTCHA_REDEEM) -> str:
    """完成完整验证码流程，返回可用于签到的 captcha_token。

    `client` 需提供 `post_json(path, body=None) -> dict`（见 client.MonkeyCodeClient）。
    求解是 CPU 密集，放线程池并行，避免阻塞事件循环的一帧。
    """
    import asyncio

    try:
        ch = await client.post_json(challenge_path, None)
    except Exception as exc:  # noqa: BLE001 - 统一收敛为求解失败
        raise CaptchaSolveError(f"captcha challenge 请求失败: {exc}") from exc

    ch = ch if isinstance(ch, dict) else {}
    cfg = ch.get("challenge") if isinstance(ch.get("challenge"), dict) else {}
    count = int(cfg.get("c") or 0)
    s_len = int(cfg.get("s") or 0)
    d_len = int(cfg.get("d") or 0)
    token = ch.get("token") or ""
    if count <= 0 or s_len <= 0 or d_len <= 0 or not token:
        raise CaptchaSolveError(f"captcha challenge 配置非法: {cfg!r}")

    solutions = await asyncio.get_running_loop().run_in_executor(
        None, solve_solutions, token, count, s_len, d_len
    )

    try:
        rd = await client.post_json(redeem_path, {"token": token, "solutions": solutions})
    except Exception as exc:  # noqa: BLE001
        raise CaptchaSolveError(f"captcha redeem 请求失败: {exc}") from exc

    rd = rd if isinstance(rd, dict) else {}
    if not rd.get("success"):
        raise CaptchaSolveError("captcha redeem 返回 success=false")
    cap_token = rd.get("token") or ""
    if not cap_token:
        raise CaptchaSolveError("captcha redeem 返回空 token")
    return cap_token
