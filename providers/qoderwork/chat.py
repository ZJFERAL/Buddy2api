"""Qoder chat client. OpenAI-compatible endpoint, plain Bearer auth (no COSY)."""

from __future__ import annotations

import json
import time
import uuid
from typing import AsyncGenerator

import httpx

import auth_manager
import database as db
from providers.qoderwork.constants import (
    CHANNEL_ID,
    CHAT_URL,
    RETRYABLE_STATUS,
    USER_AGENT,
    use_native_route,
)
from providers.qoderwork import cosy
from providers.qoderwork.token import QoderAuthError, is_token_expired, refresh_account


def translate_model(model: str) -> str:
    from providers.qoderwork.constants import ALIASES

    inner = (model or "lite").strip() or "lite"
    # Case-insensitive: display names like "Qwen3.8-Flash" resolve to slugs.
    return ALIASES.get(inner, ALIASES.get(inner.lower(), inner))


def _ids(account: dict) -> tuple[str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    uid = str(account.get("uid") or extra.get("uid") or "")
    name = str(account.get("nickname") or account.get("name") or extra.get("name") or "")
    token = str(account.get("access_token") or extra.get("security_oauth_token") or "")
    return uid, name, token


def build_body(payload: dict) -> tuple[dict, str, str]:
    model = translate_model(str(payload.get("model") or "lite"))
    request_id = str(payload.get("request_id") or uuid.uuid4())
    session_id = str(payload.get("session_id") or uuid.uuid4())
    messages = payload.get("messages") or []
    body: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "metadata": {
            "context": {
                "request_id": request_id,
                "request_set_id": request_id,
                "session_id": session_id,
                "task_id": "common",
                "client_type": "qodercli",
            }
        },
    }
    if payload.get("tools"):
        body["tools"] = payload["tools"]
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return body, raw, model


def _native_messages(messages):
    """Convert OpenAI messages to Qoder upstream messages + a system prompt string.

    IMPORTANT: assistant turns that carry `tool_calls` have `content=None`. Dropping
    them (which an early version did) strips the model's own tool-call turns out of
    the conversation, so multi-step agent requests lose their history and the task
    silently falls apart mid-way. They must be forwarded with their tool_calls.
    """
    sys_parts = []
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            text = "" if content is None else str(content)
        if role in ("system", "developer"):
            if text:
                sys_parts.append(text)
            continue
        item: dict = {"role": role, "content": text}
        # assistant turn that invoked tools: keep the calls, not just the (empty) text
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            normalized = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                normalized.append(
                    {
                        "id": tc.get("id") or "",
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "",
                        },
                    }
                )
            if normalized:
                item["tool_calls"] = normalized
        # tool result turn: the id is what lets upstream bind result -> call
        if role == "tool":
            item["tool_call_id"] = m.get("tool_call_id") or ""
            if m.get("name"):
                item["name"] = m["name"]
        out.append(item)
    return "\n\n".join(sys_parts), out


def build_native_body(payload: dict, model: str, request_id: str | None = None,
                      session_id: str | None = None) -> str:
    """Build the native (COSY) request body, mirroring qodercli2api remoteChatAskBody.

    Returned as a compact JSON string; callers must run it through cosy.encode_body
    before sending.

    NOTE: the upstream reads the system prompt from messages[0], not the top-level
    `system` field, so we send BOTH (exactly like the official client) — dropping
    the in-messages copy silently loses the system prompt.
    """
    model = translate_model(model)
    system, up_msgs = _native_messages(payload.get("messages"))
    if system:
        up_msgs = [{"role": "system", "content": system}] + up_msgs
    request_id = str(request_id or payload.get("request_id") or uuid.uuid4())
    session_id = str(session_id or payload.get("session_id") or uuid.uuid4())
    max_tokens = int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 32000)
    native = {
        "business": {
            "product": "cli",
            "version": cosy.COSY_VERSION,
            "type": "agent",
            "id": str(uuid.uuid4()),
            "name": system[:10] if system else "cli",
            "begin_at": int(time.time() * 1000),
            "stage": "start",
        },
        "request_id": request_id,
        "request_set_id": request_id,
        "chat_record_id": request_id,
        "session_id": session_id,
        "stream": True,
        "chat_task": "FREE_INPUT",
        "chat_context": {},
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "agent_id": "agent_common",
        "task_id": "common",
        "session_type": "qodercli",
        "aliyun_user_type": "",
        "model_config": {
            "key": model,
            "format": "openai",
            "source": "system",
            "enable": True,
            "is_vl": True,
        },
        "custom_model": None,
        "system": system,
        "messages": up_msgs,
        "tools": payload.get("tools") or [],
        "parameters": {"max_tokens": max_tokens},
    }
    # Only forward when the client actually asked for a specific tool behaviour;
    # omitting it keeps upstream on its default.
    if payload.get("tool_choice") is not None:
        native["tool_choice"] = payload["tool_choice"]
    return json.dumps(native, ensure_ascii=False, separators=(",", ":"))


