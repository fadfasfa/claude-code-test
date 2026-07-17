"""官方本地接口 overlay provider 手工探针。

默认只读：采样 Riot / LoL 本地接口并打印状态。只有显式传入 `--write-event` 且
provider 返回完整三槽候选时，才写入现有 overlay 事件文件。

调用方: dev_checks; 关键依赖: overlay.providers.official、overlay.events。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.infrastructure.lcu.official_overlay import (  # noqa: E402
    STATUS_CANDIDATES_READY,
    OfficialOverlayProvider,
    snapshot_to_debug_payload,
)
from hextech.modules.vision.events import (  # noqa: E402
    OVERLAY_EVENT_FILE,
    build_inactive_overlay_event,
    build_overlay_event,
    write_overlay_event,
)


def _runtime_debug_dir() -> Path:
    return OVERLAY_EVENT_FILE.parents[1] / "debug" / "official_overlay_provider"


def write_official_overlay_event(
    snapshot: Mapping[str, Any],
    *,
    event_path: str | Path | None = None,
) -> Path:
    """把 candidates_ready 快照写入现有 overlay 事件协议。"""

    if snapshot.get("status") != STATUS_CANDIDATES_READY:
        raise ValueError("official overlay event can only be written for candidates_ready snapshots")
    event = build_overlay_event(
        snapshot.get("choices") if isinstance(snapshot.get("choices"), list) else [],
        source_tag="official-api",
        selection_type="hextech",
        active=True,
    )
    return write_overlay_event(event, event_path)


def write_official_inactive_overlay_event(*, event_path: str | Path | None = None) -> Path:
    """选择结束后写一次 official-api inactive，避免 overlay 等事件过期。"""

    event = build_inactive_overlay_event(source_tag="official-api")
    return write_overlay_event(event, event_path)


def dump_runtime_snapshot(snapshot: Mapping[str, Any], *, dump_dir: str | Path | None = None) -> Path:
    target_dir = Path(dump_dir) if dump_dir is not None else _runtime_debug_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"official-overlay-{stamp}-{int(time.time() * 1000) % 1000:03d}.json"
    target.write_text(json.dumps(snapshot_to_debug_payload(snapshot), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def run_probe(
    *,
    duration_seconds: float,
    interval_ms: int,
    dump_runtime_json: bool,
    write_event: bool,
    provider: OfficialOverlayProvider | None = None,
    event_path: str | Path | None = None,
    time_func: Callable[[], float] = time.time,
    sleep_func: Callable[[float], None] = time.sleep,
    emit_snapshots: bool = True,
) -> dict[str, Any]:
    provider = provider or OfficialOverlayProvider()
    started_at = time_func()
    deadline = started_at + max(0.0, float(duration_seconds))
    interval_seconds = max(0.05, int(interval_ms) / 1000.0)
    samples: list[dict[str, Any]] = []
    writes: list[str] = []
    previous_ready = False

    while True:
        snapshot = provider.get_snapshot()
        current_ready = snapshot.get("status") == STATUS_CANDIDATES_READY
        samples.append({"status": snapshot.get("status"), "diagnostics": snapshot.get("diagnostics", {})})
        if dump_runtime_json:
            dump_runtime_snapshot(snapshot)
        if write_event and current_ready:
            writes.append(str(write_official_overlay_event(snapshot, event_path=event_path)))
        elif write_event and previous_ready:
            writes.append(str(write_official_inactive_overlay_event(event_path=event_path)))

        if emit_snapshots:
            print(json.dumps(snapshot_to_debug_payload(snapshot), ensure_ascii=False))
        previous_ready = current_ready
        if time_func() >= deadline:
            break
        sleep_func(interval_seconds)

    return {
        "sample_count": len(samples),
        "statuses": [sample["status"] for sample in samples],
        "event_writes": writes,
        "duration_seconds": round(time_func() - started_at, 3),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="探测 Riot / LoL 官方本地接口是否提供三槽候选。")
    parser.add_argument("--duration-seconds", type=float, default=5.0)
    parser.add_argument("--interval-ms", type=int, default=500)
    parser.add_argument("--dump-runtime-json", action="store_true")
    parser.add_argument("--write-event", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_probe(
        duration_seconds=args.duration_seconds,
        interval_ms=args.interval_ms,
        dump_runtime_json=bool(args.dump_runtime_json),
        write_event=bool(args.write_event),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
