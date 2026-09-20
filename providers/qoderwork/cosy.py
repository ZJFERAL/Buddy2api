"""Qoder native COSY protocol (Qoder CLI private protocol).

Mirrors qodercli2api/native_infer.go + native_body_codec.go. NOTE: unlike
QwenWork's cosy.py, Qoder hands us the RSA-wrapped `key` and AES-wrapped
`encrypt_user_info` ready-made in the local auth file (~/.qoder/.auth/user);
we do NOT generate/encrypt them client-side. We only:
  1. encode the request body with the custom base64 + outer-third swap,
  2. build the COSY payload (base64 of {version, requestId, info, cosyVersion}),
  3. sign md5(payloadB64 + key + unixSeconds + encodedBody + signedPath),
  4. assemble the native headers.

All functions here are pure so they can be unit-tested without network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

# --- endpoint / path constants (qodercli2api/native_infer.go) ---
INFER_HOST = "https://api2.qoder.sh"
NATIVE_INFER_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
NATIVE_INFER_QUERY = "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
COSY_SIGNED_PATH = "/api/v2/service/pro/sse/agent_chat_generation"  # md5 uses this (no /algo)
NATIVE_INFER_URL = INFER_HOST + NATIVE_INFER_PATH + NATIVE_INFER_QUERY

COSY_VERSION = "1.1.34"  # qoderProtocolVersion

# Default scene (qodercli2api defaultProtocolScene)
SCENE_CLIENT_TYPE = "5"
SCENE_BUSINESS_PRODUCT = "cli"
SCENE_BUSINESS_TYPE = "agent"
SCENE_NAME = "assistant"

# Custom base64 alphabet (qodercli2api/native_body_codec.go qoderBodyAlphabet)
BODY_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
BODY_PADDING = "$"
_STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_ALPHA_MAP_ENC = str.maketrans(_STD_ALPHABET, BODY_ALPHABET)
_ALPHA_MAP_DEC = str.maketrans(BODY_ALPHABET, _STD_ALPHABET)


def encode_body(raw: str) -> str:
    """Encode a JSON body the way Qoder's native endpoint expects.

    Custom-base64 (alphabet swapped, padding '$') then outer-third swap:
    out = encoded[len-q:] + encoded[q:len-q] + encoded[:q].
    """
    std = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    custom = std.translate(_ALPHA_MAP_ENC).rstrip("=")
    if len(custom) % 4 != 0:
        custom = custom + BODY_PADDING * (4 - len(custom) % 4)
    q = len(custom) // 3
    return custom[len(custom) - q:] + custom[q:len(custom) - q] + custom[:q]


def decode_body(encoded: str) -> str:
    """Inverse of encode_body (used in tests)."""
    s = encoded
    q = len(s) // 3
    restored = s[len(s) - q:] + s[q:len(s) - q] + s[:q]
    restored = restored.replace(BODY_PADDING, "")
    std = restored.translate(_ALPHA_MAP_DEC)
    pad = (-len(std)) % 4
    if pad:
        std = std + "=" * pad
    return base64.b64decode(std).decode("utf-8")


def build_cosy_payload(request_id: str, encrypt_user_info: str) -> tuple[str, str]:
    """Return (payload_json_str, payload_b64)."""
    payload = {
        "version": "v1",
        "requestId": request_id,
        "info": encrypt_user_info,
        "cosyVersion": COSY_VERSION,
        "ideVersion": "",
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return raw, base64.b64encode(raw.encode("utf-8")).decode("ascii")


def sign_cosy(payload_b64: str, key: str, unix_seconds: str, encoded_body: str) -> str:
    """md5(payloadB64 + key + unixSeconds + encodedBody + signedPath)."""
    sign_str = "\n".join([payload_b64, key, unix_seconds, encoded_body, COSY_SIGNED_PATH])
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def build_native_headers(
    *,
    encrypt_user_info: str,
    key: str,
    uid: str,
    machine_id: str,
    encoded_body: str,
    model_key: str,
    model_source: str = "system",
    data_policy_agreed: bool = True,
    organization_id: str = "",
    organization_tags: list[str] | None = None,
) -> dict[str, str]:
    request_id = uuid.uuid4().hex
    unix_seconds = str(int(time.time()))
    payload_raw, payload_b64 = build_cosy_payload(request_id, encrypt_user_info)
    signature = sign_cosy(payload_b64, key, unix_seconds, encoded_body)
    authorization = "Bearer COSY." + payload_b64 + "." + signature

    policy = "agree" if data_policy_agreed else "disagree"
    headers: dict[str, str] = {
        "Accept": "text/event-stream",
        "Authorization": authorization,
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
        "Cosy-Business-Product": SCENE_BUSINESS_PRODUCT,
        "Cosy-Business-Type": SCENE_BUSINESS_TYPE,
        "Cosy-ClientType": SCENE_CLIENT_TYPE,
        "Cosy-Data-Policy": policy,
        "Cosy-Date": unix_seconds,
        "Cosy-Key": key,
        "Cosy-MachineId": machine_id,
        "Cosy-MachineToken": machine_id,
        "Cosy-MachineType": "5",
        "Cosy-Scene": SCENE_NAME,
        "Cosy-User": uid,
        "Cosy-Version": COSY_VERSION,
        "Login-Version": "v2",
    }
    if organization_id:
        headers["Cosy-Organization-Id"] = organization_id
    if organization_tags:
        headers["Cosy-Organization-Tags"] = ",".join(organization_tags)
    headers["X-Model-Key"] = model_key
    headers["X-Model-Source"] = model_source
    return headers
