"""MonkeyCode / 长亭百智云 provider 常量。

协议事实全部来自 Phase 0 实机验证（2026-09-21，真实账号 plan=basic）：
  - 认证 = 同源 Cookie 会话，会话名 `monkeycode_ai_session`（**不是** Go 版
    `nebula_session`，已实测）。
  - 对话通道 = Agent 任务：POST /api/v1/users/tasks → WebSocket
    /api/v1/users/tasks/stream?id=<task_id>&mode=develop 拉正文。
  - WS 帧的 `data` 字段是 **base64 编码的 JSON**，正文在
    `update.sessionUpdate == "agent_message_chunk"` 的 `update.content.text`。
  - /api/v1/users/me 已 **404 失效**，身份校验改用 /subscription + /wallet。
  - 模型目录必须动态拉取（静态表有错：qwen3.5-plus UUID 与实测不符）。

文档：monkeycode_ws_events.md（见项目根 / Buddy2api docs/）
"""

from __future__ import annotations

CHANNEL_ID = "monkeycode"
DISPLAY_NAME = "MonkeyCode / 长亭百智云"

# ── 上游 Host ──────────────────────────────────────────────────────────────
BASE = "https://monkeycode-ai.com"
WS_BASE = "wss://monkeycode-ai.com"

# 会话 Cookie 名（实测 2026-09-21；Go 版常量 nebula_session 已过时）
SESSION_COOKIE_NAME = "monkeycode_ai_session"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# ── 端点（全部为 Phase 0 实测核对）─────────────────────────────────────────
# 账号 / 订阅
EP_SUBSCRIPTION = "/api/v1/users/subscription"     # GET -> {plan, auto_renew, ...}
EP_MEMBERS = "/api/v1/users/members"               # GET 成员列表
# 钱包 / 额度 / 签到
EP_WALLET = "/api/v1/users/wallet"                 # GET -> {balance, daily_token_balance, daily_token_limit}
EP_CHECKIN = "/api/v1/users/wallet/checkin"        # GET 状态; POST {captcha_token} 签到
EP_WALLET_TX = "/api/v1/users/wallet/transaction"  # GET 流水
# 模型
EP_MODELS_AVAILABLE = "/api/v1/users/models/available"  # GET 平台内置模型目录
EP_MODELS = "/api/v1/users/models"                 # GET/POST 用户自定义模型
# 任务 / 对话（Agent 通道）
EP_TASKS = "/api/v1/users/tasks"                   # POST 创建任务; GET 列表; DELETE /tasks/{id} 停任务（实测有效）
EP_TASKS_DETAIL = "/api/v1/users/tasks/{id}"       # GET 任务详情（含真实 user_id）
EP_TASKS_STREAM = "/api/v1/users/tasks/stream"     # WS 任务流（正文）
EP_TASKS_ROUNDS = "/api/v1/users/tasks/rounds"     # GET 历史帧（WS 断线恢复用; ?id=<task_id>）
# ⚠️ 实测 404 已失效（Go 版遗留常量，勿再使用）：
#   EP_TASKS_STOP  "/api/v1/users/tasks/stop"
#   EP_TASKS_CTL   "/api/v1/users/tasks/control"
# 镜像
EP_IMAGES = "/api/v1/users/images"                 # GET devbox 镜像
# 验证码（签到 PoW）
EP_CAPTCHA_CHALLENGE = "/api/v1/public/captcha/challenge"  # POST -> {challenge:{c,s,d}, token}
EP_CAPTCHA_REDEEM = "/api/v1/public/captcha/redeem"        # POST {token, solutions} -> {token}

# ── 任务创建常量（Phase 0 实测）────────────────────────────────────────────
# cli_name：平台可用的 agent CLI
CLI_NAME_OPENGODE = "opencode"

# 公共 devbox 镜像 UUID（/users/images 里 remark=="devbox" 的条目，实测一致）
PUBLIC_DEVBOX_IMAGE_ID = "2e214f06-79ba-4535-9ac1-89adc2d9c6cc"
# 公共托管主机占位符
PUBLIC_HOST = "public_host"

# 任务资源规格（与网页端一致）
RESOURCE_CORE = 2
RESOURCE_MEMORY = 8 * 1024 * 1024 * 1024  # 8G
RESOURCE_LIFE = 7200  # 秒

# task_type：chat = 独立对话（轻量，不建沙箱，实测可用）；develop = 开发任务
TASK_TYPE_CHAT = "chat"
TASK_TYPE_DEVELOP = "develop"

