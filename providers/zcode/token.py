"""zcode 账号凭据工具：JWT / API Key 解析与判定。"""

from __future__ import annotations

import base64
import json
import time


def jwt_user_id(jwt_token: str | None) -> str | None:
    """从 JWT payload 解 user_id（sub / user_id 字段）。"""
    if not jwt_token or jwt_token.count(".") != 2:
        return None
    try:
        payload_b64 = jwt_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id") or payload.get("sub")
    return str(user_id) if user_id else None


def jwt_expiry(jwt_token: str | None) -> int | None:
    """JWT 过期时间（epoch 秒）；无法解析返回 None。"""
    if not jwt_token or jwt_token.count(".") != 2:
        return None
    try:
        payload_b64 = jwt_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    try:
        return int(exp)
    except (TypeError, ValueError):
        return None


def is_jwt(value: str) -> bool:
    """三段点分即视为 JWT（zai 账号）。"""
    return bool(value) and value.count(".") == 2


def normalize_api_key(value: str) -> str:
    """API Key 归一：去空白；兼容 'id.secret' 与 'sk-*' 形态。"""
    v = (value or "").strip()
    if v.startswith("Bearer "):
        v = v[len("Bearer "):].strip()
    return v


def is_token_expired(account: dict) -> bool:
    """JWT 是否已过期（剩余 < 5 分钟视为过期，触发刷新流程）。"""
    extra = account.get("extra") or {}
    if extra.get("mode") != "jwt":
        return False
    token = account.get("access_token") or ""
    exp = jwt_expiry(token)
    if exp is None:
        return False
    return exp - time.time() < 300


def account_mode(account: dict) -> str:
    """账号模式：jwt / apiKey。"""
    extra = account.get("extra")
    if isinstance(extra, dict) and extra.get("mode"):
        return str(extra["mode"])
    token = account.get("access_token") or ""
    return "jwt" if is_jwt(token) else "apiKey"


def secret_for(account: dict) -> str | None:
    """账号的有效秘密值：jwt → access_token；apiKey → extra.api_key。"""
    mode = account_mode(account)
    if mode == "jwt":
        return account.get("access_token") or None
    extra = account.get("extra") or {}
    return extra.get("api_key") or None