"""卡名 ROI 的 recognition-only OCR Shadow 运行时。

本模块只在单独 CPU 线程中识别现有三个卡名 ROI，并把结果写入私有诊断字段。
它不拥有场景检测、候选生成、READY、槽位替换或公共 Overlay event。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from PIL import Image

from hextech.infrastructure.vision.ocr_preprocess import (
    OCR_BRIGHT_TEXT_PADDING_PX,
    OCR_BRIGHT_TEXT_THRESHOLD,
    prepare_ocr_crop,
)
from hextech.infrastructure.vision.ocr_scheduler_metrics import (
    BoundedOcrSchedulerTiming,
    percentile,
)
from hextech.infrastructure.vision.ocr_completed import (
    OcrBatch as _OcrBatch,
    CompletedOcrMailbox,
    OcrEvidenceContext,
    build_production_evidence,
    production_needed,
)
from hextech.infrastructure.vision.scene_negative import classify_scene_negative
from hextech.modules.data.ports.paths import resource_path


OCR_SHADOW_ENABLED_ENV = "HEXTECH_OCR_SHADOW_ENABLED"
OCR_MODE_ENV = "HEXTECH_OCR_MODE"
OcrMode = Literal["off", "observe", "admit"]
OCR_MODEL_FILENAME = "ch_PP-OCRv4_rec_infer.onnx"
OCR_MODEL_SIZE = 10_857_958
OCR_MODEL_SHA256 = "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b"
OCR_IMAGE_HEIGHT = 48
OCR_BASE_IMAGE_WIDTH = 320
# Shadow 宁可延后诊断也不能抢占生产匹配；latest-only 队列会自然丢弃旧批次。
OCR_INTRA_OP_THREADS = 1
OCR_CACHE_CAPACITY = 256
OCR_LATENCY_WINDOW = 512
# Shadow 没有用户可见时效要求。三槽推理约占用一个生产识别帧的 CPU，若每帧
# 都开跑会把主链 P95 拉高；保留最新批次并限频，既能持续采样又不抢占 READY。
OCR_MIN_BATCH_INTERVAL_SECONDS = 5.0
OCR_PRODUCTION_MIN_CONFIDENCE = 0.95
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def ocr_input_sha256(image: Image.Image) -> str:
    """对 OCR 规范化模型输入做精确摘要；不使用会跨文本碰撞的感知键。"""

    normalized = _resize_norm_batch((image,))
    digest = hashlib.sha256()
    digest.update(b"OCRMODELINPUTv2\0")
    digest.update(str(normalized.shape).encode("ascii"))
    digest.update(normalized.tobytes(order="C"))
    return digest.hexdigest()


def ocr_shadow_enabled(value: str | None = None) -> bool:
    """候选默认开启 Shadow；环境变量可显式关闭以执行同 corpus A/B。"""

    raw = str(value if value is not None else os.getenv(OCR_SHADOW_ENABLED_ENV, "1")).strip().lower()
    return raw not in {"0", "false", "off", "no", "disabled"}


def ocr_mode(value: str | None = None, *, legacy_enabled: str | None = None) -> OcrMode:
    """解析 off/observe/admit；旧布尔开关只映射到 off/observe。"""

    raw = str(value if value is not None else os.getenv(OCR_MODE_ENV, "")).strip().lower()
    if raw in {"off", "observe", "admit"}:
        return raw  # type: ignore[return-value]
    legacy = legacy_enabled if legacy_enabled is not None else os.getenv(OCR_SHADOW_ENABLED_ENV)
    if legacy is not None:
        return "observe" if ocr_shadow_enabled(legacy) else "off"
    return "admit"


def normalize_ocr_text(value: object) -> str:
    """保留中英文、数字并移除空白/装饰符，供严格词表规则比较。"""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, right_char in enumerate(right, start=1):
        current = [row]
        for column, left_char in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class OcrVocabularyEntry:
    canonical_id: str
    name: str
    normalized_name: str


def build_ocr_vocabulary(template_index: Sequence[Any]) -> tuple[OcrVocabularyEntry, ...]:
    """只从当前 production template index 建立固定词表。"""

    entries: list[OcrVocabularyEntry] = []
    seen: set[tuple[str, str]] = set()
    for template in template_index:
        canonical_id = str(getattr(template, "augment_id", "") or "").strip()
        name = str(getattr(template, "name", "") or "").strip()
        normalized = normalize_ocr_text(name)
        key = canonical_id, normalized
        if not canonical_id or key in seen:
            continue
        seen.add(key)
        entries.append(OcrVocabularyEntry(canonical_id, name, normalized))
    return tuple(entries)


def match_ocr_text(raw_text: object, vocabulary: Sequence[OcrVocabularyEntry]) -> dict[str, Any]:
    """精确或唯一包含才接受；编辑距离 Top-3 永远只供诊断。"""

    text = str(raw_text or "")
    normalized = normalize_ocr_text(text)
    exact = [
        entry
        for entry in vocabulary
        if normalized and entry.normalized_name and entry.normalized_name == normalized
    ]
    accepted: OcrVocabularyEntry | None = exact[0] if len(exact) == 1 else None
    rule = "exact" if accepted is not None else ""
    if accepted is None and normalized:
        contained = [
            entry
            for entry in vocabulary
            if len(entry.normalized_name) >= 3
            and entry.normalized_name in normalized
            and len(normalized) - len(entry.normalized_name) <= 2
        ]
        if len(contained) == 1:
            accepted = contained[0]
            rule = "unique_containment"

    ranked = sorted(
        (
            (_levenshtein(normalized, entry.normalized_name), abs(len(normalized) - len(entry.normalized_name)), entry)
            for entry in vocabulary
            if entry.normalized_name
        ),
        key=lambda item: (item[0], item[1], item[2].name, item[2].canonical_id),
    )[:3]
    return {
        "normalized_text": normalized,
        "matched_id": accepted.canonical_id if accepted is not None else "",
        "matched_name": accepted.name if accepted is not None else "",
        "match_rule": rule,
        "top3": [
            {
                "canonical_id": entry.canonical_id,
                "name": entry.name,
                "distance": distance,
            }
            for distance, _length_delta, entry in ranked
        ],
    }


def _resize_norm_batch(images: Sequence[Image.Image]) -> np.ndarray:
    if not images:
        return np.empty((0, 3, OCR_IMAGE_HEIGHT, OCR_BASE_IMAGE_WIDTH), dtype=np.float32)
    prepared_images = [prepare_ocr_crop(image) for image in images]
    ratios = [max(1, image.width) / max(1, image.height) for image in prepared_images]
    max_ratio = max(OCR_BASE_IMAGE_WIDTH / OCR_IMAGE_HEIGHT, *ratios)
    target_width = min(1280, max(OCR_BASE_IMAGE_WIDTH, int(round(OCR_IMAGE_HEIGHT * max_ratio))))
    batch = np.zeros((len(images), 3, OCR_IMAGE_HEIGHT, target_width), dtype=np.float32)
    resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
    for index, image in enumerate(prepared_images):
        rgb = image.convert("RGB")
        ratio = rgb.width / max(1, rgb.height)
        resized_width = min(target_width, max(1, int(np.ceil(OCR_IMAGE_HEIGHT * ratio))))
        resized = rgb.resize((resized_width, OCR_IMAGE_HEIGHT), resampling)
        # PaddleOCR recognition 训练口径是 OpenCV BGR；这里只反转 Pillow RGB 通道，
        # 不引入 OpenCV 运行时。
        array = np.asarray(resized, dtype=np.float32)[:, :, ::-1]
        normalized = (array.transpose(2, 0, 1) / 255.0 - 0.5) / 0.5
        batch[index, :, :, :resized_width] = normalized
    return np.ascontiguousarray(batch)


def _decode_ctc(predictions: np.ndarray, characters: Sequence[str]) -> list[tuple[str, float]]:
    indices = predictions.argmax(axis=2)
    probabilities = predictions.max(axis=2)
    results: list[tuple[str, float]] = []
    for row_indices, row_probabilities in zip(indices, probabilities, strict=True):
        selected: list[int] = []
        confidence: list[float] = []
        previous = -1
        for token, probability in zip(row_indices.tolist(), row_probabilities.tolist(), strict=True):
            token_id = int(token)
            if token_id != previous and token_id != 0 and 0 <= token_id < len(characters):
                selected.append(token_id)
                confidence.append(float(probability))
            previous = token_id
        text = "".join(characters[token] for token in selected)
        results.append((text, float(np.mean(confidence)) if confidence else 0.0))
    return results


class OnnxTextRecognizer:
    """最小 recognition-only ONNX 封装；不依赖 RapidOCR、OpenCV 或检测模型。"""

    def __init__(
        self,
        model_path: Path,
        *,
        expected_size: int = OCR_MODEL_SIZE,
        expected_sha256: str = OCR_MODEL_SHA256,
        session_factory: Callable[[Path], Any] | None = None,
    ) -> None:
        if not model_path.is_file():
            raise FileNotFoundError("ocr_shadow_model_missing")
        if model_path.stat().st_size != expected_size:
            raise ValueError("ocr_shadow_model_size_mismatch")
        actual_hash = _sha256(model_path)
        if actual_hash != expected_sha256:
            raise ValueError("ocr_shadow_model_hash_mismatch")
        if session_factory is None:
            import onnxruntime as ort

            options = ort.SessionOptions()
            # 单算子线程限制 Shadow 对生产识别的 CPU 争用；Python 调用本身仍只
            # 发生在 Shadow 工作线程，吞吐不足由 latest-only 队列有界降级。
            options.intra_op_num_threads = OCR_INTRA_OP_THREADS
            options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        else:
            self.session = session_factory(model_path)
        metadata = self.session.get_modelmeta().custom_metadata_map
        raw_characters = str(metadata.get("character") or "")
        character_list = raw_characters.splitlines()
        if not character_list:
            raise ValueError("ocr_shadow_character_metadata_missing")
        self.characters = ("blank", *character_list, " ")
        self.input_name = str(self.session.get_inputs()[0].name)
        self.model_sha256 = actual_hash

    def recognize(self, images: Sequence[Image.Image]) -> list[tuple[str, float]]:
        batch = _resize_norm_batch(images)
        output = self.session.run(None, {self.input_name: batch})[0]
        predictions = np.asarray(output, dtype=np.float32)
        if predictions.ndim != 3 or predictions.shape[0] != len(images):
            raise ValueError("ocr_shadow_output_shape_invalid")
        return _decode_ctc(predictions, self.characters)


class OcrShadowRuntime:
    """三槽公平 production mailbox、低优先级 Shadow 和有限 LRU。"""

    def __init__(
        self,
        template_index: Sequence[Any],
        *,
        enabled: bool = True,
        mode: OcrMode | None = None,
        model_path: str | Path | None = None,
        cache_capacity: int = OCR_CACHE_CAPACITY,
        min_batch_interval_seconds: float = OCR_MIN_BATCH_INTERVAL_SECONDS,
        recognizer_factory: Callable[[Path], Any] | None = None,
    ) -> None:
        self.mode: OcrMode = mode or ("observe" if enabled else "off")
        self.enabled = self.mode != "off"
        self.model_path = Path(model_path) if model_path is not None else resource_path("ocr", OCR_MODEL_FILENAME)
        self.vocabulary = build_ocr_vocabulary(template_index)
        self.cache_capacity = max(1, int(cache_capacity))
        self.min_batch_interval_seconds = max(0.0, float(min_batch_interval_seconds))
        self._recognizer_factory = recognizer_factory
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._production_tasks: OrderedDict[tuple[str, int, int, int], _OcrBatch] = OrderedDict()
        self._diagnostic_task: _OcrBatch | None = None
        self._active_tasks = 0
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._completed = CompletedOcrMailbox()
        self._inflight: set[tuple[str, ...]] = set()
        self._latencies: deque[float] = deque(maxlen=OCR_LATENCY_WINDOW)
        self._scheduler_timing = BoundedOcrSchedulerTiming(OCR_LATENCY_WINDOW)
        self._diagnostic_throttle_waiting = False
        self._state = "disabled" if not self.enabled else "starting"
        self._reason = "ocr_shadow_disabled" if not self.enabled else ""
        self._model_hash = ""
        self._init_ms = 0.0
        self._queue_drops = 0
        self._production_submitted = 0
        self._production_coalesced = 0
        self._production_completed = 0
        self._diagnostic_dropped = 0
        self._slot_starvation_count = 0
        self._starved_work_keys: set[tuple[str, int, int, int]] = set()
        self._cache_hits = 0
        self._batch_calls = 0
        self._slot_calls = 0
        self._production_context: dict[str, Any] = {}
        self._production_admissions = 0
        self._production_rejections = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, name="ocr-shadow", daemon=True)
            self._thread.start()

    @classmethod
    def from_environment(cls, template_index: Sequence[Any]) -> "OcrShadowRuntime":
        return cls(template_index, mode=ocr_mode())

    def set_production_context(
        self,
        *,
        session_id: str,
        selection_epoch: int,
        slot_generations: Sequence[int],
        captured_frame_id: int,
        captured_at: float = 0.0,
    ) -> None:
        """绑定下一次 observe 的生产证据上下文；observe/off 模式不会消费。"""

        with self._lock:
            self._production_context = {
                "session_id": str(session_id or ""),
                "selection_epoch": max(0, int(selection_epoch)),
                "slot_generations": tuple(max(1, int(value)) for value in slot_generations),
                "captured_frame_id": max(0, int(captured_frame_id)),
                "captured_at": float(captured_at or 0.0),
            }

    def _set_unavailable(self, reason: object) -> None:
        with self._condition:
            self._state = "unavailable"
            self._reason = str(reason or "ocr_shadow_unavailable")[:120]
            self._inflight.clear()
            self._production_tasks.clear()
            self._diagnostic_task = None
            self._condition.notify_all()

    def _worker(self) -> None:
        initialized_at = time.perf_counter()
        try:
            recognizer = (
                self._recognizer_factory(self.model_path)
                if self._recognizer_factory is not None
                else OnnxTextRecognizer(self.model_path)
            )
        except Exception as exc:
            self._init_ms = round((time.perf_counter() - initialized_at) * 1000.0, 3)
            self._set_unavailable(str(exc) or type(exc).__name__)
            return
        with self._lock:
            self._state = "ready"
            self._reason = ""
            self._model_hash = str(getattr(recognizer, "model_sha256", OCR_MODEL_SHA256))
            self._init_ms = round((time.perf_counter() - initialized_at) * 1000.0, 3)

        next_batch_at = 0.0
        while True:
            with self._condition:
                while (
                    not self._production_tasks
                    and self._diagnostic_task is None
                    and not self._stop_event.is_set()
                ):
                    self._condition.wait()
                if (
                    self._stop_event.is_set()
                    and not self._production_tasks
                    and self._diagnostic_task is None
                ):
                    return
                if self._production_tasks:
                    selected: list[_OcrBatch] = []
                    dequeued_at = time.monotonic()
                    while self._production_tasks and len(selected) < 3:
                        work_key, pending = self._production_tasks.popitem(last=False)
                        age = max(0.0, dequeued_at - pending.submitted_at)
                        self._scheduler_timing.record_queue_wait(production=True, seconds=age)
                        if age >= 3.0 and work_key not in self._starved_work_keys:
                            self._starved_work_keys.add(work_key)
                            self._slot_starvation_count += 1
                        selected.append(pending)
                    task = _OcrBatch(
                        tuple(value for item in selected for value in item.input_sha256s),
                        tuple(value for item in selected for value in item.images),
                        tuple(value for item in selected for value in item.production_context_keys),
                        tuple(value for item in selected for value in item.inflight_keys),
                        True,
                        min(item.submitted_at for item in selected),
                    )
                else:
                    remaining = next_batch_at - time.monotonic()
                    if remaining > 0.0:
                        # Keep the latest diagnostic parked in its low-priority mailbox. A
                        # production submit notifies this Condition and is selected before
                        # the diagnostic, instead of waiting behind an already-dequeued sleep.
                        self._diagnostic_throttle_waiting = True
                        self._condition.wait(timeout=remaining)
                        self._diagnostic_throttle_waiting = False
                        continue
                    task = self._diagnostic_task
                    self._diagnostic_task = None
                    if task is None:
                        continue
                    self._scheduler_timing.record_queue_wait(
                        production=False,
                        seconds=time.monotonic() - task.submitted_at,
                    )
                self._active_tasks += len(task.input_sha256s)
            started_at = time.perf_counter()
            if not task.production:
                next_batch_at = time.monotonic() + self.min_batch_interval_seconds
            try:
                recognized = recognizer.recognize(task.images)
                if len(recognized) != len(task.input_sha256s):
                    raise ValueError("ocr_shadow_batch_size_mismatch")
                elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
                with self._condition:
                    contexts = task.production_context_keys or (None,) * len(task.input_sha256s)
                    inflight_keys = task.inflight_keys or tuple(
                        ("diagnostic", input_sha256) for input_sha256 in task.input_sha256s
                    )
                    for input_sha256, context_key, inflight_key, (raw_text, confidence) in zip(
                        task.input_sha256s,
                        contexts,
                        inflight_keys,
                        recognized,
                        strict=True,
                    ):
                        result = {
                            "state": "ready",
                            "input_sha256": input_sha256,
                            "raw_text": str(raw_text),
                            "confidence": round(float(confidence), 6),
                            "elapsed_ms": elapsed_ms,
                            "scene_negative": classify_scene_negative(raw_text, confidence),
                            **match_ocr_text(raw_text, self.vocabulary),
                        }
                        self._cache[input_sha256] = result
                        self._cache.move_to_end(input_sha256)
                        while len(self._cache) > self.cache_capacity:
                            self._cache.popitem(last=False)
                        self._inflight.discard(inflight_key)
                        if context_key is not None and not self._stop_event.is_set():
                            self._completed.put(
                                result,
                                context_key,
                                minimum_confidence=OCR_PRODUCTION_MIN_CONFIDENCE,
                            )
                    self._latencies.append(elapsed_ms)
                    self._scheduler_timing.record_inference(
                        production=task.production,
                        elapsed_ms=elapsed_ms,
                    )
                    self._batch_calls += 1
                    self._slot_calls += len(task.input_sha256s)
                    if task.production:
                        self._production_completed += len(task.input_sha256s)
                    self._active_tasks = max(0, self._active_tasks - len(task.input_sha256s))
                    self._condition.notify_all()
            except Exception as exc:
                self._set_unavailable(f"ocr_shadow_inference_failed:{type(exc).__name__}")
                return

    @staticmethod
    def _comparison(result: Mapping[str, Any], slot: Mapping[str, Any]) -> str:
        matched_id = str(result.get("matched_id") or "")
        primary_id = str(slot.get("augment_id") or "")
        if not primary_id:
            return "no_primary"
        if not matched_id:
            return "no_match"
        return "agree" if matched_id == primary_id else "disagree"

    def observe(self, images: Sequence[Image.Image], slots: list[dict[str, Any]], *,
                eligible_slots: Sequence[bool] | None = None) -> None:
        """附加已缓存结论并异步提交未见指纹；调用方只在有效 hextech 场景使用。"""

        pending_input_sha256s: list[str] = []
        pending_images: list[Image.Image] = []
        pending_context_keys: list[OcrEvidenceContext | None] = []
        pending_inflight_keys: list[tuple[str, ...]] = []
        with self._lock:
            initial_state = "stopped" if self._stop_event.is_set() else self._state
            initial_reason = self._reason
        if initial_state in {"disabled", "unavailable", "stopped"}:
            for slot in slots:
                slot["ocr_shadow"] = {
                    "state": initial_state,
                    "reason": initial_reason,
                }
            return
        # RGB 规范化和像素摘要可能触发完整 ROI tobytes；它们不应持有与 OCR
        # worker 共享的 cache 锁，否则一次推理落盘会放大主识别线程尾延迟。
        prepared_inputs: list[tuple[Image.Image | None, str]] = []
        for index in range(len(slots)):
            if eligible_slots is not None and (index >= len(eligible_slots) or not eligible_slots[index]):
                prepared_inputs.append((None, ""))
                continue
            rgb = prepare_ocr_crop(images[index]) if index < len(images) else None
            prepared_inputs.append((rgb, ocr_input_sha256(rgb) if rgb is not None else ""))
        with self._lock:
            state = self._state
            reason = self._reason
            production_context = dict(self._production_context)
            for index, (slot, (rgb, input_sha256)) in enumerate(zip(slots, prepared_inputs, strict=True)):
                if eligible_slots is not None and (index >= len(eligible_slots) or not eligible_slots[index]):
                    slot["ocr_shadow"] = {"state": "skipped", "reason": "hold_slot_ineligible"}
                    continue
                fingerprint = str(slot.get("evidence_fingerprint") or "")
                generations = production_context.get("slot_generations")
                context_key: OcrEvidenceContext | None = None
                if (
                    self.mode == "admit"
                    and isinstance(generations, tuple)
                    and index < len(generations)
                    and str(production_context.get("session_id") or "")
                    and int(production_context.get("selection_epoch") or 0) > 0
                    and int(production_context.get("captured_frame_id") or 0) > 0
                    and input_sha256
                    and fingerprint
                    and production_needed(slot)
                ):
                    context_key = OcrEvidenceContext(
                        session_id=str(production_context["session_id"]),
                        selection_epoch=int(production_context["selection_epoch"]),
                        slot_index=index,
                        slot_generation=int(generations[index]),
                        input_sha256=input_sha256,
                        perceptual_fingerprint=fingerprint,
                        captured_frame_id=int(production_context["captured_frame_id"]),
                        captured_at=float(production_context["captured_at"]),
                        template_candidate_id=str(slot.get("augment_id") or ""),
                        template_evidence_grade=str(slot.get("evidence_grade") or ""),
                    )
                cached = self._cache.get(input_sha256) if input_sha256 else None
                if cached is not None:
                    self._cache.move_to_end(input_sha256)
                    self._cache_hits += 1
                    slot["ocr_shadow"] = {
                        **cached,
                        "comparison": self._comparison(cached, slot),
                    }
                    if context_key is not None:
                        slot["ocr_production"] = build_production_evidence(
                            cached,
                            context_key,
                            minimum_confidence=OCR_PRODUCTION_MIN_CONFIDENCE,
                        )
                        admitted = slot["ocr_production"]["state"] == "admitted"
                        if admitted:
                            self._production_admissions += 1
                        else:
                            self._production_rejections += 1
                        continue
                    if context_key is None:
                        continue
                if state in {"disabled", "unavailable"}:
                    slot["ocr_shadow"] = {
                        "state": state,
                        "reason": reason,
                        **({"input_sha256": input_sha256} if input_sha256 else {}),
                    }
                    continue
                slot["ocr_shadow"] = {
                    "state": "pending",
                    **({"input_sha256": input_sha256} if input_sha256 else {}),
                }
                if (
                    input_sha256
                    and rgb is not None
                ):
                    inflight_key = (
                        context_key.inflight_key
                        if context_key is not None
                        else ("diagnostic", input_sha256)
                    )
                    if inflight_key in self._inflight:
                        continue
                    self._inflight.add(inflight_key)
                    pending_input_sha256s.append(input_sha256)
                    pending_images.append(rgb.copy())
                    pending_context_keys.append(context_key)
                    pending_inflight_keys.append(inflight_key)

            if not pending_input_sha256s:
                return
            submitted_at = time.monotonic()
            diagnostic_indices: list[int] = []
            for pending_index, context_key in enumerate(pending_context_keys):
                if context_key is None:
                    diagnostic_indices.append(pending_index)
                    continue
                work_key = context_key.work_key
                task = _OcrBatch(
                    (pending_input_sha256s[pending_index],),
                    (pending_images[pending_index],),
                    (context_key,),
                    (pending_inflight_keys[pending_index],),
                    True,
                    submitted_at,
                )
                displaced = self._production_tasks.get(work_key)
                if displaced is not None:
                    self._inflight.difference_update(displaced.inflight_keys)
                    self._production_coalesced += 1
                    self._queue_drops += 1
                else:
                    self._production_submitted += 1
                self._production_tasks[work_key] = task

            if diagnostic_indices:
                task = _OcrBatch(
                    tuple(pending_input_sha256s[index] for index in diagnostic_indices),
                    tuple(pending_images[index] for index in diagnostic_indices),
                    tuple(None for _index in diagnostic_indices),
                    tuple(pending_inflight_keys[index] for index in diagnostic_indices),
                    False,
                    submitted_at,
                )
                displaced = self._diagnostic_task
                if displaced is not None:
                    self._inflight.difference_update(displaced.inflight_keys)
                    self._diagnostic_dropped += 1
                    self._queue_drops += 1
                self._diagnostic_task = task
            self._condition.notify_all()

    def drain_completed_evidence(
        self,
        *,
        session_id: str,
        selection_epoch: int,
        slot_generations: Sequence[int],
        observed_at: float,
        scene_open: bool,
    ) -> list[dict[str, Any]]:
        """以 wall-clock 校验并交付仍属于当前选择窗口的原帧证据。"""

        with self._condition:
            return self._completed.drain(
                session_id=session_id,
                selection_epoch=selection_epoch,
                slot_generations=slot_generations,
                observed_at=float(observed_at),
                scene_open=bool(scene_open) and not self._stop_event.is_set(),
            )

    def record_completed_outcomes(self, outcomes: Sequence[str]) -> None:
        with self._condition:
            self._completed.record_outcomes(outcomes)

    def status(self) -> dict[str, Any]:
        with self._condition:
            latencies = tuple(self._latencies)
            scheduler_timing = self._scheduler_timing.snapshot()
            state = self._state
            reason = self._reason
            now = time.monotonic()
            per_slot_oldest_task_age: dict[str, float] = {}
            for work_key, task in self._production_tasks.items():
                slot_key = str(work_key[2])
                age = round(max(0.0, now - task.submitted_at), 3)
                per_slot_oldest_task_age[slot_key] = max(
                    age,
                    per_slot_oldest_task_age.get(slot_key, 0.0),
                )
            payload = {
                "state": state,
                "reason": reason,
                "available": state == "ready",
                "enabled": self.enabled,
                "mode": self.mode,
                "model_path": f"resources/ocr/{OCR_MODEL_FILENAME}",
                "model_sha256": self._model_hash or OCR_MODEL_SHA256,
                "model_size": OCR_MODEL_SIZE,
                "init_ms": self._init_ms,
                "vocabulary_count": len(self.vocabulary),
                "intra_op_threads": OCR_INTRA_OP_THREADS,
                "min_batch_interval_seconds": self.min_batch_interval_seconds,
                "queue_depth": len(self._production_tasks) + int(self._diagnostic_task is not None),
                "queue_drops": self._queue_drops,
                "production_submitted": self._production_submitted,
                "production_coalesced": self._production_coalesced,
                "production_completed": self._production_completed,
                "diagnostic_dropped": self._diagnostic_dropped,
                "diagnostic_throttle_waiting": self._diagnostic_throttle_waiting,
                "slot_starvation_count": self._slot_starvation_count,
                "per_slot_oldest_task_age": per_slot_oldest_task_age,
                "cache_size": len(self._cache),
                "cache_hits": self._cache_hits,
                "batch_calls": self._batch_calls,
                "slot_calls": self._slot_calls,
                "production_admissions": self._production_admissions,
                "production_rejections": self._production_rejections,
                "batch_p50_ms": percentile(latencies, 0.50),
                "batch_p95_ms": percentile(latencies, 0.95),
                **scheduler_timing,
                **self._completed.status(),
            }
        if state == "unavailable":
            payload["diagnostic"] = "ocr_shadow_unavailable"
        return payload

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._condition:
                terminal = self._state in {"disabled", "unavailable", "stopped"}
                idle = (
                    not self._inflight
                    and not self._production_tasks
                    and self._diagnostic_task is None
                    and self._active_tasks == 0
                )
            if terminal or idle:
                return True
            time.sleep(0.005)
        return False

    def close(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        with self._condition:
            for displaced in self._production_tasks.values():
                self._inflight.difference_update(displaced.inflight_keys)
            self._production_tasks.clear()
            if self._diagnostic_task is not None:
                self._inflight.difference_update(self._diagnostic_task.inflight_keys)
            self._diagnostic_task = None
            self._stop_event.set()
            self._completed.clear()
            self._condition.notify_all()
        thread.join(timeout=max(0.0, timeout))
        with self._condition:
            if not thread.is_alive():
                self._state = "stopped"
                self._reason = ""
                self._completed.clear()


__all__ = [
    "OCR_MODEL_FILENAME",
    "OCR_MODEL_SHA256",
    "OCR_MODEL_SIZE",
    "OCR_MODE_ENV",
    "OCR_PRODUCTION_MIN_CONFIDENCE",
    "OCR_BRIGHT_TEXT_PADDING_PX",
    "OCR_BRIGHT_TEXT_THRESHOLD",
    "OCR_SHADOW_ENABLED_ENV",
    "OcrShadowRuntime",
    "OcrVocabularyEntry",
    "OnnxTextRecognizer",
    "build_ocr_vocabulary",
    "match_ocr_text",
    "normalize_ocr_text",
    "ocr_shadow_enabled",
    "ocr_mode",
    "ocr_input_sha256",
    "prepare_ocr_crop",
]
