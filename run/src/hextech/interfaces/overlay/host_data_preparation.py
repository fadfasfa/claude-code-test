"""Host 单后台数据准备器：验证、复制和投影不占用 Tk 线程。

唯一工作线程拥有 generation/Stage/hint 缓存；容量一的待处理请求按语义合并。
发布结果同时校验请求版本与游戏/英雄/选择身份，不能跨局或穿过碎片硬门回流。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from hextech.interfaces.overlay.display_contract import DisplaySelectionGate, fragment_selection, ordinary_selection
from hextech.interfaces.overlay.generation_pin import SelectionGenerationPin, first_selection_started
from hextech.interfaces.overlay.host_render_state import HostGenerationHintCache, _detecting_stat
from hextech.interfaces.overlay.host_stage_stats import OverlayStageRuntime
from hextech.interfaces.overlay.renderer import build_render_model_from_session
from hextech.interfaces.overlay.session_adapter import build_runtime_session
from hextech.interfaces.overlay.stats_pin import SelectionStatsPin
from hextech.modules.data.overlay_source import source_has_private_stats
from hextech.modules.recommendation.display_summary import DisplaySummaryCache
from hextech.interfaces.overlay.text_metrics import prepare_synergy_display_summaries


def source_refresh_identity(value: Any) -> Any:
    """状态/来源/时效改变才重投影；年龄计数和检查时间不是内容身份。"""
    if isinstance(value, Mapping):
        return tuple((str(k), source_refresh_identity(v)) for k, v in sorted(value.items())
                     if k not in {"stale_age_seconds", "age_seconds", "checked_at", "observed_at", "generated_at"})
    if isinstance(value, (tuple, list)):
        return tuple(source_refresh_identity(v) for v in value)
    return value


def preparation_key(
    event: Mapping[str, Any],
    context: Mapping[str, Any],
    completed: int | None,
    *,
    viewport_size: tuple[int, int] = (1920, 1080),
    display_mode: str = "compact",
) -> tuple[Any, ...]:
    source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
    slots = event.get("slots") if isinstance(event.get("slots"), list) else []
    return (
        str(source.get("session_id") or ""), str(source.get("game_instance_id") or ""),
        str(source.get("window_hwnd") or ""), str(context.get("champion_id") or ""),
        bool(context.get("ok")), str(context.get("context_revision") or ""),
        str(context.get("player_level") or ""), completed,
        str(source.get("selection_epoch") or ""), str(source.get("selection_revision") or ""),
        ordinary_selection(event),
        tuple((str(slot.get("slot_generation") or ""), str(slot.get("state") or ""),
               str(slot.get("augment_id") or ""), str(slot.get("name") or ""),
               slot.get("diagnostic") == "evidence_starved", str(slot.get("tier") or ""),
               str(slot.get("visual_variant_id") or ""))
              for slot in slots[:3] if isinstance(slot, Mapping)),
        first_selection_started(event),
        (int(viewport_size[0]), int(viewport_size[1])),
        "expanded" if str(display_mode) == "expanded" else "compact",
    )


@dataclass(frozen=True, slots=True)
class DisplayModel:
    """后台完整准备的一份显示输入；发布后所有字段按只读所有权交给 Tk。"""
    key: tuple[Any, ...]
    event: Mapping[str, Any]
    model: Mapping[str, Any]
    state: Any
    generation: Mapping[str, Any]
    scope: Mapping[str, Any]
    scope_key: object
    content_key: str
    phase: Literal["hints_ready", "ready"]
    timings: Mapping[str, float]
    host_read_at: float
    input_timing: Mapping[str, float] | None = None


# 保留既有调用名，不再建立第二份结构/状态源。
PreparedOverlayData = DisplayModel


class OverlayDataPreparation:
    def __init__(self, source: Any, *, initial_hints: Mapping[str, Any] | None = None) -> None:
        self.source = source
        self._condition = threading.Condition()
        self._pending: tuple[Any, ...] | None = None
        self._latest_request: tuple[Any, ...] | None = None
        self._source_identity: Any = None
        self._last_source_check = 0.0
        self._preparation_count = 0
        self._prewarmed_key: tuple[Any, ...] | None = None
        self._current_key: tuple[Any, ...] | None = None
        self._version = 0
        self._last_requested = 0.0
        self._result: PreparedOverlayData | None = None
        self._closed = False
        self._error = ""
        self._thread: threading.Thread | None = None
        self._generation = SelectionGenerationPin()
        self._hints = HostGenerationHintCache(initial_hints)
        self._scope = SelectionStatsPin()
        self._display_gate = DisplaySelectionGate()
        self._scope_identity: tuple[str, str] | None = None
        self._game_identity: tuple[Any, ...] | None = None
        self._requested_game_identity: tuple[str, str] | None = None
        self._selection_started = False
        self._observed_game_identity: tuple[str, str] | None = None
        self._observed_session_identity: tuple[str, str] | None = None
        self._observed_selection_started = False
        self._warmup_requested = False
        self._last_idle_refresh = float("-inf")
        self._bootstrap_generation = ""
        self._bootstrap_view: Any = None
        self._generation_status: dict[str, Any] = {}
        self._display_summaries = DisplaySummaryCache(max_entries=256)

    def warmup(self) -> None:
        """真实 Host 启动即后台校验 seed；不固定尚未开始的游戏局。"""
        self.request_idle_refresh()

    def observe_input(self, event: Mapping[str, Any]) -> None:
        """输入观察器即时记录首场景；Tk 尚未消费或 mailbox 合并都不能越过截止。"""
        source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
        identity = (str(source.get("session_id") or ""), str(source.get("game_instance_id") or ""))
        with self._condition:
            if self._closed:
                return
            # 无身份的瞬时缺文件不证明换局，不能清掉尚未被 Tk 消费的首场景截止。
            if identity[0] and identity != self._observed_session_identity:
                self._observed_session_identity = identity
                self._observed_selection_started = False
            self._observed_game_identity = identity
            if identity[0]:
                self._observed_selection_started |= first_selection_started(event)

    def request_idle_refresh(self, *, now: float | None = None) -> bool:
        """无对局请求时最多每秒提交一次元数据预热，不在 GUI 线程读取快照。"""
        stamp = time.monotonic() if now is None else now
        with self._condition:
            if self._closed or self._current_key is not None or stamp - self._last_idle_refresh < 1.0:
                return False
            self._last_idle_refresh = stamp
            self._warmup_requested = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="overlay-data-preparation", daemon=True)
                self._thread.start()
            self._condition.notify()
            return True

    def request(
        self, event: Mapping[str, Any], context: Mapping[str, Any], *,
        completed: int | None, host_read_at: float,
        viewport_size: tuple[int, int] = (1920, 1080),
        display_mode: str = "compact",
        input_timing: Mapping[str, float] | None = None,
    ) -> tuple[Any, ...] | None:
        # 在 GUI 请求入口只记录截止位，不读数据；不能让慢 open/合并队列漏掉首场景。
        raw_source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
        requested_game = (str(raw_source.get("session_id") or ""), str(raw_source.get("game_instance_id") or ""))
        with self._condition:
            if requested_game != self._requested_game_identity:
                self._requested_game_identity = requested_game
                self._selection_started = False
            self._selection_started = self._selection_started or first_selection_started(event)
        event = self._display_gate.filter(event)
        source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
        if fragment_selection(event) or not str(source.get("session_id") or ""):
            self.invalidate()
            return None
        key = preparation_key(
            event,
            context,
            completed,
            viewport_size=viewport_size,
            display_mode=display_mode,
        )
        now = time.monotonic()
        with self._condition:
            if self._closed:
                return None
            if key == self._current_key:
                return key
            if key != self._current_key:
                self._version += 1
                self._result = None
                self._prewarmed_key = None
                self._generation_status = {}
            self._current_key = key
            self._last_requested = now
            self._pending = (
                self._version,
                key,
                # read_event() 的独立快照在Host中只读；复制由后台执行，不在GUI锁内。
                event,
                dict(context),
                completed,
                host_read_at,
                (int(viewport_size[0]), int(viewport_size[1])),
                "expanded" if str(display_mode) == "expanded" else "compact",
                {**dict(input_timing or {}), "preparation_requested_at": time.time()},
            )
            self._latest_request = self._pending
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="overlay-data-preparation", daemon=True)
                self._thread.start()
            self._condition.notify()
        return key

    def invalidate(self) -> None:
        with self._condition:
            if self._current_key is not None or self._pending is not None or self._result is not None:
                self._version += 1
            self._current_key = None
            self._pending = None
            self._latest_request = None
            self._prewarmed_key = None
            self._result = None

    def poll(self, key: tuple[Any, ...] | None) -> PreparedOverlayData | None:
        with self._condition:
            return self._result if key is not None and key == self._current_key else None

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "state": self._result.phase if self._result is not None else
                "prewarmed" if self._current_key is not None and self._prewarmed_key == self._current_key else "preparing",
                "request_version": self._version, "queue_depth": int(self._pending is not None),
                "preparation_count": self._preparation_count,
                "error_type": self._error,
                "bootstrap_generation_id": self._bootstrap_generation,
                "generation": dict(self._generation_status),
                "timings": dict(self._result.timings) if self._result is not None else {},
            }

    def close(self, timeout: float = 2.0) -> None:
        with self._condition:
            self._closed = True
            self._version += 1
            self._pending = None
            self._latest_request = None
            self._result = None
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))

    def _valid(self, version: int, key: tuple[Any, ...]) -> bool:
        with self._condition:
            return (not self._closed and version == self._version and key == self._current_key
                    and key[:2] == self._requested_game_identity
                    and (self._observed_game_identity is None or key[:2] == self._observed_game_identity))

    def _publish(self, version: int, result: PreparedOverlayData) -> None:
        with self._condition:
            if self._valid(version, result.key):
                self._result = result
                self._error = ""

    def _refresh_idle_snapshot(self, version: int) -> None:
        identity, error = "", ""
        with self._condition:
            if self._closed or self._current_key is not None or version != self._version:
                return
        try:
            view = self.source.open_view()
            if view is not None:
                status = view.status()
                identity = str(status.get("generation_id") or "")
                if status.get("failed_generation_id"):
                    identity, error = "", "current_snapshot_invalid"
                if status.get("state") in {"failed", "unavailable"}:
                    identity = ""
                with self._condition:
                    if self._closed or self._current_key is not None or version != self._version:
                        return
                if identity and identity != self._bootstrap_generation:
                    self._hints.resolve(view, self.source.read_hint_cache)
            if not identity and not error:
                error = "snapshot_unavailable"
        except Exception as exc:
            identity, error = "", type(exc).__name__
        with self._condition:
            if (not self._closed and self._current_key is None and version == self._version
                    and not self._selection_started and not self._observed_selection_started):
                if identity:
                    self._bootstrap_generation = identity
                    self._bootstrap_view = view
                self._error = error

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None or self._warmup_requested,
                                         timeout=.1 if self._latest_request is not None else None)
                if self._closed:
                    return
                request = self._pending
                self._pending = None
                idle_version = self._version
                self._warmup_requested = False
                latest = self._latest_request
            if request is None:
                if latest is None:
                    self._refresh_idle_snapshot(idle_version)
                    continue
                try:
                    if not self._needs_refresh(latest):
                        continue
                except Exception as exc:
                    with self._condition:
                        if latest[0] == self._version:
                            self._error = type(exc).__name__
                    continue
                request = latest
            try:
                request = (*request[:2], deepcopy(dict(request[2])), *request[3:])
                if not self._valid(request[0], request[1]):
                    continue
                if preparation_key(request[2], request[3], request[4], viewport_size=request[6],
                                   display_mode=request[7]) != request[1]:
                    raise ValueError("event_snapshot_changed")
                self._preparation_count += 1
                self._prepare(*request)
            except Exception as exc:
                with self._condition:
                    if request[0] == self._version:
                        self._error = type(exc).__name__

    def _needs_refresh(self, request: tuple[Any, ...]) -> bool:
        version, key, event = request[:3]
        if not self._valid(version, key):
            return False
        result = self.poll(key)
        # Stage等待有既有截止，按100ms复核而非等下一次一秒heartbeat。
        if result is not None and result.scope.get("status") == "preparing":
            return True
        now = time.monotonic()
        if now - self._last_source_check < 1.0:
            return False
        self._last_source_check = now
        view = self._resolve_generation(version, key, event, request[3])
        if not self._valid(version, key):
            return False
        identity = source_refresh_identity(view.status()) if view is not None else None
        with self._condition:
            if version == self._version:
                self._generation_status = dict(self._generation.status())
        # 轻量 hints 已发布后，scoped stats / Stage 投影仍可能瞬时失败。
        # 这类结果不是终态；沿用一秒来源复核节流重试，避免永久停在“统计准备中”。
        return bool(
            (result is None and self._prewarmed_key != key)
            or getattr(result, "phase", "ready") != "ready"
            or identity != self._source_identity
        )

    def _prepare(
        self, version: int, key: tuple[Any, ...], event: Mapping[str, Any],
        context: Mapping[str, Any], completed: int | None, host_read_at: float,
        viewport_size: tuple[int, int], display_mode: str,
        input_timing: Mapping[str, float] | None = None,
    ) -> None:
        started = time.perf_counter()
        input_timing = {**dict(input_timing or {}), "preparation_started_at": time.time()}
        if key[:2] != self._game_identity:
            self._generation.reset()
            self._scope.reset()
            self._game_identity = key[:2]
        view = self._resolve_generation(version, key, event, context)
        timings = {"snapshot_ready_ms": (time.perf_counter() - started) * 1000.0}
        if not self._valid(version, key):
            return
        if view is None:
            with self._condition:
                if version == self._version:
                    self._error = "snapshot_unavailable"
            return
        hints = self._hints.resolve(view, self.source.read_hint_cache)
        self._source_identity = source_refresh_identity(view.status())
        self._last_source_check = time.monotonic()
        timings["hints_ready_ms"] = (time.perf_counter() - started) * 1000.0
        generation = self._generation.status()
        with self._condition:
            if version == self._version and key == self._current_key:
                self._generation_status = dict(generation)
        if not ordinary_selection(event):
            # 非选择期只预热同局的 verified view/hint；不发布普通海克斯展示。
            if context.get("ok") and context.get("champion_id") and view is not None:
                self._scope.cache.load(view, str(context["champion_id"]))
            if self._valid(version, key):
                self._prewarmed_key = key
            return
        if not self._valid(version, key):
            return
        if not context.get("ok"):
            return
        state = build_runtime_session(
            event=event, context_payload=context, snapshot_view=view,
            user_enabled=True, game_present=True, private_stats_enabled=source_has_private_stats(hints),
        )

        def publish(model: Mapping[str, Any], projected: Any, scope: Mapping[str, Any], scope_key: object, phase: Literal["hints_ready", "ready"]) -> None:
            content_key = hashlib.sha256(json.dumps(model, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            self._publish(version, PreparedOverlayData(
                key, event, model, projected, generation, scope, scope_key, content_key, phase,
                {**timings, "prepared_ms": (time.perf_counter() - started) * 1000.0}, host_read_at,
                {**input_timing, "preparation_completed_at": time.time()},
            ))

        preliminary = prepare_synergy_display_summaries(
            build_render_model_from_session(state, hint_cache=hints),
            viewport_size=viewport_size,
            display_mode=display_mode,
            cache=self._display_summaries,
        )
        # 已有联动不等待 scoped stats 的本地校验；统计在未确认前不得假装 READY。
        preliminary["stats"] = [
            {**_detecting_stat(index), "status_code": "STATS_PREPARING", "status_text": "统计准备中", "stats_text": "统计准备中"}
            if index < len(event.get("slots", [])) and event["slots"][index].get("state") == "ready"
            else _detecting_stat(index, str(event["slots"][index].get("diagnostic") or "")
                                 if index < len(event.get("slots", [])) else "")
            for index in range(3)
        ]
        previous = self.poll(key)
        if previous is None or previous.phase != "ready":
            publish(preliminary, state, {}, ("preparing",), "hints_ready")
        scope_identity = (str(key[0]), str(context.get("champion_id") or ""))
        if scope_identity != self._scope_identity:
            self._scope.reset()
            self._scope_identity = scope_identity
        scope = self._scope.resolve(event, context, view, completed_selection_count=completed)
        if not self._valid(version, key):
            return
        timings["scoped_stats_ready_ms"] = (time.perf_counter() - started) * 1000.0
        projected = OverlayStageRuntime.project(state, scope, view)
        scope_status = scope.to_status()
        scope_status.update({name: generation[name] for name in ("new_generation_available", "new_generation_id")})
        model = prepare_synergy_display_summaries(
            build_render_model_from_session(projected, hint_cache=hints, stats_scope=scope_status),
            viewport_size=viewport_size,
            display_mode=display_mode,
            cache=self._display_summaries,
        )
        publish(model, projected, scope_status, scope.semantic_key(), "ready")

    def _resolve_generation(
        self, version: int, key: tuple[Any, ...], event: Mapping[str, Any], context: Mapping[str, Any],
    ) -> Any:
        with self._condition:
            started = (
                self._selection_started and key[:2] == self._requested_game_identity
                or self._observed_selection_started and key[:2] == self._observed_game_identity
            )

        def can_adopt() -> bool:
            with self._condition:
                return bool(
                    self._valid(version, key)
                    and (started or not (self._selection_started or self._observed_selection_started))
                )

        view = self._generation.resolve(
            event, self.source.open_view,
            champion_id=str(context.get("champion_id") or "") if context.get("ok") else "",
            selection_started=started, can_adopt=can_adopt,
            initial_view=self._bootstrap_view,
            adoption_lock=self._condition,
        )
        with self._condition:
            if view is not None and self._valid(version, key):
                # 下一局/暂不可用 current 仍可使用已经验证的旧代；不重新打开冒充旧数据。
                self._bootstrap_view = view
        return view