def _cosy_session(account: dict) -> dict:
    """Pull COSY native-route fields out of an account's extra blob."""
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    return {
        "uid": str(account.get("uid") or extra.get("uid") or ""),
        "key": str(extra.get("cosy_key") or extra.get("key") or ""),
        "encrypt_user_info": str(extra.get("encrypt_user_info") or ""),
        "machine_id": str(extra.get("machine_id") or ""),
        "data_policy_agreed": bool(extra.get("data_policy_agreed") or False),
    }


def _envelope_error_message(env: dict) -> str:
    """Extract a human-readable error message from a non-200 sseEnvelope."""
    body = env.get("body")
    msg = ""
    if isinstance(body, str) and body:
        try:
            parsed = json.loads(body)
            msg = str(parsed.get("message") or parsed.get("msg") or "")
        except json.JSONDecodeError:
            msg = body
    elif isinstance(body, dict):
        msg = str(body.get("message") or body.get("msg") or "")
    if not msg:
        sc = env.get("statusCodeValue") or env.get("statusCode") or "unknown"
        msg = "upstream error status %s" % sc
    return msg


async def _native_frames(response) -> AsyncGenerator[tuple[str, dict | None], None]:
    """Parse a native SSE response into (event, envelope) frames.

    `envelope` is the parsed sseEnvelope dict, or None when the data line was not
    valid JSON / empty. `event` carries the SSE event name ('' if absent); the
    native endpoint signals completion with `event: finish`.
    """
    event = ""
    data_lines: list[str] = []

    def flush():
        nonlocal event, data_lines
        if not data_lines and not event:
            return None
        payload = "\n".join(data_lines).strip()
        env = None
        if payload:
            try:
                env = json.loads(payload)
            except json.JSONDecodeError:
                env = None
        e, d = event, env
        event, data_lines = "", []
        return (e, d)

    async for line in response.aiter_lines():
        if line == "":
            fr = flush()
            if fr is not None:
                yield fr
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            d = line[len("data:"):]
            if d.startswith(" "):
                d = d[1:]
            data_lines.append(d)
    fr = flush()
    if fr is not None:
        yield fr


def _native_headers_for(account: dict, encoded: str, upstream_model: str) -> dict | None:
    """Build native COSY headers, or None if COSY creds are missing for this account."""
    cs = _cosy_session(account)
    if not cs.get("key") or not cs.get("encrypt_user_info"):
        return None
    return cosy.build_native_headers(
        encrypt_user_info=cs["encrypt_user_info"],
        key=cs["key"],
        uid=cs["uid"],
        machine_id=cs["machine_id"],
        encoded_body=encoded,
        model_key=upstream_model,
        model_source="system",
        data_policy_agreed=cs["data_policy_agreed"],
    )


