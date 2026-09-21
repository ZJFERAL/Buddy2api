"""zcode 验证码「质量校验 + 求解器回退」测试（纯本地，不联网）。

背景（2026-09-21 真机定位）：点击「领取」提示失败，日志里是
    solver 退出码 1: fetch .../captcha-web/2.1.9/aliyun-captcha.min.js -> 404
第一层是 SDK 地址失效；修完地址后暴露出更隐蔽的第二层 —— 求解器只返回
failover 降级 param（~84 字符，仅 certifyId/sceneId/isSign，无 securityToken），
上游对这种 param 必回 `400 code=3007 captcha verify failed`。
故必须有质量闸门：降级结果不得入池、不得提交，并自动落到下一个求解器。

用法：python tests/test_zcode_captcha_quality.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import providers.zcode.captcha as cap  # noqa: E402

OUT: list[str] = []


def check(name, cond, detail=""):
    OUT.append(("OK  " if cond else "FAIL") + f" {name}" + (f" :: {detail}" if detail else ""))


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


async def _fake_config() -> dict:
    return {"enabled": True, "sceneId": "11xygtvd", "region": "cn", "prefix": "no8xfe"}


# 真机实测样本：降级（failover）与有效（含 securityToken）
DEGRADED = _b64({"certifyId": "2ZZz51S4hU", "sceneId": "11xygtvd", "isSign": True})
VALID = _b64(
    {
        "certifyId": "BhR0Jesi2U",
        "sceneId": "11xygtvd",
        "isSign": True,
        "securityToken": "6oOo" + "x" * 124,
    }
)


def _write_solver(path: str, param: str, exit_code: int = 0, delay_ms: int = 0) -> str:
    js = (
        f"setTimeout(() => {{ process.stdout.write('VERIFY_PARAM={param}\\n');"
        f" process.exit({exit_code}); }}, {delay_ms});\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(js)
    return path


async def step_quality_gate() -> None:
    check("有效 param 通过校验", cap.is_valid_verify_param(VALID), f"len={len(VALID)}")
    check("降级 param 被拒", not cap.is_valid_verify_param(DEGRADED), f"len={len(DEGRADED)}")
    check("空值被拒", not cap.is_valid_verify_param(""))
    check("None 被拒", not cap.is_valid_verify_param(None))
    check("超短串被拒", not cap.is_valid_verify_param("abc"))
    check("非 base64 被拒", not cap.is_valid_verify_param("x" * 150))
    # base64 合法但结构不对（无 securityToken）
    no_sec = _b64({"certifyId": "a", "sceneId": "b", "isSign": True, "note": "y" * 120})
    check("有长度但缺 securityToken 被拒", not cap.is_valid_verify_param(no_sec))
    short_sec = _b64({"certifyId": "a", "securityToken": "short"})
    check("securityToken 过短被拒", not cap.is_valid_verify_param(short_sec))


async def step_solver_fallback() -> None:
    tmp = tempfile.mkdtemp(prefix="zcode_captcha_")
    bad = _write_solver(os.path.join(tmp, "bad.js"), DEGRADED)
    good = _write_solver(os.path.join(tmp, "good.js"), VALID)
    orig = cap._CANDIDATE_SOLVERS
    cap._CANDIDATE_SOLVERS = (bad, good)
    try:
        mgr = cap.CaptchaManager()
        token = await mgr._solve_one({"sceneId": "11xygtvd", "region": "cn", "prefix": "no8xfe"})
        check("降级求解器被跳过、取到下一个求解器的有效结果", token is not None and token.param == VALID)
        check("成功求解器被记录", mgr._solver_used == good, str(mgr._solver_used))
        check("降级求解器被标记", bad in mgr._degraded_solvers)
        order = mgr._ordered_solvers([bad, good])
        check("后续求解把已验证求解器排在最前", order[0] == good, str([os.path.basename(p) for p in order]))

        diag = mgr.diagnostics()
        check("diagnostics 暴露求解器清单", len(diag.get("solvers", [])) == 2, json.dumps(diag, ensure_ascii=False))

        # 全部降级 → 不返回 token，且错误信息里点名原因
        cap._CANDIDATE_SOLVERS = (bad,)
        mgr2 = cap.CaptchaManager()
        token2 = await mgr2._solve_one({"sceneId": "11xygtvd", "region": "cn", "prefix": "no8xfe"})
        check("全部降级时不入池", token2 is None)
        check("错误信息含降级提示", "降级" in (mgr2._last_error or ""), (mgr2._last_error or "")[:120])

        # 求解器缺失 → 可读错误，而不是崩溃
        cap._CANDIDATE_SOLVERS = ()
        mgr3 = cap.CaptchaManager()
        token3 = await mgr3._solve_one({"sceneId": "s", "region": "cn", "prefix": "p"})
        check("无求解器时不崩溃", token3 is None)
        check("无求解器时给出可操作提示", "ZCODE_CAPTCHA_SOLVER_JS" in (mgr3._last_error or ""))
    finally:
        cap._CANDIDATE_SOLVERS = orig


async def step_invalid_param_not_pooled() -> None:
    """求解失败时池必须保持为空（避免垃圾 token 被当成功使用）。"""
    tmp = tempfile.mkdtemp(prefix="zcode_captcha2_")
    bad = _write_solver(os.path.join(tmp, "bad.js"), DEGRADED)
    orig = cap._CANDIDATE_SOLVERS
    cap._CANDIDATE_SOLVERS = (bad,)
    try:
        mgr = cap.CaptchaManager()
        # 不联网：config 直接给死值
        mgr.fetch_config = lambda: _fake_config()  # type: ignore[method-assign]
        await mgr._refill_batch(2)
        check("降级结果不会进入预解池", mgr._pool_size == 0, f"pool={mgr._pool_size}")
    finally:
        cap._CANDIDATE_SOLVERS = orig


async def main() -> None:
    await step_quality_gate()
    await step_solver_fallback()
    await step_invalid_param_not_pooled()
    print("\n".join(OUT))
    fails = [line for line in OUT if line.startswith("FAIL")]
    print(f"\n== {len(OUT) - len(fails)}/{len(OUT)} passed ==")
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