# WS 流的 mode 参数（实测固定 develop，即使 task_type=chat）
WS_MODE = "develop"

# ── 业务错误码（Phase 0 实测 + Go 版记录）─────────────────────────────────
# 10811 = 已有任务在跑（瞬态忙，非额度不足）→ 短冷却切号
BUSY_CODES = {10811}
# 4002 = 额度 / 需升级 → 长冷却（等隔日刷新）
QUOTA_CODES = {4002}

# 每日免费额度（免费用户 = 1000 万基础模型 token）
TOKEN_QUOTA_FREE_PER_DAY = 10_000_000
# 每日签到奖励积分
CHECKIN_CREDIT_REWARD = 100

# 值得换号重试的 HTTP 状态码（镜像 qoderwork 约定；400/402 由 classify_failover 处理）
RETRYABLE_STATUS = {402, 408, 409, 425, 429, 500, 502, 503, 504}

# ── 静态模型 UUID 兜底表 ───────────────────────────────────────────────────
# ⚠️ 仅作兜底：上游模型目录会动态变化，且 Go 版记录过错误 UUID（qwen3.5-plus）。
#    运行时优先用 EP_MODELS_AVAILABLE 动态目录（models.py），本表只在目录
#    拉取失败且用户请求命中时使用。
#    这些 UUID 来自 2026-09-21 实测 /users/models/available。
STATIC_MODEL_UUIDS: dict[str, str] = {
    "qwen3.6-plus": "7f292d79-1f0b-40de-9b98-46b1ba66f7a2",
    "monkeycode-basic/qwen3.5-plus": "46f84b51-566d-439c-98d2-2db8b8d04689",
    "monkeycode-basic/glm-5.3-flash": "c79063e2-a4d4-4f15-8f8c-a604f959c377",
    "qwen3.7-max": "08d76917-9d00-4b30-8496-64e29252d2cd",
    "minimax-m2.5": "8e22c508-97ad-490b-b38e-113faeeca275",
    "minimax-m3": "a734a35c-6cf2-4b9b-ae39-29f3e18ee1cf",
    "monkeycode-basic/deepseek-flash": "c5a183f1-2876-45cf-905d-c1b9a85fc819",
    "deepseek-flash": "5e69521a-cc93-4ea8-9ffb-98650a879e74",
    "glm-5": "e07243b6-4dc4-40c4-a486-a9ffa8d46c4a",
    "monkeycode-basic/minimax-m2.5": "a290bb6b-843b-427c-b6cc-0e34dd82385d",
    "glm-5.1": "7e4b3ade-20b1-4b26-9a36-dc2043623a78",
    "kimi-k2.6": "7331f932-5025-458c-9eaa-cb6f455dfc38",
    "monkeycode-basic/kimi-k2.5": "c82ac16d-aaf5-4197-9040-4227ec2299a5",
    "monkeycode-basic/qwen3.8-flash": "d4fd356d-56d3-48b1-8096-560699a3aa04",
}

# 人类可读展示名（可选，缺省直接用 slug 作 id）
MODEL_DISPLAY: dict[str, str] = {
    "monkeycode-basic/kimi-k2.5": "Kimi K2.5 (基础档)",
    "monkeycode-basic/glm-5.3-flash": "GLM-5.3-Flash (基础档)",
    "monkeycode-basic/qwen3.5-plus": "Qwen3.5-Plus (基础档)",
    "monkeycode-basic/qwen3.8-flash": "Qwen3.8-Flash (基础档)",
    "monkeycode-basic/deepseek-flash": "DeepSeek-Flash (基础档)",
    "monkeycode-basic/minimax-m2.5": "MiniMax-M2.5 (基础档)",
    "kimi-k2.6": "Kimi K2.6",
    "glm-5.1": "GLM-5.1",
    "glm-5": "GLM-5",
    "qwen3.7-max": "Qwen3.7-Max",
    "qwen3.6-plus": "Qwen3.6-Plus",
    "minimax-m3": "MiniMax-M3",
    "minimax-m2.5": "MiniMax-M2.5",
    "deepseek-flash": "DeepSeek-Flash",
}

# ── 额度语义 ───────────────────────────────────────────────────────────────
# 免费档：daily_token_balance 是当日剩余基础模型 token（每日重置）
# 到账判定：daily_token_limit > 0
QUOTA_UNIT = "token"
