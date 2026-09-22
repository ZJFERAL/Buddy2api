"""MonkeyCode provider 基础模块单测。

覆盖：
  - models.ModelCatalog：可见/basic 优先级、静态兜底、未命中透传
  - client.MonkeyCodeClient._parse：外壳解包、业务码分类、HTTP 状态分类
  - store：Cookie 归一化/会话提取/脱敏/凭据解析（离线路径，不联网）

不联网、不写库（store 的 verify=false 路径跳过上游校验）。
"""

import httpx
import pytest

from providers.monkeycode import client as mc_client
from providers.monkeycode import models as mc_models
from providers.monkeycode import store as mc_store


# ── models.ModelCatalog ───────────────────────────────────────────────────
def _model(name: str, uuid: str, *, hidden: bool = False, level: str = "basic") -> dict:
    return {"id": uuid, "name": name, "is_hidden": hidden, "access_level": level}


def test_catalog_prefers_visible_over_hidden():
    catalog = mc_models.ModelCatalog()
    catalog.load([_model("x", "u-hidden", hidden=True), _model("x", "u-visible")])
    assert catalog.resolve("x") == ("u-visible", True)


def test_catalog_prefers_basic_over_pro():
    catalog = mc_models.ModelCatalog()
    catalog.load([_model("y", "u-pro", level="pro"), _model("y", "u-basic")])
    assert catalog.resolve("y") == ("u-basic", True)


def test_catalog_excludes_hidden_from_index():
    catalog = mc_models.ModelCatalog()
    catalog.load([_model("z", "u-only-hidden", hidden=True)])
    assert len(catalog) == 0
    assert catalog.resolve("z") == ("z", False)
    assert catalog.list_visible() == []


def test_catalog_falls_back_to_static_table():
    catalog = mc_models.ModelCatalog()
    catalog.load([])
    uuid, found = catalog.resolve("monkeycode-basic/kimi-k2.5")
    assert found is True
    assert uuid == "c82ac16d-aaf5-4197-9040-4227ec2299a5"


def test_catalog_unknown_model_passes_through():
    catalog = mc_models.ModelCatalog()
    catalog.load([])
    assert catalog.resolve("no-such-model") == ("no-such-model", False)
    assert catalog.accepts("no-such-model") is False


def test_catalog_list_visible_sorted_with_flags():
    catalog = mc_models.ModelCatalog()
    catalog.load([
        _model("b-model", "u2", level="pro"),
        _model("a-model", "u1"),
        _model("c-model", "u3", hidden=True),
    ])
    rows = catalog.list_visible()
    assert [r["id"] for r in rows] == ["a-model", "b-model"]
    assert rows[0]["access_level"] == "basic"
    assert rows[1]["access_level"] == "pro"


def test_catalog_accepts_after_load():
    catalog = mc_models.ModelCatalog()
    catalog.load([_model("m1", "u1")])
    assert catalog.accepts("m1") is True
    assert catalog.visible_names() == ["m1"]


def test_catalog_dump_raw_stats():
    stats = mc_models.dump_raw([
        _model("a", "u1"), _model("b", "u2"), _model("c", "u3", hidden=True),
    ])
    assert stats["total"] == 3
    assert stats["visible"] == 2
    assert stats["hidden"] == 1


# ── client._parse ─────────────────────────────────────────────────────────
def _resp(status_code: int, **kwargs) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("GET", "https://monkeycode-ai.com/api/v1/x"),
        **kwargs,
    )


def test_parse_unwraps_envelope():
    c = mc_client.MonkeyCodeClient("cookie=1")
    body = {"code": 0, "message": "success", "data": {"plan": "basic"}}
    assert c._parse(_resp(200, json=body)) == {"plan": "basic"}


def test_parse_returns_bare_body_without_envelope():
    c = mc_client.MonkeyCodeClient()
    assert c._parse(_resp(200, json={"plan": "basic"})) == {"plan": "basic"}


def test_parse_keeps_list_payload():
    c = mc_client.MonkeyCodeClient()
    payload = [{"id": "u1", "name": "m1"}]
    assert c._parse(_resp(200, json={"code": 0, "data": payload})) == payload


def test_parse_busy_code_is_retryable():
    c = mc_client.MonkeyCodeClient()
    with pytest.raises(mc_client.MonkeyCodeError) as excinfo:
        c._parse(_resp(200, json={"code": 10811, "message": "已有任务在运行"}))
    assert excinfo.value.kind == mc_client.ERR_BUSY
    assert excinfo.value.code == 10811
    assert excinfo.value.retryable is True


def test_parse_quota_code_is_retryable():
    c = mc_client.MonkeyCodeClient()
    with pytest.raises(mc_client.MonkeyCodeError) as excinfo:
        c._parse(_resp(200, json={"code": 4002, "message": "quota"}))
    assert excinfo.value.kind == mc_client.ERR_QUOTA
    assert excinfo.value.retryable is True


def test_parse_auth_statuses_are_not_retryable():
    c = mc_client.MonkeyCodeClient()
    for status in (401, 403):
        with pytest.raises(mc_client.MonkeyCodeError) as excinfo:
            c._parse(_resp(status, text="forbidden"))
        assert excinfo.value.kind == mc_client.ERR_AUTH
        assert excinfo.value.retryable is False


