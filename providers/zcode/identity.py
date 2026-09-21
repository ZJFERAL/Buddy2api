"""上游身份头仿真 —— 镜像官方 ZCode 客户端对上游携带的 companion 头集合。

每个 LLM/控制面请求携带：
    HTTP-Referer, User-Agent, X-ZCode-App-Version, X-Title, X-ZCode-Agent,
    X-Platform, X-Release-Channel, X-Client-Language, X-Client-Timezone,
    X-Os-Category, X-Os-Version, X-Device-Mid

追踪头（每请求全新 UUID）：
  - start-plan（JWT 通道）：只发 x-request-id / x-zcode-session-type /
    x-zcode-trace-id 三个。**不发** x-query-id / x-session-id（误发触发
    上游 3012「unusual activity」）。
  - coding-plan（API Key 通道）：额外发 x-query-id / x-session-id。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from . import constants

# X-Device-Mid 持久化文件（随 DB 所在目录；首次生成永久复用）
_DEVICE_MID_FILE = Path(os.environ.get(
    "CB_DEVICE_MID_FILE",
    str(Path(__file__).resolve().parent.parent / "codebuddy_gateway.db.device_mid"),
))
_DEVICE_MID: str | None = None

_ASCII_PRINTABLE = re.compile(r"^[\x20-\x7e]+$")


def _clean(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v and _ASCII_PRINTABLE.match(v) else None


def device_mid() -> str:
    """本机设备 ID（官方 telemetry 语义）：首次生成 UUIDv4 后持久化复用。

    billing 全家桶必需 X-Device-Mid，缺失时上游返回 code=3001。
    """
    global _DEVICE_MID
    if _DEVICE_MID:
        return _DEVICE_MID
    try:
        _DEVICE_MID = _DEVICE_MID_FILE.read_text(encoding="utf-8").strip()
        if _DEVICE_MID:
            return _DEVICE_MID
    except OSError:
        pass
    _DEVICE_MID = str(uuid.uuid4())
    try:
        _DEVICE_MID_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEVICE_MID_FILE, "x", encoding="utf-8") as fh:
            fh.write(_DEVICE_MID)
    except OSError:
        pass  # 持久化失败只影响本进程，下次重启会生成新 ID
    return _DEVICE_MID


def build_identity_headers(account: dict | None = None, billing: bool = False) -> dict[str, str]:
    """构建身份头（保持 pio 字段顺序）。

    account 有指纹档案时按档案出值（每账号独立设备形态）；无 account 时
    回退全局伪装常量。billing=True 用 billing 族版本头（Z Code@electron /
    3.11.2），与对话指纹（3.10.2）刻意分离。
    """
    profile = None
    if account is not None:
        from .fingerprint import profile_for

        profile = profile_for(account)

    app_version = constants.BILLING_APP_VERSION if billing else constants.CLIENT_APP_VERSION
    title = constants.BILLING_TITLE if billing else constants.IDENTITY_TITLE

    if profile is not None:
        plat, arch = profile.platform, profile.arch
        release = profile.os_version
        language, timezone = profile.language, profile.timezone
        device_mid_val = profile.device_mid
    else:
        if "-" in constants.CLIENT_PLATFORM:
            plat, arch = constants.CLIENT_PLATFORM.split("-")[0], constants.CLIENT_PLATFORM.split("-")[1]
        else:
            plat, arch = "darwin", "arm64"
        release = constants.IDENTITY_OS_VERSION
        language = constants.IDENTITY_CLIENT_LANGUAGE
        timezone = constants.IDENTITY_CLIENT_TIMEZONE
        device_mid_val = device_mid()

    headers: dict[str, str] = {
        "HTTP-Referer": constants.ZCODE_ORIGIN if billing else constants.HTTP_REFERER,
        "User-Agent": f"ZCode/{app_version}",
    }
    headers["X-ZCode-App-Version"] = app_version
    headers["X-Title"] = title
    headers["X-ZCode-Agent"] = constants.X_ZCODE_AGENT
    headers["X-Platform"] = f"{plat}-{arch}"
    headers["X-Release-Channel"] = constants.BILLING_RELEASE_CHANNEL if billing else constants.IDENTITY_RELEASE_CHANNEL
    headers["X-Client-Language"] = language
    headers["X-Client-Timezone"] = timezone
    if plat:
        headers["X-Os-Category"] = _os_category(plat)
    if release:
        headers["X-Os-Version"] = release
    if device_mid_val:
        headers["X-Device-Mid"] = device_mid_val
    return headers


def build_billing_headers(account: dict) -> dict[str, str]:
    """billing 族请求头（含鉴权与全新 x-request-id）。"""
    headers = build_identity_headers(account, billing=True)
    headers["Content-Type"] = "application/json"
    headers["x-request-id"] = str(uuid.uuid4())
    mode = (account.get("extra") or {}).get("mode") if isinstance(account.get("extra"), dict) else None
    jwt_token = account.get("access_token") or ""
    api_key = ((account.get("extra") or {}).get("api_key") if isinstance(account.get("extra"), dict) else None)
    if mode == "jwt" and jwt_token:
        headers["Authorization"] = f"Bearer {jwt_token}"
    elif api_key:
        headers["x-api-key"] = api_key
    return headers


def build_trace_headers(plan: str = "start-plan") -> dict[str, str]:
    """追踪头：每请求全新 UUID。start-plan（JWT）通道只发三头。"""
    headers = {
        "x-request-id": str(uuid.uuid4()),
        "x-zcode-session-type": "main",
        "x-zcode-trace-id": str(uuid.uuid4()),
    }
    if plan != "start-plan":
        headers["x-query-id"] = str(uuid.uuid4())
        headers["x-session-id"] = str(uuid.uuid4())
    return headers


def _os_category(sys_platform: str) -> str:
    if sys_platform in ("darwin", "macos"):
        return "macos"
    if sys_platform in ("win32", "windows"):
        return "windows"
    return "linux"


def jwt_user_id(jwt_token: str | None) -> str | None:
    """从 JWT payload 解 user_id（sub / user_id 字段）。失败返回 None。"""
    if not jwt_token or jwt_token.count(".") != 2:
        return None
    try:
        payload_b64 = jwt_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        import base64

        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id") or payload.get("sub")
    return str(user_id) if user_id else None