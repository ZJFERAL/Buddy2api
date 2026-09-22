"""MonkeyCode Agent 任务通道：任务创建 + WebSocket 流式正文（实现 `task.py`）。

对话本质是 Agent 任务（非标准 LLM chat）：
  1. POST /api/v1/users/tasks 创建任务 → {data: {id: <task_id>}}
  2. WS  /api/v1/users/tasks/stream?id=<task_id>&mode=develop 拉正文

事件帧语法已实机定稿（见 monkeycode_ws_events.md）：
  - 外层 {type, data, kind, seq, timestamp}，其中 **data 是 base64 编码的 JSON**
  - 正文只在 type=task-running 且 update.sessionUpdate=agent_message_chunk 的
    update.content.text
  - agent_thought_chunk 是思维链，必须丢弃
  - type=task-ended 是结束标志
"""

from __future__ import annotations

import asyncio
import base64
import json

import websockets  # websockets>=14，使用 additional_headers 参数

from providers.monkeycode.client import ERR_UPSTREAM, MonkeyCodeError
from providers.monkeycode.constants import (
    CLI_NAME_OPENGODE,
    EP_TASKS,
    PUBLIC_DEVBOX_IMAGE_ID,
    PUBLIC_HOST,
    RESOURCE_CORE,
    RESOURCE_LIFE,
    RESOURCE_MEMORY,
    TASK_TYPE_CHAT,
    USER_AGENT,
    WS_BASE,
    WS_MODE,
)

BASE_ORIGIN = "https://monkeycode-ai.com"

# WS 收帧超时（秒）：首次任务 Agent 冷启动约 9-12s，给足余量
WS_IDLE_TIMEOUT = 120.0
# WS 帧大小上限（上游 usage_update 可能较大）
WS_MAX_FRAME = 16 * 1024 * 1024


# ── messages → agent 需求正文 ─────────────────────────────────────────────
def build_content(messages: list) -> str:
    """把 OpenAI messages 转成 Agent 需求正文（移植 Go 版 BuildContent）。

    首条/system 记作 `## system`，后续按 role 记 `## user` / `## assistant`。
    content 支持 string 或数组（含 image/text 结构）。
    """
    chunks: list[str] = []
    content_parts: list[str] = []
    for i, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip()
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            pieces = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str):
                    pieces.append(item["text"])
            text = "\n".join(pieces)
        if not text:
            continue
        if role == "system" or i == 0:
            prefix = "system"
        elif role == "user":
            prefix = "user"
        else:
            prefix = "assistant"
        chunks.append(f"## {prefix}\n{text}")
    return "\n\n".join(chunks)


# ── 任务创建 ───────────────────────────────────────────────────────────────
def build_task_payload(content: str, model_uuid: str, task_type: str = TASK_TYPE_CHAT) -> dict:
    """构造创建任务载荷（字段与 SPA 一致，Phase 0 实测可用）。"""
    return {
        "content": content,
        "cli_name": CLI_NAME_OPENGODE,
        "model_id": model_uuid,          # 必须传 UUID（传名字上游回 400）
        "image_id": PUBLIC_DEVBOX_IMAGE_ID,
        "host_id": PUBLIC_HOST,
        "task_type": task_type,
        "resource": {"core": RESOURCE_CORE, "memory": RESOURCE_MEMORY, "life": RESOURCE_LIFE},
    }


async def create_task(client, content: str, model_uuid: str,
                      task_type: str = TASK_TYPE_CHAT) -> str:
    """创建任务，返回 task_id。

    错误由 client 层抛 MonkeyCodeError（10811→busy / 4002→quota / 401→auth），
    交给 chat 层决定是否换号。
    """
    payload = build_task_payload(content, model_uuid, task_type)
    data = await client.post_json(EP_TASKS, payload, timeout=60.0)
    task_id = str(data.get("id") or "") if isinstance(data, dict) else ""
    if not task_id:
        # 顺带利用响应里的 user_id 回填（QQ 号标识，可选）
        if isinstance(data, dict) and data.get("user_id"):
            pass  # 留作 future：真实 user_id 来源
        raise MonkeyCodeError(
            ERR_UPSTREAM, "task create 未返回 id", code=0, status=200
        )
    return task_id


# ── 帧解析（定稿规则）─────────────────────────────────────────────────────
def decode_frame_data(raw: str):
    """base64 解码外层 data 字段。"""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def extract_text(env: dict) -> str | None:
    """从 task-running 帧抽取正文；返回 None 表示此帧不是正文。

    ⚠️ 只取 agent_message_chunk —— agent_thought_chunk 是思维链，不能输出。
    """
    if not isinstance(env, dict) or env.get("type") != "task-running":
        return None
    payload = decode_frame_data(env.get("data"))
    if not isinstance(payload, dict):
        return None
    update = payload.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") != "agent_message_chunk":
        return None
    content = update.get("content")
    text = content.get("text") if isinstance(content, dict) else None
    return text if isinstance(text, str) and text else None


def is_end_frame(env: dict) -> bool:
    """结束判据：type == task-ended。"""
    return isinstance(env, dict) and env.get("type") == "task-ended"


# ── WS 流式正文 ───────────────────────────────────────────────────────────
async def stream_task(client, account: dict, task_id: str):
    """连 WS 拉取任务正文，逐个 yield `str` 片段；结束或出错则退出。

    页面连接额外带 Cookie / Origin / Referer（Agent 通道需要会话上下文）。
    """
    url = f"{WS_BASE}/api/v1/users/tasks/stream?id={task_id}&mode={WS_MODE}"
    cookie = str(account.get("access_token") or "").strip() if account else ""

    head = {
        "Cookie": cookie,
        "User-Agent": USER_AGENT,
        "Origin": BASE_ORIGIN,
        "Referer": BASE_ORIGIN + "/",
    }

    try:
        async with websockets.connect(
            url,
            additional_headers=head,
            open_timeout=30.0,
            close_timeout=5.0,
            max_size=WS_MAX_FRAME,
            ping_interval=None,
        ) as ws:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=WS_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    raise MonkeyCodeError(
                        ERR_UPSTREAM, f"WS 空闲超时（{WS_IDLE_TIMEOUT:.0f}s 无新帧）", status=0
                    )
                except websockets.ConnectionClosed as exc:
                    raise MonkeyCodeError(
                        ERR_UPSTREAM, f"WS 连接关闭: {exc.code}", status=0
                    ) from exc

                text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                try:
                    env = json.loads(text)
                except json.JSONDecodeError:
                    continue  # 非 JSON 帧忽略

                if is_end_frame(env):
                    return
                chunk = extract_text(env)
                if chunk:
                    yield chunk
    except websockets.InvalidStatus as exc:
        raise MonkeyCodeError(
            ERR_UPSTREAM, f"WS 握手失败: {exc.response.status_code if exc.response else '?'}", status=0
        ) from exc