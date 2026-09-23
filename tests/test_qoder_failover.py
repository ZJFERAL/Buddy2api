"""qoderwork 换号（failover）测试。

背景：额度耗尽 / 没有模型权限这两类错误在上游是以 **400/402** 回来的，而
`RETRYABLE_STATUS` 只含 5xx/408/409/425/429，所以老代码会把错误原样吐给客户端，
**从不换号** —— 池子里明明还有另一个可用账号。

这里覆盖四条关键性质：
1. 归类函数把「账号问题」和「请求问题」分开；
2. 额度类错误 → 账号下架（且不会被「刷新过期账号」兜底重新捞回来）；
3. 模型权限类错误 → **只**屏蔽该 (账号, 模型)，账号本身继续给别人用；
4. 真·请求错误 → 不换号、不消耗其它账号（用 mock 上游端到端验证）。

可 pytest 运行，也可 `python tests/test_qoder_failover.py` 直接跑。
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import auth_manager  # noqa: E402
import credential_crypto  # noqa: E402
import database as db  # noqa: E402
from providers.qoderwork import chat, cosy  # noqa: E402

PROVIDER = "qoderwork"


# ------------------------------------------------------------------ 夹具

def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="qoder-failover-")
    db.DB_PATH = Path(tmp) / "gateway.db"
    os.environ["CB_GATEWAY_MASTER_KEY"] = "test-master-key"
    credential_crypto.reset_cache()
    db.init_db()
    # 用 getattr 而不是直接取属性：这样同一个文件在「补丁前」的代码上也能跑，
    # 从而能直观看到补丁前后行为差异（而不是一律 AttributeError）。
    for attr in ("_sticky_account_id", "_account_failures", "_model_blocks"):
        store = getattr(auth_manager, attr, None)
        if isinstance(store, dict):
            store.clear()


def _add_account(name: str, token: str, *, cosy_creds: bool = False) -> dict:
    extra = {}
    if cosy_creds:
        # 原生（COSY 签名）路由需要这两个字段，否则会被判为「凭证缺失」
        extra = {
            "cosy_key": "KEY-" + name,
            "encrypt_user_info": "INFO-" + name,
            "machine_id": "MACHINE-ID",
            "data_policy_agreed": True,
        }
    aid = db.add_account({
        "name": name,
        "provider": PROVIDER,
        "uid": name,
        "access_token": token,
        "status": "active",
        "expires_at": int(time.time()) + 86400,   # 远未过期，避免触发刷新
        "extra": extra,
    })
    return db.get_account(aid)


class _MockUpstream:
    """按凭证决定响应的本地 mock 上游。

    OpenAI 路由靠 `Authorization: Bearer <token>` 区分账号；
    原生路由靠 `Cosy-Key` 头区分（`build_native_headers` 会带上它）。
    `behaviors`: {key: {"status": int, "body": str} | {"status": 200, "lines": [str]}}
    """

    def __init__(self):
        self.behaviors: dict[str, dict] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静默
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                key = (self.headers.get("Cosy-Key") or "").strip()
                if not key:
                    auth = self.headers.get("Authorization") or ""
                    key = auth.split(" ", 1)[1].strip() if " " in auth else auth.strip()
                spec = outer.behaviors.get(key)
                if not spec:
                    return self._send(404, b'{"error":{"message":"unknown credential"}}',
                                      "application/json")
                status = int(spec.get("status") or 200)
                if status >= 400:
                    return self._send(status, str(spec.get("body") or "").encode("utf-8"),
                                      "application/json")
                payload = "".join((line + "\n\n") for line in spec.get("lines") or [])
                return self._send(200, payload.encode("utf-8"), "text/event-stream")

            def _send(self, status, payload, ctype):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _openai_sse(content: str = "pong") -> list:
    chunk = json.dumps({
        "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": "lite",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }, ensure_ascii=False)
    return ["data: " + chunk, "data: [DONE]"]


def _native_error_frame(message: str) -> str:
    """原生路由的信封错误（HTTP 仍是 200，错误藏在 statusCodeValue/body 里）。"""
    return "data: " + json.dumps({"statusCodeValue": 400, "body": json.dumps({"message": message})})


def _native_ok_frames(content: str = "pong") -> list:
    inner = json.dumps({
        "id": "n1", "choices": [{"index": 0, "delta": {"content": content},
                                 "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }, ensure_ascii=False)
    return ["data: " + json.dumps({"statusCodeValue": 0, "body": inner}),
            "event: finish\ndata: {}"]


# ------------------------------------------------------ 1) 错误归类

def test_classify_failover_splits_account_vs_request():
    cf = auth_manager.classify_failover
    # 额度类
    assert cf(400, "Billing daily count exceeded") == auth_manager.FAILOVER_QUOTA
    assert cf(400, "You have exceeded your quota") == auth_manager.FAILOVER_QUOTA
    assert cf(402, "Payment Required") == auth_manager.FAILOVER_QUOTA
    assert cf(400, "insufficient balance") == auth_manager.FAILOVER_QUOTA
    # 模型权限类
    assert cf(400, "Cannot find model") == auth_manager.FAILOVER_MODEL
    assert cf(400, 'Unsupported model "qfmodel"') == auth_manager.FAILOVER_MODEL
    assert cf(403, "model access denied") == auth_manager.FAILOVER_MODEL
    # 临时不可用类（超时/断流）：换号可解，但不下架账号
    assert cf(400, "First Token Timeout or Upstream Timeout") == auth_manager.FAILOVER_TRANSIENT
    assert cf(400, "stream failed") == auth_manager.FAILOVER_TRANSIENT
    assert cf(400, "stream interrupted: read timeout") == auth_manager.FAILOVER_TRANSIENT
    assert cf(400, "connection reset by peer") == auth_manager.FAILOVER_TRANSIENT
    assert cf(400, "upstream unavailable") == auth_manager.FAILOVER_TRANSIENT
    # 请求本身的问题：换号没意义，绝不能连带消耗其它账号
    assert cf(400, "invalid request: messages must not be empty") is None
    assert cf(413, "request too large") is None
    assert cf(500, "internal server error") is None
    assert cf(429, "too many requests") is None
    assert cf(400, "") is None


# --------------------------------------------- 2) 额度类 → 下架 + 换号

def test_quota_error_expires_account_and_switches():
    _fresh_db()
    a = _add_account("A", "tok-a")
    b = _add_account("B", "tok-b")

    auth_manager.mark_account_failure(
        a["id"], 400, "Billing daily count exceeded", provider=PROVIDER, model="lite"
    )
    fresh = db.get_account(a["id"])
    assert fresh["status"] == "expired", "额度耗尽应把账号下架"
    assert auth_manager.account_quota_blocked(fresh) is True
    assert float(fresh["extra"]["quota_blocked_until"]) > time.time()

    nxt = auth_manager.pick_account(provider=PROVIDER, model="lite")
    assert nxt and nxt["id"] == b["id"], "应切换到另一个账号"


def test_quota_block_survives_account_reactivation():
    """人工把账号改回 active 即视为解除隔离（否则会给用户一个「改了也没用」的假象）。"""
    _fresh_db()
    a = _add_account("A", "tok-a")
    auth_manager.mark_account_failure(a["id"], 402, "", provider=PROVIDER, model="lite")
    assert auth_manager.account_quota_blocked(db.get_account(a["id"])) is True

    db.update_account(a["id"], {"status": "active"})
    assert auth_manager.account_quota_blocked(db.get_account(a["id"])) is False


# ------------------------------------------ 3) 模型权限类 → 只屏蔽该模型

def test_model_error_blocks_only_that_model():
    _fresh_db()
    a = _add_account("A", "tok-a")
    b = _add_account("B", "tok-b")

    auth_manager.mark_account_failure(
        a["id"], 400, "Cannot find model", provider=PROVIDER, model="qfmodel"
    )
    fresh = db.get_account(a["id"])
    assert fresh["status"] == "active", "模型类错误不该把账号整体下架"
    assert auth_manager.account_model_blocked(a["id"], PROVIDER, "qfmodel") is True

    # 同一个模型：跳过 A
    got = auth_manager.pick_account(provider=PROVIDER, model="qfmodel")
    assert got and got["id"] == b["id"]

    # 别的模型：A 依然可用（清掉粘性，否则会粘在 B 上）
    auth_manager._sticky_account_id.pop(PROVIDER, None)
    got2 = auth_manager.pick_account(provider=PROVIDER, model="lite")
    assert got2 and got2["id"] == a["id"], "账号只是缺这一个模型的权限，别的模型仍应用它"

    # 成功一次即解除该模型的屏蔽
    auth_manager.mark_account_success(a["id"], provider=PROVIDER, model="qfmodel")
    assert auth_manager.account_model_blocked(a["id"], PROVIDER, "qfmodel") is False


# ----------------------------- 3.5) 临时类 → 短冷却、不下架、到期自动恢复

def test_transient_error_does_not_expire_account():
    _fresh_db()
    a = _add_account("A", "tok-a")
    auth_manager.mark_account_failure(
        a["id"], 400, "First Token Timeout or Upstream Timeout",
        provider=PROVIDER, model="lite",
    )
    fresh = db.get_account(a["id"])
    # 临时抖动：账号保持 active，绝不整体下架
    assert fresh["status"] == "active", "临时超时不该把账号下架"
    # 冷却期内被隔离
    assert auth_manager.account_quota_blocked(fresh) is False
    assert auth_manager.account_is_cooling_down(a["id"]) is True
    nxt = auth_manager.pick_account(provider=PROVIDER, model="lite")
    assert nxt is None, "冷却期内该账号不可选，且无其它账号可用"


def test_transient_cooldown_expires_and_recovers():
    """冷却 3 分钟一到，账号自动回到池子（无需人工干预）。"""
    _fresh_db()
    a = _add_account("A", "tok-a")
    auth_manager.mark_account_failure(
        a["id"], 400, "stream failed", provider=PROVIDER, model="lite"
    )
    assert auth_manager.account_is_cooling_down(a["id"]) is True
    # 模拟 3 分钟冷却结束：直接把到期时间拨回过去
    auth_manager._account_failures[a["id"]] = (1, time.monotonic() - 1)
    assert auth_manager.account_is_cooling_down(a["id"]) is False
    got = auth_manager.pick_account(provider=PROVIDER, model="lite")
    assert got and got["id"] == a["id"], "冷却结束后账号应自动恢复可选"
    assert db.get_account(a["id"])["status"] == "active"


# -------------------------------- 4) 端到端：OpenAI 路由 402 → 换号成功

def test_openai_route_failover_on_quota():
    _fresh_db()
    mock = _MockUpstream()
    old_url = chat.CHAT_URL
    try:
        a = _add_account("A", "tok-a")
        b = _add_account("B", "tok-b")
        mock.behaviors = {
            "tok-a": {"status": 402, "body": '{"error":{"message":"quota exceeded"}}'},
            "tok-b": {"status": 200, "lines": _openai_sse("pong")},
        }
        chat.CHAT_URL = mock.base + "/v1/chat/completions"
        kind, body = asyncio.run(chat.chat_completions(
            {"model": "lite", "messages": [{"role": "user", "content": "hi"}]}, None))
    finally:
        chat.CHAT_URL = old_url
        mock.close()

    assert kind == "json", "老代码会把 402 直接抛给客户端，这里应当已换号成功: %r" % (body,)
    assert body["choices"][0]["message"]["content"] == "pong"
    assert db.get_account(a["id"])["status"] == "expired"
    assert db.get_account(b["id"])["status"] == "active"


# ------------------- 5) 端到端：真·请求错误不该换号、不该消耗其它账号

def test_request_level_error_does_not_burn_other_accounts():
    _fresh_db()
    mock = _MockUpstream()
    old_url = chat.CHAT_URL
    try:
        a = _add_account("A", "tok-a")
        b = _add_account("B", "tok-b")
        mock.behaviors = {
            "tok-a": {"status": 400, "body": '{"error":{"message":"invalid request: messages must not be empty"}}'},
            "tok-b": {"status": 200, "lines": _openai_sse("pong")},
        }
        chat.CHAT_URL = mock.base + "/v1/chat/completions"
        kind, payload = asyncio.run(chat.chat_completions(
            {"model": "lite", "messages": [{"role": "user", "content": "hi"}]}, None))
    finally:
        chat.CHAT_URL = old_url
        mock.close()

    assert kind == "error"
    assert payload[0] == 400
    assert db.get_account(a["id"])["status"] == "active"
    assert db.get_account(b["id"])["status"] == "active", "请求错误不该连累其它账号"


# -------------------- 6) 端到端：原生路由信封错误 → 换号（HTTP 仍是 200）

def test_native_route_failover_inside_sse_envelope():
    _fresh_db()
    mock = _MockUpstream()
    old_chat, old_native = chat.CHAT_URL, cosy.NATIVE_INFER_URL
    try:
        a = _add_account("A", "tok-a", cosy_creds=True)
        b = _add_account("B", "tok-b", cosy_creds=True)
        mock.behaviors = {
            "KEY-A": {"status": 200, "lines": [_native_error_frame("Cannot find model")]},
            "KEY-B": {"status": 200, "lines": _native_ok_frames("pong")},
        }
        chat.CHAT_URL = mock.base + "/v1/chat/completions"
        cosy.NATIVE_INFER_URL = mock.base + "/native/infer"

        async def run():
            kind, agen = await chat.chat_completions(
                {"model": "qfmodel", "stream": True,
                 "messages": [{"role": "user", "content": "hi"}]}, None)
            assert kind == "stream"
            out = []
            async for piece in agen:
                out.append(piece.decode("utf-8"))
            return "".join(out)

        joined = asyncio.run(run())
    finally:
        chat.CHAT_URL = old_chat
        cosy.NATIVE_INFER_URL = old_native
        mock.close()

    assert "pong" in joined, "信封错误也应换号重试而不是直接中断: %r" % joined
    assert "[DONE]" in joined
    fresh_a = db.get_account(a["id"])
    assert fresh_a["status"] == "active", "缺模型权限不等于账号坏掉"
    assert auth_manager.account_model_blocked(a["id"], PROVIDER, "qfmodel") is True
    assert auth_manager.account_model_blocked(b["id"], PROVIDER, "qfmodel") is False
    assert db.get_account(b["id"])["status"] == "active"


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in funcs:
        try:
            fn()
            print("PASS %s" % fn.__name__)
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print("FAIL %s: %s" % (fn.__name__, exc))
            traceback.print_exc()
            failed += 1
    print("\n%d passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
