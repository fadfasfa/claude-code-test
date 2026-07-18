"""DataService 定期刷新状态的 v1 持久化实现。"""

from __future__ import annotations

import json
from pathlib import Path

from hextech.contracts import RefreshScheduleV1, RefreshSourceState, utc_now_iso
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir


SCHEDULE_SOURCES = ("catalog", "hextech", "apex", "mayhem")


class RefreshScheduleStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else get_var_dir()
        self.path = self.root / "state" / "data-service" / "refresh_schedule.v1.json"

    def load(self) -> RefreshScheduleV1:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("schedule 必须是对象")
            return RefreshScheduleV1.from_mapping(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return RefreshScheduleV1(
                updated_at=utc_now_iso(),
                sources={source: RefreshSourceState() for source in SCHEDULE_SOURCES},
            )

    def save(self, schedule: RefreshScheduleV1) -> None:
        atomic_write_json(self.path, schedule.to_dict(), ensure_ascii=False, indent=2)


__all__ = ["RefreshScheduleStore", "SCHEDULE_SOURCES"]
