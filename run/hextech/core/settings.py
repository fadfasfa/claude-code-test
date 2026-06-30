"""桌面 UI 功能开关运行态配置。

本模块只保存 Web 前端、游戏内显示、浏览器自动打开、私用统计显示和低频监听的
用户偏好。配置落在 `data/runtime/state/`，属于运行态，不作为发布源数据提交。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from hextech.catalog.runtime_store import build_runtime_state_path
from hextech.support.atomic_io import atomic_write_json


FEATURE_FLAGS_FILE = Path(build_runtime_state_path("ui_feature_flags.json"))
DEFAULT_ON_MIGRATION_MARKER_FILE = Path(build_runtime_state_path("ui_feature_flags.defaults.v2.json"))

DEFAULT_UI_FEATURE_FLAGS: dict[str, bool] = {
    "web_frontend_enabled": False,
    "game_overlay_enabled": True,
    "auto_open_browser": True,
    "private_policy_stats_enabled": True,
    "low_frequency_listener_enabled": True,
}

DEFAULT_ON_MIGRATION_KEYS = (
    "game_overlay_enabled",
    "private_policy_stats_enabled",
    "low_frequency_listener_enabled",
)


def normalize_ui_feature_flags(raw_flags: Mapping[str, Any] | None) -> dict[str, bool]:
    """把未知或损坏字段收口到稳定布尔配置，避免运行态文件污染 UI 状态。"""

    normalized = dict(DEFAULT_UI_FEATURE_FLAGS)
    if not isinstance(raw_flags, Mapping):
        return normalized
    for key in normalized:
        if key in raw_flags:
            value = raw_flags[key]
            if isinstance(value, bool):
                normalized[key] = value
            elif isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
                normalized[key] = value.strip().casefold() == "true"
    return normalized


def _migrate_source_default_on_flags(normalized: dict[str, bool]) -> dict[str, bool]:
    """一次性迁移旧源码态运行配置，让新默认开启项不被历史 false 压住。

    迁移只在源码态默认配置文件上执行；写入 marker 后，用户后续手动关闭这些开关
    会继续保留，不会被每次启动强行改回开启。
    """

    if getattr(sys, "frozen", False) or DEFAULT_ON_MIGRATION_MARKER_FILE.exists():
        return normalized

    migrated = dict(normalized)
    changed = False
    for key in DEFAULT_ON_MIGRATION_KEYS:
        if migrated.get(key) is not True:
            migrated[key] = True
            changed = True

    try:
        if changed:
            atomic_write_json(FEATURE_FLAGS_FILE, migrated, ensure_ascii=False, indent=2)
        atomic_write_json(
            DEFAULT_ON_MIGRATION_MARKER_FILE,
            {"version": 2, "default_on_keys": list(DEFAULT_ON_MIGRATION_KEYS)},
            ensure_ascii=False,
            indent=2,
        )
    except OSError:
        return migrated
    return migrated


def load_ui_feature_flags(path: str | Path | None = None) -> dict[str, bool]:
    """读取 UI 功能开关；文件缺失或损坏时返回安全默认值。"""

    target = Path(path) if path is not None else FEATURE_FLAGS_FILE
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_UI_FEATURE_FLAGS)
    normalized = normalize_ui_feature_flags(payload)
    if path is None:
        return _migrate_source_default_on_flags(normalized)
    return normalized


def save_ui_feature_flags(flags: Mapping[str, Any], path: str | Path | None = None) -> dict[str, bool]:
    """原子写入 UI 功能开关，并返回实际落盘的规范化配置。"""

    target = Path(path) if path is not None else FEATURE_FLAGS_FILE
    normalized = normalize_ui_feature_flags(flags)
    atomic_write_json(target, normalized, ensure_ascii=False, indent=2)
    return normalized
