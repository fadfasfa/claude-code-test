"""桌面 UI 功能开关运行态配置。

本模块只保存 Web 前端、游戏内显示、浏览器自动打开、私用统计显示和低频监听的
用户偏好。配置落在 `data/runtime/state/`，属于运行态，不作为发布源数据提交。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from processing.runtime_store import build_runtime_state_path
from tools.atomic_io import atomic_write_json


FEATURE_FLAGS_FILE = Path(build_runtime_state_path("ui_feature_flags.json"))

DEFAULT_UI_FEATURE_FLAGS: dict[str, bool] = {
    "web_frontend_enabled": False,
    "game_overlay_enabled": False,
    "auto_open_browser": True,
    "private_policy_stats_enabled": False,
    "low_frequency_listener_enabled": True,
}


def normalize_ui_feature_flags(raw_flags: Mapping[str, Any] | None) -> dict[str, bool]:
    """把未知或损坏字段收口到稳定布尔配置，避免运行态文件污染 UI 状态。"""

    normalized = dict(DEFAULT_UI_FEATURE_FLAGS)
    if not isinstance(raw_flags, Mapping):
        return normalized
    for key in normalized:
        if key in raw_flags:
            normalized[key] = bool(raw_flags[key])
    return normalized


def load_ui_feature_flags(path: str | Path | None = None) -> dict[str, bool]:
    """读取 UI 功能开关；文件缺失或损坏时返回安全默认值。"""

    target = Path(path) if path is not None else FEATURE_FLAGS_FILE
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_UI_FEATURE_FLAGS)
    return normalize_ui_feature_flags(payload)


def save_ui_feature_flags(flags: Mapping[str, Any], path: str | Path | None = None) -> dict[str, bool]:
    """原子写入 UI 功能开关，并返回实际落盘的规范化配置。"""

    target = Path(path) if path is not None else FEATURE_FLAGS_FILE
    normalized = normalize_ui_feature_flags(flags)
    atomic_write_json(target, normalized, ensure_ascii=False, indent=2)
    return normalized

