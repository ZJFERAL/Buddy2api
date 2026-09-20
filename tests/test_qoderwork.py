"""Tests for the qoderwork provider.

Covers the local-auth parse/upsert round-trip, model binding, and the
channel registry. Live network tests (chat) are covered by the smoke test in
Step 4, not here, so this file stays hermetic and fast.

Runnable with pytest (`pytest tests/test_qoderwork.py -q`) or directly
(`python tests/test_qoderwork.py`).
"""

import os
import sys
import json
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import database as db  # noqa: E402
import credential_crypto  # noqa: E402
import providers  # noqa: E402
import router  # noqa: E402
from providers.protocol import KeyChannelMismatch, UnknownChannel  # noqa: E402
from providers.qoderwork import PROVIDER, chat, constants as qc, store  # noqa: E402


def _setup_isolated_db():
    tmp = tempfile.mkdtemp(prefix="qoder-test-")
    db_path = Path(tmp) / "gateway.db"
    db.DB_PATH = db_path
    os.environ["CB_GATEWAY_MASTER_KEY"] = "test-master-key"
    credential_crypto.reset_cache()
    db.init_db()
    return str(db_path)


def _enable_qoder():
    os.environ["CB_GATEWAY_PROVIDERS"] = "workbuddy,qoderwork"


def test_qoderwork_in_default_registry():
    os.environ.pop("CB_GATEWAY_PROVIDERS", None)
    ids = providers.enabled_provider_ids()
    assert "qoderwork" in ids
    assert providers.get_provider("qoderwork") is not None
    assert "qoderwork" in providers._LOADED


def test_qoderwork_known_channel_set():
    from providers.protocol import KNOWN_CHANNEL_SET

    assert "qoderwork" in KNOWN_CHANNEL_SET
    assert providers.is_channel_enabled("qoderwork") is True


def test_translate_model():
    # Confirmed catalog (from ~/.qoder/.models/<uid>/catalog-v6, chat group).
    assert chat.translate_model("auto") == "auto"
    assert chat.translate_model("lite") == "lite"
    assert chat.translate_model("ultimate") == "ultimate"
    assert chat.translate_model("qfmodel") == "qfmodel"          # Qwen3.8-Flash
    assert chat.translate_model("qoder-lite") == "lite"          # client-side alias
    assert chat.translate_model("qwen3.8-flash") == "qfmodel"    # display-name alias
    assert chat.translate_model("something-odd") == "something-odd"


def test_accepts_model():
    assert PROVIDER.accepts_model("lite") is True
    assert PROVIDER.accepts_model("auto") is True
    assert PROVIDER.accepts_model("performance") is True
    assert PROVIDER.accepts_model("ultimate") is True
    assert PROVIDER.accepts_model("qfmodel") is True            # Qwen3.8-Flash
    assert PROVIDER.accepts_model("qwen3.8-flash") is True      # alias present
    assert PROVIDER.accepts_model("qoder-ultimate") is True     # alias present
    assert PROVIDER.accepts_model("nonexistent-xyz") is False


def test_parse_credentials_reads_local_auth():
    if not qc.AUTH_DIR.is_dir():
        return  # skip: no Qoder CLI login on this machine
    parsed = store.parse_credentials({"auth_dir": str(qc.AUTH_DIR)})
    assert parsed["provider"] == "qoderwork"
    assert parsed["uid"]
    assert parsed.get("security_oauth_token") or parsed.get("access_token")


def test_parse_credentials_pasted_token():
    parsed = store.parse_credentials({"security_oauth_token": "tok-123", "uid": "u1", "name": "x"})
    assert parsed["provider"] == "qoderwork"
    assert parsed["access_token"] == "tok-123"
    assert parsed["uid"] == "u1"


def test_upsert_account_roundtrip():
    _setup_isolated_db()
    _enable_qoder()
    if not qc.AUTH_DIR.is_dir():
        return  # skip: no local auth to import
    parsed = store.parse_credentials({"auth_dir": str(qc.AUTH_DIR)})
    res = store.upsert_account(parsed)
    assert res["id"]
    rows = db.list_accounts(provider="qoderwork")
    assert any(str(r.get("uid")) == str(parsed["uid"]) for r in rows)


