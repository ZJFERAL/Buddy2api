"""验证码求解 + 预解 token 池（阿里云无痕验证）。

通过 Node 子进程在 happy-dom 模拟浏览器环境运行阿里云无痕 SDK，
求得 verifyParam（X-Aliyun-Captcha-Verify-Param）。

池设计（热路径永不等待）：
- 请求到来时从池里直取一枚已解好的 token（亚毫秒），后台任务持续补库存；
- token 时效 ~2 分钟，池内按 FIFO + 年龄淘汰；
- 上游返回挑战时 invalidate() 清空整池（该批 token 可能已被风控盯上）。

solver 程序（Node 脚本）通过环境变量 ZCODE_CAPTCHA_SOLVER_JS 指定；
缺省按优先级回退：Buddy2api 自带 captcha_node/solver.js → 相邻仓库
zocdedemo/zcode2api/captcha_node/solver.js（AGPL 参考实现，仅运行时
外部引用，不并入 MIT 仓库）。

⚠ 求解结果必须过 is_valid_verify_param 质量校验：
真 param 是 base64(JSON) 且含 securityToken（≥50 字符，整体 ~300+ 字符）。
只含 certifyId/sceneId/isSign 的短结果（~76~90 字符）是 SDK 走 failover 的
降级产物 —— 实测上游回 `400 code=3007 captcha verify failed`，绝不能入池。
自带 solver 线上只能出这种降级结果（其反检测能力不足），故校验不通过时
会自动落到下一个求解器。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time

import httpx

from . import constants

# ── 池参数 ─────────────────────────────────────────────────────────────────
POOL_MIN = int(os.environ.get("ZCODE_CAPTCHA_POOL_MIN", "3"))
POOL_MAX = int(os.environ.get("ZCODE_CAPTCHA_POOL_MAX", "10"))
TOKEN_TTL_MS = int(os.environ.get("ZCODE_CAPTCHA_TOKEN_TTL", "95000"))  # 上游 ~2min
CONFIG_CACHE_TTL_MS = int(os.environ.get("ZCODE_CAPTCHA_CONFIG_CACHE_TTL", "600000"))

NODE_PATH = os.environ.get("ZCODE_NODE_PATH", "node")
SOLVE_RETRIES = int(os.environ.get("ZCODE_CAPTCHA_RETRIES", "4"))
SOLVE_TIMEOUT = int(os.environ.get("ZCODE_CAPTCHA_TIMEOUT", "40"))

# 外部 solver 位置（按优先级回退）：
#   1. 环境变量 ZCODE_CAPTCHA_SOLVER_JS 显式指定
#   2. 相邻仓库 zcode2api 的已验证实现（AGPL，仅运行时外部引用，不并入 MIT 仓库）
#      实测 ~3s 出含 securityToken 的完整 param
#   3. 仓库自带 captcha_node/solver.js（自研、可独立运行，但反检测能力不足，
#      线上只能拿到 failover 降级 param 且要等 ~25s，故仅作最后兜底）
_CANDIDATE_SOLVERS = (
    os.environ.get("ZCODE_CAPTCHA_SOLVER_JS", ""),
    r"E:\AiWorkspace\Tools\zocdedemo\zcode2api\captcha_node\solver.js",
    str(os.path.join(os.path.dirname(__file__), "captcha_node", "solver.js")),
)

# 外部 solver 的依赖解析路径：把仓库自带的 happy-dom 借给它，
# 免得在外部仓库里再装一份 node_modules。
_SOLVER_NODE_PATH = str(
    os.path.join(os.path.dirname(__file__), "captcha_node", "node_modules")
)


def _resolve_solvers() -> list[str]:
    """按优先级返回全部存在的 solver 路径（去重、保序）。"""
    found: list[str] = []
    for candidate in _CANDIDATE_SOLVERS:
        if not candidate:
            continue
        path = os.path.expandvars(candidate)
        if os.path.isfile(path) and path not in found:
            found.append(path)
    return found


def is_valid_verify_param(param: str | None) -> bool:
    """verifyParam 质量校验（决定能否入池 / 能否提交上游）。

    真 param 是 base64(JSON)，内含 securityToken（≥50 字符），整体 ~300+ 字符。
    只有 certifyId/sceneId/isSign 的短结果（~76~90 字符）是 SDK 走 failover 的
    降级产物，上游必然回 `400 code=3007 captcha verify failed`（实测确认），
    绝不能当成成功求解。
    """
    if not param or len(param) < 100:
        return False
    try:
        padded = param + "=" * (-len(param) % 4)
        decoded = json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(decoded, dict):
        return False
    token = decoded.get("securityToken") or decoded.get("SecurityToken")
    return bool(token) and len(str(token)) >= 50


class CaptchaSolveError(RuntimeError):
    """验证码求解最终失败（重试耗尽 / 求解器不可用）。

    继承 RuntimeError：调用方（claim.py）统一按 RuntimeError 兜底，
    避免求解失败被当成 500 抛到前端、只剩一句无信息的「领取失败」。
    """


class _Token:
    __slots__ = ("param", "region", "born_at")

    def __init__(self, param: str, region: str | None) -> None:
        self.param = param
        self.region = region
        self.born_at = time.monotonic()

    def expired(self) -> bool:
        return (time.monotonic() - self.born_at) * 1000 >= TOKEN_TTL_MS


class CaptchaManager:
    def __init__(self) -> None:
        self._pool: asyncio.Queue[_Token] = asyncio.Queue(maxsize=POOL_MAX)
        self._pool_size = 0
        self._refill_task: asyncio.Task | None = None
        self._refilling = False
        self._config_lock = asyncio.Lock()
        self._config_cache: dict | None = None
        self._config_cache_at: float = 0.0
        self._last_error: str | None = None
        self._solver_used: str | None = None
        self._degraded_solvers: set[str] = set()

    # ── 配置（免鉴权拉取验证码 sceneId/region/prefix）────────────────────────
    async def fetch_config(self) -> dict:
        now = time.time() * 1000
        if self._config_cache and now - self._config_cache_at < CONFIG_CACHE_TTL_MS:
            return self._config_cache
        async with self._config_lock:
            if self._config_cache and time.time() * 1000 - self._config_cache_at < CONFIG_CACHE_TTL_MS:
                return self._config_cache
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    res = await client.get(
                        f"{constants.CLIENT_CONFIGS_URL}?{constants.CLIENT_CONFIGS_QUERY}"
                    )
                res.raise_for_status()
                captcha = ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
                if captcha:
                    self._config_cache = captcha
                    self._config_cache_at = time.time() * 1000
                    return captcha
            except (httpx.HTTPError, ValueError):
                pass
            return dict(constants.CAPTCHA_DEFAULTS)

    # ── 预解池 ───────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._refill_task is None or self._refill_task.done():
            self._refill_task = asyncio.create_task(self._refill_loop())

    async def close(self) -> None:
        if self._refill_task and not self._refill_task.done():
            self._refill_task.cancel()
            try:
                await self._refill_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._refill_task = None

    def _gate_open(self) -> bool:
        """仅当存在可服务的 JWT 账号才预热（apiKey 账号不需要验证码）。"""
        import database as db

        for account in db.list_accounts(provider="zcode"):
            extra = account.get("extra") or {}
            if extra.get("mode") == "jwt" and account.get("status") in ("active", ""):
                return True
        return False

    async def _refill_loop(self) -> None:
        while True:
            try:
                if not self._gate_open():
                    await self._evict_expired()
                    await asyncio.sleep(3)
                    continue
                need = POOL_MIN - self._pool_size
                if need > 0:
                    await self._refill_batch(need)
                else:
                    await self._evict_expired()
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - 后台循环永不退出
                self._last_error = str(err)
                await asyncio.sleep(5)

    async def _refill_batch(self, need: int) -> None:
        if self._refilling:
            return
        self._refilling = True
        try:
            config = await self.fetch_config()
            for _ in range(need):
                if self._pool_size >= POOL_MAX:
                    break
                token = await self._solve_one(config)
                if token is None:
                    break
                self._put(token)
        finally:
            self._refilling = False

    def _put(self, token: _Token) -> None:
        try:
            self._pool.put_nowait(token)
            self._pool_size += 1
        except asyncio.QueueFull:
            pass

    async def _evict_expired(self) -> None:
        kept: list[_Token] = []
        while True:
            try:
                token = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pool_size = max(0, self._pool_size - 1)
            if not token.expired() and len(kept) < POOL_MAX:
                kept.append(token)
        for token in kept:
            self._put(token)

    async def get_verify_param(self) -> tuple[str, str | None]:
        """取一枚可用 token：优先池内现成，池空才同步现解。"""
        self.start()  # 幂等；不显式启动的话预热池永远是空的（历史遗留：无人调用 start）
        while self._pool_size > 0:
            try:
                token = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pool_size = max(0, self._pool_size - 1)
            if not token.expired():
                asyncio.create_task(self._refill_batch(1))
                return token.param, token.region
        # 池空/全过期：同步现解一次（首启兜底）
        config = await self.fetch_config()
        token = await self._solve_one(config)
        if token is None:
            raise CaptchaSolveError(f"验证码求解失败: {self._last_error or '多次重试无结果'}")
        return token.param, token.region

    # ── 求解 ─────────────────────────────────────────────────────────────────
    def _ordered_solvers(self, solvers: list[str]) -> list[str]:
        """排序：上次成功过的优先；已知只会出降级结果的排到最后（省时间）。"""
        return sorted(
            solvers,
            key=lambda p: (
                0 if p == self._solver_used else 1,
                1 if p in self._degraded_solvers else 0,
            ),
        )

    async def _solve_one(self, config: dict) -> _Token | None:
        """按优先级遍历求解器，取第一个「有效」param（降级结果直接跳过）。"""
        scene = config.get("sceneId") or constants.CAPTCHA_DEFAULTS["sceneId"]
        region = config.get("region") or constants.CAPTCHA_DEFAULTS["region"]
        prefix = config.get("prefix") or constants.CAPTCHA_DEFAULTS["prefix"]
        solvers = _resolve_solvers()
        if not solvers:
            self._last_error = (
                "未找到验证码求解器（solver.js）。设置 ZCODE_CAPTCHA_SOLVER_JS "
                "指向 zcode2api 仓库的 captcha_node/solver.js"
            )
            return None
        errors: list[str] = []
        all_degraded = True
        for path in self._ordered_solvers(solvers):
            name = os.path.basename(os.path.dirname(path)) or path
            for _ in range(SOLVE_RETRIES):
                try:
                    param = await _run_solver(path, scene, region, prefix)
                except RuntimeError as err:
                    errors.append(f"{name}: {err}")
                    all_degraded = False
                    await asyncio.sleep(0.5)
                    continue
                if not param:
                    errors.append(f"{name}: 求解器无输出")
                    all_degraded = False
                    await asyncio.sleep(0.5)
                    continue
                if not is_valid_verify_param(param):
                    # failover 降级是确定性结果，重试无意义 → 换下一个求解器
                    errors.append(f"{name}: 降级结果(len={len(param)}，缺 securityToken)")
                    self._degraded_solvers.add(path)
                    break
                self._solver_used = path
                self._degraded_solvers.discard(path)
                return _Token(param, region)
        if all_degraded:
            errors.append("全部求解器只出降级结果（反检测能力不足）")
        self._last_error = " | ".join(errors[-4:])
        return None

    def diagnostics(self) -> dict:
        """求解器可用性与最近一次失败原因（供探针 / 管理页排障）。"""
        return {
            "solvers": _resolve_solvers(),
            "solver_used": self._solver_used,
            "degraded_solvers": sorted(self._degraded_solvers),
            "pool_size": self._pool_size,
            "last_error": self._last_error,
        }

    def invalidate(self) -> None:
        """上游返回验证码挑战时清空整池。"""
        drained = 0
        while True:
            try:
                self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pool_size = max(0, self._pool_size - 1)
            drained += 1
        if drained:
            # 用标准 logging 替代内部日志
            import logging

            logging.getLogger("zcode").warning(f"验证码失效，清空池 {drained} 枚")


async def _run_solver(solver_path: str, scene: str, region: str, prefix: str) -> str | None:
    """跑指定 Node solver.js，解析 stdout 的 VERIFY_PARAM= 行。"""
    node = shutil.which(NODE_PATH) or NODE_PATH
    if not node:
        raise RuntimeError(f"无法定位 Node 可执行文件（{NODE_PATH}）")
    env = dict(os.environ)
    if _SOLVER_NODE_PATH and os.path.isdir(_SOLVER_NODE_PATH):
        # 让外部（AGPL）求解器复用仓库自带的 happy-dom
        env["NODE_PATH"] = os.pathsep.join(
            [p for p in (_SOLVER_NODE_PATH, env.get("NODE_PATH", "")) if p]
        )
    proc = await asyncio.create_subprocess_exec(
        node, solver_path, scene, region, prefix,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SOLVE_TIMEOUT)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"验证码求解超时（>{SOLVE_TIMEOUT}s）") from None
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "ignore")[-400:]
        raise RuntimeError(f"solver 退出码 {proc.returncode}: {err}")
    param = None
    for line in (stdout or b"").decode("utf-8", "ignore").splitlines():
        if line.startswith("VERIFY_PARAM="):
            param = line[len("VERIFY_PARAM="):].strip()
    return param


captcha_manager = CaptchaManager()