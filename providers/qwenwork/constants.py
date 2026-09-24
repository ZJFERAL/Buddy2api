"""Official QwenWorkCN 1.0.4-26090412 protocol constants.

RSA PEM was extracted from the official desktop asar
(`resources/app.asar`, `RSA_PUBLIC_KEY` used by generateAuthToken).
Cosy-Version was frozen from bundled qoderclicn 1.0.4 (`mm = "1.1.32"`).
Chat infer on 1.0.4 is prepared by official WASM `prepareInferRequest`.

``OPENAPI_BASE`` / ``REALM_GATEWAY`` / ``MODEL_SERVER_PATH`` below are CN hosts
recorded from the client's endpoint cache (``endpoint-cache.json``) for a future
protocol iteration. They are diagnostic-only (see ``scripts/qwenwork_probe.py``);
the live chat path stays on ``GATEWAY``. The 2026-09-21 ``503 Model catalog
unavailable`` outage on that path was a body-field bug -- the gateway reads
``business.product`` / ``business.type`` from the request body -- fixed in
v2.1.15, not a dead transport.
"""

from __future__ import annotations

CHANNEL_ID = "qwenwork"
DISPLAY_NAME = "QwenWork / 千问办公"

# Legacy SSE gateway. Token realm for imported credentials: tokens issued for
# this host are accepted here and nowhere else (see REALM_GATEWAY).
GATEWAY = "https://gateway.qwenwork.cn"
CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
CHAT_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common"
REFRESH_PATH = "/api/v1/deviceToken/refresh"
ACCOUNT_CONTEXT_PATH = "/api/v1/adapter/user/account-context"
MODELS_PATH = "/api/v2/model/list"

# CN OpenAPI host advertised by the endpoint cache. Serves the new
# `/algo/api/v3/*` surface with plain OAuth Bearer and encrypted responses
# (decrypted by the bundled qoder-auth-wasm `decrypt_server_response`).
OPENAPI_BASE = "https://openapi.qoder.com.cn"
# CN gateway reported by the same cache. Different token realm from GATEWAY.
REALM_GATEWAY = "https://gateway.qoder.com.cn"
# New model server used by the bundled CLI (global). CN counterpart is derived
# by the client via QODER_API_DOMAIN_SUFFIX and is not publicly resolvable.
MODEL_SERVER_PATH = "/model/v1/chat/completions"

IDE_VERSION = "1.0.4"
RELEASE_VERSION = "1.0.4-26090412"
BUILD = "26090412"
COSY_VERSION = "1.1.32"
COSY_VERSION_FROZEN = True
CLIENT_TYPE = "6"
BUSINESS_PRODUCT = "qoder_work"
BUSINESS_TYPE = "agent"
SCENE = "qwork"
MACHINE_OS = "x86_64_win32"
MACHINE_TYPE = "5"
LOGIN_VERSION = "v2"
USER_AGENT = "qoderwork/1.0.4"
DATA_POLICY = "disagree"

# Official 0.1.8 asar generateAuthToken public key (PKCS#1 v1.5).
RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

# Official 1.0.4 desktop enum: STANDARD=qwork-auto, ADVANCED=qwork-advanced,
# PREMIUM=qwork-ultimate, LITE=qwork-lite (retired 2026-09-18).
STATIC_MODELS = (
    "qwork-auto",
    "qwork-advanced",
    "qwork-ultimate",
    "qwork-lite",
    "qmodel_latest",
    "qwen3.8-max-preview",
    "pro",
    "flash",
)

ALIASES = {
    "auto": "qwork-advanced",
    "pro": "qwork-advanced",
    "advanced": "qwork-advanced",
    "qwork-advanced": "qwork-advanced",
    "flash": "qwork-auto",
    "standard": "qwork-auto",
    "qwork-auto": "qwork-auto",
    "ultimate": "qwork-ultimate",
    "premium": "qwork-ultimate",
    "flagship": "qwork-ultimate",
    "qwork-ultimate": "qwork-ultimate",
    "lite": "qwork-lite",
    "qwork-lite": "qwork-lite",
}

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
