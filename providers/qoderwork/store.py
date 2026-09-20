"""Qoder account storage: parse / discover / import / upsert.

The Qoder credential is the shared `~/.qoder/.auth` directory. Importing reads
and decrypts it once, storing the resulting identity in Buddy2API's account DB
(keyed by uid). `refresh` (in token.py) re-reads the file on demand.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from providers.qoderwork.constants import AUTH_DIR, CHANNEL_ID
from providers.qoderwork.token import QoderAuthError, load_local_session


def session_to_account(sess: dict, source: str = "", auth_dir: Path | str | None = None) -> dict:
    """Build an account row from a decrypted local session.

    `auth_dir` must be the directory the session was actually read from: it is
    persisted in `extra.auth_dir` and drives `token.refresh_account()`, so a
    wrong value makes every account refresh into the same (default) login.
    """
    uid = sess.get("uid") or ""
    name = sess.get("name") or ""
    token = sess.get("security_oauth_token") or ""
    # Qoder's expire_time is already in Unix *seconds* (e.g. 1792421720);
    # dividing by 1000 here produced 1970-era values and made every account
    # look permanently expired.
    expires_at = int(sess["expire_time"]) if sess.get("expire_time") else 0
    return {
        "name": name or f"qoder-{(uid or 'user')[:8]}",
        "uid": uid,
        "nickname": name,
        "access_token": token,
        "refresh_token": sess.get("refresh_token") or "",
        "expires_at": expires_at,
        "refresh_expires_at": 0,
        "provider": CHANNEL_ID,
        "domain": "api2-v2.qoder.sh",
        "extra": {
            "auth_dir": str(auth_dir or AUTH_DIR),
            "login_method": "qoder_cli_local_session",
            "source": source or "import",
            "security_oauth_token": token,
            "cosy_key": sess.get("key") or "",
            "encrypt_user_info": sess.get("encrypt_user_info") or "",
            "machine_id": sess.get("machine_id") or "",
            "data_policy_agreed": sess.get("data_policy_agreed") or False,
        },
        "status": "active",
    }


def parse_credentials(body: dict) -> dict:
    """Accept either a pointer to the local auth dir/file or a pasted token."""
    if not isinstance(body, dict):
        raise ValueError("Qoder credentials must be a JSON object")
    auth_dir = body.get("auth_dir") or body.get("path") or ""
    if auth_dir:
        ad = Path(auth_dir)
        try:
            sess = load_local_session(ad if ad.is_dir() else ad.parent)
        except QoderAuthError as exc:
            raise ValueError(str(exc)) from exc
        resolved = ad if ad.is_dir() else ad.parent
        return session_to_account(sess, source="paste", auth_dir=resolved)
    token = body.get("security_oauth_token") or body.get("access_token") or ""
    if token:
        # COSY native-route credentials are normally sourced from the local
        # auth dir. If the caller pastes the decrypted user blob directly we
        # capture them here so native models (qfmodel, etc.) work without a
        # later refresh; otherwise the native route stays disabled until the
        # next auth-dir refresh.
        cosy_key = body.get("key") or body.get("cosy_key") or ""
        eui = body.get("encrypt_user_info") or ""
        machine_id = body.get("machine_id") or ""
        data_policy = body.get("data_policy_agreed") or False
        extra = {
            # No local auth dir to refresh from: keep it empty so
            # token.refresh_account() refuses instead of silently pulling the
            # default login's token over this pasted account.
            "auth_dir": "",
            "login_method": "paste",
            "security_oauth_token": token,
        }
        if cosy_key:
            extra["cosy_key"] = cosy_key
        if eui:
            extra["encrypt_user_info"] = eui
        if machine_id:
            extra["machine_id"] = machine_id
        if data_policy:
            extra["data_policy_agreed"] = bool(data_policy)
        return {
            "name": body.get("name") or "qoder-paste",
            "uid": body.get("uid") or "",
            "nickname": body.get("name") or "",
            "access_token": token,
            "refresh_token": body.get("refresh_token") or "",
            "expires_at": 0,
            "refresh_expires_at": 0,
            "provider": CHANNEL_ID,
            "domain": "api2-v2.qoder.sh",
            "extra": extra,
            "status": "active",
        }
    raise ValueError("Qoder credentials need auth_dir/path or security_oauth_token")


def candidate_auth_dirs(auth_dir: str | None = None) -> list[Path]:
    """Auth directories to scan.

    Priority: explicit argument > CB_QODER_AUTH_DIRS (os.pathsep separated, for
    multiple per-account auth copies) > CB_QODER_AUTH_DIR > default ~/.qoder/.auth.
    """
    if auth_dir:
        return [Path(auth_dir).expanduser()]
    multi = os.environ.get("CB_QODER_AUTH_DIRS") or ""
    dirs = [Path(part).expanduser() for part in multi.split(os.pathsep) if part.strip()]
    if not dirs:
        dirs = [AUTH_DIR]
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def discover(auth_dir: str | None = None) -> dict:
    """Scan every configured auth dir so multiple Qoder logins can be imported."""
    import database as db

    existing = {
        str(row.get("uid"))
        for row in db.list_accounts(provider=CHANNEL_ID)
        if row.get("uid")
    }
    files = []
    dirs = []
    for ad in candidate_auth_dirs(auth_dir):
        valid = False
        reason = ""
        account_name = ""
        uid = ""
        try:
            sess = load_local_session(ad)
            valid = bool(sess.get("security_oauth_token"))
            account_name = sess.get("name") or "qoder"
            uid = str(sess.get("uid") or "")
        except Exception as exc:  # noqa: BLE001 - surface reason to admin UI
            reason = str(exc)[:160]
        uid_masked = (uid[:6] + "…") if len(uid) > 6 else uid
        files.append(
            {
                "channel": CHANNEL_ID,
                "path": str(ad),
                "valid": valid,
                "reason": reason,
                "account_name": account_name,
                "uid_masked": uid_masked,
                "already_imported": bool(uid and uid in existing),
            }
        )
        dirs.append({"path": str(ad), "exists": ad.is_dir(), "file_count": 1 if valid else 0})
    return {
        "dirs": dirs,
        "files": files,
        "file_count": len(files),
        "valid_count": sum(1 for f in files if f.get("valid")),
        "importable_count": sum(1 for f in files if f.get("valid") and not f.get("already_imported")),
        "channel": CHANNEL_ID,
    }


def _resolve_auth_dir(path: str) -> Path:
    target = Path(path)
    if target.is_dir():
        return target
    # Allow pointing directly at the user/machine_id file.
    return target.parent if target.name in {"user", "machine_id"} else target


def _load_from_path(path: str) -> dict:
    return load_local_session(_resolve_auth_dir(path))


def import_discovered(path: str) -> dict:
    ad = _resolve_auth_dir(path)
    try:
        sess = load_local_session(ad)
    except QoderAuthError as exc:
        raise ValueError(str(exc)) from exc
    return session_to_account(sess, source=path, auth_dir=ad)


def import_path(path: str) -> dict:
    return import_discovered(path)


def upsert_account(parsed: dict) -> dict:
    import database as db

    uid = str(parsed.get("uid") or "")
    if uid:
        for row in db.list_accounts(provider=CHANNEL_ID):
            if str(row.get("uid") or "") == uid:
                patch = {
                    "access_token": parsed.get("access_token") or "",
                    "refresh_token": parsed.get("refresh_token") or "",
                    "expires_at": parsed.get("expires_at") or 0,
                    "nickname": parsed.get("nickname") or row.get("nickname") or "",
                    "name": parsed.get("name") or row.get("name") or "",
                    "extra": parsed.get("extra") or row.get("extra") or {},
                    "status": "active",
                }
                db.update_account(row["id"], patch)
                return {"id": row["id"], "updated": True}
    aid = db.add_account(parsed)
    return {"id": aid, "updated": False}
