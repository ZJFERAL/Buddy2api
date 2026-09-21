"""zcode 通道的 chat_completions 实现。

流程：OpenAI payload → Anthropic body → 选账号 → 验证码（JWT 通道）→ 上游流式
→ 失败分类（验证码挑战重试 / 3012 禁用 / 额度耗尽换号 / 401/403 标 invalid/429
原地重试/5xx 重试/其它 4xx 直接回传）→ Anthropic 响应转换回 OpenAI（json/SSE）。

返回契约（providers/protocol.py）：
  ("error", (status_code, detail_dict))
  ("json", obj)
  ("stream", async_generator)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Optional

import httpx

import auth_manager
import database as db
from providers.zcode import agent, constants
from providers.zcode.captcha import captcha_manager, CaptchaSolveError
from providers.zcode.openai_compat import (
    StreamConverter,
    anthropic_to_openai,
    iter_sse_events,
    openai_to_anthropic,
)
from providers.zcode.token import account_mode, secret_for

log = logging.getLogger("zcode")

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5
RETRY_429_TIMES = int(__import__("os").environ.get("ZCODE_RETRY_429_TIMES", "5"))
RETRY_429_WAIT = int(__import__("os").environ.get("ZCODE_RETRY_429_WAIT", "60"))
RETRY_5XX_TIMES = int(__import__("os").environ.get("ZCODE_RETRY_5XX_TIMES", "3"))
RETRY_5XX_WAIT = int(__import__("os").environ.get("ZCODE_RETRY_5XX_WAIT", "5"))
COOLING_SECONDS = int(__import__("os").environ.get("ZCODE_COOLING_SECONDS", "300"))


def translate_model(model: str) -> str:
    """模型名 → 上游官方名（大小写敏感）。"""
    value = (model or "").strip()
    return constants.MODEL_NAME_MAP.get(value.lower(), value)


def _normalize_body(body: dict) -> dict:
    """body 归一：模型小写别名 → 官方名；max_tokens 钳制；content 字符串分块。"""
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        body["model"] = translate_model(model)

    raw = body.get("max_tokens")
    if raw is not None and not isinstance(raw, bool):
        try:
            mt = int(float(raw))
        except (TypeError, ValueError):
            mt = None
        if mt is not None:
            clamped = max(1, min(mt, constants.MAX_TOKENS_LIMIT))
            body["max_tokens"] = clamped
            if clamped != mt:
                log.warning("max_tokens %s 超出上游范围，钳制为 %s", mt, clamped)

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


# ── 账号状态标记 ───────────────────────────────────────────────────────────
def _mark(account: dict, status_value: str, error: str | None = None) -> None:
    from providers.zcode import store as zstore

    extra = dict(account.get("extra") or {})
    extra["last_error"] = error
    if status_value == "cooling":
        extra["cooling_until"] = time.time() + COOLING_SECONDS
    db.update_account(int(account["id"]), {
        "status": status_value,
        "extra": extra,
    })
    account["status"] = status_value
    account["extra"] = extra


def _ban_for_risk(account: dict) -> None:
    """3012「unusual activity」：禁用账号（人工确认恢复后再启用）。"""
    extra = dict(account.get("extra") or {})
    extra["risk_strikes"] = int(extra.get("risk_strikes") or 0) + 1
    db.update_account(int(account["id"]), {"status": "disabled", "extra": extra})
    account["status"] = "disabled"
    account["extra"] = extra


# ── 上游调用 ───────────────────────────────────────────────────────────────
async def _stream_upstream(
    account: dict,
    url: str,
    headers: dict,
    payload: bytes,
) -> tuple[int, httpx.AsyncClient, object, object]:
    """打开上游流。返回 (status, client, response_ctx)。调用方负责关闭。"""
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0))
    cm = client.stream("POST", url, headers=headers, content=payload)
    try:
        resp = await cm.__aenter__()
    except httpx.HTTPError as err:
        await client.aclose()
        raise err
    return resp.status_code, client, cm, resp


def _log_usage(
    api_key_info: dict | None,
    account: dict,
    model_name: str,
    stream: bool,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: str,
    status_code: int,
    error_msg: str,
    t0: float,
) -> None:
    total = prompt_tokens + completion_tokens
    try:
        db.add_log({
            "api_key_id": (api_key_info or {}).get("id"),
            "api_key_name": (api_key_info or {}).get("name"),
            "account_id": account.get("id"),
            "account_name": account.get("nickname") or account.get("name"),
            "model": model_name,
            "stream": int(stream),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total,
            "finish_reason": finish_reason,
            "duration_ms": int((time.time() - t0) * 1000),
            "status_code": status_code,
            "error_msg": error_msg,
            "created_at": int(time.time()),
        })
    except Exception:  # noqa: BLE001 - 日志失败不阻断主流程
        pass


def _http_error_detail(status_code: int, text: str) -> dict:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"error": {"message": text[:500], "type": "upstream_error"}}
    except (json.JSONDecodeError, ValueError):
        return {"error": {"message": text[:500], "type": "upstream_error"}}


async def _pick(tried: set[int]) -> dict | None:
    """从通道池选可用账号（排除已试），no error、inactive 之外均可。"""
    for account in db.list_accounts(provider="zcode"):
        if account["id"] in tried:
            continue
        status = account.get("status")
        if status in ("disabled", "invalid", "expired", "exhausted"):
            continue
        extra = account.get("extra") or {}
        cooling_until = extra.get("cooling_until")
        if cooling_until and time.time() < cooling_until:
            continue
        if not secret_for(account):
            continue
        return account
    return None


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    client_wants_stream = bool(payload.get("stream"))
    log_model = None
    if isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    model_name = log_model or str(payload.get("model") or "")

    # 转换到 Anthropic body
    converted, err = openai_to_anthropic(payload)
    if converted is None:
        return (
            "error",
            (400, {"error": {"message": err or "请求体不合法", "type": "invalid_request_error"}}),
        )
    body = _normalize_body(converted)

    if client_wants_stream:
        return ("stream", _stream(body, api_key_info, model_name))

    # 非流式：账号循环
    tried: set[int] = set()
    last_error = (
        "error",
        (503, {
            "error": {
                "message": "No available accounts",
                "type": "channel_unavailable",
                "code": "channel_unavailable",
                "channel": "zcode",
            }
        }),
    )
    for attempt in range(MAX_ACCOUNT_ATTEMPTS):
        account = await _pick(tried)
        if not account:
            return last_error
        tried.add(account["id"])
        t0 = time.time()
        result = await _try_account(account, body, incoming_headers=None, stream_mode=False)
        if result is _NEXT_ACCOUNT:
            continue
        if isinstance(result, tuple) and result and result[0] == "upstream":
            # 成功：消费上游流 → anthropic message → OpenAI json
            _, client, cm, resp, status_code, account = result
            consumed = await _consume_json_upstream(client, cm, resp, account, body, model_name)
            if consumed[0] == "json":
                usage = (consumed[1].get("usage") or {})
                _log_usage(
                    api_key_info, account, model_name, False,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    (consumed[1].get("choices") or [{}])[0].get("finish_reason") or "stop",
                    200, "", t0,
                )
                return consumed
            last_error = consumed
            continue
        last_error = result
        if result[0] == "error" and result[1][0] < 500 and result[1][0] not in (402, 401, 403, 405):
            # 非可换号的错误直接回传（不浪费账号）
            return result
    return last_error


async def _try_account(
    account: dict,
    body: dict,
    incoming_headers: dict | None,
    stream_mode: bool,
) -> object:
    """单账号尝试：验证码 → 请求 → 分类处置。返回 _NEXT_ACCOUNT 表示换号。"""
    mode = account_mode(account)
    needs_captcha = mode == "jwt"
    captcha_retries = 0
    retries_429 = 0
    retries_5xx = 0

    while True:
        verify_param = verify_region = None
        if needs_captcha:
            try:
                verify_param, verify_region = await captcha_manager.get_verify_param()
            except CaptchaSolveError as err:
                return (
                    "error",
                    (500, {"error": {"message": f"无法完成人机校验: {err}", "type": "captcha_error"}}),
                )

        try:
            url, headers, payload = agent.build_request(
                account, body, verify_param, incoming_headers, verify_region
            )
        except RuntimeError as err:
            _mark(account, "invalid", str(err))
            return _NEXT_ACCOUNT

        try:
            status_code, client, cm, resp = await _stream_upstream(account, url, headers, payload)
        except httpx.HTTPError as err:
            _mark(account, "cooling", f"连接失败: {err}")
            return _NEXT_ACCOUNT

        if status_code >= 400:
            text = (await resp.aread()).decode("utf-8", "ignore")
            await cm.__aexit__(None, None, None)
            await client.aclose()
            resp_headers = dict(resp.headers)

            # 验证码挑战：清池重试（不改账号状态）
            challenge = agent.detect_captcha_challenge(status_code, resp_headers, text) if needs_captcha else None
            if challenge:
                captcha_manager.invalidate()
                captcha_retries += 1
                if captcha_retries >= MAX_CAPTCHA_RETRIES:
                    return _NEXT_ACCOUNT
                log.warning("账号 %s 验证码挑战（%s），刷新重试", account.get("name"), challenge)
                continue

            # 风控（3012 / 405）：禁用账号，人工恢复
            if agent.is_risk_control(status_code, text):
                _ban_for_risk(account)
                log.warning("账号 %s 命中风控 HTTP %s，已禁用", account.get("name"), status_code)
                return _NEXT_ACCOUNT

            if agent.is_exhausted(status_code, text):
                _mark(account, "exhausted", "额度已用完")
                return _NEXT_ACCOUNT

            if status_code == 401:
                _mark(account, "invalid", "鉴权失败 HTTP 401")
                return _NEXT_ACCOUNT

            if status_code == 403:  # 已排除挑战形态
                _mark(account, "invalid", "鉴权失败 HTTP 403")
                return _NEXT_ACCOUNT

            if status_code == 429:
                if retries_429 < RETRY_429_TIMES:
                    retries_429 += 1
                    wait = agent.parse_retry_after(resp.headers.get("retry-after")) or RETRY_429_WAIT
                    log.warning("账号 %s 限流 429，%ss 后重试", account.get("name"), wait)
                    await asyncio.sleep(wait)
                    continue
                return _NEXT_ACCOUNT

            if status_code >= 500:
                if retries_5xx < RETRY_5XX_TIMES:
                    retries_5xx += 1
                    log.warning("账号 %s 上游 %s，%ss 后重试", account.get("name"), status_code, RETRY_5XX_WAIT)
                    await asyncio.sleep(RETRY_5XX_WAIT)
                    continue
                _mark(account, "cooling", f"上游 {status_code} 重试耗尽")
                return _NEXT_ACCOUNT

            # 其它 4xx：直接回传客户端
            return (
                "error",
                (status_code, _http_error_detail(status_code, text)),
            )

        # 成功：标记 + 交付上游流
        account["status"] = "active"
        return ("upstream", client, cm, resp, status_code, account)


# 非流式成功消费
async def _consume_json_upstream(client, cm, resp, account, body, model_name) -> object:
    content_type = (resp.headers.get("content-type") or "").lower()
    try:
        if "event-stream" in content_type:
            # SSE 事件流：逐行合并为单个 message
            events: list[dict] = []
            async for line in resp.aiter_lines():
                events.extend(iter_sse_events([line]))
            data = _merge_events(events)
        else:
            # 纯 JSON（Anthropic 非流式响应）
            raw = await resp.aread()
            try:
                data = json.loads(raw.decode("utf-8", "ignore"))
            except ValueError:
                data = None
            if data is None and raw:
                # 兜底：仍可能是 SSE 形态的响应
                events = iter_sse_events(raw.decode("utf-8", "ignore").splitlines())
                data = _merge_events(events)
    finally:
        await cm.__aexit__(None, None, None)
        await client.aclose()

    if not data or not isinstance(data, dict):
        return (
            "error",
            (502, {"error": {"message": "上游响应格式异常", "type": "upstream_error"}}),
        )
    account["status"] = "active"
    return ("json", anthropic_to_openai(data, model_name))


def _merge_events(events: list[dict]) -> dict | None:
    """SSE 事件合并为单个 Anthropic message（message_start + blocks + message_delta）。"""
    if not events:
        return None
    merged: dict = {}
    for evt in events:
        etype = evt.get("type")
        if etype == "message_start":
            merged.update(evt.get("message") or {})
        elif etype == "content_block_start":
            merged.setdefault("content", []).append(evt.get("content_block") or {})
        elif etype == "content_block_delta":
            block = (merged.get("content") or [])
            if block:
                delta = evt.get("delta") or {}
                if delta.get("type") == "text_delta" and block[-1].get("type") == "text":
                    block[-1]["text"] = block[-1].get("text", "") + (delta.get("text") or "")
                elif delta.get("type") == "input_json_delta" and block[-1].get("type") == "tool_use":
                    cur = block[-1]
                    cur.setdefault("input", {})
                    # 增量合并 JSON：直接拼接字符串再解析（简化；工具参数场景少见）
                    part = delta.get("partial_json") or ""
                    if part:
                        _append_partial_json(cur, part)
        elif etype == "message_delta":
            delta = evt.get("delta") or {}
            if delta.get("stop_reason"):
                merged["stop_reason"] = delta["stop_reason"]
            usage = evt.get("usage") or {}
            if usage.get("output_tokens") is not None:
                merged.setdefault("usage", {})["output_tokens"] = usage["output_tokens"]
    return merged


def _append_partial_json(block: dict, part: str) -> None:
    """增量 append partial_json 字符串（下轮完整解析时再转 dict）。"""
    raw = block.get("_input_raw", "") + part
    block["_input_raw"] = raw
    try:
        block["input"] = json.loads(raw)
    except ValueError:
        block["input"] = {"_partial": raw}


# 流式：async 生成器
async def _stream(body: dict, api_key_info: dict | None, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b'data: {"error":{"message":"No available accounts","type":"channel_unavailable"}}\n\n'
    for attempt in range(MAX_ACCOUNT_ATTEMPTS):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        result = await _try_account(account, body, incoming_headers=None, stream_mode=True)

        if result is _NEXT_ACCOUNT:
            continue

        if result[0] == "error":
            status, detail = result[1]
            try:
                payload = json.dumps(detail, ensure_ascii=False)
            except (TypeError, ValueError):
                payload = json.dumps({"error": {"message": str(detail)[:500], "type": "upstream_error"}})
            logged = f'data: {payload}\n\n'
            last_error = logged.encode("utf-8")
            if status < 500 and status not in (402, 401, 403, 405):
                yield last_error
                return
            continue

        # 上游流成功：转换 SSE → OpenAI chunks
        _, client, cm, resp, status_code, account = result
        content_type = (resp.headers.get("content-type") or "").lower()
        converter = StreamConverter(model_name)
        try:
            yield converter.start().encode("utf-8")
            prompt_tokens = 0
            completion_tokens = 0
            finish_reason = "stop"
            if "event-stream" in content_type:
                async for line in resp.aiter_lines():
                    for evt in iter_sse_events([line]):
                        for chunk in converter.feed(evt):
                            yield chunk.encode("utf-8")
            else:
                # 纯 JSON（异常形态：上游对流式请求返回了完整响应）→ 一次性转发
                raw = await resp.aread()
                try:
                    data = json.loads(raw.decode("utf-8", "ignore"))
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    # 通过 feed 合成：以 message_delta 形式产出 finish chunk
                    evts = [
                        {"type": "message_start", "message": data},
                    ]
                    for block in (data.get("content") or []):
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                evts.append({"type": "content_block_start", "content_block": block})
                                evts.append({"type": "content_block_delta",
                                             "delta": {"type": "text_delta", "text": block.get("text", "")}})
                            elif block.get("type") == "tool_use":
                                evts.append({"type": "content_block_start", "content_block": block})
                    evts.append({"type": "message_delta",
                                 "delta": {"stop_reason": data.get("stop_reason", "end_turn")},
                                 "usage": {"output_tokens": ((data.get("usage") or {}).get("output_tokens") or 0)}})
                    for evt in evts:
                        for chunk in converter.feed(evt):
                            yield chunk.encode("utf-8")
                else:
                    yield 'data: {"error":{"message":"upstream response format error","type":"upstream_error"}}\n\n'.encode("utf-8")
            yield converter.done().encode("utf-8")
            prompt_tokens = converter.usage.get("prompt_tokens") or 0
            completion_tokens = converter.usage.get("completion_tokens") or 0
            finish_reason = converter.finish_reason or "stop"
            _log_usage(
                api_key_info, account, model_name, True,
                int(prompt_tokens), int(completion_tokens), finish_reason, 200, "", t0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("流传输中断: %s", exc)
        finally:
            await cm.__aexit__(None, None, None)
            await client.aclose()
        return

    yield last_error


_NEXT_ACCOUNT = object()