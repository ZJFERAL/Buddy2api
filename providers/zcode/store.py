"""zcode 账号存储：凭据解析 / 入池 / 更新。

账号形态：
  - JWT（zai 通道）：三段点分 token，来自 OAuth 登录或 zcode 客户端登录态
  - API Key（zai 回退 / bigmodel 通道）：'id.secret' 或 'sk-*'，来自 Z.AI 开放平台
    或 OAuth 兑换链自动获取

持久化走 Buddy2api accounts 表（provider='zcode'）：
  - access_token：JWT 或 API Key（加密存储）
  - uid：JWT 解出的 user_id（API Key 无则留空）
  - extra：{ mode, api_key?, oauth_email?, fingerprint? }
"""

from __future__ import annotations

import json
import re
import time

import database as db
from providers.zcode import token as ztoken

JWT_RE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")
API_KEY_RE = re.compile(r"^[A-Za-z0-9\-_.]+(\.[A-Za-z0-9\-_]+)?$")


def parse_credentials(body: dict) -> dict:
    """管理页粘贴表单 → 归一化账号数据。

    接受字段：
      - token / jwt / api_key：凭据本体
      - mode：jwt / apiKey（可推断）
      - provider：zai / bigmodel（默认 zai）
      - name / nickname：显示名
    """
    name = str(body.get("name") or body.get("nickname") or "").strip()
    raw = str(
        body.get("token")
        or body.get("jwt")
        or body.get("api_key")
        or body.get("secret")
        or ""
    ).strip()
    provider = str(body.get("provider") or "zai").strip().lower()
    mode = str(body.get("mode") or "").strip().lower()

    if not raw:
        raise ValueError("缺少凭据：请粘贴 JWT 或 API Key")

    if not mode:
        mode = "jwt" if ztoken.is_jwt(raw) else "apiKey"
    mode = mode.lower()
    if mode == "apikey":
        mode = "apiKey"
    if mode not in ("jwt", "apiKey"):
        raise ValueError(f"未知模式: {mode}")

    if mode == "jwt" and provider != "zai":
        raise ValueError("JWT 凭据仅支持 zai 提供商（bigmodel 走 API Key）")

    user_id = ztoken.jwt_user_id(raw) if mode == "jwt" else None
    display = name or user_id or (raw[:10] + "…")
    extra = {
        "mode": mode,
        "provider": provider,
        **({"api_key": raw} if mode == "apiKey" else {}),
        **({"oauth_email": str(body.get("oauth_email") or "")} if body.get("oauth_email") else {}),
    }
    if body.get("fingerprint") and isinstance(body.get("fingerprint"), dict):
        extra["fingerprint"] = body["fingerprint"]

    account: dict = {
        "name": display,
        "provider": "zcode",
        "access_token": raw,
        "uid": user_id or "",
        "nickname": display,
        "status": "active",
        "extra": extra,
    }
    exp = ztoken.jwt_expiry(raw) if mode == "jwt" else None
    if exp:
        account["expires_at"] = int(exp * 1000)
    return account


def upsert_account(parsed: dict) -> dict:
    """入池：uid 相同则更新，否则新增。"""
    uid = parsed.get("uid") or ""
    name = parsed.get("name") or "zcode-account"
    existing = _find_by_uid(uid) if uid else None
    if existing is not None:
        db.update_account(existing["id"], {
            "access_token": parsed["access_token"],
            "nickname": parsed.get("nickname") or existing.get("nickname") or name,
            "status": "active",
            "expires_at": parsed.get("expires_at", existing.get("expires_at")) if parsed.get("expires_at") else existing.get("expires_at"),
            "extra": parsed.get("extra") or existing.get("extra") or {},
        })
        return {"id": existing["id"], "updated": True}
    aid = db.add_account(parsed)
    return {"id": aid, "updated": False}


def _find_by_uid(uid: str):
    if not uid:
        return None
    for row in db.list_accounts(provider="zcode"):
        if str(row.get("uid") or "") == uid:
            return row
    return None


