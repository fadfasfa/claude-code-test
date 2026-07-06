"""游戏内显示的数据源边界。

当前实现复用 ``hextech.overlay`` 的本地事件、提示缓存和英雄上下文；未来若游戏内
显示改用独立数据，只需替换 ``OverlayDataSource``，host 与 renderer 不需要改动。

调用方: overlay.__main__、overlay.host、overlay.lifecycle; 关键依赖: overlay.hints、core.settings、overlay.events。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol


class OverlayDataSource(Protocol):
    """host 每次绘制所需的最小只读数据接口。"""

    def read_event(self) -> dict[str, Any]: ...

    def read_hint_cache(self) -> dict[str, Any]: ...

    def read_context(self) -> dict[str, Any]: ...


class SharedOverlayDataSource:
    """复用当前本地 runtime 文件的数据源，并按文件签名缓存 hint JSON。"""

    def __init__(self, *, cache_path: str | Path | None = None) -> None:
        if cache_path is None:
            from hextech.overlay.hints import OVERLAY_HINT_CACHE_FILE

            cache_path = OVERLAY_HINT_CACHE_FILE
        self._cache_path = Path(cache_path)
        self._cache_signature: tuple[int, int] | None | object = object()
        self._cache_payload: dict[str, Any] | None = None

    def read_event(self) -> dict[str, Any]:
        from hextech.overlay.events import read_overlay_event

        return read_overlay_event()

    def read_hint_cache(self) -> dict[str, Any]:
        from hextech.overlay.hints import load_overlay_hint_cache

        try:
            stat = self._cache_path.stat()
            signature: tuple[int, int] | None = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            signature = None
        if signature == self._cache_signature and isinstance(self._cache_payload, dict):
            return self._cache_payload
        # cache 写入方统一使用 atomic_write_json；这里按 mtime/size 签名读取即可避开半写窗口。
        payload = load_overlay_hint_cache(self._cache_path)
        self._cache_signature = signature
        self._cache_payload = payload
        return payload

    def read_context(self) -> dict[str, Any]:
        from hextech.overlay.context import read_overlay_context

        return read_overlay_context()


def prepare_shared_overlay_data(*, include_private_stats: bool | None = None) -> dict[str, Any]:
    """生成 host 使用的本地轻量缓存；不启动 Web 或发起远端访问。"""

    from hextech.overlay.hints import (
        build_overlay_hint_cache_from_precomputed,
        write_overlay_hint_cache,
    )
    from hextech.core.settings import load_ui_feature_flags

    if include_private_stats is None:
        flags = load_ui_feature_flags()
        include_private_stats = bool(flags.get("private_policy_stats_enabled", False))
    payload = build_overlay_hint_cache_from_precomputed(
        include_private_stats=bool(include_private_stats),
        source_tag="game-overlay",
    )
    write_overlay_hint_cache(payload)
    return payload


def source_has_private_stats(cache: Mapping[str, Any] | None) -> bool:
    source = cache.get("source") if isinstance(cache, Mapping) else None
    return bool(isinstance(source, Mapping) and source.get("private_policy_stats_enabled") is True)
