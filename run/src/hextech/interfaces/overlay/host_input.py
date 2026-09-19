"""Host 自有的有界输入邮箱：磁盘 JSON 读取不进入 Tk tick。"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from hextech.interfaces.overlay.generation_pin import first_selection_started
from hextech.modules.vision.events import EVENT_MAX_AGE_SECONDS


# Context poller 默认 1.5s 产生新 publication；mailbox 允许一次轻微抖动，
# 但不将同游戏的 last-good Context 无限续期。这是 Host 自有单调时钟门，
# publication 时钟只在首次观察时映射到单调时钟；重复读盘不能续期。
CONTEXT_MAILBOX_MAX_AGE_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class HostInputSnapshot:
    # 工作线程复制后转交只读所有权；GUI 不修改这些输入对象。
    event: Mapping[str, Any]
    context: Mapping[str, Any]
    host_read_at: float
    observed_at: float
    sequence: int = 0
    error: str = ""
    event_read_started_at: float = 0.0
    event_read_completed_at: float = 0.0
    context_requested_at: float = 0.0
    context_read_started_at: float = 0.0
    context_read_completed_at: float = 0.0
    context_sequence: int = 0
    context_error: str = ""
    context_game_instance_id: str = ""
    context_observed_at: float = 0.0
    context_age_seconds: float | None = None
    first_event_read_started_at: float = 0.0
    first_event_read_completed_at: float = 0.0


def _unavailable(reason: str) -> HostInputSnapshot:
    return HostInputSnapshot(
        {"active": False, "visible": False, "slots": [], "source": {}, "error": reason},
        {"ok": False, "error": reason}, 0.0, 0.0, error=reason,
    )


class HostInputObserver:
    """事件和 Context 各自有界；无 Tk 引用、无外部服务。"""

    def __init__(
        self, source: Any, *, config: Mapping[str, Any] | None = None,
        on_event: Callable[[Mapping[str, Any]], None] | None = None,
        now: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], float] = time.time,
    ) -> None:
        config = config or {}
        self._source = source
        self._on_event = on_event
        self._now = now
        self._wall_now = wall_now
        self._fast_ms = max(16, int(config.get("fast_event_poll_ms", 16) or 16))
        # 游戏实例存在时，事件邮箱与 Tk 的轻量检查使用同一 16ms
        # 节奏。Context 读取独立后，不会被这个频率放大为无界并发。
        self._game_ms = min(
            self._fast_ms,
            max(16, int(config.get("game_event_poll_ms", self._fast_ms) or self._fast_ms)),
        )
        self._idle_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
        self._context_max_age_seconds = max(
            0.1,
            float(
                config.get(
                    "context_mailbox_max_age_seconds",
                    CONTEXT_MAILBOX_MAX_AGE_SECONDS,
                )
                or CONTEXT_MAILBOX_MAX_AGE_SECONDS
            ),
        )
        self._requested_ms = self._idle_ms
        self._condition = threading.Condition()
        self._latest = _unavailable("input_preparing")
        self._closed = False
        self._thread: threading.Thread | None = None
        self._context_thread: threading.Thread | None = None
        self._sequence = 0
        self._current_identity: tuple[str, int] | None = None
        self._context_request_identity: tuple[str, int] | None = None
        self._context_request_sequence = 0
        self._context_request_at = 0.0
        self._context_result: Mapping[str, Any] = {}
        self._context_result_identity: tuple[str, int] | None = None
        self._context_result_sequence = 0
        self._context_result_requested_at = 0.0
        self._context_read_started_at = 0.0
        self._context_read_completed_at = 0.0
        self._context_result_observed_at = 0.0
        self._context_error = "context_preparing"
        self._freshness_identity: tuple[str, int] | None = None
        self._publication_publisher = ""
        self._publication_sequence = 0
        self._publication_wall_at = 0.0
        self._publication_payload: Mapping[str, Any] = {}
        self._publication_observed_at = 0.0
        self._retired_publishers: set[str] = set()
        self._legacy_observed_at: float | None = None
        self._event_publication_key: tuple[Any, ...] | None = None
        self._first_event_read_started_at = 0.0
        self._first_event_read_completed_at = 0.0

    def start(self) -> None:
        with self._condition:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="overlay-input-observer", daemon=True)
            self._context_thread = threading.Thread(
                target=self._run_context,
                name="overlay-context-observer",
                daemon=True,
            )
            self._context_thread.start()
            self._thread.start()

    def set_poll_ms(self, value: int) -> None:
        with self._condition:
            previous = self._requested_ms
            self._requested_ms = max(16, min(self._idle_ms, int(value)))
            if self._requested_ms < previous:
                # 事件和 Context 共用 Condition；notify() 可能只唤醒 Context，
                # 使原本要求的事件提速丢失。
                self._condition.notify_all()

    def snapshot(self) -> HostInputSnapshot:
        with self._condition:
            latest = self._latest
            if self._closed:
                return _unavailable("input_closed")
        now = self._now()
        # Context 完成不修改 event observed_at；阻塞的下一次事件读取不能
        # 把旧 event 无限当作新事件。
        if latest.sequence and now - latest.observed_at > EVENT_MAX_AGE_SECONDS:
            return _unavailable("input_expired")
        if latest.context and latest.context_sequence > 0:
            context_age = max(0.0, now - latest.context_observed_at)
            if context_age > self._context_max_age_seconds:
                return replace(
                    latest,
                    context={},
                    context_error="context_expired",
                    context_age_seconds=context_age,
                )
        return latest

    def close(self, timeout: float = 2.0) -> None:
        with self._condition:
            self._closed = True
            self._latest = _unavailable("input_closed")
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(max(0.0, timeout))
        if self._context_thread is not None:
            self._context_thread.join(max(0.0, timeout))

    @staticmethod
    def _event_identity(event: Mapping[str, Any]) -> tuple[str, int] | None:
        raw_source = event.get("source")
        source = raw_source if isinstance(raw_source, Mapping) else {}
        game_instance_id = str(
            source.get("game_instance_id") or source.get("session_id") or ""
        ).strip()
        if not game_instance_id:
            return None
        try:
            window_hwnd = int(source.get("window_hwnd") or 0)
        except (TypeError, ValueError):
            window_hwnd = 0
        return game_instance_id, window_hwnd

    @staticmethod
    def _context_matches(
        identity: tuple[str, int], context: Mapping[str, Any]
    ) -> bool:
        context_game = str(
            context.get("game_instance_id") or context.get("session_id") or ""
        ).strip()
        if context_game and context_game != identity[0]:
            return False
        try:
            context_hwnd = int(context.get("window_hwnd") or 0)
        except (TypeError, ValueError):
            return False
        return not (identity[1] > 0 and context_hwnd > 0 and context_hwnd != identity[1])

    def _publish_event(
        self,
        event: Mapping[str, Any],
        *,
        identity: tuple[str, int] | None,
        event_read_started_at: float,
        event_read_completed_at: float,
        observed_at: float,
        error: str = "",
    ) -> None:
        with self._condition:
            if self._closed:
                return
            self._current_identity = identity
            timing = event.get("timing") if isinstance(event.get("timing"), Mapping) else {}
            source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
            written = timing.get("event_written_at")
            publication = (
                identity, event.get("build_id"), source.get("sidecar_instance_id"),
                source.get("selection_epoch"), source.get("selection_revision"), written,
            ) if isinstance(written, (int, float)) and not isinstance(written, bool) and math.isfinite(written) and written > 0 else None
            if publication is None or publication != self._event_publication_key:
                self._event_publication_key = publication
                self._first_event_read_started_at = event_read_started_at
                self._first_event_read_completed_at = event_read_completed_at
            if identity is None:
                self._context_request_identity = None
            else:
                self._context_request_sequence += 1
                self._context_request_identity = identity
                self._context_request_at = time.time()
            if identity is not None and self._context_result_identity == identity:
                context = self._context_result
                context_error = self._context_error
                context_sequence = self._context_result_sequence
                context_requested_at = self._context_result_requested_at
                context_started_at = self._context_read_started_at
                context_completed_at = self._context_read_completed_at
                context_observed_at = self._context_result_observed_at
            else:
                context = {}
                context_error = "context_preparing" if identity is not None else "context_not_requested"
                context_sequence = 0
                context_requested_at = self._context_request_at if identity is not None else 0.0
                context_started_at = 0.0
                context_completed_at = 0.0
                context_observed_at = 0.0
            context_age_seconds = (
                max(0.0, observed_at - context_observed_at)
                if context_sequence > 0
                else None
            )
            if (
                context
                and context_age_seconds is not None
                and context_age_seconds > self._context_max_age_seconds
            ):
                context = {}
                context_error = "context_expired"
            self._sequence += 1
            self._latest = HostInputSnapshot(
                event=event,
                context=context,
                host_read_at=event_read_completed_at,
                observed_at=observed_at,
                sequence=self._sequence,
                error=error,
                event_read_started_at=event_read_started_at,
                event_read_completed_at=event_read_completed_at,
                context_requested_at=context_requested_at,
                context_read_started_at=context_started_at,
                context_read_completed_at=context_completed_at,
                context_sequence=context_sequence,
                context_error=context_error,
                context_game_instance_id=identity[0] if identity is not None else "",
                context_observed_at=context_observed_at,
                context_age_seconds=context_age_seconds,
                first_event_read_started_at=self._first_event_read_started_at,
                first_event_read_completed_at=self._first_event_read_completed_at,
            )
            self._condition.notify_all()

    def _context_freshness(
        self, identity: tuple[str, int], context: Mapping[str, Any],
    ) -> tuple[float, str]:
        """在锁内校验上游 publication；读取成功本身不是一次新发布。"""
        now = self._now()
        if self._freshness_identity != identity:
            self._freshness_identity = identity
            self._publication_publisher = ""
            self._publication_sequence = 0
            self._publication_wall_at = 0.0
            self._publication_payload = {}
            self._retired_publishers.clear()
            self._legacy_observed_at = None
        has_publication = any(key in context for key in (
            "publisher", "publisher_instance_id", "publication_seq", "published_at",
        ))
        if not has_publication:
            # 旧适配器没有 publication 身份：同游戏只给一次有限观察宽限，
            # 不用内容变化或 read_at 冒充上游生成。已有正式发布不能降级洗时效。
            if self._publication_publisher:
                return now, "context_publication_metadata_missing"
            if self._legacy_observed_at is None:
                observed = now
                if "generated_at" in context:
                    try:
                        stamp = float(context["generated_at"])
                    except (TypeError, ValueError, OverflowError):
                        return now, "context_publication_timestamp_invalid"
                    if not math.isfinite(stamp) or stamp <= 0 or stamp > self._wall_now():
                        return now, "context_publication_timestamp_invalid"
                    observed = now - (self._wall_now() - stamp)
                self._legacy_observed_at = observed
            observed = self._legacy_observed_at
            return observed, "context_expired" if now - observed > self._context_max_age_seconds else ""
        publisher = context.get("publisher_instance_id")
        sequence = context.get("publication_seq")
        if (not isinstance(publisher, str) or not publisher.strip()
                or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0):
            return now, "context_publication_metadata_missing"
        try:
            stamp = float(context["published_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return now, "context_publication_timestamp_invalid"
        wall_age = self._wall_now() - stamp
        if not math.isfinite(stamp) or stamp <= 0 or wall_age < 0:
            return now, "context_publication_timestamp_invalid"
        if publisher in self._retired_publishers:
            return now, "context_publication_replayed"
        if publisher == self._publication_publisher:
            if sequence < self._publication_sequence:
                return now, "context_publication_replayed"
            if sequence == self._publication_sequence:
                if context != self._publication_payload:
                    return now, "context_publication_changed_without_sequence"
                observed = self._publication_observed_at
                age = max(now - observed, wall_age)
                self._publication_observed_at = now - age
                return self._publication_observed_at, "context_expired" if age > self._context_max_age_seconds else ""
        # 新 seq / Broker 重启都必须证明新的上游时间；已退休 publisher 不回流。
        if self._publication_publisher and stamp <= self._publication_wall_at:
            return now, "context_publication_replayed"
        if wall_age > self._context_max_age_seconds:
            return now - wall_age, "context_expired"
        if self._publication_publisher and publisher != self._publication_publisher:
            # 极端重启风暴保持有界并 fail closed，直到新的游戏身份。
            if len(self._retired_publishers) >= 64:
                return now, "context_publication_restart_limit"
            self._retired_publishers.add(self._publication_publisher)
        self._publication_publisher = publisher
        self._publication_sequence = sequence
        self._publication_wall_at = stamp
        self._publication_payload = deepcopy(dict(context))
        self._publication_observed_at = now - wall_age
        return self._publication_observed_at, ""

    def _publish_context(
        self,
        context: Mapping[str, Any],
        *,
        identity: tuple[str, int],
        request_sequence: int,
        requested_at: float,
        read_started_at: float,
        read_completed_at: float,
        error: str,
    ) -> None:
        with self._condition:
            # 旧游戏或旧窗口的迟到 Context 不能进入当前 mailbox。
            if self._closed or self._current_identity != identity:
                return
            if not self._context_matches(identity, context):
                context = {}
                error = "context_game_identity_mismatch"
            context_observed_at = self._now()
            if not error:
                context_observed_at, error = self._context_freshness(identity, context)
                if error:
                    context = {}
            self._context_result = context
            self._context_result_identity = identity
            self._context_result_sequence = request_sequence
            self._context_result_requested_at = requested_at
            self._context_read_started_at = read_started_at
            self._context_read_completed_at = read_completed_at
            self._context_result_observed_at = context_observed_at
            self._context_error = error
            current = self._latest
            self._sequence += 1
            self._latest = HostInputSnapshot(
                event=current.event,
                context=context,
                host_read_at=current.host_read_at,
                observed_at=current.observed_at,
                sequence=self._sequence,
                error=current.error,
                event_read_started_at=current.event_read_started_at,
                event_read_completed_at=current.event_read_completed_at,
                context_requested_at=requested_at,
                context_read_started_at=read_started_at,
                context_read_completed_at=read_completed_at,
                context_sequence=request_sequence,
                context_error=error,
                context_game_instance_id=identity[0],
                context_observed_at=context_observed_at,
                context_age_seconds=max(0.0, self._now() - context_observed_at),
                first_event_read_started_at=current.first_event_read_started_at,
                first_event_read_completed_at=current.first_event_read_completed_at,
            )

    def _run_context(self) -> None:
        handled_sequence = 0
        while True:
            with self._condition:
                while not self._closed and (
                    self._context_request_identity is None
                    or self._context_request_sequence <= handled_sequence
                ):
                    self._condition.wait()
                if self._closed:
                    return
                identity = self._context_request_identity
                request_sequence = self._context_request_sequence
                requested_at = self._context_request_at
            if identity is None:
                continue
            read_started_at = time.time()
            try:
                context = deepcopy(dict(self._source.read_context()))
                error = ""
            except Exception as exc:
                context = {"ok": False, "error": type(exc).__name__}
                error = type(exc).__name__
            read_completed_at = time.time()
            handled_sequence = request_sequence
            self._publish_context(
                context,
                identity=identity,
                request_sequence=request_sequence,
                requested_at=requested_at,
                read_started_at=read_started_at,
                read_completed_at=read_completed_at,
                error=error,
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
            event_read_started_at = time.time()
            try:
                event = deepcopy(dict(self._source.read_event()))
                event_read_completed_at, observed_at = time.time(), self._now()
                identity = self._event_identity(event)
                # 首选场景先记录截止，再提交 Context；慢 Context 不延迟 freeze。
                with self._condition:
                    if self._closed:
                        return
                if self._on_event is not None:
                    self._on_event(event)
                self._publish_event(
                    event,
                    identity=identity,
                    event_read_started_at=event_read_started_at,
                    event_read_completed_at=event_read_completed_at,
                    observed_at=observed_at,
                )
                source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
            except Exception as exc:
                failed = _unavailable(type(exc).__name__)
                event, error = failed.event, failed.error
                event_read_completed_at, observed_at = time.time(), self._now()
                identity = None
                source = {}
                self._publish_event(
                    event,
                    identity=None,
                    event_read_started_at=event_read_started_at,
                    event_read_completed_at=event_read_completed_at,
                    observed_at=observed_at,
                    error=error,
                )
            interval = self._fast_ms if first_selection_started(event) else (
                self._game_ms
                if source.get("game_instance_id") or source.get("session_id")
                else self._idle_ms
            )
            with self._condition:
                if self._closed:
                    return
                self._condition.wait(timeout=min(interval, self._requested_ms) / 1000.0)