async def _native_stream(encoded: str, request_id: str, session_id: str, upstream_model: str,
                         api_key_info, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b'data: {"error":{"message":"No available accounts"}}\n\n'
    last_status = 503
    for _ in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        headers = _native_headers_for(account, encoded, upstream_model)
        if headers is None:
            auth_manager.mark_account_failure(account["id"], 401)
            last_error = (
                f"data: {json.dumps({'error': {'message': 'COSY credentials missing for this account'}}, ensure_ascii=False)}\n\n"
            ).encode()
            last_status = 401
            continue
        output_started = False
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", cosy.NATIVE_INFER_URL, headers=headers, content=encoded) as response:
                    last_status = response.status_code
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = (
                            f"data: {json.dumps({'error': {'message': text}}, ensure_ascii=False)}\n\n"
                        ).encode()
                        if response.status_code not in RETRYABLE_STATUS:
                            yield last_error
                            _log(api_key_info, account, model_name, True, 0, 0, 0, "error", response.status_code, text, t0)
                            return
                        continue
                    auth_manager.mark_account_success(account["id"])
                    finish_reason = ""
                    usage: dict = {}
                    async for event, env in _native_frames(response):
                        if event == "finish":
                            break
                        if not isinstance(env, dict):
                            continue
                        sc = env.get("statusCodeValue")
                        if sc not in (0, 200, None):
                            msg = _envelope_error_message(env)
                            auth_manager.mark_account_failure(account["id"], 400)
                            yield (
                                f"data: {json.dumps({'error': {'message': msg, 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"
                            ).encode("utf-8")
                            _log(api_key_info, account, model_name, True, 0, 0, 0, "error", 400, msg, t0)
                            return
                        body = env.get("body")
                        if not body:
                            continue
                        try:
                            inner = json.loads(body) if isinstance(body, str) else body
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(inner, dict):
                            continue
                        err = _is_error_chunk(inner)
                        if err:
                            auth_manager.mark_account_failure(account["id"], 400)
                            yield (
                                f"data: {json.dumps({'error': {'message': err, 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"
                            ).encode("utf-8")
                            _log(api_key_info, account, model_name, True, 0, 0, 0, "error", 400, err, t0)
                            return
                        if isinstance(inner.get("usage"), dict):
                            usage = inner["usage"]
                        for choice in inner.get("choices") or []:
                            if isinstance(choice, dict) and choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                        # Upstream hardcodes "auto" here; echo what the client asked
                        # for so strict clients don't choke on a mismatched model.
                        if "model" in inner:
                            inner["model"] = model_name
                        output_started = True
                        yield f"data: {json.dumps(inner, ensure_ascii=False)}\n\n".encode("utf-8")
            if not output_started:
                # Connected fine but produced nothing -> treat as a bad account and
                # let the retry loop try the next one.
                auth_manager.mark_account_failure(account["id"], 502)
                last_error = (
                    f"data: {json.dumps({'error': {'message': 'empty upstream stream'}}, ensure_ascii=False)}\n\n"
                ).encode()
                last_status = 502
                continue
            yield b"data: [DONE]\n\n"
            _log(
                api_key_info, account, model_name, True,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                finish_reason or "stop", 200, "", t0,
            )
            return
        except httpx.HTTPError as exc:
            if output_started:
                # Mid-stream disconnect. Previously this returned silently: the
                # client got an unterminated SSE body (looks like "gave up halfway")
                # and no log row was written, making it invisible in the dashboard.
                yield b"data: [DONE]\n\n"
                _log(api_key_info, account, model_name, True, 0, 0, 0, "error", 502,
                     "stream interrupted: %s" % str(exc)[:200], t0)
                return
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = (
                f"data: {json.dumps({'error': {'message': str(exc)[:240]}}, ensure_ascii=False)}\n\n"
            ).encode()
            last_status = 503
            continue
    yield last_error
    _log(api_key_info, None, model_name, True, 0, 0, 0, "error", last_status, "stream failed", time.time())


async def _native_json(encoded: str, request_id: str, session_id: str, upstream_model: str,
                       api_key_info, model_name: str) -> tuple:
    tried: set[int] = set()
    last_error = None
    for _ in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        headers = _native_headers_for(account, encoded, upstream_model)
        if headers is None:
            auth_manager.mark_account_failure(account["id"], 401)
            last_error = ("error", (401, {"error": {"message": "COSY credentials missing for this account", "type": "auth_error"}}))
            continue
        try:
            chunks: list[dict] = []
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", cosy.NATIVE_INFER_URL, headers=headers, content=encoded) as response:
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = ("error", (response.status_code, {"error": {"message": text, "type": "server_error"}}))
                        if response.status_code not in RETRYABLE_STATUS:
                            return last_error
                        continue
                    auth_manager.mark_account_success(account["id"])
                    async for event, env in _native_frames(response):
                        if event == "finish":
                            break
                        if not isinstance(env, dict):
                            continue
                        sc = env.get("statusCodeValue")
                        if sc not in (0, 200, None):
                            msg = _envelope_error_message(env)
                            auth_manager.mark_account_failure(account["id"], 400)
                            last_error = ("error", (400, {"error": {"message": msg, "type": "upstream_error"}}))
                            _log(api_key_info, account, model_name, False, 0, 0, 0, "error", 400, msg, t0)
                            return last_error
                        body = env.get("body")
                        if not body:
                            continue
                        try:
                            inner = json.loads(body) if isinstance(body, str) else body
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(inner, dict):
                            continue
                        err = _is_error_chunk(inner)
                        if err:
                            auth_manager.mark_account_failure(account["id"], 400)
                            last_error = ("error", (400, {"error": {"message": err, "type": "upstream_error"}}))
                            _log(api_key_info, account, model_name, False, 0, 0, 0, "error", 400, err, t0)
                            return last_error
                        chunks.append(inner)
            aggregated = _aggregate(chunks, model_name)
            usage = aggregated.get("usage") or {}
            _log(
                api_key_info, account, model_name, False,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                aggregated["choices"][0].get("finish_reason") or "stop",
                200, "", t0,
            )
            return ("json", aggregated)
        except httpx.HTTPError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            continue
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


def _headers(token: str, request_id: str, session_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": USER_AGENT,
        "X-Request-ID": request_id,
        "X-Session-ID": session_id,
    }


def _is_error_chunk(data: dict) -> str | None:
    """Return an upstream error message if `data` is an SSE error payload.

    Qoder's OpenAI-compatible endpoint returns HTTP 200 even on failure,
    carrying the error as an `event: error` SSE frame with a `{"code": "...",
    "message": "...", "type": "..._error"}` data payload. Without this check the
    provider would silently report success on an empty stream.
    """
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "upstream error")
    if str(data.get("type", "")).endswith("_error") and data.get("code"):
        return str(data.get("message") or data.get("type"))
    return None


