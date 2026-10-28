"""单工作线程、分阶段发布与跨局/碎片版本栅栏的确定性回归。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from hextech.interfaces.overlay import host_data_preparation as module


def event(session="game-a", epoch=1, revision=1):
    return {
        "selection_type": "hextech", "active": True, "visible": True,
        "source": {"session_id": session, "game_instance_id": session, "window_hwnd": 20,
                   "selection_epoch": epoch, "selection_revision": revision,
                   "selection_window_active": True, "scene_state": "active"},
        "slots": [{"slot": i, "state": "ready", "augment_id": str(i + 1), "name": f"卡{i}",
                   "slot_generation": 1} for i in range(3)],
        "timing": {"event_written_at": time.time()},
    }


class View:
    def status(self):
        return {"generation_id": "generation-a"}

    def get_overlay_hints(self):
        return {"source": {}, "hints": {}, "name_index": {}}


class Source:
    def __init__(self, blocked=False):
        self.opened = threading.Event()
        self.release = threading.Event()
        self.threads = []
        self.opens = 0
        if not blocked:
            self.release.set()

    def open_view(self):
        self.threads.append(threading.get_ident())
        self.opens += 1
        self.opened.set()
        assert self.release.wait(3.0)
        return View()

    def read_hint_cache(self):
        raise AssertionError("verified view must provide hints")


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(module, "build_runtime_session", lambda **kw: SimpleNamespace(event=kw["event"]))
    monkeypatch.setattr(module.OverlayStageRuntime, "project", lambda state, *_args: state)

    def model(state, **_kwargs):
        return {
            "stats": [{"slot": i, "state": "matched", "status_code": "READY", "stats_text": "胜率 50%"}
                      for i in range(3)],
            "synergies": [{"slot": 0, "content": "已校验联动", "augment_name": state.event["slots"][0]["name"]}],
        }

    monkeypatch.setattr(module, "build_render_model_from_session", model)
    workers = []

    def create(source):
        worker = module.OverlayDataPreparation(source)
        # 真 Host 启动先做 verified warmup；这些测试显式提供已知的首代，
        # 不把首次选择后才打开的 current 当作截止前证据。
        worker._bootstrap_view = View()
        worker._scope.resolve = lambda *_a, **_k: SimpleNamespace(
            to_status=lambda: {"status": "ready"}, semantic_key=lambda: ("stage", 1),
        )
        workers.append((worker, source))
        return worker

    yield create
    for worker, source in workers:
        source.release.set()
        worker.close()
        assert worker._thread is None or not worker._thread.is_alive()


def request(worker, payload, hero="114"):
    return worker.request(payload, {"ok": True, "champion_id": hero, "player_level": 3},
                          completed=0, host_read_at=time.time())


def wait_result(worker, key, phase="ready"):
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        result = worker.poll(key)
        if result is not None and result.phase == phase:
            return result
        threading.Event().wait(.005)
    raise AssertionError(worker.status())


def test_data_load_never_blocks_request_thread_and_coalesces(prepared):
    source = Source(blocked=True)
    worker = prepared(source)
    started = time.perf_counter()
    key = request(worker, event())
    assert time.perf_counter() - started < .1
    assert source.opened.wait(1)
    for _ in range(50):
        assert request(worker, event()) == key
    assert source.opens == 1
    assert source.threads == [worker._thread.ident]
    assert threading.get_ident() not in source.threads
    assert worker.poll(key) is None
    source.release.set()
    assert wait_result(worker, key).generation["generation_id"] == "generation-a"


@pytest.mark.parametrize("snapshot_seconds", [0.0, 1.2])
@pytest.mark.parametrize("cancel", [False, True])
def test_actual_stage_confirmation_keeps_timely_samples_across_slow_superseded_snapshot(
    monkeypatch, snapshot_seconds, cancel,
):
    from hextech.interfaces.overlay.stats_pin import SelectionStatsPin
    from hextech.interfaces.overlay.generation_pin import SelectionGenerationPin
    from test_overlay_stage_confirmation import context as live_context

    clock = [100.0]
    timer = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0], perf_counter=lambda: clock[0])
    monkeypatch.setattr(module, "time", timer)
    monkeypatch.setattr(module, "build_runtime_session", lambda **kw: SimpleNamespace(event=kw["event"]))
    monkeypatch.setattr(module.OverlayStageRuntime, "project", lambda state, *_args: state)
    monkeypatch.setattr(module, "build_render_model_from_session", lambda *_a, **_kw: {"stats": [], "synergies": []})
    second_arrival = [None]

    def open_view():
        entered = clock[0]
        if second_arrival[0] is not None:
            callback, second_arrival[0] = second_arrival[0], None
            clock[0] = max(clock[0], 100.65)
            callback()
        clock[0] = max(clock[0], entered + snapshot_seconds)
        return View()

    worker = module.OverlayDataPreparation(SimpleNamespace(open_view=open_view, read_hint_cache=lambda: {}))
    worker._thread = SimpleNamespace()  # 手动推进实际后台方法，不启动线程或产品。
    worker._bootstrap_view = View()
    worker._generation = SelectionGenerationPin(now=lambda: clock[0])
    worker._input_scope = SelectionStatsPin(now=lambda: clock[0], wall_now=lambda: clock[0])
    worker._scope = SelectionStatsPin(now=lambda: clock[0], wall_now=lambda: clock[0],
        cache=SimpleNamespace(load=lambda *_a: SimpleNamespace(available=True, view=None, run_id="unit", reason="")))
    worker.observe_input(event())

    def submit(seq, start):
        ctx = {**live_context(seq=seq, start=start), "game_instance_id": "game-a", "window_hwnd": 20}
        return worker.request(event(), ctx, completed=None, host_read_at=clock[0])

    clock[0] = 100.25
    old_key = submit(1, 100.2)
    first = worker._pending
    if snapshot_seconds:
        second_arrival[0] = lambda: submit(2, 100.6)
    worker._prepare(*first)
    if not snapshot_seconds:
        clock[0] = 100.65
        submit(2, 100.6)
    second = worker._pending
    if cancel:
        worker.invalidate()
    elif snapshot_seconds:
        # 已在输入端确认后，第三份 heartbeat 不再作废统计 IO，避免永远追不上发布频率。
        version = worker._version
        second_arrival[0] = lambda: submit(3, 100.9)
    worker._prepare(*second)
    if cancel:
        assert worker.poll(second[1]) is None
        assert worker._input_scope._input_confirmation is None
    else:
        result = worker.poll(second[1])
        assert result.scope["status"] == "ready" and result.scope["stage"] == 3
        assert result.scope["confirmation"]["publication_seq"] == 2
        assert worker.poll(old_key) is None
        if snapshot_seconds:
            assert worker._version == version


def test_new_game_rejects_old_inflight_result(prepared):
    source = Source(blocked=True)
    worker = prepared(source)
    old_key = request(worker, event())
    assert source.opened.wait(1)
    new_key = request(worker, event("game-b"), hero="777")
    source.release.set()
    result = wait_result(worker, new_key)
    assert result.key[0] == "game-b"
    assert result.key[3] == "777"
    assert worker.poll(old_key) is None


@pytest.mark.parametrize("old_ready", [False, True])
def test_same_epoch_sidecar_restart_rejects_confirmed_old_pending_and_published_scope(monkeypatch, old_ready):
    from hextech.interfaces.overlay.stats_pin import SelectionStatsPin

    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0], perf_counter=lambda: clock[0],
    ))
    monkeypatch.setattr(module, "build_runtime_session", lambda **kw: SimpleNamespace(event=kw["event"]))
    monkeypatch.setattr(module.OverlayStageRuntime, "project", lambda state, *_args: state)
    monkeypatch.setattr(module, "build_render_model_from_session", lambda *_a, **_kw: {"stats": [], "synergies": []})
    worker = module.OverlayDataPreparation(SimpleNamespace(open_view=lambda: View(), read_hint_cache=lambda: {}))
    worker._thread = SimpleNamespace()
    worker._bootstrap_view = View()
    worker._input_scope = SelectionStatsPin(now=lambda: clock[0], wall_now=lambda: clock[0])
    worker._scope = SelectionStatsPin(now=lambda: clock[0], wall_now=lambda: clock[0],
        cache=SimpleNamespace(load=lambda *_a: SimpleNamespace(available=True, view=None, run_id="unit", reason="")))
    ctx = {"ok": True, "champion_id": "114", "game_instance_id": "game-a", "window_hwnd": 20}

    def submit(sidecar):
        payload = event()
        payload["source"]["sidecar_instance_id"] = sidecar
        worker.observe_input(payload)
        return worker.request(payload, ctx, completed=0, host_read_at=clock[0])

    old_key = submit("vision-a")
    old_request = worker._pending
    assert old_request[3]["_stage_input_confirmation"] is not None
    assert old_request[3]["_stage_wait_revision"] is None
    old_result = None
    if old_ready:
        worker._prepare(*old_request)
        old_result = worker.poll(old_key)
        assert old_result.scope["frozen"] and old_result.scope["stage"] == 1
    clock[0] = 100.5
    new_key = submit("vision-b")
    new_request = worker._pending
    assert new_request[3]["_stage_input_confirmation"] is not None
    assert new_request[3]["_stage_wait_revision"] is None
    assert new_key != old_key and new_request[0] > old_request[0]
    assert worker.poll(old_key) is None
    # 迟到旧工作和迟到旧发布均不能越过新请求的版本门。
    worker._prepare(*old_request)
    if old_result is not None:
        worker._publish(old_request[0], old_result)
    assert worker.poll(old_key) is None and worker.poll(new_key) is None
    worker._prepare(*new_request)
    result = worker.poll(new_key)
    assert result.event["source"]["sidecar_instance_id"] == "vision-b"
    assert result.scope["confirmation"]["boundary_at"] == 100.5
    assert worker._scope._identity[-1] == "vision-b"
    assert result.generation["stats_generation_id"] == "generation-a"


def test_fragment_invalidates_inflight_and_latches_same_epoch(prepared):
    source = Source(blocked=True)
    worker = prepared(source)
    key = request(worker, event())
    assert source.opened.wait(1)
    fragment = event()
    fragment["selection_type"] = "body_shard"
    assert request(worker, fragment) is None
    source.release.set()
    assert request(worker, event()) is None
    assert worker.poll(key) is None
    next_key = request(worker, event(epoch=2))
    assert wait_result(worker, next_key).event["source"]["selection_epoch"] == 2


def test_synergy_publishes_before_slow_scoped_statistics(prepared):
    worker = prepared(Source())
    original = worker._scope.resolve
    entered = threading.Event()
    release = threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    worker._scope.resolve = delayed
    try:
        key = request(worker, event())
        assert entered.wait(1)
        interim = wait_result(worker, key, "hints_ready")
        assert interim.model["synergies"][0]["content"] == "已校验联动"
        assert all(row["status_code"] == "STATS_PREPARING" for row in interim.model["stats"])
        release.set()
        assert all(row["status_code"] == "READY" for row in wait_result(worker, key).model["stats"])
    finally:
        release.set()


def test_interim_result_retries_after_scoped_statistics_failure(prepared):
    worker = prepared(Source())
    original = worker._scope.resolve
    attempts = 0

    def flaky(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary scope failure")
        return original(*args, **kwargs)

    worker._scope.resolve = flaky
    key = request(worker, event())
    interim = wait_result(worker, key, "hints_ready")
    assert all(row["status_code"] == "STATS_PREPARING" for row in interim.model["stats"])
    assert wait_result(worker, key, "ready").phase == "ready"
    assert attempts == 2


def test_unknown_context_never_publishes_numeric_model(prepared):
    worker = prepared(Source())
    key = worker.request(event(), {"ok": False}, completed=0, host_read_at=time.time())
    assert worker.source.opened.wait(1)
    assert worker.poll(key) is None
    good_key = request(worker, event())
    assert wait_result(worker, good_key).phase == "ready"


def test_display_gate_preserves_pause_but_rejects_fragment_old_ready():
    from hextech.interfaces.overlay.display_contract import DisplaySelectionGate

    gate = DisplaySelectionGate()
    ordinary = event()
    pause = {**ordinary, "source": {**ordinary["source"], "scene_state": "paused", "transient_pause": True}}
    assert gate.filter(pause) is pause
    fragment = {**ordinary, "selection_type": "body_shard"}
    assert not gate.filter(fragment)["visible"]
    assert not gate.filter(ordinary)["slots"]
    assert not gate.filter(pause)["visible"]
    assert gate.filter(event(epoch=2))["visible"]


def test_display_hint_projection_preserves_original_and_omits_heavy_statistics():
    from hextech.modules.data.generation import DataSnapshotView

    raw = {"source": {"private_policy_stats_enabled": True}, "name_index": {"测试": "1"},
           "hints": {"1": {"augment_id": "1", "name": "测试", "tier": "黄金",
                           "stats_by_champion_id": {"114": {"winrate": .5}},
                           "synergies": [{"hero_id": "114", "content": "联动"}]}}}
    from copy import deepcopy
    query = SimpleNamespace(hints=lambda: deepcopy(raw))
    view = DataSnapshotView(SimpleNamespace(), query)
    slim = view.get_overlay_display_hints()
    assert "stats_by_champion_id" not in slim["hints"]["1"]
    assert slim["hints"]["1"]["synergies"] == raw["hints"]["1"]["synergies"]
    slim["hints"]["1"]["synergies"][0]["content"] = "变更副本"
    assert raw["hints"]["1"]["synergies"][0]["content"] == "联动"


def test_bootstrap_warmup_is_background_and_does_not_pin_a_game(prepared):
    source = Source(blocked=True)
    worker = prepared(source)
    worker.warmup()
    assert source.opened.wait(1)
    assert source.threads == [worker._thread.ident]
    assert worker.status()["generation"] == {}
    source.release.set()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not worker.status()["bootstrap_generation_id"]:
        threading.Event().wait(.005)
    assert worker.status()["bootstrap_generation_id"] == "generation-a"
    assert worker.status()["generation"] == {}


def test_slow_preselection_upgrade_cannot_cross_candidate_cutoff_even_if_coalesced(prepared):
    source = Source()
    latest = [View()]
    original = source.open_view
    def open_current():
        original()
        return latest[0]
    source.open_view = open_current
    worker = prepared(source)
    worker._scope.cache.load = lambda *_a: None
    inactive = event()
    inactive.update(active=False, visible=False)
    inactive["source"].update(selection_window_active=False, scene_state="absent")
    request(worker, inactive)
    deadline = time.monotonic() + 2
    while worker.status()["state"] != "prewarmed" and time.monotonic() < deadline:
        threading.Event().wait(.005)
    assert worker.status()["generation"]["generation_id"] == "generation-a"
    newer = View()
    newer.status = lambda: {"generation_id": "generation-b"}
    latest[0] = newer
    source.release.clear()
    source.opened.clear()
    worker._generation._last_latest_probe_at -= 2
    inactive["source"]["selection_revision"] = 2
    request(worker, inactive)
    assert source.opened.wait(1)
    candidate = event(revision=3)
    candidate["source"].update(scene_state="candidate", scene_present=True, selection_window_active=False)
    candidate["slots"] = [{"state": "detecting"}] * 3
    request(worker, candidate)
    # 单待处理队列可以跳过 candidate；请求入口的整局截止位不能被下一次 absent 清掉。
    inactive["source"]["selection_revision"] = 4
    request(worker, inactive)
    source.release.set()
    deadline = time.monotonic() + 2
    while worker.status()["state"] != "prewarmed" and time.monotonic() < deadline:
        threading.Event().wait(.005)
    status = worker.status()["generation"]
    assert status["generation_id"] == "generation-a"
    assert status["stats_frozen"] is True


def test_inactive_prewarm_is_complete_without_visible_result(prepared):
    source = Source()
    worker = prepared(source)
    payload = event()
    payload.update(active=False, visible=False)
    payload["source"].update(selection_window_active=False, scene_state="absent")
    worker._scope.cache.load = lambda *_a: None
    key = request(worker, payload)
    deadline = time.monotonic()+2
    while worker.status()["state"] != "prewarmed" and time.monotonic() < deadline:
        threading.Event().wait(.005)
    assert worker.status()["state"] == "prewarmed"
    assert worker.poll(key) is None
    count = worker.status()["preparation_count"]
    threading.Event().wait(1.15)
    assert worker.status()["preparation_count"] == count
