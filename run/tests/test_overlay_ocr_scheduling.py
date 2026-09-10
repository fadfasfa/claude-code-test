"""OCR production/diagnostic scheduling regressions."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from PIL import Image

from hextech.infrastructure.vision.ocr_shadow import OcrShadowRuntime


def _templates() -> list[SimpleNamespace]:
    return [SimpleNamespace(augment_id="1225", name="双刀流")]


def _image(value: int) -> Image.Image:
    image = Image.new("RGB", (24, 12), (value, value, value))
    image.putpixel((value % 23, value % 11), (255 - value, value, value))
    return image


class _RecordingRecognizer:
    model_sha256 = "test-model"

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.first_pixels: list[int] = []
        self.call_times: list[float] = []

    def recognize(self, images: list[Image.Image] | tuple[Image.Image, ...]) -> list[tuple[str, float]]:
        with self._condition:
            self.call_times.append(time.monotonic())
            self.first_pixels.extend(image.convert("RGB").tobytes()[0] for image in images)
            self._condition.notify_all()
        return [("双刀流", 0.99)] * len(images)

    def wait_for_pixel(self, value: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while value not in self.first_pixels:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return True


def _wait_until_diagnostic_is_throttled(runtime: OcrShadowRuntime, timeout: float = 1.0) -> bool:
    """Synchronize with both the broken dequeue/sleep and fixed parked-queue states."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with runtime._condition:  # noqa: SLF001 - scheduler white-box regression
            broken_sleep = runtime._diagnostic_task is None and runtime._active_tasks == 1  # noqa: SLF001
            fixed_wait = bool(getattr(runtime, "_diagnostic_throttle_waiting", False))
        if broken_sleep or fixed_wait:
            return True
        time.sleep(0.002)
    return False


def _wait_for_completed_production(runtime: OcrShadowRuntime, timeout: float = 1.0) -> dict:
    deadline = time.monotonic() + timeout
    status = runtime.status()
    while status["production_completed"] < 1 and time.monotonic() < deadline:
        time.sleep(0.002)
        status = runtime.status()
    return status


def test_dequeued_throttled_diagnostic_never_delays_later_production() -> None:
    recognizer = _RecordingRecognizer()
    runtime = OcrShadowRuntime(
        _templates(),
        mode="admit",
        min_batch_interval_seconds=0.6,
        recognizer_factory=lambda _path: recognizer,
    )
    first_diagnostic = _image(17)
    throttled_diagnostic = _image(83)
    production = _image(149)
    try:
        runtime.observe(
            [first_diagnostic],
            [{"augment_id": "1225", "evidence_grade": "strong", "evidence_fingerprint": "diag-1"}],
        )
        assert recognizer.wait_for_pixel(17, 1.0)
        assert runtime.wait_until_idle(1.0)

        runtime.observe(
            [throttled_diagnostic],
            [{"augment_id": "1225", "evidence_grade": "strong", "evidence_fingerprint": "diag-2"}],
        )
        assert _wait_until_diagnostic_is_throttled(runtime)

        runtime.set_production_context(
            session_id="session-scheduling",
            selection_epoch=4,
            slot_generations=(2,),
            captured_frame_id=9,
        )
        submitted_at = time.monotonic()
        runtime.observe([production], [{"evidence_fingerprint": "production-9"}])

        assert recognizer.wait_for_pixel(149, 0.2)
        assert time.monotonic() - submitted_at < 0.2
        status = _wait_for_completed_production(runtime)
        assert status["production_completed"] == 1
        assert status["production_queue_wait_p95_ms"] < 200.0
        assert status["production_inference_p95_ms"] >= 0.0
        assert status["diagnostic_queue_wait_p95_ms"] >= 0.0
    finally:
        runtime.close()


def test_throttled_diagnostic_remains_latest_only_and_keeps_cadence() -> None:
    recognizer = _RecordingRecognizer()
    runtime = OcrShadowRuntime(
        _templates(),
        mode="observe",
        min_batch_interval_seconds=0.08,
        recognizer_factory=lambda _path: recognizer,
    )
    try:
        runtime.observe([_image(17)], [{"evidence_fingerprint": "diag-1"}])
        assert recognizer.wait_for_pixel(17, 1.0)
        assert runtime.wait_until_idle(1.0)

        runtime.observe([_image(83)], [{"evidence_fingerprint": "diag-2"}])
        assert _wait_until_diagnostic_is_throttled(runtime)
        runtime.observe([_image(149)], [{"evidence_fingerprint": "diag-3"}])
        assert recognizer.wait_for_pixel(149, 1.0)
        assert runtime.wait_until_idle(1.0)

        assert recognizer.first_pixels == [17, 149]
        assert recognizer.call_times[1] - recognizer.call_times[0] >= 0.06
        status = runtime.status()
        assert status["diagnostic_dropped"] == 1
        assert status["diagnostic_queue_wait_p95_ms"] >= 60.0
    finally:
        runtime.close()


def test_close_interrupts_parked_diagnostic_throttle_wait() -> None:
    recognizer = _RecordingRecognizer()
    runtime = OcrShadowRuntime(
        _templates(),
        mode="observe",
        min_batch_interval_seconds=5.0,
        recognizer_factory=lambda _path: recognizer,
    )
    runtime.observe([_image(17)], [{"evidence_fingerprint": "diag-1"}])
    assert recognizer.wait_for_pixel(17, 1.0)
    assert runtime.wait_until_idle(1.0)
    runtime.observe([_image(83)], [{"evidence_fingerprint": "diag-2"}])
    assert _wait_until_diagnostic_is_throttled(runtime)

    started_at = time.monotonic()
    runtime.close(timeout=0.5)

    assert time.monotonic() - started_at < 0.5
    assert runtime.status()["state"] == "stopped"
    assert recognizer.first_pixels == [17]
