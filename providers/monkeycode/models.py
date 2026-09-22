"""MonkeyCode 动态模型目录。

上游模型目录随订阅/平台变化，**必须运行时拉取**——Go 版的静态 UUID 表已被
证有误（qwen3.5-plus 的 UUID 与 2026-09-21 实测不一致）。

数据源：GET /api/v1/users/models/available
返回：[{id(UUID), name(slug), access_level, is_free, input_price,
       support_image, is_hidden, ...}]

用途：
  - `/v1/models` 暴露可见模型
  - 创建任务时把用户请求的 slug 解析成平台 UUID（model_id 必须传 UUID）
"""

from __future__ import annotations

import time
from typing import Any

from providers.monkeycode.constants import STATIC_MODEL_UUIDS

# 目录缓存有效期（秒）——目录变化不频繁，避免每个请求都拉
CATALOG_TTL = 600


def _is_hidden(item: dict) -> bool:
    return bool(item.get("is_hidden"))


def _access_level(item: dict) -> str:
    return str(item.get("access_level") or "").strip().lower()


def _sort_key(item: dict):
    """排序：可见优先 → basic 档优先 → id 升序（同名冲突时优取前者）。"""
    return (
        1 if _is_hidden(item) else 0,
        0 if _access_level(item) == "basic" else 1,
        str(item.get("id") or ""),
    )


class ModelCatalog:
    """模型名 → UUID 的运行时索引。"""

    def __init__(self) -> None:
        self._models: list[dict] = []
        self._by_name: dict[str, str] = {}
        self._refreshed_at: float = 0.0

    # ── 构建 ───────────────────────────────────────────────────────────────
    def load(self, models: list) -> None:
        """从原始列表重建索引（同名冲突取排序靠前者：可见 > basic > id 小）。"""
        rows = [m for m in (models or []) if isinstance(m, dict) and m.get("name")]
        rows.sort(key=_sort_key)

        by_name: dict[str, str] = {}
        for item in rows:
            if _is_hidden(item):
                continue                      # 隐藏模型不进索引
            name = str(item["name"])
            if name in by_name:
                continue                      # 已取到更优的同名项
            model_id = str(item.get("id") or "").strip()
            if model_id:
                by_name[name] = model_id

        self._models = rows
        self._by_name = by_name
        self._refreshed_at = time.time()

    async def refresh(self, client) -> int:
        """用客户端拉一次目录并重建索引；返回可见模型数。失败时保留旧目录。"""
        models = await client.get_models_available()
        if models:
            self.load(models)
        return len(self._by_name)

    @property
    def stale(self) -> bool:
        return (time.time() - self._refreshed_at) > CATALOG_TTL

    async def ensure_fresh(self, client, *, force: bool = False) -> int:
        """目录过期（或强制）时刷新一次。"""
        if force or not self._by_name or self.stale:
            return await self.refresh(client)
        return len(self._by_name)

    # ── 查询 ───────────────────────────────────────────────────────────────
    def resolve(self, name: str) -> tuple[str, bool]:
        """模型名 → UUID。命中返回 (uuid, True)；未命中返回 (name, False)。

        未命中时原样透传，让上游给出明确报错，而不是本地直接拒绝
        （目录可能只是暂时拉取失败）。
        """
        key = (name or "").strip()
        if not key:
            return name, False
        if key in self._by_name:
            return self._by_name[key], True
        # 静态兜底表（仅在动态目录未覆盖时）
        if key in STATIC_MODEL_UUIDS:
            return STATIC_MODEL_UUIDS[key], True
        return name, False

    def list_visible(self) -> list[dict]:
        """可见模型（按名称排序），供 /v1/models 使用。"""
        rows = [
            {
                "id": str(m.get("name")),        # 对外以 slug 作 id
                "name": str(m.get("name")),
                "uuid": str(m.get("id") or ""),
                "access_level": _access_level(m),
                "is_free": bool(m.get("is_free")),
                "input_price": m.get("input_price"),
                "support_image": bool(m.get("support_image")),
            }
            for m in self._models
            if not _is_hidden(m)
        ]
        rows.sort(key=lambda r: r["id"])
        return rows

    def visible_names(self) -> list[str]:
        return sorted(self._by_name)

    def accepts(self, name: str) -> bool:
        key = (name or "").strip()
        if not key:
            return False
        return key in self._by_name or key in STATIC_MODEL_UUIDS

    def __len__(self) -> int:
        return len(self._by_name)


# 模块级单例：跨请求复用（Provider 无状态，目录需要有状态）
CATALOG = ModelCatalog()


def uuid_of(name: str) -> tuple[str, bool]:
    """快捷解析（用模块级目录）。"""
    return CATALOG.resolve(name)


def dump_raw(models: list) -> dict[str, Any]:
    """调试辅助：把原始列表整理成统计信息。"""
    rows = [m for m in (models or []) if isinstance(m, dict)]
    visible = [m for m in rows if not _is_hidden(m)]
    return {
        "total": len(rows),
        "visible": len(visible),
        "hidden": len(rows) - len(visible),
        "access_levels": sorted({_access_level(m) for m in visible}),
    }
