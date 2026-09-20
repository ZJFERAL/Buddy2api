"""Qoder multi-account management helpers (snapshot capture / adopt).

Qoder keeps exactly ONE global login at ~/.qoder/.auth, so holding several
accounts means keeping a full copy (user + machine_id) of each login under
<repo>/.qoder-auth/<name>/.auth. The AES key is machine_id[:16], therefore both
files must always be copied together.

These helpers back the admin endpoints so the whole "re-login then renew"
cycle is a single click in the web UI.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from providers.qoderwork.constants import AUTH_DIR, CHANNEL_ID
from providers.qoderwork.token import QoderAuthError, load_local_session

REPO_ROOT = Path(__file__).resolve().parents[2]
# Per-account auth copies live next to the Qoder home, NOT inside the repo, so
# credentials can never be picked up by git even if .gitignore is bypassed.
# `snapshots\` (not `accounts\`) keeps clear of any Qoder-CLI-owned subtree.
SNAPSHOT_ROOT = Path.home() / ".qoder" / "snapshots"
GLOBAL_AUTH_DIR = AUTH_DIR
_COPIED_FILES = ("user", "machine_id")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


class QoderManageError(Exception):
    """Raised for operator-facing failures (bad name, uid mismatch, ...)."""


def _extra(account: dict) -> dict:
    extra = account.get("extra")
    if isinstance(extra, dict):
        return extra
    if isinstance(extra, str):
        try:
            return json.loads(extra)
        except ValueError:
            return {}
    return {}


def current_login() -> dict:
    """Identity currently logged in with the Qoder CLI (global auth dir)."""
    try:
        sess = load_local_session(GLOBAL_AUTH_DIR)
    except QoderAuthError as exc:
        return {"ok": False, "path": str(GLOBAL_AUTH_DIR), "reason": str(exc)}
    return {
        "ok": True,
        "path": str(GLOBAL_AUTH_DIR),
        "uid": str(sess.get("uid") or ""),
        "name": str(sess.get("name") or ""),
        "expires_at": int(sess.get("expire_time") or 0),
    }


def list_accounts() -> list[dict]:
    import database as db

    now = time.time()
    rows = []
    for row in db.list_accounts(provider=CHANNEL_ID):
        extra = _extra(row)
        auth_dir = str(extra.get("auth_dir") or "")
        exp = int(row.get("expires_at") or 0)
        remaining_h = (exp - now) / 3600 if exp else None
        rows.append(
            {
                "id": row.get("id"),
                "name": row.get("name") or row.get("nickname") or "",
                "uid": str(row.get("uid") or ""),
                "status": row.get("status") or "",
                "auth_dir": auth_dir,
                "is_global": bool(auth_dir) and _same_path(auth_dir, GLOBAL_AUTH_DIR),
                "snapshot_exists": bool(auth_dir) and Path(auth_dir).is_dir(),
                "expires_at": exp or None,
                "remaining_hours": round(remaining_h, 1) if remaining_h is not None else None,
                "expired": bool(exp) and exp <= now,
                "expiring_soon": bool(exp) and 0 < exp - now < 3 * 24 * 3600,
            }
        )
    return rows


def _same_path(a: str, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _snapshot_dir_for(name: str) -> Path:
    if not _SAFE_NAME.match(name or ""):
        raise QoderManageError(
            "名称只允许字母/数字/下划线/连字符（1-40 字符，首字符非符号）"
        )
    return SNAPSHOT_ROOT / name / ".auth"


def _copy_auth_dir(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for f in _COPIED_FILES:
        src_file = src / f
        if not src_file.is_file():
            raise QoderManageError(f"登录文件不完整，缺少 {f}：{src}")
        shutil.copy2(src_file, dest / f)


def capture(account_id: int | None = None, name: str | None = None) -> dict:
    """Copy the current Qoder CLI login into an account's auth dir and refresh it.

    - account_id given: overwrite that account's existing auth dir (uid must match).
    - name given: create/overwrite <repo>/.qoder-auth/<name>/.auth and import it.
    """
    import database as db
    from providers.qoderwork import store

    login = current_login()
    if not login.get("ok"):
        raise QoderManageError(
            "未检测到 Qoder 登录：%s" % login.get("reason", "请先运行 Qoder CLI 登录")
        )

    target_row = None
    if account_id is not None:
        target_row = db.get_account(int(account_id))
        if not target_row or str(target_row.get("provider") or "") != CHANNEL_ID:
            raise QoderManageError(f"账号 {account_id} 不是 Qoder 账号")
        auth_dir = Path(str(_extra(target_row).get("auth_dir") or ""))
        if not str(auth_dir):
            raise QoderManageError(
                "该账号没有 auth_dir（可能是粘贴 token 导入），请用「纳管为新账号」重建"
            )
        if str(target_row.get("uid") or "") != login["uid"]:
            raise QoderManageError(
                "当前 Qoder 登录的是「%s」(uid %s)，与目标账号「%s」(uid %s) 不一致，"
                "请先在 Qoder CLI 登录目标账号"
                % (
                    login.get("name") or "?",
                    login["uid"][:8],
                    target_row.get("name") or "?",
                    str(target_row.get("uid") or "")[:8],
                )
            )
    else:
        if not name:
            raise QoderManageError("需要 account_id 或 name")
        # "adopt" means bringing in an account that is not managed yet. If the
        # current login already exists, renewing it via capture is the right
        # action - silently moving it to a snapshot would drop the CLI's own
        # auto-refresh (which only touches the global dir).
        for row in db.list_accounts(provider=CHANNEL_ID):
            if str(row.get("uid") or "") == login["uid"]:
                raise QoderManageError(
                    "当前登录的「%s」已在列表中（id=%s，目录 %s），续期请点该行的「捕获当前登录」"
                    % (
                        login.get("name") or row.get("name") or "?",
                        row.get("id"),
                        _extra(row).get("auth_dir") or "-",
                    )
                )
        auth_dir = _snapshot_dir_for(name)

    # An account may point at the global dir itself (Qoder keeps it fresh on its
    # own); copying a file onto itself raises SameFileError, so just re-read it.
    if not _same_path(str(auth_dir), GLOBAL_AUTH_DIR):
        _copy_auth_dir(GLOBAL_AUTH_DIR, auth_dir)
    parsed = store.import_discovered(str(auth_dir))
    info = store.upsert_account(parsed)
    return {
        "ok": True,
        "action": "updated" if info.get("updated") else "imported",
        "account_id": info.get("id"),
        "uid": parsed.get("uid"),
        "name": parsed.get("name"),
        "auth_dir": str(auth_dir),
        "expires_at": parsed.get("expires_at"),
    }
