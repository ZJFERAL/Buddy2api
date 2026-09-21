"""Z.AI OAuth 免密登录流程（Buddy2api 管理端集成）。

协议契约来源（两份参考实现对拍一致）：
- zcode-api `src/auth/oauth.ts`（MIT，逆向官方 3.12.3 桌面端 bundle）
- zcode2api `cli.py` / `app/oauth.py`（AGPL，仅对照行为，未抄码）

流程：
  1. POST {ZCODE_ORIGIN}/api/v1/oauth/cli/init  → flow_id + authorize_url
  2. **覆盖 authorize_url 的 redirect_uri 为 /app/oauth/login 中转页**（关键，
     见 constants.OAUTH_INTERSTITIAL_*）；浏览器完成授权后服务端才会记 ready
  3. GET {ZCODE_ORIGIN}/api/v1/oauth/cli/poll/{flow_id} 轮询
     → 状态机 pending / ready / failed / expired
  4. ready 响应结构：{"status":"ready", "token":<Coding Plan JWT>,
                       "user":{"user_id":...}, "zai":{"access_token":...}}
     - `token`         → 入池的 JWT（对话走 zcode.z.ai Plan 通道）
     - `zai.access_token` → 兑换链凭证：api.z.ai z/login → getCustomerInfo
       → 默认机构/项目 → api_keys → `'{apiKey}.{secretKey}'` 回退 Key
  5. 入池：`store.oauth_account(jwt, api_key)`（JWT 主凭证 + 回退 Key 双写）

⚠ 历史坑（本文件曾因此整条链路失效）：
  早期实现把 `access_token` 当成顶层字段、把 `token`（真 JWT）整个漏掉，
  于是 poll 永远取不到凭证 → 一律返回 waiting；且未加注中转页，服务端
  永远 pending。两处都必须按上述契约处理。
"""

from __future__ import annotations

import asyncio
import secrets
import time
import urllib.parse

import httpx

from . import constants

EXCHANGE_ORIGIN = constants.EXCHANGE_ORIGIN
OAUTH_BASE = constants.OAUTH_BASE
OAUTH_ORIGIN = constants.ZCODE_ORIGIN

# 单次 complete 请求内的轮询预算（秒）。前端会按 ~3s 间隔反复调用，
# 所以这里做短预算，既能让"授权刚完成"的情况当场命中，又不会挂住请求。
POLL_BUDGET_SEC = 8.0
# 轮询间隔兜底（上游 poll_interval_sec 缺失时）
DEFAULT_POLL_INTERVAL_SEC = 2.0

# 可重试的 HTTP 状态（其余 4xx 视为致命，与官方客户端语义一致）
RETRYABLE_HTTP_STATUSES = frozenset({408, 429})


# ── 授权页中转（官方客户端 `Ed`）────────────────────────────────────────────
def build_interstitial(app_version: str | None = None) -> str:
    """构建 /app/oauth/login 中转页 URL。"""
    version = app_version or constants.OAUTH_INTERSTITIAL_APP_VERSION
    query = urllib.parse.urlencode({
        "redirect": constants.OAUTH_INTERSTITIAL_REDIRECT,
        "app_version": version,
    })
    return f"{OAUTH_ORIGIN}{constants.OAUTH_INTERSTITIAL_PATH}?{query}"


