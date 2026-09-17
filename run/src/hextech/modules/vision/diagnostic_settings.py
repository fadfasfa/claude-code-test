"""Overlay 受限 ROI 诊断开关。

Desktop 启动 Sidecar 时读取；部署器只在用户显式选择 on/off 时写入。它只控制
受限 ROI，不保存完整游戏画面，也不负责识别阈值或模板选择。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir


OVERLAY_DIAGNOSTIC_SETTINGS_FILENAME = "overlay_diagnostic_settings.v1.json"
ROI_DUMP_ENV = "HEXTECH_OVERLAY_SIDECAR_DEBUG_DUMP"
SELECTION_CAPTURE_ENV = "HEXTECH_SELECTION_CAPTURE_ENABLED"
RoiDumpMode = Literal["preserve", "on", "off"]


def selection_capture_enabled() -> bool:
    """默认开启；仅显式关闭值禁用自动缓存，供同条件性能对照。"""
    return os.getenv(SELECTION_CAPTURE_ENV, "1").strip().lower() not in {"0", "false", "off", "no"}


def overlay_diagnostic_settings_path(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else get_var_dir()
    return base / "state" / OVERLAY_DIAGNOSTIC_SETTINGS_FILENAME


def _parse_bool(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def load_overlay_diagnostic_settings(path: Path | None = None) -> dict[str, object]:
    target = Path(path) if path is not None else overlay_diagnostic_settings_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema_version": 1, "roi_dump_enabled": False}
    if not isinstance(payload, Mapping):
        return {"schema_version": 1, "roi_dump_enabled": False}
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError):
        return {"schema_version": 1, "roi_dump_enabled": False}
    if schema_version != 1:
        return {"schema_version": 1, "roi_dump_enabled": False}
    return {"schema_version": 1, "roi_dump_enabled": payload.get("roi_dump_enabled") is True}


def resolve_roi_dump_enabled(
    explicit: bool | None = None,
    *,
    env: Mapping[str, str] | None = None,
    settings_path: Path | None = None,
) -> bool:
    """显式参数、显式环境变量、持久设置、默认关闭，依次解析。"""

    if explicit is not None:
        return bool(explicit)
    environment = os.environ if env is None else env
    if ROI_DUMP_ENV in environment:
        return _parse_bool(environment.get(ROI_DUMP_ENV))
    return bool(load_overlay_diagnostic_settings(settings_path)["roi_dump_enabled"])


def set_roi_dump_mode(mode: RoiDumpMode, *, path: Path | None = None) -> dict[str, object] | None:
    """部署使用的原子写入口；preserve 不读取也不改写用户现有设置。"""

    if mode == "preserve":
        return None
    if mode not in {"on", "off"}:
        raise ValueError(f"ROI dump mode 无效：{mode}")
    payload: dict[str, object] = {"schema_version": 1, "roi_dump_enabled": mode == "on"}
    atomic_write_json(
        Path(path) if path is not None else overlay_diagnostic_settings_path(),
        payload,
        ensure_ascii=False,
        indent=2,
    )
    return payload


__all__ = [
    "OVERLAY_DIAGNOSTIC_SETTINGS_FILENAME",
    "ROI_DUMP_ENV",
    "RoiDumpMode",
    "load_overlay_diagnostic_settings",
    "overlay_diagnostic_settings_path",
    "resolve_roi_dump_enabled",
    "set_roi_dump_mode",
]
