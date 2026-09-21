"""zcode 上游协议常量收口。

值均为协议事实（端点 / 模型名 / 头名 / 错误码），来源：
- zcode.z.ai 官方客户端与 ZCode Proxy（zcode-api，未授权版）实证
- 2026-09 真机验证记录（GLM-5.3 编码套餐）

模块内禁止硬编码上游 URL / 模型名 / 关键字，一律 import 本模块。
"""

from __future__ import annotations

import os

# ── 上游 origin ────────────────────────────────────────────────────────────
# Plan 通道（JWT + 验证码）：zcode.z.ai 的 zcode-plan 代理端点
ZCODE_ORIGIN = "https://zcode.z.ai"
# API Key 回退通道：api.z.ai 的 anthropic 兼容端点
ZAI_API_ORIGIN = "https://api.z.ai"
# bigmodel（智谱开放平台）anthropic 兼容端点
BIGMODEL_ORIGIN = "https://open.bigmodel.cn"

MESSAGES_PATHS = {
    # zai + jwt（Plan 通道，需 X-Aliyun-Captcha-Verify-Param）
    "zai": "/api/v1/zcode-plan/anthropic/v1/messages",
    # zai + apiKey 回退通道（免验证码）
    "zai_fallback": "/api/anthropic/v1/messages",
    "bigmodel": "/api/anthropic/v1/messages",
}
# 环境变量可覆盖（测试指向 Mock 上游）
MESSAGES_URLS = {
    "zai": os.environ.get("ZAI_UPSTREAM_URL", ZCODE_ORIGIN + MESSAGES_PATHS["zai"]),
    "zai_fallback": os.environ.get("ZAI_FALLBACK_URL", ZAI_API_ORIGIN + MESSAGES_PATHS["zai_fallback"]),
    "bigmodel": os.environ.get("BIGMODEL_UPSTREAM_URL", BIGMODEL_ORIGIN + MESSAGES_PATHS["bigmodel"]),
}
# OAuth / billing 端点覆盖（测试用）
OAUTH_BASE = os.environ.get("ZCODE_OAUTH_BASE", f"{ZCODE_ORIGIN}/api/v1")
EXCHANGE_ORIGIN = os.environ.get("ZCODE_EXCHANGE_ORIGIN", ZAI_API_ORIGIN)
BILLING_BASE_OVERRIDE = os.environ.get("ZCODE_BILLING_BASE", "")

# ── 计费 / 额度端点 ─────────────────────────────────────────────────────────
BILLING_BASE = os.environ.get("ZCODE_BILLING_BASE", f"{ZCODE_ORIGIN}/api/v1/zcode-plan")
# /billing/balance 是唯一同时携带「生效套餐(plans) + 额度窗口(balances) + server_time」
# 的端点（2026-09-21 真机 dump 确认），额度展示只依赖它。
BILLING_BALANCE_PATH = "/billing/balance"
# /billing/preview：可领取套餐列表（entitlements[].grant_units，period=one_time 等），
# 与 claim 领取用的是同一端点。
BILLING_PREVIEW_PATH = "/billing/preview"
# 以下两个端点 2026-09-21 真机实测**不可用**，请求它们只增加风控暴露面，勿再启用：
#   /billing/current → HTTP 405 + code 3012「unusual activity」（风控拦截）
#   /usage           → HTTP 404
# WAF 风险点：billing/* 连续查询易触发拦截，轮询必须错峰

# ── OAuth ───────────────────────────────────────────────────────────────────
OAUTH_CLI_INIT_PATH = "/api/v1/oauth/cli/init"
OAUTH_CLI_POLL_PATH = "/api/v1/oauth/cli/poll"  # + /{flow_id}
# 授权页中转（官方桌面端 3.12.3 bundle `Ed`，经 zcode-api src/auth/oauth.ts 实证）：
# init 返回的 authorize_url 里带的 redirect_uri 指向 zcode.z.ai 自身的 cli 回调，
# 直接用它浏览器授权完，服务端 poll 状态**永远停在 pending**；官方客户端会把
# redirect_uri 覆盖成 /app/oauth/login 中转页（它先在服务端登记授权，再把浏览器
# 弹回 zcode:// 深链），poll 才会翻 ready。故必须覆盖，不可沿用上游返回值。
OAUTH_INTERSTITIAL_PATH = "/app/oauth/login"
OAUTH_INTERSTITIAL_REDIRECT = "zcode://oauth/callback"
OAUTH_INTERSTITIAL_APP_VERSION = "3.12.3"
# poll 状态机（仅此四态）：pending 继续 / ready 拿凭证 / failed 授权失败 / expired 链接过期
OAUTH_POLL_STATUSES = ("pending", "ready", "failed", "expired")

