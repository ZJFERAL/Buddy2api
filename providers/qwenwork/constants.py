"""Official QwenWorkCN client protocol constants.

RSA PEM was extracted from the official desktop asar
(`resources/app.asar`, `RSA_PUBLIC_KEY` used by generateAuthToken).
Cosy-Version was frozen from bundled qoderclicn (`l0A = "1.1.18"`).

Client identity tracks the *installed* desktop build. QwenWorkCN 1.1.0-26091701
(2026-09-21) is what the client sends today; claiming 0.1.8 is untruthful and
risks version gating upstream.

MIGRATION NOTE (2026-09-21)
---------------------------
The desktop client moved its chat transport off the legacy HTTP+SSE endpoint:

  * legacy  : COSY-signed POST ``/algo/api/v2/service/pro/sse/agent_chat_generation``
              on ``GATEWAY``. Still answers HTTP 200 with an SSE envelope, but the
              business layer now rejects it with HTTP-equivalent 503
              ``Model catalog unavailable`` for every model key.
  * current : a long-lived gateway connection (bundled ``@qwen-work/gateway-sdk``
              native module + Discovery lookup + JWT registration) with model
              requests issued by the bundled ``qoderclicn.exe`` agent SDK.

``OPENAPI_BASE`` / ``REALM_GATEWAY`` below are the CN hosts advertised by the
client's endpoint cache (``endpoint-cache.json``). They are recorded for the
next protocol iteration; the legacy chat path is NOT served from them
(``/algo/api/v2/...`` on ``REALM_GATEWAY`` answers ``Login expired`` because the
gateway.qwenwork.cn token is scoped to the ``gateway.qwenwork.cn`` realm).
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

# CN OpenAPI host advertised by the 1.1.0 endpoint cache. Serves the new
# `/algo/api/v3/*` surface with plain OAuth Bearer and encrypted responses
# (decrypted by the bundled qoder-auth-wasm `decrypt_server_response`).
OPENAPI_BASE = "https://openapi.qoder.com.cn"
# CN gateway reported by the same cache. Different token realm from GATEWAY.
REALM_GATEWAY = "https://gateway.qoder.com.cn"
# New model server used by the bundled CLI (global). CN counterpart is derived
# by the client via QODER_API_DOMAIN_SUFFIX and is not publicly resolvable.
MODEL_SERVER_PATH = "/model/v1/chat/completions"

IDE_VERSION = "1.1.0"
RELEASE_VERSION = "1.1.0-26091701"
BUILD = "26091701"
COSY_VERSION = "1.1.18"
COSY_VERSION_FROZEN = True
CLIENT_TYPE = "6"
BUSINESS_PRODUCT = "qoder_work"
BUSINESS_TYPE = "agent"
SCENE = "qwork"
MACHINE_OS = "x86_64_win32"
LOGIN_VERSION = "v2"
USER_AGENT = "qoderwork/1.1.0"

# Official 0.1.8 asar generateAuthToken public key (PKCS#1 v1.5).
RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

STATIC_MODELS = (
    "pro",
    "flash",
    "qwen3.8-max-preview",
    "qwork-advanced",
    "qwork-auto",
    "qwork-lite",
    "qmodel_latest",
)

ALIASES = {
    "auto": "pro",
    "qwork-advanced": "pro",
}

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
