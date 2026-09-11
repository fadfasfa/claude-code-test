"""显式真机诊断采集的有界会话。

本模块不创建线程，也不决定何时抓屏。生产循环只在用户显式开启诊断后调用
``plan``，再把返回的 draft 交给现有低优先级 writer 调用 ``write``。默认关闭时
不会创建目录或写入文件。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

from PIL import Image

from hextech.infrastructure.vision.capture_geometry import capture_regions_valid
from hextech.infrastructure.vision.sidecar_common import LayoutTransform, SLOT_COUNT, apply_transform
from hextech.infrastructure.vision.sidecar_scene_geometry import resolve_roi_preset


DIAGNOSTIC_CAPTURE_SCHEMA_VERSION = 1
DIAGNOSTIC_CAPTURE_ROOT = Path("debug") / "overlay_vision" / "explicit_capture_sessions"
MAX_FULL_CLIENT_FRAMES = 3
MAX_ROI_SETS = 12
MAX_SESSION_BYTES = 64 * 1024 * 1024
_MANIFEST_RESERVE_MAX = 64 * 1024


def _reject_reparse_path(path: Path) -> None:
    for component in (path, *path.parents):
        try:
            attributes = component.lstat()
        except FileNotFoundError:
            continue
        if component.is_symlink() or getattr(attributes, "st_file_attributes", 0) & 0x400:
            raise ValueError("diagnostic_reparse_path_rejected")
_FULL_CLIENT_MODES = frozenset({"client_full", "client_full_recovery"})
_TERMINAL_REASONS = frozenset(
    {
        "selection_completed",
        "scene_loss_confirmed",
        "gameflow_ended",
        "user_stopped",
        "budget_exhausted",
        "writer_failed",
        "queue_dropped",
    }
)


def _finite_number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(value) if isinstance(value, int) else number


def _int_list(value: object, *, length: int) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != length:
        return []
    try:
        return [int(item) for item in value]
    except (TypeError, ValueError):
        return []


def _float_list(value: object, *, limit: int) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[float] = []
    for item in list(value)[:limit]:
        number = _finite_number(item)
        if number is not None:
            result.append(float(number))
    return result


def _safe_text(value: object, *, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def _rgb_sha256(image: Image.Image) -> str:
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    digest = hashlib.sha256()
    digest.update(f"RGB:{rgb.width}x{rgb.height}\0".encode("ascii"))
    digest.update(rgb.tobytes())
    return digest.hexdigest()


def _png_bytes(image: Image.Image) -> bytes:
    stream = BytesIO()
    (image if image.mode == "RGB" else image.convert("RGB")).save(
        stream,
        format="PNG",
        optimize=False,
    )
    return stream.getvalue()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_exclusive(path: Path, payload: bytes) -> None:
    _reject_reparse_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def _write_replace(path: Path, payload: bytes) -> None:
    _reject_reparse_path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass(frozen=True)
class DiagnosticCaptureDraft:
    sequence: int
    frame: Image.Image | None
    event: dict[str, Any]
    include_full_client: bool
    include_roi_set: bool
    terminal_reason: str


class DiagnosticCaptureSession:
    """一次显式诊断会话；计数在 ``plan`` 时预约，队列积压不能越过上限。"""

    def __init__(
        self,
        var_dir: str | Path,
        *,
        enabled: bool = False,
        max_full_client_frames: int = MAX_FULL_CLIENT_FRAMES,
        max_roi_sets: int = MAX_ROI_SETS,
        max_bytes: int = MAX_SESSION_BYTES,
        clock: Any = time.time,
    ) -> None:
        self._enabled = bool(enabled)
        self._root = Path(var_dir) / DIAGNOSTIC_CAPTURE_ROOT
        self._max_full = max(0, min(MAX_FULL_CLIENT_FRAMES, int(max_full_client_frames)))
        self._max_roi = max(0, min(MAX_ROI_SETS, int(max_roi_sets)))
        self._max_bytes = max(1024, min(MAX_SESSION_BYTES, int(max_bytes)))
        self._manifest_reserve = min(
            _MANIFEST_RESERVE_MAX,
            max(2048, self._max_bytes // 4),
            max(512, self._max_bytes // 2),
        )
        self._clock = clock
        self._lock = Lock()
        self._plan_lock = Lock()
        self._pending_drops = 0
        self._last_planned_event: Mapping[str, Any] = {}
        self._session_id = ""
        self._session_path: Path | None = None
        self._started_at = 0.0
        self._sequence = 0
        self._planned_full = 0
        self._planned_roi = 0
        self._written_full = 0
        self._written_roi = 0
        self._written_observations = 0
        self._bytes_committed = 0
        self._failed_count = 0
        self._dropped_count = 0
        self._terminal_reason = ""
        self._terminal_at = 0.0
        self._last_error = ""
        self._observations: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def session_path(self) -> Path | None:
        return self._session_path

    def plan(
        self,
        frame: Image.Image | None,
        event: Mapping[str, Any],
        *,
        include_full_client: bool = False,
        include_roi_set: bool = True,
        terminal_reason: str = "",
    ) -> DiagnosticCaptureDraft | None:
        """预约一个有限任务；不编码图片、不创建目录。"""

        if not self._enabled:
            return None
        terminal = _safe_text(terminal_reason, limit=80)
        if terminal and terminal not in _TERMINAL_REASONS:
            terminal = "user_stopped"
        with self._plan_lock:
            if self._terminal_reason:
                return None
            want_full = bool(
                include_full_client
                and frame is not None
                and self._planned_full < self._max_full
            )
            want_roi = bool(
                include_roi_set
                and frame is not None
                and self._planned_roi < self._max_roi
            )
            if frame is None and not terminal:
                return None
            if not want_full and not want_roi and not terminal:
                return None
            self._sequence += 1
            self._last_planned_event = event
            if want_full:
                self._planned_full += 1
            if want_roi:
                self._planned_roi += 1
            return DiagnosticCaptureDraft(
                sequence=self._sequence,
                frame=frame,
                event=deepcopy({str(key): value for key, value in event.items()}),
                include_full_client=want_full,
                include_roi_set=want_roi,
                terminal_reason=terminal,
            )

    def record_drop(self, draft: DiagnosticCaptureDraft, *, reason: str = "queue_dropped") -> None:
        """现有 writer 拒绝任务时留下明确的不完整终态。"""

        if not self._enabled:
            return
        with self._plan_lock:
            self._pending_drops += 1
            self._last_planned_event = draft.event

    def write(self, draft: DiagnosticCaptureDraft) -> Path | None:
        """由既有后台 writer 调用；单个 observation 要么完整提交，要么只记失败。"""

        if not self._enabled:
            return None
        with self._lock:
            self._apply_pending_drops_locked()
            try:
                self._ensure_session_locked(draft.event)
            except (OSError, ValueError) as exc:
                self._failed_count += 1
                self._last_error = type(exc).__name__
                self._set_terminal_locked("writer_failed")
                return None
            if self._terminal_reason and not draft.terminal_reason:
                self._ensure_session_locked(draft.event)
                self._write_manifest_locked()
                return None
            self._ensure_session_locked(draft.event)
            assert self._session_path is not None
            try:
                record, assets = self._prepare_observation_locked(draft)
                metadata = _json_bytes(record)
                observation_bytes = sum(len(payload) for _, payload in assets) + len(metadata)
                if self._bytes_committed + observation_bytes > self._max_bytes - self._manifest_reserve:
                    self._last_error = "session_byte_budget_exhausted"
                    self._failed_count += 1
                    self._set_terminal_locked("budget_exhausted")
                    self._write_manifest_locked()
                    return None
                observations_root = self._session_path / "observations"
                _reject_reparse_path(observations_root)
                observations_root.mkdir(parents=True, exist_ok=True)
                target = observations_root / f"{draft.sequence:04d}"
                pending = observations_root / f".pending-{draft.sequence:04d}-{uuid.uuid4().hex}"
                _reject_reparse_path(pending)
                pending.mkdir(exist_ok=False)
                for relative, payload in assets:
                    _write_exclusive(pending / relative, payload)
                _write_exclusive(pending / "metadata.json", metadata)
                pending.replace(target)
                self._bytes_committed += observation_bytes
                self._written_observations += 1
                if draft.include_full_client:
                    self._written_full += 1
                if draft.include_roi_set:
                    self._written_roi += 1
                self._observations.append(
                    {
                        "sequence": draft.sequence,
                        "path": str(Path("observations") / f"{draft.sequence:04d}" / "metadata.json"),
                        "bytes": observation_bytes,
                        "frame_id": record["binding"]["captured_frame_id"],
                        "scene_state": record["recognition"]["scene_state"],
                        "reason": record["recognition"]["reason"],
                        "full_client": draft.include_full_client,
                        "roi_set": draft.include_roi_set,
                    }
                )
                if draft.terminal_reason:
                    self._set_terminal_locked(draft.terminal_reason)
                self._write_manifest_locked()
                return target
            except Exception as exc:
                self._failed_count += 1
                self._last_error = exc.__class__.__name__
                self._set_terminal_locked("writer_failed")
                self._write_manifest_locked()
                return None

    def finalize(self, *, reason: str = "user_stopped") -> Path | None:
        if not self._enabled:
            return None
        terminal = _safe_text(reason, limit=80)
        if terminal not in _TERMINAL_REASONS:
            terminal = "user_stopped"
        with self._lock:
            self._apply_pending_drops_locked()
            if self._session_path is None and self._last_planned_event:
                try:
                    self._ensure_session_locked(self._last_planned_event)
                except (OSError, ValueError):
                    self._failed_count += 1
                    self._last_error = "diagnostic_output_unavailable"
                    self._set_terminal_locked("writer_failed")
                    return None
            if self._session_path is None:
                return None
            self._set_terminal_locked(terminal)
            self._write_manifest_locked()
            return self._session_path / "manifest.json"

    def status(self) -> dict[str, Any]:
        with self._plan_lock:
            return {
                "enabled": self._enabled,
                "session_id": self._session_id,
                "session_path": str(self._session_path) if self._session_path is not None else "",
                "planned_full_client_frames": self._planned_full,
                "planned_roi_sets": self._planned_roi,
                "written_full_client_frames": self._written_full,
                "written_roi_sets": self._written_roi,
                "written_observations": self._written_observations,
                "bytes_committed": self._bytes_committed,
                "failed_count": self._failed_count,
                "dropped_count": self._dropped_count + self._pending_drops,
                "terminal_reason": self._terminal_reason,
                "last_error": self._last_error,
            }

    def _ensure_session_locked(self, event: Mapping[str, Any]) -> None:
        _reject_reparse_path(self._root)
        if self._session_path is not None:
            _reject_reparse_path(self._session_path)
            return
        source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
        self._started_at = float(self._clock())
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._started_at))
        session_hint = hashlib.sha256(str(source.get("session_id") or "unknown").encode()).hexdigest()[:12]
        self._session_id = f"{stamp}-{session_hint}-{uuid.uuid4().hex[:12]}"
        self._root.mkdir(parents=True, exist_ok=True)
        self._session_path = self._root / self._session_id
        self._session_path.mkdir(exist_ok=False)

    def _apply_pending_drops_locked(self) -> None:
        with self._plan_lock:
            drops, self._pending_drops = self._pending_drops, 0
        if drops:
            self._dropped_count += drops
            self._last_error = "queue_dropped"
            self._set_terminal_locked("queue_dropped")

    def _set_terminal_locked(self, reason: str) -> None:
        if not self._terminal_reason:
            self._terminal_reason = reason
            self._terminal_at = float(self._clock())

    def _prepare_observation_locked(
        self,
        draft: DiagnosticCaptureDraft,
    ) -> tuple[dict[str, Any], list[tuple[Path, bytes]]]:
        event = draft.event
        source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
        timing = event.get("timing") if isinstance(event.get("timing"), Mapping) else {}
        frame = draft.frame
        if frame is not None:
            rect = _int_list(source.get("client_rect"), length=4)
            captured = _finite_number(timing.get("captured_at"))
            dpi = _finite_number(source.get("dpi_scale"))
            if (not source.get("build_id") or not source.get("session_id") or not source.get("game_instance_id")
                or int(source.get("window_hwnd") or 0) <= 0 or int(source.get("frame_id") or 0) <= 0
                or len(rect) != 4 or (rect[2]-rect[0], rect[3]-rect[1]) != frame.size
                or captured is None or captured <= 0 or dpi is None or dpi <= 0
                or not (source.get("monitor") or source.get("monitor_id") or source.get("monitor_device") or source.get("target_monitor"))):
                raise ValueError("diagnostic_frame_binding_incomplete")
        assets: list[tuple[Path, bytes]] = []
        asset_records: list[dict[str, Any]] = []
        frame_sha = ""
        capture_mode = _safe_text(
            (frame.info.get("hextech_capture_mode") if frame is not None else "")
            or source.get("capture_mode")
            or "unknown",
            limit=40,
        )
        if frame is not None:
            frame_sha = _rgb_sha256(frame)
        if draft.include_full_client:
            if frame is None or capture_mode not in _FULL_CLIENT_MODES:
                raise ValueError("full_client_frame_required")
            client_size = _int_list(frame.info.get("hextech_client_size"), length=2)
            origin = _int_list(frame.info.get("hextech_roi_origin"), length=2)
            roi_size = _int_list(frame.info.get("hextech_roi_size"), length=2)
            if client_size != list(frame.size) or origin != [0, 0] or roi_size != list(frame.size):
                raise ValueError("full_client_metadata_invalid")
            payload = _png_bytes(frame)
            assets.append((Path("full_client.png"), payload))
            asset_records.append(self._asset_record("full_client", None, frame, payload, None))
        if draft.include_roi_set:
            if frame is None:
                raise ValueError("roi_frame_required")
            source_transform = source.get("layout_transform")
            transform_mapping = source_transform if isinstance(source_transform, Mapping) else {}
            transform = LayoutTransform(
                dx_ratio=float(transform_mapping.get("dx_ratio") or 0.0),
                dy_ratio=float(transform_mapping.get("dy_ratio") or 0.0),
                scale=float(transform_mapping.get("scale") or 1.0),
            )
            preset = resolve_roi_preset(*frame.size, preset=_safe_text(source.get("preset"), limit=40) or "auto")
            icon_boxes = [apply_transform(box, frame.size, transform) for box in preset.slots[:SLOT_COUNT]]
            name_boxes = [apply_transform(box, frame.size, transform) for box in preset.name_slots[:SLOT_COUNT]]
            if not capture_regions_valid(frame, (*icon_boxes, *name_boxes)):
                raise ValueError("roi_outside_valid_capture")
            for kind, boxes in (("icon", icon_boxes), ("name", name_boxes)):
                for slot, box in enumerate(boxes):
                    crop = frame.crop(box).convert("RGB")
                    payload = _png_bytes(crop)
                    relative = Path(f"{kind}_{slot}.png")
                    assets.append((relative, payload))
                    asset_records.append(self._asset_record(kind, slot, crop, payload, box))

        raw_slots = event.get("_raw_slots") if isinstance(event.get("_raw_slots"), list) else []
        rendered_slots = event.get("slots") if isinstance(event.get("slots"), list) else []
        slots: list[dict[str, Any]] = []
        for index in range(SLOT_COUNT):
            raw = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
            rendered = (
                rendered_slots[index]
                if index < len(rendered_slots) and isinstance(rendered_slots[index], Mapping)
                else {}
            )
            shadow = raw.get("ocr_shadow") if isinstance(raw.get("ocr_shadow"), Mapping) else {}
            production = raw.get("ocr_production") if isinstance(raw.get("ocr_production"), Mapping) else {}
            slots.append(
                {
                    "slot": index,
                    "state": _safe_text(rendered.get("state") or raw.get("state"), limit=40),
                    "augment_id": _safe_text(rendered.get("augment_id"), limit=80),
                    "name": _safe_text(rendered.get("name"), limit=160),
                    "acceptance_rule": _safe_text(rendered.get("acceptance_rule"), limit=80),
                    "rejection_reason": _safe_text(
                        rendered.get("rejection_reason") or raw.get("diagnostic"), limit=120
                    ),
                    "ocr_input_sha256": _safe_text(
                        production.get("rgb_sha256") or shadow.get("input_sha256"), limit=64
                    ),
                    "ocr_state": _safe_text(production.get("state") or shadow.get("state"), limit=40),
                    "ocr_text": _safe_text(shadow.get("normalized_text"), limit=160),
                }
            )
        client_rect = _int_list(source.get("client_rect"), length=4)
        if not client_rect and frame is not None:
            client_rect = _int_list(frame.info.get("hextech_client_rect"), length=4)
        valid_origin = (
            _int_list(frame.info.get("hextech_roi_origin"), length=2)
            if frame is not None
            else _int_list(source.get("capture_roi_origin"), length=2)
        )
        valid_size = (
            _int_list(frame.info.get("hextech_roi_size"), length=2)
            if frame is not None
            else _int_list(source.get("capture_roi_size"), length=2)
        )
        valid_rect = (
            [valid_origin[0], valid_origin[1], valid_origin[0] + valid_size[0], valid_origin[1] + valid_size[1]]
            if len(valid_origin) == 2 and len(valid_size) == 2
            else []
        )
        record = {
            "schema_version": DIAGNOSTIC_CAPTURE_SCHEMA_VERSION,
            "diagnostic_session_id": self._session_id,
            "observation_seq": draft.sequence,
            "recorded_at": float(self._clock()),
            "terminal_reason": draft.terminal_reason,
            "binding": {
                "build_id": _safe_text(source.get("build_id"), limit=120),
                "sidecar_instance_id": _safe_text(source.get("sidecar_instance_id"), limit=120),
                "sidecar_pid": int(source.get("sidecar_pid") or 0),
                "session_id": _safe_text(source.get("session_id"), limit=160),
                "game_instance_id": _safe_text(source.get("game_instance_id"), limit=160),
                "window_hwnd": int(source.get("window_hwnd") or 0),
                "client_rect": client_rect,
                "dpi_scale": _finite_number(source.get("dpi_scale")),
                "monitor": _safe_text(
                    source.get("monitor")
                    or source.get("monitor_id")
                    or source.get("monitor_device")
                    or source.get("target_monitor"),
                    limit=160,
                ),
                "selection_epoch": int(source.get("selection_epoch") or 0),
                "selection_revision": int(source.get("selection_revision") or 0),
                "captured_frame_id": int(source.get("frame_id") or 0),
                "frame_rgb_sha256": frame_sha,
            },
            "capture": {
                "mode": capture_mode,
                "frame_size": list(frame.size) if frame is not None else [],
                "screen_rect": _int_list(
                    frame.info.get("hextech_capture_screen_rect") if frame is not None else None,
                    length=4,
                ),
                "valid_origin": valid_origin,
                "valid_size": valid_size,
                "valid_rect": valid_rect,
                "capture_started_at": _finite_number(timing.get("capture_started_at")),
                "captured_at": _finite_number(timing.get("captured_at")),
                "recognition_completed_at": _finite_number(timing.get("recognition_completed_at")),
                "event_written_at": _finite_number(timing.get("event_written_at")),
            },
            "recognition": {
                "selection_type": _safe_text(event.get("selection_type"), limit=40),
                "public_active": bool(event.get("active")),
                "reason": _safe_text(source.get("reason"), limit=120),
                "scene_state": _safe_text(source.get("scene_state"), limit=40),
                "scene_kind": _safe_text(source.get("scene_kind"), limit=40),
                "scene_score": _finite_number(source.get("scene_score")),
                "scene_recovery_state": _safe_text(source.get("scene_recovery_state"), limit=80),
                "scene_recovery_edge_id": _safe_text(source.get("scene_recovery_edge_id"), limit=160),
                "scene_recovery_full_capture": bool(source.get("scene_recovery_full_capture")),
                "layout_transform": {
                    "dx_ratio": _finite_number(
                        source.get("layout_transform", {}).get("dx_ratio")
                        if isinstance(source.get("layout_transform"), Mapping)
                        else None
                    ),
                    "dy_ratio": _finite_number(
                        source.get("layout_transform", {}).get("dy_ratio")
                        if isinstance(source.get("layout_transform"), Mapping)
                        else None
                    ),
                    "scale": _finite_number(
                        source.get("layout_transform", {}).get("scale")
                        if isinstance(source.get("layout_transform"), Mapping)
                        else None
                    ),
                },
                "panel_scores": _float_list(source.get("panel_scores"), limit=3),
                "body_shard_scores": _float_list(source.get("body_shard_scores"), limit=3),
                "slots": slots,
            },
            "assets": asset_records,
        }
        return record, assets

    @staticmethod
    def _asset_record(
        kind: str,
        slot: int | None,
        image: Image.Image,
        png: bytes,
        source_box: Sequence[int] | None,
    ) -> dict[str, Any]:
        filename = "full_client.png" if slot is None else f"{kind}_{slot}.png"
        return {
            "kind": kind,
            "slot": slot,
            "path": filename,
            "source_box": list(source_box) if source_box is not None else [],
            "size": list(image.size),
            "rgb_sha256": _rgb_sha256(image),
            "png_sha256": hashlib.sha256(png).hexdigest(),
            "bytes": len(png),
        }

    def _manifest_payload_locked(self) -> dict[str, Any]:
        complete = bool(
            self._terminal_reason
            and self._terminal_reason not in {"budget_exhausted", "writer_failed", "queue_dropped"}
            and self._failed_count == 0
            and self._dropped_count == 0
        )
        return {
            "schema_version": DIAGNOSTIC_CAPTURE_SCHEMA_VERSION,
            "session_id": self._session_id,
            "started_at": self._started_at,
            "updated_at": float(self._clock()),
            "terminal_at": self._terminal_at or None,
            "terminal_reason": self._terminal_reason,
            "status": "complete" if complete else ("incomplete" if self._terminal_reason else "collecting"),
            "limits": {
                "full_client_frames": self._max_full,
                "roi_sets": self._max_roi,
                "bytes": self._max_bytes,
            },
            "counts": {
                "planned_full_client_frames": self._planned_full,
                "planned_roi_sets": self._planned_roi,
                "written_full_client_frames": self._written_full,
                "written_roi_sets": self._written_roi,
                "written_observations": self._written_observations,
                "failed": self._failed_count,
                "dropped": self._dropped_count,
            },
            "bytes_committed_excluding_manifest": self._bytes_committed,
            "last_error": self._last_error,
            "observations": list(self._observations),
        }

    def _write_manifest_locked(self) -> None:
        self._apply_pending_drops_locked()
        _reject_reparse_path(self._session_path or self._root)
        if self._session_path is None:
            return
        payload = _json_bytes(self._manifest_payload_locked())
        if len(payload) > self._manifest_reserve:
            self._last_error = self._last_error or "manifest_reserve_exhausted"
            self._terminal_reason = self._terminal_reason or "writer_failed"
            self._terminal_at = self._terminal_at or float(self._clock())
            payload = _json_bytes(
                {
                    "schema_version": DIAGNOSTIC_CAPTURE_SCHEMA_VERSION,
                    "session_id": self._session_id,
                    "status": "incomplete",
                    "terminal_reason": self._terminal_reason,
                    "last_error": self._last_error,
                }
            )
        _write_replace(self._session_path / "manifest.json", payload)


__all__ = [
    "DIAGNOSTIC_CAPTURE_ROOT",
    "MAX_FULL_CLIENT_FRAMES",
    "MAX_ROI_SETS",
    "MAX_SESSION_BYTES",
    "DiagnosticCaptureDraft",
    "DiagnosticCaptureSession",
]