def test_bind_qoderwork_prefixed():
    _enable_qoder()
    bound = router.bind({"model": "qoderwork/lite"}, {"default_channel": "qoderwork"})
    assert bound.channel == "qoderwork"
    assert bound.inner == "lite"


def test_bind_requires_matching_key_channel():
    _enable_qoder()
    try:
        router.bind({"model": "qoderwork/lite"}, {"default_channel": "workbuddy"})
        raise AssertionError("expected KeyChannelMismatch")
    except KeyChannelMismatch:
        pass


def test_bind_unknown_channel_when_disabled():
    os.environ["CB_GATEWAY_PROVIDERS"] = "workbuddy"
    try:
        router.bind({"model": "qoderwork/lite"}, {"default_channel": "qoderwork"})
        raise AssertionError("expected UnknownChannel")
    except UnknownChannel:
        pass
    finally:
        os.environ.pop("CB_GATEWAY_PROVIDERS", None)


def test_use_native_route():
    # Built-ins stay on the OpenAI-compatible endpoint.
    assert qc.use_native_route("lite") is False
    assert qc.use_native_route("auto") is False
    assert qc.use_native_route("performance") is False
    assert qc.use_native_route("ultimate") is False
    # Premium models go through the native COSY-signed endpoint.
    assert qc.use_native_route("qfmodel") is True
    assert qc.use_native_route("qwen3.8-flash") is True  # alias -> qfmodel
    assert qc.use_native_route("qmodel_38max") is True


def test_cosy_body_roundtrip():
    from providers.qoderwork import cosy

    raw = json.dumps({"hello": "世界", "n": 1, "arr": [1, 2, 3]}, ensure_ascii=False)
    enc = cosy.encode_body(raw)
    assert enc != raw
    assert cosy.decode_body(enc) == raw


def test_cosy_sign_matches_manual_md5():
    from providers.qoderwork import cosy
    import hashlib

    pb, key, secs, body = "payloadB64==", "theKey", "1700000000", "encodedBodyXXX"
    sig = cosy.sign_cosy(pb, key, secs, body)
    expected = hashlib.md5(
        (pb + "\n" + key + "\n" + secs + "\n" + body + "\n" + cosy.COSY_SIGNED_PATH).encode()
    ).hexdigest()
    assert sig == expected


def test_cosy_headers_shape():
    from providers.qoderwork import cosy
    import base64

    h = cosy.build_native_headers(
        encrypt_user_info="INFO", key="KEY", uid="UID", machine_id="MID",
        encoded_body="BODY", model_key="qfmodel",
    )
    assert h["Authorization"].startswith("Bearer COSY.")
    assert h["Cosy-Key"] == "KEY"
    assert h["Cosy-User"] == "UID"
    assert h["X-Model-Key"] == "qfmodel"
    assert h["X-Model-Source"] == "system"
    assert h["Cosy-Scene"] == cosy.SCENE_NAME
    assert h["Cosy-Version"] == cosy.COSY_VERSION
    parts = h["Authorization"].split(".")
    assert len(parts) == 3 and parts[0] == "Bearer COSY"
    payload = json.loads(base64.b64decode(parts[1]))
    assert payload["version"] == "v1"
    assert payload["info"] == "INFO"
    assert payload["cosyVersion"] == cosy.COSY_VERSION


def test_build_native_body_structure():
    payload = {
        "model": "qfmodel",
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
        ],
    }
    raw = chat.build_native_body(payload, "qfmodel", "req-1", "ses-1")
    d = json.loads(raw)
    assert d["model_config"]["key"] == "qfmodel"
    assert d["model_config"]["format"] == "openai"
    assert d["stream"] is True
    assert d["request_id"] == "req-1"
    assert d["session_id"] == "ses-1"
    # System prompt must appear BOTH as top-level field and messages[0] (upstream
    # reads it from messages[0]; the top-level field alone is silently dropped).
    assert d["system"] == "Be brief."
    assert d["messages"][0]["role"] == "system"
    assert d["messages"][0]["content"] == "Be brief."
    assert d["messages"][1]["role"] == "user"


