"""zcode 套餐领取真机探针（诊断用，不改动任何状态，除非显式 --claim）。

用途：区分「验证码求解失败 / 求解结果降级 / 上游业务码拒绝 / 凭据失效」四类问题，
避免只看 HTTP 码或只看前端 toast 就下结论。

用法（必须用仓库自带 venv）：
    .venv/Scripts/python.exe scripts/zcode_claim_probe.py                # 只查预览
    .venv/Scripts/python.exe scripts/zcode_claim_probe.py --solve        # 顺带跑求解器
    .venv/Scripts/python.exe scripts/zcode_claim_probe.py --claim        # 真领一次
    .venv/Scripts/python.exe scripts/zcode_claim_probe.py --param <vp>    # 用给定验证码领

输出：JSON（stdout），字段含每个环节的原始响应与业务码语义。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.zcode import claim as claim_mod  # noqa: E402
from providers.zcode.captcha import captcha_manager  # noqa: E402


def _pick_account() -> dict | None:
    import database as db

    for account in db.list_accounts(provider="zcode"):
        extra = account.get("extra") or {}
        if isinstance(extra, dict) and extra.get("mode") == "jwt" and account.get("access_token"):
            return account
    return None


def describe_param(param: str) -> dict:
    info: dict = {"len": len(param)}
    try:
        raw = base64.b64decode(param + "=" * (-len(param) % 4)).decode("utf-8")
        info["decoded"] = raw[:300]
        obj = json.loads(raw)
        if isinstance(obj, dict):
            info["keys"] = sorted(obj.keys())
            sec = obj.get("securityToken") or obj.get("SecurityToken")
            info["has_security_token"] = bool(sec)
            info["security_token_len"] = len(str(sec)) if sec else 0
    except Exception as err:  # noqa: BLE001
        info["decode_error"] = f"{type(err).__name__}: {err}"
    # 76 字符 / 无 securityToken = 降级 failover 结果，上游大概率回 3007
    info["degraded"] = info["len"] < 200 or not info.get("has_security_token", False)
    return info


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--solve", action="store_true", help="跑一次验证码求解")
    ap.add_argument("--claim", action="store_true", help="真正提交领取")
    ap.add_argument("--param", default="", help="直接使用该 verifyParam")
    ap.add_argument("--raw", action="store_true", help="打印上游原始响应体")
    args = ap.parse_args()

    out: dict = {}
    account = _pick_account()
    if not account:
        print(json.dumps({"error": "库里没有 jwt 模式的 zcode 账号"}, ensure_ascii=False))
        return 2
    out["account"] = {"id": account.get("id"), "name": account.get("nickname") or account.get("name")}

    # 1) preview
    try:
        plans = await claim_mod._fetch_previews(account)
        out["preview"] = {
            "ok": True,
            "count": len(plans),
            "plans": [
                {
                    "plan_id": p["plan_id"],
                    "name": p["name"],
                    "priority": p["priority"],
                    "entitlements": [
                        {"name": e["name"], "units": e["units"], "period": e["period"]}
                        for e in p["entitlements"]
                    ],
                }
                for p in plans
            ],
        }
    except Exception as err:  # noqa: BLE001
        out["preview"] = {"ok": False, "error": f"{type(err).__name__}: {err}"}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 3

    # 2) 验证码
    param = args.param
    if args.solve or args.claim:
        try:
            config = await captcha_manager.fetch_config()
            out["captcha_config"] = config
            param, region = await captcha_manager.get_verify_param()
            out["verify_param"] = describe_param(param)
            out["verify_region"] = region
        except Exception as err:  # noqa: BLE001
            out["verify_param"] = {"error": f"{type(err).__name__}: {err}"}
            out["captcha_diag"] = captcha_manager.diagnostics()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 4
        out["captcha_diag"] = captcha_manager.diagnostics()
    elif not param:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not args.claim:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # 3) claim
    if not out["preview"]["count"]:
        out["claim"] = {"skipped": "无可领取套餐"}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    plan_id = out["preview"]["plans"][0]["plan_id"]
    out["claim_plan_id"] = plan_id
    config = out.get("captcha_config") or await captcha_manager.fetch_config()
    region = out.get("verify_region") or (config or {}).get("region")
    try:
        body = await claim_mod._raw_claim(account, plan_id, param, region)
        out["claim"] = body
    except Exception as err:  # noqa: BLE001
        out["claim"] = {"error": f"{type(err).__name__}: {err}"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
