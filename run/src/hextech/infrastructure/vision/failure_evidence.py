"""Overlay 识别失败的有界、脱敏 ROI 证据链。

只保存名称、图标和选择按钮 ROI；不保存完整游戏画面，不参与识别决策，也不
自动生成或晋升 exemplar。公共槽继续保持 detecting，证据写入失败仅进入 journal。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import tempfile
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Thread
from typing import Any, Mapping, Sequence

from PIL import Image

from hextech.infrastructure.vision.sidecar_common import LayoutTransform, apply_transform
from hextech.infrastructure.vision.sidecar_scene_geometry import resolve_roi_preset
from hextech.infrastructure.vision.slot_evidence import slot_evidence_fingerprint
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.session.build_identity import get_build_identity


FAILURE_EVIDENCE_SCHEMA_VERSION = 1
FAILURE_INBOX_INDEX_SCHEMA_VERSION = 1
FAILURE_WRITER_MAX_QUEUE = 32
FAILURE_RECORD_LIMIT = 200
FAILURE_OCCURRENCE_LIMIT = 20


def failure_inbox_root() -> Path:
    """始终使用当前运行态根；源码默认是本 worktree 的 ignored ``run/var``。"""

    return get_var_dir() / "recognition" / "failure-inbox"


@dataclass(frozen=True)
class RoiImage:
    mode: str
    size: tuple[int, int]
    pixels: bytes

    @classmethod
    def from_image(cls, image: Image.Image) -> "RoiImage":
        normalized = image.convert("RGB")
        return cls("RGB", normalized.size, normalized.tobytes())

    def to_image(self) -> Image.Image:
        return Image.frombytes(self.mode, self.size, self.pixels)


@dataclass(frozen=True)
class FailureEvidenceDraft:
    slot_key: str
    fingerprint: str
    record: dict[str, Any]
    title_crop: RoiImage
    icon_crop: RoiImage
    button_crop: RoiImage


def _png_bytes(image: RoiImage) -> bytes:
    stream = io.BytesIO()
    image.to_image().save(stream, format="PNG", optimize=False)
    return stream.getvalue()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _blob(root: Path, image: RoiImage) -> dict[str, Any]:
    content = _png_bytes(image)
    sha256 = hashlib.sha256(content).hexdigest()
    relative = Path("blobs") / f"{sha256}.png"
    target = root / relative
    if not target.exists():
        _atomic_write_bytes(target, content)
    elif hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
        raise OSError(f"failure evidence blob hash mismatch: {target.name}")
    return {"relative_path": relative.as_posix(), "sha256": sha256, "byte_size": len(content)}


def _empty_index() -> dict[str, Any]:
    return {"schema_version": FAILURE_INBOX_INDEX_SCHEMA_VERSION, "records": {}, "slot_keys": {}}


def load_failure_index(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / "index.v1.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty_index()
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != FAILURE_INBOX_INDEX_SCHEMA_VERSION:
        return _empty_index()
    return {
        "schema_version": FAILURE_INBOX_INDEX_SCHEMA_VERSION,
        "records": dict(payload.get("records") or {}),
        "slot_keys": dict(payload.get("slot_keys") or {}),
    }


class FailureEvidenceWriter:
    """低优先级有界 writer；重复感知指纹只增加 occurrence，不复制图片。"""

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_queue: int = FAILURE_WRITER_MAX_QUEUE,
        record_limit: int = FAILURE_RECORD_LIMIT,
        retention_worker: Any | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else failure_inbox_root()
        self.max_queue = max(1, int(max_queue))
        self.record_limit = max(1, int(record_limit))
        self._retention_worker = retention_worker
        self._condition = Condition()
        self._tasks: deque[FailureEvidenceDraft] = deque()
        self._journal: deque[dict[str, Any]] = deque(maxlen=200)
        self._thread: Thread | None = None
        self._stopping = False
        self._active = False
        self._submitted_count = 0
        self._completed_count = 0
        self._dropped_count = 0
        self._failed_count = 0
        self._last_error = ""

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._thread = Thread(target=self._run, name="overlay-failure-evidence", daemon=True)
            self._thread.start()

    def submit(self, draft: FailureEvidenceDraft) -> bool:
        # 延迟启动：没有 evidence-starved 槽时不创建后台线程，也让短路测试/once
        # 模式保持无副作用。
        self.start()
        with self._condition:
            duplicate = next((item for item in self._tasks if item.fingerprint == draft.fingerprint), None)
            if duplicate is not None:
                # queued duplicate 只保留首张 ROI；slot_key 会在持久层 occurrence 中补入。
                duplicate.record.setdefault("queued_occurrences", []).append(
                    {"slot_key": draft.slot_key, **_occurrence(draft.record)}
                )
                return True
            if len(self._tasks) >= self.max_queue:
                self._journal.append(_journal(draft.slot_key, "queue_full_unique_record"))
                self._dropped_count += 1
                return False
            self._tasks.append(draft)
            self._submitted_count += 1
            self._condition.notify_all()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._tasks and not self._stopping:
                    self._condition.wait()
                if not self._tasks and self._stopping:
                    return
                draft = self._tasks.popleft()
                self._active = True
            try:
                self._persist(draft)
            except Exception as exc:
                with self._condition:
                    self._journal.append(_journal(draft.slot_key, exc.__class__.__name__))
                    self._failed_count += 1
                    self._last_error = exc.__class__.__name__
            finally:
                with self._condition:
                    self._completed_count += 1
                    self._active = False
                    self._condition.notify_all()

    def _persist(self, draft: FailureEvidenceDraft) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        index = load_failure_index(self.root)
        records: dict[str, Any] = index["records"]
        slot_keys: dict[str, Any] = index["slot_keys"]
        record_id = hashlib.sha256(draft.fingerprint.encode("utf-8")).hexdigest()[:24]
        if record_id not in records and len(records) >= self.record_limit:
            # writer 只负责 append/更新；旧记录与 blob 的淘汰统一交给
            # diagnostic_retention，避免两个线程/进程同时改 index 和文件。
            with self._condition:
                self._journal.append(_journal(draft.slot_key, "record_limit_reached"))
                self._dropped_count += 1
            request = getattr(self._retention_worker, "request", None)
            if callable(request):
                request()
            return
        entry = dict(records.get(record_id) or {})
        existing_record: dict[str, Any] | None = None
        if entry:
            record_path = self.root / str(entry["record_relative_path"])
            existing_record = json.loads(record_path.read_text(encoding="utf-8"))
        previous_occurrences = list(existing_record.get("occurrences") or []) if existing_record is not None else []
        new_occurrences = [{"slot_key": draft.slot_key, **_occurrence(draft.record)}]
        new_occurrences.extend(list(draft.record.get("queued_occurrences") or []))
        occurrences = (previous_occurrences + new_occurrences)[-FAILURE_OCCURRENCE_LIMIT:]
        previous_total = int(
            (existing_record or {}).get("occurrence_total_count")
            or (existing_record or {}).get("occurrence_count")
            or len(previous_occurrences)
        )
        occurrence_total = previous_total + len(new_occurrences)

        if not entry:
            record = {
                **deepcopy(draft.record),
                "schema_version": FAILURE_EVIDENCE_SCHEMA_VERSION,
                "record_id": record_id,
                "evidence_fingerprint": draft.fingerprint,
                "automatic_exemplar_eligible": False,
                "requires_manual_truth": True,
                "title_roi": _blob(self.root, draft.title_crop),
                "icon_roi": _blob(self.root, draft.icon_crop),
                "button_roi": _blob(self.root, draft.button_crop),
                "occurrences": occurrences,
                "occurrence_count": occurrence_total,
                "occurrence_total_count": occurrence_total,
            }
        else:
            record = existing_record or {}
            record["occurrences"] = occurrences
            record["occurrence_count"] = occurrence_total
            record["occurrence_total_count"] = occurrence_total
        record.pop("queued_occurrences", None)
        record.pop("record_sha256", None)
        record_bytes = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        record["record_sha256"] = hashlib.sha256(record_bytes).hexdigest()
        relative = Path("records") / f"{record_id}.json"
        atomic_write_json(self.root / relative, record, ensure_ascii=False, indent=2)
        records[record_id] = {
            "record_relative_path": relative.as_posix(),
            "evidence_fingerprint": draft.fingerprint,
            "occurrence_count": occurrence_total,
            "retained_occurrence_count": len(occurrences),
        }
        for slot_key, linked_record_id in list(slot_keys.items()):
            if linked_record_id == record_id:
                slot_keys.pop(slot_key, None)
        for occurrence in occurrences:
            slot_key = str(occurrence.get("slot_key") or "")
            if slot_key:
                slot_keys[slot_key] = record_id
        atomic_write_json(self.root / "index.v1.json", index, ensure_ascii=False, indent=2)
        request = getattr(self._retention_worker, "request", None)
        if callable(request):
            request()

    def wait_empty(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while (self._tasks or self._active) and time.monotonic() < deadline:
                self._condition.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            return not self._tasks and not self._active

    def close(self, timeout: float = 5.0) -> None:
        self.wait_empty(timeout)
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))

    def drain_journal_events(self) -> list[dict[str, Any]]:
        with self._condition:
            result = list(self._journal)
            self._journal.clear()
            return result

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "queue_depth": len(self._tasks),
                "active": self._active,
                "journal_count": len(self._journal),
                "submitted": self._submitted_count,
                "completed": self._completed_count,
                "dropped": self._dropped_count,
                "failed": self._failed_count,
                "last_error": self._last_error,
            }


def _journal(slot_key: str, detail: str) -> dict[str, Any]:
    return {
        "reason": "failure_evidence_write_failed",
        "detail": detail,
        "slot_key": slot_key,
        "recorded_at": time.time(),
    }


def _occurrence(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(record.get("session_id") or ""),
        "game_instance_id": str(record.get("game_instance_id") or ""),
        "selection_epoch": int(record.get("selection_epoch") or 0),
        "slot_generation": int(record.get("slot_generation") or 0),
        "slot_index": int(record.get("slot_index") or 0),
        "frame_id": int(record.get("frame_id") or 0),
        "captured_at": _finite_diagnostic_number(record.get("captured_at")),
        **({"roi_geometry": deepcopy(record["roi_geometry"])} if isinstance(record.get("roi_geometry"), Mapping) else {}),
    }


def load_failure_index_record(root: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads((root / str(entry.get("record_relative_path") or "")).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _channel_diagnostics(raw_slot: Mapping[str, Any]) -> dict[str, Any]:
    channels = raw_slot.get("channels") if isinstance(raw_slot.get("channels"), Mapping) else {}
    result: dict[str, Any] = {}
    for name, channel in channels.items():
        if not isinstance(channel, Mapping):
            continue
        candidates = channel.get("top_candidates") if isinstance(channel.get("top_candidates"), list) else []
        result[str(name)] = {
            "confidence": float(candidates[0].get("confidence") or 0.0) if candidates and isinstance(candidates[0], Mapping) else 0.0,
            "margin": float(channel.get("margin") or 0.0),
            "top_candidates": [dict(item) for item in candidates[:3] if isinstance(item, Mapping)],
        }
    return result


def _finite_diagnostic_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _roi_geometry(frame: Image.Image, source: Mapping[str, Any], title, icon, button) -> dict[str, Any]:
    """记录实际裁剪输入的有限几何，不复制任意source字段或增加PNG。"""
    raw = source.get("layout_transform")
    transform = raw if isinstance(raw, Mapping) else {}
    origin, size = frame.info.get("hextech_roi_origin"), frame.info.get("hextech_roi_size")
    capture_rect: list[int] = []
    if isinstance(origin, (tuple, list)) and len(origin) == 2 and isinstance(size, (tuple, list)) and len(size) == 2:
        values = [*origin, *size]
        if all(isinstance(v, int) and not isinstance(v, bool) for v in values) and size[0] > 0 and size[1] > 0:
            capture_rect = [origin[0], origin[1], origin[0] + size[0], origin[1] + size[1]]
    return {
        "coordinate_space": "physical_client", "frame_size": list(frame.size),
        "title_box": list(title), "icon_box": list(icon), "button_box": list(button),
        "capture_rect": capture_rect,
        "title_inside_capture": (
            capture_rect[0] <= title[0] < title[2] <= capture_rect[2]
            and capture_rect[1] <= title[1] < title[3] <= capture_rect[3]
        ) if capture_rect else None,
        "layout_transform": {key: _finite_diagnostic_number(transform.get(key) or default)
                             for key, default in (("dx_ratio", 0.0), ("dy_ratio", 0.0), ("scale", 1.0))},
    }


class FailureEvidenceCollector:
    """同一槽代选最佳 ROI；达到 evidence_starved 时提交一次，公共状态不变。"""

    def __init__(self, writer: FailureEvidenceWriter, *, pool_id: str) -> None:
        self.writer = writer
        self.pool_id = pool_id
        self._best: dict[tuple[str, int, int, int], tuple[float, FailureEvidenceDraft]] = {}
        self._recorded: set[tuple[str, int, int, int]] = set()

    @staticmethod
    def _boxes(frame: Image.Image, source: Mapping[str, Any], index: int):
        preset = resolve_roi_preset(*frame.size, preset=str(source.get("preset") or "auto"))
        raw = source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {}
        transform = LayoutTransform(
            dx_ratio=float(raw.get("dx_ratio") or 0.0),
            dy_ratio=float(raw.get("dy_ratio") or 0.0),
            scale=float(raw.get("scale") or 1.0),
        )
        title = apply_transform(preset.name_slots[index], frame.size, transform)
        icon = apply_transform(preset.slots[index], frame.size, transform)
        button_raw = source.get("button_box") if isinstance(source.get("button_box"), list) else []
        button = tuple(int(value) for value in button_raw) if len(button_raw) == 4 else (0, 0, 1, 1)
        return title, icon, button

    def observe(
        self,
        frame: Image.Image,
        raw_event: Mapping[str, Any],
        event: Mapping[str, Any],
        *,
        slot_generations: Sequence[int] | None = None,
    ) -> list[str]:
        source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
        raw_slots = raw_event.get("_raw_slots") if isinstance(raw_event.get("_raw_slots"), list) else []
        slots = event.get("slots") if isinstance(event.get("slots"), list) else []
        clean = bool(
            source.get("scene_present")
            and source.get("scene_state") in {"candidate", "active"}
            and not source.get("blocking_modal")
            and not source.get("scoreboard_key_down")
        )
        cursor_slots = set(source.get("cursor_over_slots") or []) if isinstance(source.get("cursor_over_slots"), list) else set()
        submitted: list[str] = []
        for index in range(min(3, len(slots))):
            slot = slots[index] if isinstance(slots[index], Mapping) else {}
            raw_slot = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
            session_id = str(source.get("session_id") or "")
            epoch = int(source.get("selection_epoch") or 0)
            generation = int(
                slot_generations[index]
                if slot_generations is not None and index < len(slot_generations)
                else slot.get("slot_generation") or 0
            )
            if not session_id or epoch <= 0 or generation <= 0:
                continue
            key = (session_id, epoch, index, generation)
            state = str(slot.get("state") or "detecting")
            temporal_state = str(slot.get("temporal_state") or "")
            if state == "ready":
                self._best.pop(key, None)
                continue
            if clean and index not in cursor_slots and state == "detecting":
                title_box, icon_box, button_box = self._boxes(frame, source, index)
                title = frame.crop(title_box).convert("RGB")
                icon = frame.crop(icon_box).convert("RGB")
                button = frame.crop(button_box).convert("RGB")
                fingerprint = str(raw_slot.get("evidence_fingerprint") or slot_evidence_fingerprint(title, icon))
                top_candidates = raw_slot.get("top_candidates") if isinstance(raw_slot.get("top_candidates"), list) else []
                confidence = float(top_candidates[0].get("confidence") or 0.0) if top_candidates and isinstance(top_candidates[0], Mapping) else 0.0
                record = {
                    "session_id": session_id,
                    "game_instance_id": str(source.get("game_instance_id") or ""),
                    "selection_epoch": epoch,
                    "slot_generation": generation,
                    "slot_index": index,
                    "frame_id": int(source.get("frame_id") or 0),
                    "build_id": str(get_build_identity().get("build_id") or "dev"),
                    "pool_id": self.pool_id,
                    "layout_id": str(source.get("layout_id") or ""),
                    "capture_size": list(source.get("capture_size") or []),
                    "roi_geometry": _roi_geometry(frame, source, title_box, icon_box, button_box),
                    "captured_at": _finite_diagnostic_number(
                        (raw_event.get("timing") or {}).get("captured_at")
                        if isinstance(raw_event.get("timing"), Mapping) else None
                    ),
                    "scene_type": str(source.get("scene_kind") or "hextech"),
                    "failure_reason": str(slot.get("rejection_reason") or raw_slot.get("diagnostic") or temporal_state),
                    "top_candidates": [dict(item) for item in top_candidates[:3] if isinstance(item, Mapping)],
                    "channel_diagnostics": _channel_diagnostics(raw_slot),
                }
                draft = FailureEvidenceDraft(
                    slot_key=f"{session_id}:{epoch}:{index}:{generation}",
                    fingerprint=fingerprint,
                    record=record,
                    title_crop=RoiImage.from_image(title),
                    icon_crop=RoiImage.from_image(icon),
                    button_crop=RoiImage.from_image(button),
                )
                rank = confidence + min(1.0, len(top_candidates) / 3.0) * 0.01
                if key not in self._best or rank > self._best[key][0]:
                    self._best[key] = (rank, draft)
            if temporal_state != "evidence_starved" or key in self._recorded or not clean:
                continue
            best = self._best.get(key)
            if best is not None and self.writer.submit(best[1]):
                submitted.append(best[1].slot_key)
            self._recorded.add(key)
            self._best.pop(key, None)
        return submitted


__all__ = [
    "FailureEvidenceCollector",
    "FailureEvidenceDraft",
    "FailureEvidenceWriter",
    "RoiImage",
    "failure_inbox_root",
    "load_failure_index",
]
