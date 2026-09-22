"""MonkeyCode 对话编排：chat_completions / test_chat。

流程（单次对话）：
  pick_account（auth_manager 路由）→ 拉取动态模型目录 → create_task → WS stream_task → SSE

特殊性：
  - **单账号同时只能跑一个任务**（10811 busy）→ busy 也触发换号；
  - 无 refresh 流（Cookie 会话）→ 选中的账号直接可用；
  - 首次任务约 12s（Agent 冷启动）→ 请求/连接超时给足（60s+）。

返回契约（protocol.py）：
  ("error", (status_code, detail_dict)) / ("json", obj) / ("stream", async_generator)
其中 stream 生成器 yield **bytes**（`data: {...}\\n\\n`），以 `data: [DONE]\\n\\n` 收尾。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import AsyncGenerator, Optional

import auth_manager
import database as db
import providers.monkeycode as mc
from providers.monkeycode import models, store
from providers.monkeycode.client import (
    ERR_AUTH,
    ERR_BUSY,
    ERR_NOT_FOUND,
    ERR_QUOTA,
    ERR_RATE_LIMIT,
    ERR_UPSTREAM,
    MonkeyCodeError,
    client_for,
)
from providers.monkeycode.constants import CHANNEL_ID, EP_TASKS
from providers.monkeycode.task import build_content, create_task, stream_task

MAX_RETRY_ACCOUNTS = 3
CHAT_TIMEOUT = 120.0  # 单次上游往返上限（含 Agent 冷启动）

# ── 单账号任务槽（MonkeyCode 硬限制：一个账号同时只能跑一个任务）──────────
# 任务结束后上游 status 会短暂滞留 processing，期间新任务返回 10811 busy。
# 策略：同账号对话**串行化**（asyncio.Lock）+ 等残留释放 + busy 退避重试。
SLOT_ACQUIRE_TIMEOUT = 120.0  # 同账号排队等锁上限（秒）
SLOT_BUSY_RETRY = 3           # 真 busy 时的重试次数
SLOT_BUSY_BACKOFF = 2.0       # busy 退避基数（秒）
# 滞留任务清理：任务正文已通过 WS 拿到，但上游 status 仍滞留 processing。
# 仅在**真** busy 时清理 age >= 该阈值（秒）的 processing 任务 —— 避免误删活跃任务。
# 设 MC_TASK_SLOT_CLEAN=0 可禁用（只重试不删除）。
SLOT_STALE_TASK_AGE = 30.0
SLOT_CLEAN_STALE = os.environ.get("MC_TASK_SLOT_CLEAN", "1") != "0"

_slot_locks: dict[int, asyncio.Lock] = {}


def _slot_lock(account_id: int) -> asyncio.Lock:
    lock = _slot_locks.get(account_id)
    if lock is None:
        lock = asyncio.Lock()
        _slot_locks[account_id] = lock
    return lock


async def _acquire_slot(account_id: int, timeout: float = SLOT_ACQUIRE_TIMEOUT) -> asyncio.Lock:
    """获取账号任务槽（串行化同账号请求）。超时抛 busy。"""
    lock = _slot_lock(account_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MonkeyCodeError(
            ERR_BUSY, f"账号 {account_id} 任务槽排队超时（{timeout:.0f}s）", status=409
        ) from exc
    return lock


async def _list_tasks(client) -> list[dict]:
    """列出账号当前任务。"""
    data = await client.get_json(EP_TASKS)
    items = data.get("tasks", []) if isinstance(data, dict) else data
    return [t for t in (items or []) if isinstance(t, dict)]


async def _clear_stale_tasks(client, items: list[dict], min_age: float) -> int:
    """删除滞留的 processing 任务（正文已通过 WS 拿到，上游状态未刷新）。

    只动 age >= min_age 的任务 —— 刚创建的活跃任务绝不误删。
    """
    now = time.time()
    cleared = 0
    for task in items:
        if str(task.get("status") or "") != "processing":
            continue
        created = task.get("created_at") or 0
        age: float | None = None
        try:
            if created:
                age = now - float(created)
        except (TypeError, ValueError):
            age = None
        if age is not None and age < min_age:
            continue  # 刚创建 → 可能是活跃任务，不动
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        try:
            await client.request("DELETE", f"{EP_TASKS}/{task_id}")
            cleared += 1
        except MonkeyCodeError:
            pass
    return cleared


async def _create_task_with_slot(client, content: str, model_uuid: str) -> str:
    """创建任务：乐观直连，仅在**真** busy 时清理滞留任务 + 退避重试。

    ⚠️ 不要用 `GET /tasks` 预判槽占用 —— 实测上游 status 滞留 processing
    时任务槽其实**已释放**（预判会导致无谓等待甚至误报 busy）。
    真实判据只有一个：create 是否返回 10811。
    """
    last: MonkeyCodeError | None = None
    for attempt in range(SLOT_BUSY_RETRY):
        try:
            return await create_task(client, content, model_uuid)
        except MonkeyCodeError as exc:
            if exc.kind != ERR_BUSY:
                raise
            last = exc
            if attempt >= SLOT_BUSY_RETRY - 1:
                break
            # 真 busy → 清理滞留任务（正文已通过 WS 拿到，删除安全）
            if SLOT_CLEAN_STALE:
                try:
                    items = await _list_tasks(client)
                    await _clear_stale_tasks(client, items, SLOT_STALE_TASK_AGE)
                except MonkeyCodeError:
                    pass
            await asyncio.sleep(SLOT_BUSY_BACKOFF * (attempt + 1))
    raise last or MonkeyCodeError(ERR_BUSY, "任务槽繁忙", status=409)


# ── 辅助 ──────────────────────────────────────────────────────────────────
def _err_detail(status: int, message: str, err_type: str = "upstream_error") -> tuple:
    return ("error", (status, {"error": {"message": message[:500], "type": err_type}}))


def _status_for(kind: str) -> int:
    return {
        ERR_AUTH: 401,
        ERR_QUOTA: 402,
        ERR_BUSY: 409,
        ERR_RATE_LIMIT: 429,
        ERR_UPSTREAM: 503,
        ERR_NOT_FOUND: 404,
    }.get(kind, 502)


def _mark(account: dict, exc: MonkeyCodeError, model_slug: str) -> None:
    """把上游错误记给 auth_manager（冷却/下架/模型屏蔽）。"""
    auth_manager.mark_account_failure(
        int(account.get("id") or 0), _status_for(exc.kind), str(exc),
        provider=CHANNEL_ID, model=model_slug,
    )


def _pick(tried: set[int], model_slug: str) -> Optional[dict]:
    """选号：复用 auth_manager 路由（含冷却/额度/模型级屏蔽）。"""
    return auth_manager.pick_account(tried, provider=CHANNEL_ID, model=model_slug)


async def _ensure_uuid(model_slug: str, account: dict | None = None) -> str:
    """模型 slug → 平台 UUID（优先动态目录，静态表兜底）。

    目录接口 /models/available 需要 Cookie，必须用账号的 client 拉取；
    无账号时（理论不会发生）跳过刷新，静态表兜底。
    """
    client = client_for(account)
    try:
        await models.CATALOG.ensure_fresh(client)
    except MonkeyCodeError:
        pass  # 目录刷新失败不致命，静态表兜底
    model_uuid, _found = models.CATALOG.resolve(model_slug)
    return model_uuid


def _chunk(model_name: str, delta_text: str, finish_reason: str | None = None) -> dict:
    choice: dict = {"index": 0, "finish_reason": finish_reason}
    if delta_text:
        choice["delta"] = {"content": delta_text}
    else:
        choice["delta"] = {}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_name,
        "choices": [choice],
    }


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _sse_error(message: str) -> bytes:
    return _sse({"error": {"message": message[:500], "type": "upstream_error"}})


def _completion(model_name: str, reply: str, request_id: str | None = None) -> dict:
    return {
        "id": request_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ── 单次对话（一个账号）───────────────────────────────────────────────────
async def _run_once(account: dict, model_slug: str, content: str,
                    model_uuid: str) -> str:
    """在指定账号上跑一次对话，返回拼接后的正文。

    全程持有该账号的任务槽（同账号串行），避免 10811 busy。
    """
    client = client_for(account)
    aid = int(account.get("id") or 0)
    lock = await _acquire_slot(aid)
    try:
        task_id = await _create_task_with_slot(client, content, model_uuid)
        chunks: list[str] = []
        async for text in stream_task(client, account, task_id):
            chunks.append(text)
        return "".join(chunks)
    finally:
        lock.release()


# ── chat_completions ──────────────────────────────────────────────────────
async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    client_wants_stream = bool(payload.get("stream"))
    log_model = None
    if isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    model_slug = mc.PROVIDER.translate_model(str(payload.get("model") or ""))
    model_name = log_model or payload.get("model") or model_slug
    content = build_content(payload.get("messages") or [])

    if client_wants_stream:
        return ("stream", _stream(content, model_slug, model_name))

    tried: set[int] = set()
    last_error: tuple | None = None
    for _ in range(MAX_RETRY_ACCOUNTS):
        account = _pick(tried, model_slug)
        if not account:
            break
        tried.add(int(account.get("id") or 0))
        try:
            model_uuid = await _ensure_uuid(model_slug, account)
            reply = await _run_once(account, model_slug, content, model_uuid)
        except MonkeyCodeError as exc:
            _mark(account, exc, model_slug)
            last_error = _err_detail(_status_for(exc.kind), str(exc))
            # 请求本身的问题（端点/模型不存在）换号无用；其余换号重试
            if exc.kind == ERR_NOT_FOUND:
                return last_error
            continue
        auth_manager.mark_account_success(
            int(account.get("id") or 0), provider=CHANNEL_ID, model=model_slug
        )
        return ("json", _completion(model_name, reply))

    return last_error or (
        "error",
        (503, {
            "error": {
                "message": "No available accounts",
                "type": "channel_unavailable",
                "code": "channel_unavailable",
                "channel": CHANNEL_ID,
            }
        }),
    )


# ── 流式 ──────────────────────────────────────────────────────────────────
async def _stream(content: str, model_slug: str, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = _sse_error("No available accounts")
    for _ in range(MAX_RETRY_ACCOUNTS):
        account = _pick(tried, model_slug)
        if not account:
            break
        aid = int(account.get("id") or 0)
        tried.add(aid)
        output_started = False
        lock: asyncio.Lock | None = None
        try:
            model_uuid = await _ensure_uuid(model_slug, account)
            client = client_for(account)
            lock = await _acquire_slot(aid)
            task_id = await _create_task_with_slot(client, content, model_uuid)
            async for text in stream_task(client, account, task_id):
                if not text:
                    continue
                output_started = True
                yield _sse(_chunk(model_name, text))
            if not output_started:
                yield _sse_error("上游未返回任何正文")
                return
            yield _sse(_chunk(model_name, "", finish_reason="stop"))
            yield b"data: [DONE]\n\n"
            return
        except MonkeyCodeError as exc:
            _mark(account, exc, model_slug)
            last_error = _sse_error(str(exc))
            if output_started:
                # 已开始输出 → 无法换号，把错误作为流尾部吐出
                yield last_error
                return
            if exc.kind == ERR_NOT_FOUND:
                yield last_error
                return
            continue
        finally:
            # 释放账号任务槽（客户端提前断开时同样生效）
            if lock is not None:
                lock.release()
    yield last_error


# ── 管理页测试 ────────────────────────────────────────────────────────────
async def test_chat(account: dict, model: str = "", prompt: str = "ping") -> dict:
    """管理页「测试」：单账号只读探测，回传真实上游结果。"""
    from providers.monkeycode.task import TASK_TYPE_CHAT  # noqa: F401 (常量校验)

    model_slug = mc.PROVIDER.translate_model(model) or "monkeycode-basic/kimi-k2.5"
    content = build_content([{"role": "user", "content": prompt}])
    started = time.time()
    try:
        model_uuid = await _ensure_uuid(model_slug, account)
        reply = await _run_once(account, model_slug, content, model_uuid)
        return {
            "ok": True,
            "message": "对话成功",
            "duration": round(time.time() - started, 2),
            "reply": reply[:500],
            "model": model_slug,
            "channel": CHANNEL_ID,
        }
    except MonkeyCodeError as exc:
        return {
            "ok": False,
            "message": str(exc),
            "duration": round(time.time() - started, 2),
            "reply": "",
            "model": model_slug,
            "channel": CHANNEL_ID,
            "kind": exc.kind,
        }