def test_parse_rate_limit_status():
    c = mc_client.MonkeyCodeClient()
    with pytest.raises(mc_client.MonkeyCodeError) as excinfo:
        c._parse(_resp(429, text="too many"))
    assert excinfo.value.kind == mc_client.ERR_RATE_LIMIT


def test_parse_upstream_status():
    c = mc_client.MonkeyCodeClient()
    with pytest.raises(mc_client.MonkeyCodeError) as excinfo:
        c._parse(_resp(502, text="bad gateway"))
    assert excinfo.value.kind == mc_client.ERR_UPSTREAM


def test_parse_not_found_status():
    c = mc_client.MonkeyCodeClient()
    with pytest.raises(mc_client.MonkeyCodeError) as excinfo:
        c._parse(_resp(404, text="Not Found"))
    assert excinfo.value.kind == mc_client.ERR_NOT_FOUND


def test_parse_unknown_biz_code_maps_to_upstream():
    c = mc_client.MonkeyCodeClient()
    with pytest.raises(mc_client.MonkeyCodeError) as excinfo:
        c._parse(_resp(200, json={"code": 9999, "message": "?"}))
    assert excinfo.value.kind == mc_client.ERR_UPSTREAM


def test_parse_non_json_body():
    c = mc_client.MonkeyCodeClient()
    with pytest.raises(mc_client.MonkeyCodeError) as excinfo:
        c._parse(_resp(200, text="<html>oops</html>"))
    assert excinfo.value.kind == mc_client.ERR_UPSTREAM


def test_classify_biz_code():
    assert mc_client.classify_biz_code(10811) == mc_client.ERR_BUSY
    assert mc_client.classify_biz_code(4002) == mc_client.ERR_QUOTA
    assert mc_client.classify_biz_code(1234) == mc_client.ERR_UPSTREAM


# ── headers / cookie ──────────────────────────────────────────────────────
def test_headers_include_cookie_only_when_present():
    with_cookie = mc_client.MonkeyCodeClient("monkeycode_ai_session=abc").headers()
    assert with_cookie["Cookie"] == "monkeycode_ai_session=abc"
    assert with_cookie["Origin"] == "https://monkeycode-ai.com"

    without = mc_client.MonkeyCodeClient().headers()
    assert "Cookie" not in without


def test_cookie_of_reads_access_token():
    assert mc_client.cookie_of({"access_token": "abc"}) == "abc"
    assert mc_client.cookie_of({}) == ""
    assert mc_client.cookie_of(None) == ""


# ── store：Cookie 归一化 / 脱敏 / 解析（离线）──────────────────────────────
def test_normalize_cookie_bare_session_value():
    value = "93135e0e-174b-4baa-9ce0-692a88607cbe"
    assert mc_store.normalize_cookie(value) == f"monkeycode_ai_session={value}"


def test_normalize_cookie_full_string_kept():
    full = "_ga=GA1.1.1; monkeycode_ai_session=abc; _pk_ses.2=1"
    assert mc_store.normalize_cookie(full) == full


def test_extract_session_value():
    assert mc_store.extract_session_value(
        "_ga=1; monkeycode_ai_session=abc; _pk=2"
    ) == "abc"
    assert mc_store.extract_session_value("_ga=1; _pk=2") == ""
    assert mc_store.extract_session_value("") == ""


def test_mask_cookie_never_leaks_secret():
    secret = "93135e0e-174b-4baa-9ce0-692a88607cbe"
    masked = mc_store.mask_cookie(f"_ga=1; monkeycode_ai_session={secret}")
    assert secret not in masked
    assert "monkeycode_ai_session=" in masked


def test_parse_credentials_offline_path():
    account = mc_store.parse_credentials({
        "cookie": "monkeycode_ai_session=abc123",
        "name": "测试账号",
        "verify": "false",
    })
    assert account["provider"] == "monkeycode"
    assert account["uid"] == "abc123"
    assert account["access_token"] == "monkeycode_ai_session=abc123"
    assert account["status"] == "active"
    assert account["name"] == "测试账号"
    assert account["extra"]["session_value"] == "abc123"


def test_parse_credentials_accepts_bare_value_with_name_default():
    account = mc_store.parse_credentials({
        "cookie": "abc123def456",
        "verify": "false",
    })
    assert account["uid"] == "abc123def456"
    assert account["access_token"] == "monkeycode_ai_session=abc123def456"
    assert account["name"].startswith("MonkeyCode-")


def test_parse_credentials_rejects_cookie_without_session():
    with pytest.raises(ValueError) as excinfo:
        mc_store.parse_credentials({"cookie": "_ga=1; _pk=2", "verify": "false"})
    assert "monkeycode_ai_session" in str(excinfo.value)


def test_parse_credentials_rejects_empty_input():
    with pytest.raises(ValueError):
        mc_store.parse_credentials({})


def test_go_auth_dir_candidates_deduplicated():
    dirs = mc_store.candidate_auth_dirs()
    keys = [str(d).lower() for d in dirs]
    assert len(keys) == len(set(keys))
    assert dirs