def apply_interstitial(authorize_url: str, app_version: str | None = None) -> str:
    """把 authorize_url 的 redirect_uri 覆盖为官方中转页（保留其余参数）。

    上游 init 返回的 redirect_uri 指向 zcode.z.ai 自身的 cli 回调，不覆盖的话
    浏览器授权完服务端不会翻 ready（poll 恒 pending）。
    """
    parts = urllib.parse.urlsplit(authorize_url)
    query = [
        (k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k != "redirect_uri"
    ]
    query.append(("redirect_uri", build_interstitial(app_version)))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


class ZaiAuthFlow:
    """api_base / exchange_origin 可注入（测试指向 Mock 上游）。"""

    def __init__(self, api_base: str | None = None, exchange_origin: str | None = None) -> None:
        self.api_base = api_base or OAUTH_BASE
        self.exchange_origin = exchange_origin or EXCHANGE_ORIGIN
        self.poll_token = secrets.token_hex(32)
        # init 回填
        self.flow_id: str = ""
        self.authorize_url: str = ""
        self.poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC
        self.expires_at: float = 0.0

    async def init(self) -> dict:
        """发起 OAuth：返回 flow_id / 加注后的 authorize_url / 轮询元数据。"""
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{self.api_base}/oauth/cli/init",
                headers={
                    "Authorization": f"Bearer {self.poll_token}",
                    "Content-Type": "application/json",
                },
                json={"provider": "zai"},
            )
        res.raise_for_status()
        data = res.json().get("data") or {}
        flow_id = data.get("flow_id")
        authorize_url = data.get("authorize_url")
        if not flow_id or not authorize_url:
            raise RuntimeError("返回的 OAuth 流程数据不完整")

        self.flow_id = str(flow_id)
        self.authorize_url = apply_interstitial(str(authorize_url))
        try:
            self.poll_interval_sec = max(1.0, float(data.get("poll_interval_sec") or DEFAULT_POLL_INTERVAL_SEC))
        except (TypeError, ValueError):
            self.poll_interval_sec = DEFAULT_POLL_INTERVAL_SEC
        try:
            self.expires_at = float(data.get("expires_at") or 0)
        except (TypeError, ValueError):
            self.expires_at = 0.0
        return {
            "flow_id": self.flow_id,
            "authorize_url": self.authorize_url,
            "poll_interval_sec": self.poll_interval_sec,
            "expires_at": self.expires_at,
        }

    async def poll_once(self, flow_id: str) -> tuple[str, object]:
        """单轮轮询。返回 (kind, payload)：
        - ("data", dict)  拿到合法 data，交给状态机判定
        - ("retry", None) 网络抖动 / 5xx / 408 / 429 / 信封畸形 → 当作 pending 继续
        - ("fatal", str)  4xx（可重试者除外）/ 信封 code!=0 → 终止并回传原因
        """
        url = f"{self.api_base}/oauth/cli/poll/{urllib.parse.quote(flow_id, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.get(url, headers={"Authorization": f"Bearer {self.poll_token}"})
        except httpx.HTTPError:
            return "retry", None

        if not res.is_success:
            if 400 <= res.status_code < 500 and res.status_code not in RETRYABLE_HTTP_STATUSES:
                return "fatal", f"轮询被拒：HTTP {res.status_code}"
            return "retry", None

        try:
            raw = res.json()
        except ValueError:
            return "retry", None
        if not isinstance(raw, dict):
            return "retry", None
        code = raw.get("code")
        if isinstance(code, int) and code != 0:
            return "fatal", f"轮询失败：code={code} msg={raw.get('msg') or '(none)'}"
        data = raw.get("data")
        if not isinstance(data, dict):
            return "retry", None
        return "data", data

    async def exchange_api_key(self, access_token: str) -> str:
        """OAuth access_token → 业务 token → 机构/项目 → API Key。"""
        async with httpx.AsyncClient(timeout=30) as client:
            login = await client.post(
                f"{self.exchange_origin}/api/auth/z/login",
                headers={"Content-Type": "application/json"},
                json={"token": access_token},
            )
            login.raise_for_status()
            biz = login.json().get("data") or {}
            biz_token = biz.get("access_token") or biz.get("accessToken")
            if not biz_token:
                raise RuntimeError("返回数据中不含业务凭证")

            info = await client.get(
                f"{self.exchange_origin}/api/biz/customer/getCustomerInfo",
                headers={"Authorization": f"Bearer {biz_token}"},
            )
            info.raise_for_status()
            orgs = (info.json().get("data") or {}).get("organizations") or []
            org = next(
                (o for o in orgs if "默认机构" in (o.get("organizationName") or "")),
                None,
            ) or (orgs[0] if orgs else None)
            if not org:
                raise RuntimeError("找不到可用的机构")
            projects = org.get("projects") or []
            proj = next(
                (p for p in projects if "默认项目" in (p.get("projectName") or "")),
                None,
            ) or (projects[0] if projects else None)
            if not proj:
                raise RuntimeError("找不到可用的项目")

            org_id, proj_id = org["organizationId"], proj["projectId"]
            key_url = (
                f"{self.exchange_origin}/api/biz/v1/organization/"
                f"{org_id}/projects/{proj_id}/api_keys"
            )

            keys_res = await client.get(key_url, headers={"Authorization": f"Bearer {biz_token}"})
            keys_res.raise_for_status()
            keys = keys_res.json().get("data") or []
            key_obj = next((k for k in keys if k.get("name") == "zcode-api-key"), None)
            if key_obj is None:
                create = await client.post(
                    key_url,
                    headers={
                        "Authorization": f"Bearer {biz_token}",
                        "Content-Type": "application/json",
                    },
                    json={"name": "zcode-api-key"},
                )
                create.raise_for_status()
                key_obj = create.json().get("data")
            api_key = (key_obj or {}).get("apiKey")
            if not api_key:
                raise RuntimeError("获取 API Key 失败")
            copy = await client.get(
                f"{key_url}/copy/{api_key}",
                headers={"Authorization": f"Bearer {biz_token}"},
            )
            copy.raise_for_status()
            secret_key = (copy.json().get("data") or {}).get("secretKey")
            if not secret_key:
                raise RuntimeError("未能解密 Secret Key")
        return f"{api_key}.{secret_key}"


# ── 管理端会话（单用户简化态；并发登录用内存 dict）───────────────────────────
_sessions: dict[str, dict] = {}


