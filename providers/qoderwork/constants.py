"""Qoder / Qoder CLI provider constants.

New (current) Qoder protocol = OpenAI-compatible endpoint with a plain
`Bearer <security_oauth_token>` header. No COSY signature required.

The local credential lives at `~/.qoder/.auth` (maintained by the official
Qoder CLI; also consumed by qodercli2api and QoderGateway). It is encrypted
with AES-128-CBC using key/IV = machine_id[:16] (UTF-8), standard base64.
"""

from __future__ import annotations

import os
from pathlib import Path

CHANNEL_ID = "qoderwork"
DISPLAY_NAME = "Qoder / Qoder CLI"

# Verified 2026-09-19: OpenAI-compatible, pure Bearer, no COSY.
CHAT_URL = "https://api2-v2.qoder.sh/model/v1/chat/completions"
USER_AGENT = "qoder/1.1.16"

# Server-side token refresh (verified live 2026-09-20). Same device-token
# protocol family as QwenWork: POST {"refresh_token": ...} -> new device_token
# + rotated refresh_token. No Authorization / machine fingerprint required.
# Response carries RFC3339 `expires_at` / `refresh_token_expires_at`
# (access ~30 days, refresh ~360 days), not `expires_in`.
OPENAPI_BASE = "https://openapi.qoder.sh"
REFRESH_PATH = "/api/v1/deviceToken/refresh"

# Local shared credential directory (single sign-on bridge for qodercli2api /
# QoderGateway / Buddy2api). Override with CB_QODER_AUTH_DIR if needed.
def default_auth_dir() -> Path:
    override = (os.environ.get("CB_QODER_AUTH_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".qoder" / ".auth"


AUTH_DIR = default_auth_dir()


def aes_key_from_machine_id(machine_id: str) -> bytes:
    """AES key/IV = first 16 bytes of the machine_id string (UTF-8)."""
    return machine_id[:16].encode("ascii")


# Models: authoritative list recovered 2026-09-19 from the live Qoder model
# catalog at ~/.qoder/.models/<uid>/catalog-v6 (group "chat", 17 entries).
# This is the real server-side slug set (decrypted from the CLI's encrypted
# model cache), so these keys are stable and routable. The four built-ins
# (auto/ultimate/performance/lite) were also confirmed live; the rest come
# from the catalog and are probed in tests. `auto` is a real key.
STATIC_MODELS = (
    "lite",
    "auto",
    "performance",
    "ultimate",
    # Qwen family
    "qmodel_38max",   # Qwen3.8-Max
    "qfmodel",        # Qwen3.8-Flash
    "qmodel_latest",  # Qwen3.7-Max
    "qmodel",         # Qwen3.7-Plus
    # others (kept for completeness; see catalog)
    "smodel",         # Sonus
    "cmodel",         # Cantus
    "kmodel_latest",  # Kimi-K3
    "kmodel",         # Kimi-K2.8-Preview
    "gmodel",         # GLM-5.3
    "gfmodel",        # GLM-5.3-Flash
    "dmodel",         # DeepSeek-V4-Pro
    "dfmodel",        # DeepSeek-Flash
    "mmodel",         # MiniMax-M3
    "efficient",      # Efficient
)

# Human-readable display names for the server-side slugs. Surfaced in
# GET /v1/models and the admin catalog so clients see "Qwen3.8-Flash"
# instead of the bare slug "qfmodel". The slug itself stays the routing id.
MODEL_DISPLAY = {
    "lite": "Lite",
    "auto": "Auto",
    "performance": "Performance",
    "ultimate": "Ultimate",
    "qmodel_38max": "Qwen3.8-Max",
    "qfmodel": "Qwen3.8-Flash",
    "qmodel_latest": "Qwen3.7-Max",
    "qmodel": "Qwen3.7-Plus",
    "smodel": "Sonus",
    "cmodel": "Cantus",
    "kmodel_latest": "Kimi-K3",
    "kmodel": "Kimi-K2.8-Preview",
    "gmodel": "GLM-5.3",
    "gfmodel": "GLM-5.3-Flash",
    "dmodel": "DeepSeek-V4-Pro",
    "dfmodel": "DeepSeek-Flash",
    "mmodel": "MiniMax-M3",
    "efficient": "Efficient",
}

# Client-side convenience aliases (translated locally before sending upstream).
# Display names (lowercased) are all accepted as call-time model ids, so
# clients can pass "Qwen3.8-Flash" / "qoderwork/Qwen3.8-Flash" directly.
ALIASES = {
    "qoder-lite": "lite",
    "qoder-auto": "auto",
    "qoder-performance": "performance",
    "qoder-ultimate": "ultimate",
    # built-in display names
    "lite": "lite",
    "auto": "auto",
    "performance": "performance",
    "ultimate": "ultimate",
    # premium model display names -> slugs
    "qwen3.8-flash": "qfmodel",
    "qwen3.8-max": "qmodel_38max",
    "qwen3.7-max": "qmodel_latest",
    "qwen3.7-plus": "qmodel",
    "sonus": "smodel",
    "cantus": "cmodel",
    "kimi-k3": "kmodel_latest",
    "kimi-k2.8-preview": "kmodel",
    "glm-5.3": "gmodel",
    "glm-5.3-flash": "gfmodel",
    "deepseek-v4-pro": "dmodel",
    "deepseek-flash": "dfmodel",
    "minimax-m3": "mmodel",
    "efficient": "efficient",
}

# Status codes worth retrying on (mirrors qwenwork).
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Route selection: the four built-ins are served by the OpenAI-compatible
# endpoint (plain Bearer); every other model requires the native COSY-signed
# endpoint (where Qwen3.8-Flash / Qwen3.8-Max / GLM / DeepSeek / etc. live).
OPENAI_MODELS = ("lite", "auto", "performance", "ultimate")


def use_native_route(model: str) -> bool:
    """True => send over the native COSY-signed endpoint (advanced models)."""
    from providers.qoderwork.chat import translate_model

    inner = translate_model((model or "lite").strip() or "lite")
    return inner not in OPENAI_MODELS


# When a model isn't in the built-in set, it goes through COSY.
NATIVE_MODELS = tuple(m for m in STATIC_MODELS if m not in OPENAI_MODELS)