def test_native_messages_keeps_tool_call_turns():
    """Multi-step agent turns must survive conversion.

    Regression: assistant turns that carry tool_calls have content=None and were
    dropped entirely, so the upstream lost the model's own tool-call history and
    long agent tasks derailed mid-way (client saw "gave up before finishing").
    """
    msgs = [
        {"role": "system", "content": "Be a coding assistant."},
        {"role": "user", "content": "List the files."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": '{"command":"ls"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_abc", "content": "main.py"},
    ]
    system, out = chat._native_messages(msgs)
    assert system == "Be a coding assistant."
    # system is extracted, the other three must all survive
    assert len(out) == 3, f"turns dropped: {out}"
    assert [m["role"] for m in out] == ["user", "assistant", "tool"]

    assistant = out[1]
    assert assistant["content"] == ""
    assert len(assistant["tool_calls"]) == 1
    assert assistant["tool_calls"][0]["id"] == "call_abc"
    assert assistant["tool_calls"][0]["function"]["name"] == "Bash"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"command":"ls"}'

    # the result must remain bound to its call, otherwise upstream cannot match it
    assert out[2]["tool_call_id"] == "call_abc"
    assert out[2]["content"] == "main.py"


def test_native_body_forwards_tool_choice():
    payload = {
        "model": "qfmodel",
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [{"type": "function", "function": {"name": "Bash"}}],
        "tool_choice": {"type": "function", "function": {"name": "Bash"}},
    }
    d = json.loads(chat.build_native_body(payload, "qfmodel"))
    assert d["tool_choice"]["function"]["name"] == "Bash"
    # absent -> must stay absent, not be injected as null/auto
    d2 = json.loads(chat.build_native_body({"model": "qfmodel", "messages": []}, "qfmodel"))
    assert "tool_choice" not in d2


def test_list_models_display_names():
    """list_models() must surface human-readable names, not just slugs."""
    models = PROVIDER.list_models()
    by_id = {m["id"]: m for m in models}
    assert by_id["qfmodel"]["name"] == "Qwen3.8-Flash"
    assert by_id["qmodel_38max"]["name"] == "Qwen3.8-Max"
    assert by_id["efficient"]["name"] == "Efficient"
    assert by_id["kmodel"]["name"] == "Kimi-K2.8-Preview"
    # name must be present (non-empty) for every model
    assert all((m.get("name") or "").strip() for m in models)
    # slug (id) must remain the routing key, untouched
    assert by_id["qfmodel"]["id"] == "qfmodel"


def test_live_qwen38_flash_call():
    """Live probe: lite (OpenAI route) + qfmodel (native COSY route).

    With Step 8 (native COSY routing) implemented, Qwen3.8-Flash (slug qfmodel)
    should now work via the COSY-signed endpoint, not be rejected by the OpenAI
    endpoint. We assert lite still works AND that qfmodel no longer returns the
    `Unsupported model` OpenAI error (i.e. it is being routed natively).
    """
    if not qc.AUTH_DIR.is_dir():
        print("SKIP test_live_qwen38_flash_call: no local Qoder auth")
        return
    import asyncio

    parsed = store.parse_credentials({"auth_dir": str(qc.AUTH_DIR)})
    res = store.upsert_account(parsed)
    account = next(
        (r for r in db.list_accounts(provider="qoderwork") if r.get("id") == res["id"]),
        None,
    )
    assert account, "upserted account not found in DB"
    try:
        lite = asyncio.run(PROVIDER.test_chat(account, model="lite", prompt="Reply with exactly: pong"))
        print("  lite live ->", lite)
        assert lite.get("ok") is True, f"lite baseline failed: {lite}"

        flash = asyncio.run(PROVIDER.test_chat(account, model="qfmodel", prompt="Say hello in one word."))
        print("  qfmodel live ->", flash)
        msg = flash.get("message") or ""
        assert "Unsupported model" not in msg, (
            f"qfmodel still hitting the OpenAI endpoint (routing bug): {flash}"
        )
        assert flash.get("ok") is True, f"qfmodel expected to work via native COSY route: {flash}"
    finally:
        db.delete_account(account["id"])


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