def import_from_zcode2api(db_path: str) -> dict:
    """从 zcode2api 的 accounts.db 导入全部账号（JWT / API Key）。"""
    import os
    import sqlite3

    if not os.path.isfile(db_path):
        raise ValueError(f"zcode2api 数据库不存在: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, name, provider, mode, jwt_token, api_key, enabled FROM accounts"
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        raise ValueError(f"无法读取 {db_path}（不是 zcode2api 数据库？）") from None
    conn.close()

    imported, skipped = 0, 0
    for row in rows:
        _, name, provider, mode, jwt_token, api_key, enabled = row
        secret = jwt_token if mode == "jwt" else api_key
        if not secret:
            skipped += 1
            continue
        try:
            parsed = parse_credentials({
                "token": secret,
                "provider": provider or "zai",
                "mode": mode or ("jwt" if ztoken.is_jwt(secret) else "apiKey"),
                "name": name or "",
            })
            result = upsert_account(parsed)
            imported += 1 if result else 0
        except ValueError:
            skipped += 1
    return {"imported": imported, "skipped": skipped}


def oauth_account(jwt_token: str, api_key: str | None = None, email: str = "") -> dict:
    """OAuth 登录完成 → 入池（JWT + 回退 Key 双凭证）。"""
    raw = jwt_token.strip()
    user_id = ztoken.jwt_user_id(raw)
    extra = {"mode": "jwt", "provider": "zai", "oauth_email": email}
    if api_key:
        extra["api_key"] = api_key
    display = email or user_id or (raw[:10] + "…")
    parsed = {
        "name": display,
        "provider": "zcode",
        "access_token": raw,
        "uid": user_id or "",
        "nickname": display,
        "status": "active",
        "extra": extra,
    }
    exp = ztoken.jwt_expiry(raw)
    if exp:
        parsed["expires_at"] = int(exp * 1000)
    return upsert_account(parsed)


def discover() -> dict:
    """通道发现：返回本机候选账号（当前支持 zcode2api 数据库导入）。"""
    import glob
    import os

    candidates = []
    patterns = [
        os.path.expanduser("~"),
        r"E:\AiWorkspace\Tools\zocdedemo",
        r"E:\AiWorkspace\Tools",
    ]
    seen = set()
    for base in patterns:
        for db_file in glob.glob(os.path.join(base, "**", "accounts.db"), recursive=True):
            if db_file in seen:
                continue
            seen.add(db_file)
            try:
                conn_count = _count_accounts(db_file)
            except Exception:  # noqa: BLE001
                continue
            candidates.append({
                "path": db_file,
                "channel": "zcode",
                "valid": conn_count > 0,
                "reason": "zcode2api 数据库" if conn_count else "空库",
                "account_name": f"{conn_count} 个账号" if conn_count else "-",
                "uid_masked": "",
                "already_imported": False,
            })
    return {
        "dirs": [],
        "files": candidates,
        "file_count": len(candidates),
        "valid_count": sum(1 for c in candidates if c["valid"]),
        "importable_count": sum(1 for c in candidates if c["valid"]),
    }


def _count_accounts(db_path: str) -> int:
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
    finally:
        conn.close()


def import_discovered(path: str) -> dict:
    result = import_from_zcode2api(path)
    if result["imported"] == 0 and result["skipped"] == 0:
        raise ValueError("该数据库没有可导入的账号")
    return result


def save_fingerprint(account: dict, profile: dict) -> None:
    """持久化账号指纹（identity/fingerprint 共用）。"""
    extra = dict(account.get("extra") or {})
    extra["fingerprint"] = profile
    db.update_account(int(account.get("id") or 0), {"extra": extra})


def update_status(aid: int, status: str, error: str | None = None, **extra_fields) -> None:
    """更新账号状态与错误信息（last_error 存入 extra，matches accounts 表结构）。"""
    patch: dict = {"status": status}
    if error is not None or extra_fields:
        row = db.get_account(aid) or {}
        extra = dict(row.get("extra") or {})
        if error is not None:
            extra["last_error"] = error
        extra.update(extra_fields)
        patch["extra"] = extra
    db.update_account(aid, patch)