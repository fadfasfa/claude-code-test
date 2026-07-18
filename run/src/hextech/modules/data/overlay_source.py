"""游戏内显示的数据源边界。

当前实现从 DataService 发布的固定 generation 读取提示数据，同时复用本地事件和
英雄上下文；替换数据实现时只需保持 ``OverlayDataSource`` 接口不变。

调用方: overlay.__main__、overlay.host、overlay.lifecycle; 关键依赖: data_snapshot、overlay.events、overlay.context。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from hextech.modules.data import SnapshotViewPort
from hextech.modules.vision.runtime_paths import overlay_runtime_state_path


_CONTEXT_SCHEMA_VERSION = 1
_CONTEXT_MAX_AGE_SECONDS = 6 * 60 * 60.0


def _empty_context(error: str) -> dict[str, Any]:
    return {
        "schema_version": _CONTEXT_SCHEMA_VERSION,
        "ok": False,
        "error": error,
        "generated_at": 0.0,
        "champion_id": "",
        "champion_name": "",
        "source": "",
        "session_id": "",
        "connection_state": "",
        "health": "degraded",
    }


def _read_overlay_context() -> dict[str, Any]:
    """只读 Overlay 上下文状态，不引入 UI adapter。"""

    target = Path(overlay_runtime_state_path("game_overlay_context.v1.json"))
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError:
        return _empty_context("context_missing")
    except json.JSONDecodeError:
        return _empty_context("context_damaged")
    if not isinstance(payload, Mapping):
        return _empty_context("context_damaged")
    if payload.get("schema_version") != _CONTEXT_SCHEMA_VERSION:
        return _empty_context("schema_mismatch")
    if not str(payload.get("champion_id") or "").strip():
        return _empty_context("context_missing")
    try:
        generated_at = float(payload.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        return _empty_context("context_missing")
    if generated_at <= 0:
        return _empty_context("context_missing")
    if time.time() - generated_at > _CONTEXT_MAX_AGE_SECONDS:
        return _empty_context("context_expired")
    return dict(payload)


class OverlayDataSource(Protocol):
    """host 每次绘制所需的最小只读数据接口。"""

    def read_event(self) -> dict[str, Any]: ...

    def read_hint_cache(self) -> dict[str, Any]: ...

    def read_context(self) -> dict[str, Any]: ...

    def open_view(self) -> SnapshotViewPort | None: ...


class SharedOverlayDataSource:
    """只读取 DataService 当前完整 generation，不混入旧共享 cache。"""

    def __init__(self, *, snapshot_client=None, privacy_provider=None) -> None:
        from hextech.modules.data.generation import DataSnapshotClient

        self._snapshot_client = snapshot_client or DataSnapshotClient()
        self._privacy_provider = privacy_provider or _display_private_stats_enabled

    def read_event(self) -> dict[str, Any]:
        from hextech.modules.vision.events import read_overlay_event

        return read_overlay_event()

    def read_hint_cache(self) -> dict[str, Any]:
        try:
            snapshot_view = self._snapshot_client.open_view()
        except Exception:
            snapshot_view = None
        if snapshot_view is not None:
            status = snapshot_view.status()
            payload = snapshot_view.get_overlay_hints()
            payload.setdefault("snapshot", {}).update(status)
            return apply_overlay_display_policy(payload, enabled=bool(self._privacy_provider()))
        status = self._snapshot_client.status()
        return {
            "schema_version": 1,
            "source": {"private_policy_stats_enabled": False},
            "hints": {},
            "name_index": {},
            "snapshot": status,
        }

    def read_context(self) -> dict[str, Any]:
        return _read_overlay_context()

    def open_view(self) -> SnapshotViewPort | None:
        """固定打开当前 generation；一次 render tick 内不得切换数据代。"""

        try:
            return self._snapshot_client.open_view()
        except Exception:
            return None


def prepare_shared_overlay_data() -> dict[str, Any]:
    """只读探测当前快照，不在 Overlay 启动路径构建或写入共享数据。"""

    return SharedOverlayDataSource().read_hint_cache()


def source_has_private_stats(cache: Mapping[str, Any] | None) -> bool:
    source = cache.get("source") if isinstance(cache, Mapping) else None
    return bool(isinstance(source, Mapping) and source.get("private_policy_stats_enabled") is True)


def _display_private_stats_enabled() -> bool:
    from hextech.modules.session.settings import load_ui_feature_flags

    return bool(load_ui_feature_flags().get("private_policy_stats_enabled", False))


def apply_overlay_display_policy(payload: Mapping[str, Any], *, enabled: bool | None = None) -> dict[str, Any]:
    """只覆盖展示策略标记，不修改 canonical generation 内容或 generation ID。"""

    result = dict(payload)
    source = result.get("source")
    result["source"] = {
        **(dict(source) if isinstance(source, Mapping) else {}),
        "private_policy_stats_enabled": _display_private_stats_enabled() if enabled is None else bool(enabled),
    }
    return result
