"""MonkeyCode Cap.js PoW 验证码求解器单测。

算法移植自 monkeycode2api/internal/upstream/captcha.go（Go 版已实机验证签到成功）。
这里验证：FNV-1a 与标准算法一致、PRNG 确定性、挖 nonce 正确性、完整流程编排。
"""

import asyncio
import hashlib

import pytest

from providers.monkeycode import captcha


# ── FNV-1a ────────────────────────────────────────────────────────────────
def test_fnv1a_known_vectors():
    """标准 FNV-1a 32bit 已知向量。"""
    assert captcha.fnv1a("") == 0x811C9DC5
    assert captcha.fnv1a("a") == 0xE40C292C
    assert captcha.fnv1a("foobar") == 0xBF9CF968


def test_fnv1a_equals_standard_multiplication_form():
    """cap.js 用移位加法优化（h + h<<1 + h<<4 + h<<7 + h<<8 + h<<24），
    数学上等于标准乘法 h * 16777619 —— 两者必须一致，否则与 widget PRNG 脱轨。"""

    def reference(s: str) -> int:
        h = 2166136261
        for byte in s.encode("utf-8"):
            h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
        return h

    for s in ("", "a", "cap.js", "monkeycode", "你好", "x" * 64):
        assert captcha.fnv1a(s) == reference(s), s


# ── PRNG ──────────────────────────────────────────────────────────────────
def test_prng_is_deterministic_hex():
    value = captcha.prng("seed", 16)
    assert len(value) == 16
    assert value == captcha.prng("seed", 16)
    assert value != captcha.prng("other-seed", 16)
    assert all(ch in "0123456789abcdef" for ch in value)


def test_prng_respects_requested_length():
    for length in (1, 7, 8, 9, 32, 64):
        assert len(captcha.prng("s", length)) == length


# ── 挖 nonce ──────────────────────────────────────────────────────────────
def _verify_nonce(token: str, idx: int, s_len: int, d_len: int, nonce: int) -> bool:
    salt = captcha.prng(f"{token}{idx}", s_len)
    target = captcha.prng(f"{token}{idx}d", d_len)
    digest = hashlib.sha256((salt + str(nonce)).encode("utf-8")).hexdigest()
    return digest[:d_len] == target


def test_solve_sub_challenge_satisfies_target():
    token, idx, s_len, d_len = "tok", 3, 8, 1
    nonce = captcha.solve_sub_challenge(token, idx, s_len, d_len)
    assert nonce >= 0
    assert _verify_nonce(token, idx, s_len, d_len, nonce)


def test_solve_solutions_covers_all_indices():
    token, count, s_len, d_len = "tok2", 4, 6, 1
    solutions = captcha.solve_solutions(token, count, s_len, d_len)
    assert len(solutions) == count
    for idx, nonce in enumerate(solutions, start=1):
        assert _verify_nonce(token, idx, s_len, d_len, nonce)


# ── 完整流程 ──────────────────────────────────────────────────────────────
class _FakeClient:
    """模拟 client.MonkeyCodeClient 的 post_json。"""

    def __init__(self, *, count=3, success=True, token="captcha-token-xyz"):
        self.calls: list[tuple[str, object]] = []
        self._count = count
        self._success = success
        self._token = token

    async def post_json(self, path, body=None):
        self.calls.append((path, body))
        if "challenge" in path:
            return {"challenge": {"c": self._count, "s": 8, "d": 1}, "token": "chal-token"}
        return {"success": self._success, "token": self._token}


def test_solve_captcha_full_flow():
    client = _FakeClient()
    result = asyncio.run(captcha.solve_captcha(client))

    assert result == "captcha-token-xyz"
    assert len(client.calls) == 2

    challenge_path, challenge_body = client.calls[0]
    redeem_path, redeem_body = client.calls[1]

    assert "challenge" in challenge_path
    assert challenge_body is None

    assert "redeem" in redeem_path
    assert redeem_body["token"] == "chal-token"
    assert len(redeem_body["solutions"]) == 3
    for idx, nonce in enumerate(redeem_body["solutions"], start=1):
        assert _verify_nonce("chal-token", idx, 8, 1, nonce)


def test_solve_captcha_rejects_invalid_config():
    client = _FakeClient(count=0)
    with pytest.raises(captcha.CaptchaSolveError):
        asyncio.run(captcha.solve_captcha(client))


def test_solve_captcha_rejects_empty_challenge_token():
    class NoToken:
        async def post_json(self, path, body=None):
            return {"challenge": {"c": 1, "s": 8, "d": 1}, "token": ""}

    with pytest.raises(captcha.CaptchaSolveError):
        asyncio.run(captcha.solve_captcha(NoToken()))


def test_solve_captcha_rejects_redeem_failure():
    client = _FakeClient(success=False)
    with pytest.raises(captcha.CaptchaSolveError):
        asyncio.run(captcha.solve_captcha(client))


def test_solve_captcha_wraps_network_error():
    class Boom:
        async def post_json(self, path, body=None):
            raise RuntimeError("connection reset")

    with pytest.raises(captcha.CaptchaSolveError) as excinfo:
        asyncio.run(captcha.solve_captcha(Boom()))
    assert "connection reset" in str(excinfo.value)


def test_captcha_error_is_runtime_error():
    """必须能被 except RuntimeError 捕获（否则上层只会看到无信息的 500）。"""
    assert issubclass(captcha.CaptchaSolveError, RuntimeError)
