"""Qoder local session decryption + token refresh.

Reads `~/.qoder/.auth/{user,machine_id}`, decrypts `user` with the
machine_id-derived AES key, and exposes the security_oauth_token used as the
Bearer credential for the new OpenAI-compatible endpoint.

`refresh_account` re-reads the local file because the official Qoder CLI keeps
it fresh; this avoids reimplementing Qoder's OAuth refresh flow.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from providers.qoderwork.constants import (
    AUTH_DIR,
    OPENAPI_BASE,
    REFRESH_PATH,
    USER_AGENT,
    aes_key_from_machine_id,
)


class QoderAuthError(Exception):
    """Local Qoder auth missing or undecryptable."""


def _decrypt_user(auth_dir: Path) -> dict:
    mid_path = auth_dir / "machine_id"
    user_path = auth_dir / "user"
    if not mid_path.is_file() or not user_path.is_file():
        raise QoderAuthError(
            "Qoder local auth not found at %s; log in with the Qoder CLI first" % auth_dir
        )
    machine_id = mid_path.read_text(encoding="utf-8").strip()
    key = aes_key_from_machine_id(machine_id)
    cipher_bytes = base64.b64decode(user_path.read_text(encoding="utf-8").strip())
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    padded = decryptor.update(cipher_bytes) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    try:
        return json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise QoderAuthError("Qoder auth file is not valid JSON after decryption") from exc


def load_local_session(auth_dir: Path | None = None) -> dict[str, Any]:
    data = _decrypt_user(auth_dir or AUTH_DIR)
    token = data.get("security_oauth_token") or data.get("access_token") or ""
    if not token:
        raise QoderAuthError("No security_oauth_token found in local Qoder auth")
    ad = auth_dir or AUTH_DIR
    machine_id = ""
    mid_path = ad / "machine_id"
    if mid_path.is_file():
        machine_id = mid_path.read_text(encoding="utf-8").strip()
    # COSY native-route fields are handed to us ready-made in the local auth
    # file (no client-side RSA/AES needed, unlike QwenWork's cosy.py).
    return {
        "uid": str(data.get("uid") or ""),
        "name": str(data.get("name") or ""),
        "security_oauth_token": token,
        "refresh_token": str(data.get("refresh_token") or ""),
        "expire_time": data.get("expire_time") or 0,
        "encrypt_user_info": str(data.get("encrypt_user_info") or ""),
        "key": str(data.get("key") or ""),
        "data_policy_agreed": bool(data.get("data_policy_agreed") or False),
        "machine_id": machine_id,
    }


def is_token_expired(account: dict, skew_s: int = 300) -> bool:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    # DB rows carry `expires_at` (epoch seconds); raw CLI sessions use `expire_time`.
    exp = (
        account.get("expires_at")
        or account.get("expire_time")
        or extra.get("expires_at")
        or extra.get("expire_time")
        or 0
    )
    if not exp:
        # No expiry info available -> treat as fresh; rely on refresh-on-failure.
        return False
    now = time.time()
    exp_s = exp / 1000 if exp > 10_000_000_000 else exp
    # Renew 5 minutes early so requests never race the exact expiry instant.
    return now >= exp_s - skew_s


def _iso_to_epoch(value) -> int:
    """Parse Qoder's RFC3339 expiry strings ("2026-10-19T16:05:24Z") -> unix seconds."""
    text = str(value or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


async def _refresh_via_api(refresh_token: str) -> dict:
    """Server-side refresh: POST {refresh_token} -> new device_token (+ rotated refresh).

    Verified live 2026-09-20. Raises QoderAuthError on any failure.
    """
    if not refresh_token:
        raise QoderAuthError("Qoder account has no refresh_token")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                OPENAPI_BASE + REFRESH_PATH,
                headers=headers,
                json={"refresh_token": refresh_token},
            )
    except httpx.HTTPError as exc:
        raise QoderAuthError("Qoder refresh network error: %s" % exc) from exc
    if response.status_code >= 400:
        raise QoderAuthError(
            "Qoder refresh failed: HTTP %d %s"
            % (response.status_code, response.text[:160])
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise QoderAuthError("Qoder refresh returned non-JSON") from exc
    if not isinstance(data, dict):
        raise QoderAuthError("Qoder refresh returned non-object")
    token = str(data.get("device_token") or data.get("token") or "")
    if not token:
        raise QoderAuthError("Qoder refresh response missing device_token")
    new_refresh = str(data.get("refresh_token") or "") or refresh_token
    # Qoder returns RFC3339 timestamps (not expires_in).
    expires_at = _iso_to_epoch(data.get("expires_at"))
    refresh_expires_at = _iso_to_epoch(data.get("refresh_token_expires_at"))
    if not expires_at and isinstance(data.get("expires_in"), (int, float)):
        expires_at = int(time.time() + float(data["expires_in"]))
    if not refresh_expires_at and isinstance(data.get("refresh_token_expires_in"), (int, float)):
        refresh_expires_at = int(time.time() + float(data["refresh_token_expires_in"]))
    return {
        "access_token": token,
        "refresh_token": new_refresh,
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
    }


async def refresh_account(account: dict) -> dict:
    """Refresh via Qoder's server API, falling back to re-reading the local auth file.

    Server refresh keeps snapshot accounts alive without the Qoder CLI: the
    refresh_token rotates and stays valid ~360 days. COSY fields
    (cosy_key / encrypt_user_info) are independent of the access token, so the
    native (advanced-model) route keeps working after a refresh.
    """
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    aid = account.get("id")
    api_error = ""

    # 1) Server-side refresh (preferred).
    refresh = str(account.get("refresh_token") or "")
    if refresh:
        try:
            renewed = await _refresh_via_api(refresh)
        except QoderAuthError as exc:
            renewed = None
            api_error = str(exc)
        else:
            api_error = ""
        if renewed:
            patched_extra = dict(extra)
            patched_extra["security_oauth_token"] = renewed["access_token"]
            result = {
                **account,
                "access_token": renewed["access_token"],
                "refresh_token": renewed["refresh_token"],
                "expires_at": renewed["expires_at"],
                "refresh_expires_at": renewed["refresh_expires_at"],
                "status": "active",
                "extra": patched_extra,
            }
            if aid:
                try:
                    import database as db

                    db.update_account(
                        int(aid),
                        {
                            "access_token": renewed["access_token"],
                            "refresh_token": renewed["refresh_token"],
                            "expires_at": renewed["expires_at"],
                            "refresh_expires_at": renewed["refresh_expires_at"],
                            "status": "active",
                            "extra": patched_extra,
                        },
                    )
                except Exception:  # noqa: BLE001 - in-memory result stays valid
                    pass
            return result

    # 2) Fallback: re-read the local auth file (kept fresh by the Qoder CLI).
    auth_dir = extra.get("auth_dir")
    if not auth_dir:
        if refresh and api_error:
            raise QoderAuthError("Qoder refresh failed and no auth_dir to fall back on: %s" % api_error)
        # Pasted credentials have no local file to re-read; refreshing from the
        # default dir would overwrite this account with another login's token.
        raise QoderAuthError(
            "Qoder account has no auth_dir; re-import it from its auth directory"
        )
    ad = Path(auth_dir)
    try:
        sess = load_local_session(ad)
    except QoderAuthError as exc:
        raise QoderAuthError("Qoder local auth refresh failed: %s" % exc) from exc
    expires_at = int(sess["expire_time"]) if sess.get("expire_time") else 0
    patched_extra = dict(extra)
    patched_extra["security_oauth_token"] = sess["security_oauth_token"]
    patched_extra["auth_dir"] = str(ad)
    patched_extra["cosy_key"] = sess["key"]
    patched_extra["encrypt_user_info"] = sess["encrypt_user_info"]
    patched_extra["machine_id"] = sess["machine_id"]
    patched_extra["data_policy_agreed"] = sess["data_policy_agreed"]
    result = {
        **account,
        "access_token": sess["security_oauth_token"],
        "refresh_token": sess["refresh_token"],
        "expires_at": expires_at,
        "refresh_expires_at": 0,
        "status": "active",
        "extra": patched_extra,
    }
    if aid:
        try:
            import database as db

            db.update_account(
                int(aid),
                {
                    "access_token": sess["security_oauth_token"],
                    "refresh_token": sess["refresh_token"],
                    "expires_at": expires_at,
                    "status": "active",
                    "extra": patched_extra,
                },
            )
        except Exception:  # noqa: BLE001 - in-memory result stays valid
            pass
    return result