def create_session() -> str:
    """发起 OAuth：返回 session_id（管理端用），内含 flow_id + authorize_url + poll_token。"""
    sid = secrets.token_hex(16)
    _sessions[sid] = {
        "status": "pending",
        "flow_id": None,
        "authorize_url": None,
        "poll_token": None,
        "poll_interval_sec": DEFAULT_POLL_INTERVAL_SEC,
        "expires_at": 0.0,
    }
    return sid


def get_session(sid: str) -> dict | None:
    return _sessions.get(sid)


async def start_flow(sid: str) -> dict:
    flow = _sessions.get(sid)
    if flow is None:
        raise ValueError("会话不存在")
    # 每次 start 生成新的 flow（poll_token 也换新）
    auth = ZaiAuthFlow()
    meta = await auth.init()
    flow["flow_id"] = meta["flow_id"]
    flow["authorize_url"] = meta["authorize_url"]
    flow["poll_token"] = auth.poll_token
    flow["poll_interval_sec"] = meta["poll_interval_sec"]
    flow["expires_at"] = meta["expires_at"]
    flow["status"] = "pending"
    return {"flow_id": meta["flow_id"], "authorize_url": meta["authorize_url"]}


def _ready_payload(data: dict) -> dict | None:
    """从 ready 响应抽出凭证；缺 JWT 返回 None。

    契约：{"status":"ready","token":<JWT>,"user":{...},"zai":{"access_token":...}}
    """
    jwt_token = str(data.get("token") or "").strip()
    if not jwt_token:
        return None
    zai = data.get("zai") if isinstance(data.get("zai"), dict) else {}
    return {
        "jwt": jwt_token,
        "access_token": str((zai or {}).get("access_token") or "").strip(),
        "user_id": str(((data.get("user") or {}) if isinstance(data.get("user"), dict) else {}).get("user_id") or ""),
    }


async def complete_flow(sid: str) -> dict:
    """轮询直到 ready/failed/expired 或预算耗尽；ready 则入池。

    返回 {"status": "done"|"pending"|"failed"|"expired", ...}
    前端按 status 判定：done 收尾、pending 继续轮询、failed/expired 报错终止。
    """
    flow = _sessions.get(sid)
    if flow is None:
        raise ValueError("会话不存在")
    if flow.get("status") == "done":
        # 幂等：重复点击不再重复入池
        return {"status": "done", **(flow.get("result") or {})}
    flow_id = flow.get("flow_id")
    poll_token = flow.get("poll_token")
    if not flow_id or not poll_token:
        raise ValueError("尚未发起 OAuth 流程")

    auth = ZaiAuthFlow()
    auth.poll_token = poll_token
    auth.authorize_url = str(flow.get("authorize_url") or "")
    interval = max(1.0, float(flow.get("poll_interval_sec") or DEFAULT_POLL_INTERVAL_SEC))
    budget_end = time.monotonic() + POLL_BUDGET_SEC
    upstream_expiry = float(flow.get("expires_at") or 0)
    if upstream_expiry and time.time() >= upstream_expiry:
        flow["status"] = "expired"
        return {"status": "expired", "message": "授权链接已过期，请重新发起"}

    while True:
        kind, payload = await auth.poll_once(flow_id)
        if kind == "fatal":
            flow["status"] = "failed"
            return {"status": "failed", "message": payload}

        if kind == "data":
            data = payload if isinstance(payload, dict) else {}
            state = str(data.get("status") or "").strip().lower()
            if state == "failed":
                flow["status"] = "failed"
                return {"status": "failed", "message": "授权失败或被拒绝"}
            if state == "expired":
                flow["status"] = "expired"
                return {"status": "expired", "message": "授权链接已过期，请重新发起"}
            if state == "ready":
                creds = _ready_payload(data)
                if creds is None:
                    flow["status"] = "failed"
                    return {
                        "status": "failed",
                        "message": "授权响应缺少 Coding Plan JWT（data.token）",
                    }
                api_key = ""
                if creds["access_token"]:
                    try:
                        api_key = await auth.exchange_api_key(creds["access_token"])
                    except Exception:  # noqa: BLE001 - 兑换失败不阻断 JWT 入池
                        api_key = ""
                from . import store

                result = store.oauth_account(creds["jwt"], api_key or None)
                flow["status"] = "done"
                flow["result"] = {
                    "account_id": result.get("id"),
                    "jwt_added": True,
                    "fallback_key": bool(api_key),
                }
                return {"status": "done", **flow["result"]}
            if state not in ("pending", ""):
                # 未知状态 = 协议漂移，明确暴露而非静默等待
                flow["status"] = "failed"
                return {"status": "failed", "message": f"上游返回未知状态：{state}"}

        if time.monotonic() >= budget_end:
            flow["status"] = "pending"
            return {"status": "pending", "message": "仍在等待浏览器完成授权"}
        await asyncio.sleep(min(interval, max(0.0, budget_end - time.monotonic())))