def _aggregate(chunks: list[dict], model: str) -> dict:
    content: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish = "stop"
    usage: dict = {}
    response_id = "qoderwork"
    for item in chunks:
        if not isinstance(item, dict):
            continue
        response_id = item.get("id") or response_id
        usage = item.get("usage") or usage
        for choice in item.get("choices") or [{}]:
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(str(delta["content"]))
            for call in delta.get("tool_calls") or []:
                index = int(call.get("index") or 0)
                slot = tool_calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                if call.get("type"):
                    slot["type"] = call["type"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += str(fn["arguments"])
    message: dict = {"role": "assistant", "content": "".join(content)}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {
        "id": response_id,
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _log(api_key_info, account, model_name, stream, prompt_t, completion_t, total_t,
         finish_reason, status_code, error_msg, t0, increment_usage=True):
    try:
        db.record_request(
            {
                "api_key_id": api_key_info["id"] if api_key_info else None,
                "api_key_name": api_key_info["name"] if api_key_info else None,
                "account_id": account["id"] if account else None,
                "account_name": account.get("name") if account else None,
                "provider": CHANNEL_ID,
                "model": model_name,
                "stream": 1 if stream else 0,
                "prompt_tokens": prompt_t,
                "completion_tokens": completion_t,
                "total_tokens": total_t,
                "credit": 0,
                "finish_reason": finish_reason,
                "duration_ms": int((time.time() - t0) * 1000),
                "status_code": status_code,
                "error_msg": error_msg,
                "increment_usage": increment_usage,
            }
        )
    except Exception:
        pass


async def _pick(tried: set[int]) -> dict | None:
    account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
    if account and is_token_expired(account):
        try:
            account = await refresh_account(account)
        except QoderAuthError:
            account = None
    if account:
        return account
    for row in db.list_accounts(provider=CHANNEL_ID):
        if row.get("status") == "expired" and row.get("id") not in tried:
            try:
                return await refresh_account(row)
            except QoderAuthError:
                continue
    return None


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    client_wants_stream = bool(payload.get("stream"))
    log_model = None
    if isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    body, raw, upstream_model = build_body(payload)
    model_name = log_model if log_model is not None else payload.get("model", upstream_model)
    context = (body.get("metadata") or {}).get("context", {})
    request_id = str(context.get("request_id") or uuid.uuid4())
    session_id = str(context.get("session_id") or uuid.uuid4())
    native = use_native_route(upstream_model)

    if client_wants_stream:
        if native:
            encoded = cosy.encode_body(build_native_body(payload, upstream_model, request_id, session_id))
            return ("stream", _native_stream(encoded, request_id, session_id, upstream_model, api_key_info, model_name))
        return ("stream", _stream(raw, request_id, session_id, upstream_model, api_key_info, model_name))

    if native:
        encoded = cosy.encode_body(build_native_body(payload, upstream_model, request_id, session_id))
        return await _native_json(encoded, request_id, session_id, upstream_model, api_key_info, model_name)

    tried: set[int] = set()
    last_error = None
    for _ in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        try:
            token = _ids(account)[2]
            headers = _headers(token, request_id, session_id)
            chunks: list[dict] = []
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", CHAT_URL, headers=headers, content=raw) as response:
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = (
                            "error",
                            (response.status_code, {"error": {"message": text, "type": "server_error"}}),
                        )
                        if response.status_code not in RETRYABLE_STATUS:
                            return last_error
                        continue
                    auth_manager.mark_account_success(account["id"])
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        err = _is_error_chunk(parsed)
                        if err:
                            auth_manager.mark_account_failure(account["id"], 400)
                            _log(api_key_info, account, model_name, False, 0, 0, 0, "error", 400, err, t0)
                            return ("error", (400, {"error": {"message": err, "type": "upstream_error"}}))
                        chunks.append(parsed)
            aggregated = _aggregate(chunks, model_name)
            usage = aggregated.get("usage") or {}
            _log(
                api_key_info, account, model_name, False,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                aggregated["choices"][0].get("finish_reason") or "stop",
                200, "", t0,
            )
            return ("json", aggregated)
        except httpx.HTTPError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            continue
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


async def _stream(raw: str, request_id: str, session_id: str, upstream_model: str,
                 api_key_info, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b'data: {"error":{"message":"No available accounts"}}\n\n'
    last_status = 503
    for _ in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        output_started = False
        try:
            token = _ids(account)[2]
            headers = _headers(token, request_id, session_id)
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", CHAT_URL, headers=headers, content=raw) as response:
                    last_status = response.status_code
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = (
                            f"data: {json.dumps({'error': {'message': text}}, ensure_ascii=False)}\n\n"
                        ).encode()
                        if response.status_code not in RETRYABLE_STATUS:
                            yield last_error
                            _log(api_key_info, account, model_name, True, 0, 0, 0, "error", response.status_code, text, t0)
                            return
                        continue
                    auth_manager.mark_account_success(account["id"])
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        err = _is_error_chunk(parsed)
                        if err:
                            auth_manager.mark_account_failure(account["id"], 400)
                            yield (
                                f"data: {json.dumps({'error': {'message': err, 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"
                            ).encode("utf-8")
                            _log(api_key_info, account, model_name, True, 0, 0, 0, "error", 400, err, t0)
                            return
                        output_started = True
                        yield f"data: {data}\n\n".encode("utf-8")
            if output_started:
                yield b"data: [DONE]\n\n"
            _log(api_key_info, account, model_name, True, 0, 0, 0, "stop", 200, "", t0)
            return
        except httpx.HTTPError as exc:
            if output_started:
                return
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = (
                f"data: {json.dumps({'error': {'message': str(exc)[:240]}}, ensure_ascii=False)}\n\n"
            ).encode()
            last_status = 503
            continue
    yield last_error
    _log(api_key_info, None, model_name, True, 0, 0, 0, "error", last_status, "stream failed", time.time())


async def test_chat(account: dict, model: str = "lite", prompt: str = "ping") -> dict:
    payload = {
        "model": model or "lite",
        "messages": [{"role": "user", "content": prompt or "ping"}],
        "stream": False,
        "max_tokens": 64,
    }
    t0 = time.time()
    body, raw, upstream_model = build_body(payload)
    context = (body.get("metadata") or {}).get("context", {})
    request_id = str(context.get("request_id") or uuid.uuid4())
    session_id = str(context.get("session_id") or uuid.uuid4())

    # Native (COSY) models go through the signature endpoint, not the OpenAI one.
    if use_native_route(upstream_model):
        encoded = cosy.encode_body(build_native_body(payload, upstream_model, request_id, session_id))
        headers = _native_headers_for(account, encoded, upstream_model)
        if headers is None:
            return {
                "ok": False, "status_code": 401,
                "duration_ms": int((time.time() - t0) * 1000),
                "model": upstream_model,
                "message": "COSY credentials missing for this account",
            }
        chunks: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                async with client.stream("POST", cosy.NATIVE_INFER_URL, headers=headers, content=encoded) as response:
                    status = response.status_code
                    if status >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        return {"ok": False, "status_code": status, "duration_ms": int((time.time() - t0) * 1000), "model": upstream_model, "message": text}
                    async for event, env in _native_frames(response):
                        if event == "finish":
                            break
                        if not isinstance(env, dict):
                            continue
                        sc = env.get("statusCodeValue")
                        if sc not in (0, 200, None):
                            return {"ok": False, "status_code": 400, "duration_ms": int((time.time() - t0) * 1000), "model": upstream_model, "message": _envelope_error_message(env)[:400]}
                        b = env.get("body")
                        if not b:
                            continue
                        try:
                            inner = json.loads(b) if isinstance(b, str) else b
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(inner, dict):
                            continue
                        err = _is_error_chunk(inner)
                        if err:
                            return {"ok": False, "status_code": 200, "duration_ms": int((time.time() - t0) * 1000), "model": upstream_model, "message": str(err)[:400]}
                        chunks.append(inner)
        except httpx.HTTPError as exc:
            return {"ok": False, "status_code": 0, "duration_ms": int((time.time() - t0) * 1000), "model": upstream_model, "message": str(exc)[:240]}
        if not chunks:
            return {"ok": True, "status_code": 200, "duration_ms": int((time.time() - t0) * 1000), "model": upstream_model, "message": "(no content)"}
        aggregated = _aggregate(chunks, upstream_model)
        message_obj = ((aggregated.get("choices") or [{}])[0].get("message") or {})
        return {
            "ok": True, "status_code": 200,
            "duration_ms": int((time.time() - t0) * 1000),
            "model": aggregated.get("model"),
            "message": str(message_obj.get("content") or "")[:240],
            "usage": aggregated.get("usage") or {},
        }

    # Built-in OpenAI-compatible models.
    token = _ids(account)[2]
    try:
        headers = _headers(token, request_id, session_id)
        chunks: list[dict] = []
        async with httpx.AsyncClient(timeout=45.0) as client:
            async with client.stream("POST", CHAT_URL, headers=headers, content=raw) as response:
                status = response.status_code
                if status >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                    return {"ok": False, "status_code": status, "duration_ms": int((time.time() - t0) * 1000), "message": text}
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        continue
                    try:
                        chunks.append(json.loads(data))
                    except json.JSONDecodeError:
                        continue
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 0, "duration_ms": int((time.time() - t0) * 1000), "message": str(exc)[:240]}
    for chunk in chunks:
        err = _is_error_chunk(chunk)
        if err:
            return {
                "ok": False,
                "status_code": 200,
                "duration_ms": int((time.time() - t0) * 1000),
                "model": upstream_model,
                "message": str(err)[:400],
            }
    aggregated = _aggregate(chunks, upstream_model)
    message_obj = ((aggregated.get("choices") or [{}])[0].get("message") or {})
    message = message_obj.get("content") or ""
    return {
        "ok": True,
        "status_code": 200,
        "duration_ms": int((time.time() - t0) * 1000),
        "model": aggregated.get("model"),
        "message": str(message)[:240],
        "usage": aggregated.get("usage") or {},
    }