# ── 客户端版本 ──────────────────────────────────────────────────────────────
# 对话指纹：3.10.2（真机验证 200；3.0.x 已被上游拒绝）
CLIENT_APP_VERSION = "3.10.2"
CLIENT_PLATFORM = "darwin-arm64"  # 服务端固定伪装（官方桌面端形态）
CLIENT_CONFIGS_URL = f"{ZCODE_ORIGIN}/api/v1/client/configs"
CLIENT_CONFIGS_QUERY = f"app_version={CLIENT_APP_VERSION}"
# billing 族版本：3.11.2（官方桌面端现行版；与对话指纹刻意分离）
BILLING_APP_VERSION = "3.11.2"
BILLING_TITLE = "Z Code@electron"
BILLING_RELEASE_CHANNEL = "stable"

# ── 验证码默认配置（client/configs 拉取失败时兜底）────────────────────────────
CAPTCHA_DEFAULTS = {"enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd"}

# ── 模型名（上游大小写敏感；客户端小写别名 → 官方名）───────────────────────────
MODEL_NAME_MAP = {
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.3": "GLM-5.3",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
    "glm-5": "GLM-5",
    "glm-5v-turbo": "GLM-5V-Turbo",
    "glm-4.6": "GLM-4.6",
    "glm-4.6v": "GLM-4.6V",
    "glm-4.5-air": "GLM-4.5-Air",
}

# ── 模型目录（对外公布；contextWindow / maxOutputTokens 参考 ZCode 3.11.2 目录）
MODEL_CATALOG = [
    {"id": "glm-4.5-air", "name": "GLM 4.5 Air", "contextWindow": 131072, "maxOutputTokens": 98304, "reasoning": True},
    {"id": "glm-4.6", "name": "GLM 4.6", "contextWindow": 200000, "maxOutputTokens": 131072, "reasoning": True},
    {"id": "glm-4.6v", "name": "GLM 4.6V", "contextWindow": 131072, "maxOutputTokens": 32768, "reasoning": False},
    {"id": "glm-4.7", "name": "GLM 4.7", "contextWindow": 200000, "maxOutputTokens": 131072, "reasoning": True},
    {"id": "glm-5", "name": "GLM 5", "contextWindow": 200000, "maxOutputTokens": 64000, "reasoning": True},
    {"id": "glm-5-turbo", "name": "GLM 5 Turbo", "contextWindow": 200000, "maxOutputTokens": 64000, "reasoning": True},
    {"id": "glm-5v-turbo", "name": "GLM 5V Turbo", "contextWindow": 200000, "maxOutputTokens": 131072, "reasoning": False},
    {"id": "glm-5.1", "name": "GLM 5.1", "contextWindow": 200000, "maxOutputTokens": 64000, "reasoning": True},
    {"id": "glm-5.2", "name": "GLM 5.2", "contextWindow": 1000000, "maxOutputTokens": 128000, "reasoning": True},
    {"id": "glm-5.3", "name": "GLM 5.3", "contextWindow": 1000000, "maxOutputTokens": 128000, "reasoning": True},
    {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash", "contextWindow": 1000000, "maxOutputTokens": 128000, "reasoning": True},
]
DEFAULT_MODEL = "glm-5.3-flash"

# 上游 max_tokens 合法范围（超限报 400 code 1210）
MAX_TOKENS_LIMIT = 131072

# ── 请求头 ───────────────────────────────────────────────────────────────────
ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = f"ZCode/{CLIENT_APP_VERSION}"
X_ZCODE_APP_VERSION = CLIENT_APP_VERSION
X_PLATFORM = CLIENT_PLATFORM
X_ZCODE_AGENT = "glm"
HTTP_REFERER = "https://zcode.z.ai/"
CAPTCHA_HEADER = "X-Aliyun-Captcha-Verify-Param"
CAPTCHA_REGION_HEADER = "X-Aliyun-Captcha-Verify-Region"

# ── 身份头仿真（pio 头集合；X-Device-Mid 持久化复用）──────────────────────────
IDENTITY_TITLE = "Z Code@cli"  # X-Title = "Z Code@{sourceTitle}"
IDENTITY_RELEASE_CHANNEL = "production"
IDENTITY_CLIENT_LANGUAGE = "zh-CN"
IDENTITY_CLIENT_TIMEZONE = "Asia/Shanghai"
IDENTITY_OS_CATEGORY = "macos"  # darwin→macos / win32→windows / 其它→linux
IDENTITY_OS_VERSION = "25.5.0"  # os.release() 语义（darwin 25.x ↔ macOS 15）

# ── 上游被拒信号 → 账号动作 ──────────────────────────────────────────────────
EXHAUST_HTTP_STATUSES = (402,)
EXHAUST_KEYWORDS = ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足")
# 验证码挑战：HTTP 403 + 文案，或 HTTP 400/403 + body {"code":3007}
CAPTCHA_BODY_MARKERS = ('"code":3007', '"code": 3007')

# ── 风控信号（3012「unusual activity」，HTTP 405 承载）────────────────────────
RISK_CONTROL_HTTP_STATUSES = (405,)
RISK_CONTROL_MARKERS = (
    '"code":3012', '"code": 3012',
    "unusual activity",
)
