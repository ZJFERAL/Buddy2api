#!/usr/bin/env python
"""Qoder 每日 100 Credits 守望脚本。

机制（本机逆向所得）：
  1. GET  https://openapi.qoder.sh/sash/api/v1/me/campaigns
         -> {showCampaign, claimable, campaignUrl, campaigns:[{...uuid...}]}
  2. POST https://openapi.qoder.sh/sash/api/v1/me/campaigns/{campaignUUID}/claim
         -> 领取本轮 100 Credits

请求必须带本机真实机器身份头（Cosy-MachineCode/Token/Type），
由 Qoder 自带的 UMID 组件 runtime-info.exe 提供（本机取值，非伪造）。

用法：
  python scripts/qoder_campaign_watch.py              # 查询 + 领取（默认）
  python scripts/qoder_campaign_watch.py --dry-run    # 只查询，不领取
  python scripts/qoder_campaign_watch.py --json       # 输出 JSON（供定时任务解析）

轮询（每 10 分钟，抓 10:00 开放那一刻）：
  python scripts/qoder_campaign_watch.py --loop 600
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

BASE = "https://openapi.qoder.sh"
CID_KEY = "client_launch_26"  # 客户端内置活动代号（仅用于 limited-number）
# 实测（2026-09-20）：clientType=10（桌面端）才会下发 campaigns[]；1-5 返回空。
# 另需桌面端同款头：Cosy-Version: 0.3.4、User-Agent: Qoder、Cosy-MachineHostname。
UA = "Qoder"
COSY_VERSION = "0.3.4"


def find_umid() -> Path:
    pats = [
        os.path.expanduser("~/.qoder/.bin/umid-*/runtime-info.exe"),
        os.path.expanduser("~/.qodersec/.bin/umid-*/runtime-info.exe"),
    ]
    for p in pats:
        for m in glob.glob(p):
            return Path(m)
    raise SystemExit("未找到 UMID runtime-info.exe")


def machine_identity() -> dict:
    exe = find_umid()
    out = subprocess.run([str(exe)], capture_output=True, timeout=60)
    txt = (out.stdout or b"").decode("utf-8", "replace").strip().splitlines()[0]
    return json.loads(txt)


def auth_dirs() -> list[Path]:
    env = os.environ.get("CB_QODER_AUTH_DIRS", "")
    dirs = [Path(p) for p in env.split(";") if p.strip()]
    if not dirs:
        dirs = [Path(os.path.expanduser("~/.qoder/.auth"))]
    snap = Path(os.path.expanduser("~/.qoder/snapshots"))
    if snap.is_dir():
        for d in sorted(snap.iterdir()):
            ad = d / ".auth"
            if ad.is_dir() and ad not in dirs:
                dirs.append(ad)
    return dirs


def load_session(ad: Path) -> dict:
    from providers.qoderwork.token import load_local_session

    return load_local_session(ad)


def headers(sess: dict, mi: dict) -> dict:
    return {
        "Accept": "application/json",
        "Authorization": "Bearer " + str(sess.get("security_oauth_token") or ""),
        "Cosy-MachineId": str(sess.get("machine_id") or ""),
        "Cosy-MachineCode": str(mi.get("machineCode") or ""),
        "Cosy-MachineToken": str(mi.get("machineToken") or ""),
        "Cosy-MachineType": str(mi.get("machineType") or ""),
        "Cosy-MachineOS": "Windows_NT",
        "Cosy-MachineHostname": platform.node(),
        "Cosy-ClientType": "10",
        "Cosy-Version": COSY_VERSION,
        "User-Agent": UA,
        "Content-Type": "application/json",
    }


def extract_uuid(campaigns: list) -> str | None:
    """从 campaigns 数组里找 claimStatus=CLAIMABLE 的活动 UUID。

    注意：不按 actionType 过滤——实测 act-20260901-493 的 actionType 是
    VIEW_DETAILS，照样能 POST claim 成功（返回 grantId）。
    """
    for c in campaigns or []:
        if not isinstance(c, dict):
            continue
        if c.get("claimStatus") != "CLAIMABLE":
            continue
        v = c.get("campaignId") or c.get("id") or c.get("uuid")
        if isinstance(v, str) and len(v) >= 32:
            return v
    return None


def check(client: httpx.Client, sess: dict, mi: dict) -> dict:
    h = headers(sess, mi)
    r = client.get(BASE + "/sash/api/v1/me/campaigns", headers=h)
    info = {
        "name": sess.get("name"),
        "uid": str(sess.get("uid") or "")[:8],
        "status": r.status_code,
        "raw": None,
        "claimable": False,
        "uuid": None,
        "campaignUrl": "",
    }
    try:
        data = r.json()
    except Exception:
        info["raw"] = r.text[:200]
        return info
    info["raw"] = data
    info["claimable"] = bool(data.get("claimable"))
    info["campaignUrl"] = data.get("campaignUrl") or ""
    info["uuid"] = extract_uuid(data.get("campaigns"))
    return info


def claim(client: httpx.Client, sess: dict, mi: dict, uuid: str) -> tuple[int, str]:
    h = headers(sess, mi)
    r = client.post(BASE + f"/sash/api/v1/me/campaigns/{uuid}/claim", headers=h, json={})
    return r.status_code, r.text[:300]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只查询不领取")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--loop", type=int, default=0, help="循环间隔秒，0=只跑一次")
    args = ap.parse_args()

    mi = machine_identity()
    results = []
    while True:
        results = []
        with httpx.Client(timeout=30) as client:
            for ad in auth_dirs():
                try:
                    sess = load_session(ad)
                except Exception as exc:
                    results.append({"dir": str(ad), "error": str(exc)})
                    continue
                info = check(client, sess, mi)
                info["dir"] = str(ad)
                if info["uuid"] and info["claimable"] and not args.dry_run:
                    code, body = claim(client, sess, mi, info["uuid"])
                    info["claim_status"] = code
                    info["claim_body"] = body
                results.append(info)

        if args.json:
            print(json.dumps(results, ensure_ascii=False))
        else:
            for r in results:
                if r.get("error"):
                    print("[ERR ] %s %s" % (r["dir"], r["error"]))
                    continue
                print("[%s] uid=%s claimable=%s uuid=%s url=%s"
                      % (r.get("name"), r.get("uid"), r.get("claimable"),
                         r.get("uuid"), r.get("campaignUrl")))
                if "claim_status" in r:
                    print("      claim -> %s %s" % (r["claim_status"], r["claim_body"]))
                elif r.get("claimable") and args.dry_run:
                    print("      (dry-run) 可领取，未执行")

        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
