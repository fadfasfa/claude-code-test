"""桌面启动 timing 诊断。

本模块只写轻量 runtime state 文件，用于判断首屏和后台服务是否进入预期预算。
它不启动服务、不访问网络，也不参与生产业务决策。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hextech.modules.data.ports.paths import RUNTIME_DATA_DIR
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.session.build_identity import current_build_id


def build_desktop_runtime_state_path(filename: str) -> Path:
    """返回 desktop 轻量启动阶段可用的 runtime state 路径。"""

    name = str(filename or "").strip()
    if not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError(f"invalid runtime state filename: {filename!r}")
    return Path(RUNTIME_DATA_DIR) / "state" / name


STARTUP_TIMING_FILE = build_desktop_runtime_state_path("startup_timing.v1.json")
STARTUP_TIMING_SCHEMA_VERSION = 1


class StartupTimingProbe:
    """记录 desktop 冷启动阶段耗时，并 best-effort 写入 runtime state。"""

    def __init__(self, *, output_path: str | Path | None = None) -> None:
        self.started_at = time.perf_counter()
        self.wall_started_at = time.time()
        self.output_path = Path(output_path) if output_path is not None else STARTUP_TIMING_FILE
        self._marks: list[dict[str, Any]] = []
        # Build 一致性核对要求所有 runtime state 都能自证来源；启动即取一次，
        # 失败降级为空串而不阻塞启动。
        try:
            self._build_id = str(current_build_id() or "")
        except Exception:
            self._build_id = ""

    def mark(self, name: str, **fields: Any) -> None:
        elapsed_ms = round((time.perf_counter() - self.started_at) * 1000.0, 3)
        payload = {
            "name": str(name),
            "elapsed_ms": elapsed_ms,
            "at": time.time(),
        }
        payload.update(_json_safe_fields(fields))
        self._marks.append(payload)
        self.flush()

    def flush(self) -> None:
        data = {
            "schema_version": STARTUP_TIMING_SCHEMA_VERSION,
            "build_id": self._build_id,
            "generated_at": time.time(),
            "started_at": self.wall_started_at,
            "marks": list(self._marks),
        }
        try:
            atomic_write_json(self.output_path, data, ensure_ascii=False, indent=2)
        except Exception:
            # timing 诊断不能影响桌面启动。
            return


def _json_safe_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value
        else:
            safe[str(key)] = str(value)
    return safe
