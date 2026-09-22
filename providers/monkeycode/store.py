"""MonkeyCode 账号存储：Cookie 解析 / 上游校验 / 入池 / 发现。

账号形态（与其它渠道不同）：
  - **同源 Cookie 会话**，不是 Bearer / OAuth。
  - Cookie 原文存 `accounts.access_token`（由 database 层自动加密）。
  - `uid` = Cookie 中 `monkeycode_ai_session` 的值（稳定且唯一的会话标识）。
    ⚠️ 上游 `/api/v1/users/me` 已 404、`/users/wallet` 的 `id` 是全零 UUID，
    均不可用作账号标识，故用会话值。
  - `extra` 存 plan / wallet 快照（额度查询与展示用）。

上游端点事实见 constants.py 顶部注释（Phase 0 实测）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

import database as db
from providers.monkeycode.constants import (
    BASE,
    CHANNEL_ID,
    EP_SUBSCRIPTION,
    EP_WALLET,
    SESSION_COOKIE_NAME,
    USER_AGENT,
)

PROVIDER = CHANNEL_ID

# Go 版 monkeycode2api 的 auths 目录候选（用于兼容导入）
_GO_AUTH_DIR_CANDIDATES = (
    r"E:\AiWorkspace\Tools\monkeycode2api\auths",
)


# ── Cookie 处理 ────────────────────────────────────────────────────────────
def extract_session_value(cookie: str) -> str:
    """从 Cookie 串里取会话值；没有则返回空串。"""
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith(SESSION_COOKIE_NAME + "="):
            return part.split("=", 1)[1].strip()
    return ""


def normalize_cookie(raw: str) -> str:
    """把用户输入归一化成完整 Cookie 串。

    支持两种输入：
      1. 完整 Cookie 串（含 `k=v; k=v`）→ 原样返回（去空白）
      2. 只粘贴了会话 UUID → 补成 `monkeycode_ai_session=<uuid>`
    """
    text = (raw or "").strip().strip('"').replace("\r", "").replace("\n", "")
    if not text:
        return ""
    if "=" in text:
        return text
    # 纯 UUID / 纯值 → 补上 Cookie 名
    return f"{SESSION_COOKIE_NAME}={text}"


def mask_cookie(cookie: str) -> str:
    """脱敏展示（日志/接口回传用，绝不返回原文）。"""
    parts = []
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        if len(value) <= 10:
            parts.append(f"{key}=***")
        else:
            parts.append(f"{key}={value[:6]}...{value[-4:]}")
    return "; ".join(parts)


# ── 同步校验（管理页添加账号时立即反馈）────────────────────────────────────
def probe_cookie(cookie: str, timeout: float = 15.0) -> dict:
    """同步校验 Cookie 并拉取 plan / wallet。

    返回 {ok, plan, wallet, error}；Cookie 无效时 ok=False。
    用同步 httpx 是为了让管理页「添加账号」能立即给出成败反馈。
    """
    head = {
        "Cookie": cookie,
        "User-Agent": USER_AGENT,
        "Referer": BASE + "/",
        "Origin": BASE,
        "Accept": "application/json, text/plain, */*",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            sub_resp = client.get(BASE + EP_SUBSCRIPTION, headers=head)
            if sub_resp.status_code in (401, 403):
                return {"ok": False, "error": "Cookie 无效或已过期（HTTP %d）" % sub_resp.status_code}
            if sub_resp.status_code != 200:
                return {"ok": False, "error": f"校验失败 HTTP {sub_resp.status_code}"}

            wal_resp = client.get(BASE + EP_WALLET, headers=head)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络错误: {exc}"}

    plan = ""
    try:
        sub_body = sub_resp.json()
        if isinstance(sub_body, dict):
            data = sub_body.get("data") if isinstance(sub_body.get("data"), dict) else sub_body
            plan = str(data.get("plan") or "")
    except ValueError:
        pass

    wallet: dict[str, Any] = {}
    if wal_resp.status_code == 200:
        try:
            wal_body = wal_resp.json()
            if isinstance(wal_body, dict):
                data = wal_body.get("data") if isinstance(wal_body.get("data"), dict) else wal_body
                wallet = {
                    "balance": data.get("balance"),
                    "daily_token_balance": data.get("daily_token_balance"),
                    "daily_token_limit": data.get("daily_token_limit"),
                }
        except ValueError:
            pass

    return {"ok": True, "plan": plan, "wallet": wallet}


# ── 解析 / 入池 ────────────────────────────────────────────────────────────
def parse_credentials(body: dict) -> dict:
    """管理页粘贴表单 → 归一化账号数据（含上游校验）。

    接受字段：
      - cookie（必需）：完整 Cookie 串，或仅 `monkeycode_ai_session` 的值
      - name / nickname（可选）：显示名
      - verify=false（可选）：跳过上游校验（离线导入时用）
    """
    raw = str(body.get("cookie") or body.get("session") or body.get("token") or "").strip()
    if not raw:
        raise ValueError("缺少凭据：请粘贴完整 Cookie，或 monkeycode_ai_session 的值")

    cookie = normalize_cookie(raw)
    session_value = extract_session_value(cookie)
    if not session_value:
        raise ValueError(
            f"Cookie 中未找到 {SESSION_COOKIE_NAME}，请确认从 monkeycode-ai.com 复制了完整 Cookie"
        )

    name = str(body.get("name") or body.get("nickname") or "").strip()

    extra: dict[str, Any] = {"session_value": session_value}
    plan = ""
    wallet: dict[str, Any] = {}

    verify = body.get("verify")
    if verify is None or str(verify).lower() not in ("false", "0", "no"):
        result = probe_cookie(cookie)
        if not result.get("ok"):
            raise ValueError(result.get("error") or "Cookie 校验失败")
        plan = str(result.get("plan") or "")
        wallet = result.get("wallet") or {}
        extra["plan"] = plan
        extra["wallet"] = wallet
        extra["wallet_checked_at"] = int(time.time())

    display = name or (f"MonkeyCode-{session_value[:8]}" if session_value else "MonkeyCode")

    account: dict = {
        "name": display,
        "provider": PROVIDER,
        "uid": session_value,
        "nickname": name or display,
        "access_token": cookie,
        "status": "active",
        "extra": extra,
    }
    if plan:
        account["account_type"] = plan
    return account


def _find_by_uid(uid: str) -> dict | None:
    if not uid:
        return None
    for row in db.list_accounts(provider=PROVIDER):
        if str(row.get("uid") or "") == str(uid):
            return row
    return None


def upsert_account(parsed: dict) -> dict:
    """入池：uid（会话值）相同则更新，否则新增。"""
    uid = str(parsed.get("uid") or "")
    name = parsed.get("name") or "monkeycode-account"
    existing = _find_by_uid(uid)

    if existing is not None:
        patch = {
            "access_token": parsed["access_token"],
            "nickname": parsed.get("nickname") or existing.get("nickname") or name,
            "name": existing.get("name") or name,
            "status": "active",
            "extra": parsed.get("extra") or existing.get("extra") or {},
        }
        if parsed.get("account_type"):
            patch["account_type"] = parsed["account_type"]
        db.update_account(existing["id"], patch)
        return {"id": existing["id"], "updated": True}

    aid = db.add_account(parsed)
    return {"id": aid, "updated": False}


def update_status(aid: int, status: str, reason: str = "") -> None:
    """更新账号状态；reason 写入 extra.last_error。"""
    if not aid:
        return
    row = db.get_account(aid) or {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    patch_extra = dict(extra)
    if reason:
        patch_extra["last_error"] = reason[:200]
    else:
        patch_extra.pop("last_error", None)
    db.update_account(aid, {"status": status, "extra": patch_extra})


def save_wallet_snapshot(aid: int, wallet: dict, plan: str = "") -> None:
    """把额度快照写回 extra（供管理页展示，避免每次查询都打上游）。"""
    if not aid:
        return
    row = db.get_account(aid) or {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    patch = dict(extra)
    if wallet:
        patch["wallet"] = wallet
        patch["wallet_checked_at"] = int(time.time())
    if plan:
        patch["plan"] = plan
    db.update_account(aid, {"extra": patch})


# ── 发现本机已有凭据 ───────────────────────────────────────────────────────
def candidate_auth_dirs() -> list[Path]:
    """会被扫描的目录候选项（含不存在的）。"""
    dirs: list[Path] = []
    env = (os.environ.get("MC2A_AUTH_DIR") or "").strip()
    if env:
        dirs.append(Path(env).expanduser())
    for raw in _GO_AUTH_DIR_CANDIDATES:
        dirs.append(Path(raw))
    dirs.append(Path.home() / ".monkeycode" / "auths")

    out: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _parse_go_auth_file(path: Path) -> dict | None:
    """解析 Go 版 auths/monkeycode-<uid>.json。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    wallet = data.get("wallet") if isinstance(data.get("wallet"), dict) else {}
    cookie = str(session.get("cookie") or "").strip()
    if not cookie:
        return None
    session_value = extract_session_value(cookie)
    if not session_value:
        return None
    return {
        "path": str(path),
        "uid": str(account.get("uid") or session_value),
        "session_value": session_value,
        "nickname": str(account.get("nickname") or ""),
        "plan": str(account.get("plan") or ""),
        "wallet": wallet,
        "cookie": cookie,
    }


def discover() -> dict:
    """扫描候选目录，返回可导入的账号文件元信息（不含 Cookie 原文）。"""
    existing_uids = {
        str(a.get("uid") or "") for a in db.list_accounts(provider=PROVIDER) if a.get("uid")
    }

    dirs_info = []
    files_info = []
    for d in candidate_auth_dirs():
        exists = d.is_dir()
        found: list[Path] = []
        if exists:
            try:
                found = sorted(d.glob("monkeycode-*.json"))
            except OSError:
                found = []
        dirs_info.append({"path": str(d), "exists": exists, "file_count": len(found)})

        for path in found:
            parsed = _parse_go_auth_file(path)
            if not parsed:
                files_info.append({
                    "name": path.name, "path": str(path), "valid": False,
                    "reason": "不是有效的 MonkeyCode 凭据文件",
                    "account_name": "", "uid_masked": "", "already_imported": False,
                })
                continue
            uid = parsed["uid"]
            files_info.append({
                "name": path.name,
                "path": str(path),
                "valid": True,
                "reason": "ok",
                "account_name": parsed["nickname"] or path.stem,
                "uid_masked": (uid[:8] + "..." + uid[-4:]) if len(uid) > 14 else uid,
                "already_imported": uid in existing_uids or parsed["session_value"] in existing_uids,
                "extra_preview": {"plan": parsed["plan"]},
            })

    return {
        "dirs": dirs_info,
        "files": files_info,
        "file_count": len(files_info),
        "valid_count": sum(1 for f in files_info if f.get("valid")),
        "importable_count": sum(
            1 for f in files_info if f.get("valid") and not f.get("already_imported")
        ),
    }


def import_path(path: str) -> dict:
    """导入单个发现的凭据文件（Go 版格式）。"""
    parsed = _parse_go_auth_file(Path(path))
    if not parsed:
        raise ValueError("不是有效的 MonkeyCode 凭据文件")

    cookie = parsed["cookie"]
    session_value = parsed["session_value"]
    extra: dict[str, Any] = {"session_value": session_value, "imported_from": path}
    if parsed.get("plan"):
        extra["plan"] = parsed["plan"]
    if parsed.get("wallet"):
        extra["wallet"] = parsed["wallet"]
        extra["wallet_checked_at"] = int(time.time())

    account = {
        "name": parsed["nickname"] or f"MonkeyCode-{session_value[:8]}",
        "provider": PROVIDER,
        "uid": session_value,
        "nickname": parsed["nickname"] or f"MonkeyCode-{session_value[:8]}",
        "access_token": cookie,
        "status": "active",
        "extra": extra,
    }
    if parsed.get("plan"):
        account["account_type"] = parsed["plan"]
    return upsert_account(account)
