"""Deterministic GUI budgets and phase attribution; no native windows or real inputs."""
from types import MethodType, SimpleNamespace
import queue
import threading

import pytest

from hextech.interfaces.desktop import candidate_application as application
from hextech.interfaces.desktop.app_view import DesktopViewMixin
from hextech.interfaces.desktop.responsive_view import DesktopResponsiveMixin
from hextech.interfaces.desktop.window_presentation import CandidateDisplay
from test_desktop_window_presentation import controller, observation


@pytest.fixture
def budget_ui(monkeypatch):
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(application.time, "perf_counter", lambda: clock.value)
    threads, built = [], []

    class Frame:
        def __init__(self, parent=None, **kwargs):
            self.children = []
            self.destroyed = False
            clock.value += .0001
        def bind(self, *args): pass
        def destroy(self): self.destroyed = True
        def winfo_reqheight(self): return max(1, len(self.children)*100)

    class Card:
        def __init__(self, parent, identity):
            self.parent, self.identity = parent, identity
            self.destroyed = False
            parent.children.append(self)
        def destroy(self):
            self.destroyed = True
            clock.value += .001
        def pack_forget(self): pass
        def pack(self, **kwargs): pass
        def winfo_y(self): return self.parent.children.index(self)*100
        def winfo_height(self): return 100

    class Canvas:
        def __init__(self): self.window = None
        def itemconfigure(self, identity, *, window): self.window = window
        def canvasy(self, value): return value

    ui, owner = controller()
    ui.canvas = Canvas()
    ui.list_frame = Frame()
    ui.canvas.window = ui.list_frame
    ui._list_window_id = 1
    ui._card_rows, ui._card_order = {}, []
    ui._ui_scale_value = lambda: 1.0
    ui._snapshot_generation_id = "g1"
    ui.current_candidate_groups = {"bench_champion_ids": ["previous"]}
    ui._ui_callbacks = queue.Queue()
    ui._set_status = lambda *args: None
    ui._sync_list_scrollbar = lambda: None
    for name in ("_advance_candidate_display", "_ensure_card_state", "_commit_candidate_display_metadata"):
        setattr(ui, name, MethodType(getattr(DesktopViewMixin, name), ui))
    ui._drain_ui_callbacks = MethodType(DesktopResponsiveMixin._drain_ui_callbacks, ui)
    def build(item, scale, *, parent=None):
        threads.append(threading.get_ident())
        built.append(item["id"])
        clock.value += .001
        return {"card": Card(ui.list_frame if parent is None else parent, item["id"])}
    ui._build_candidate_card = build
    monkeypatch.setattr(application.tk, "Frame", Frame)
    return ui, owner, clock, threads, built


def publish(owner, count, *, prefix=""):
    groups = {"bench_champion_ids": [str(i) for i in range(count)]}
    owner.publish_candidates(groups)
    request = owner.take_candidate_request()
    display = CandidateDisplay(request, "g1", groups, tuple({"id": f"{prefix}{i}"} for i in range(count)))
    assert owner.publish_candidate_display(display)
    return display


def settle(ui, owner, limit=200):
    for _ in range(limit):
        owner.apply_latest(now=10.2)
        if (owner._candidate_display is None and getattr(ui, "_candidate_application", None) is None
                and getattr(ui, "_candidate_retired_application", None) is None):
            return
    pytest.fail("candidate task did not settle")


def test_100_card_old_synchronous_path_vs_budgeted_atomic_publication(budget_ui):
    ui, owner, clock, threads, built = budget_ui
    rows = [{"id": str(i)} for i in range(100)]
    old_renderer = DesktopViewMixin._update_candidate_cards_content.__wrapped__.__wrapped__
    start = clock.value
    old_renderer(ui, rows)
    assert clock.value-start == pytest.approx(.100)
    ui._card_order = list(ui._card_rows)
    previous_frame, previous_rows = ui.list_frame, ui._card_rows.copy()
    built.clear()
    display = publish(owner, 100, prefix="new-")
    owner.apply_latest(now=10.2)
    assert 1 <= len(built) <= 4
    assert ui.list_frame is previous_frame and ui._card_rows == previous_rows
    assert ui.current_candidate_groups == {"bench_champion_ids": ["previous"]}
    assert owner.status()["gui_tick_elapsed_ms"] <= 4.2
    rounds = 1
    while owner._candidate_display is not None:
        owner.apply_latest(now=10.2)
        rounds += 1
        if ui.list_frame is previous_frame:
            assert ui._card_rows == previous_rows
        else:
            assert set(ui._card_rows) == {f"new-{i}" for i in range(100)}
    assert rounds >= 25
    assert ui._candidate_display_revision == display.request.revision
    assert threads and set(threads) == {threading.get_ident()}
    settle(ui, owner)
    assert previous_frame.destroyed
    assert all(row["card"].destroyed for row in previous_rows.values())
    owner.close()


@pytest.mark.parametrize("invalidate", ["request", "generation", "phase", "game", "client", "expiry", "dpi"])
def test_partial_old_list_is_never_published_after_invalidation(budget_ui, invalidate):
    ui, owner, clock, _, built = budget_ui
    old_frame = ui.list_frame
    publish(owner, 100, prefix="old-")
    owner.apply_latest(now=10.2)
    task = ui._candidate_application
    assert task.rows and ui.list_frame is old_frame
    if invalidate == "request":
        publish(owner, 2, prefix="new-")
    elif invalidate == "generation":
        ui._snapshot_generation_id = "g2"
    elif invalidate == "phase":
        owner.publish_phase({"context_phase": "lobby"}, client_hwnd=100, observed_at=10.3)
    elif invalidate == "game":
        owner.publish_window(observation(game_visible=True, game_window_hwnd=200, observed_at=10.3))
    elif invalidate == "client":
        owner.publish_window(observation(client_hwnd=101, observed_at=10.3))
    elif invalidate == "expiry":
        owner._candidate_observed_at -= 100
    else:
        ui._desktop_layout_key = (480, 1.5, "mixed-dpi")
    settle(ui, owner)
    assert task.frame is None
    if invalidate in {"request", "dpi"}:
        expected = {"new-0", "new-1"} if invalidate == "request" else {f"old-{i}" for i in range(100)}
        assert set(ui._card_rows) == expected
    else:
        assert ui.list_frame is old_frame and ui._card_rows == {}
    owner.close()


@pytest.mark.parametrize("gate", ["game", "minimized", "manual", "foreground", "expired_observation"])
def test_hard_hide_runs_before_candidate_slice_and_observation_ttl_stays_one_second(budget_ui, gate, monkeypatch):
    ui, owner, _, _, _ = budget_ui
    owner.apply_latest(now=10.2)
    publish(owner, 100)
    build = ui._build_candidate_card
    def checked(*args, **kwargs):
        assert not ui._window_visible
        return build(*args, **kwargs)
    ui._build_candidate_card = checked
    if gate == "game":
        owner.publish_window(observation(game_visible=True, observed_at=10.3))
    elif gate == "minimized":
        owner.publish_window(observation(client_visible=False, observed_at=10.3))
    elif gate == "manual":
        monkeypatch.setattr(application.time, "time", lambda: 10.2)
        owner.request_hide()
    elif gate == "foreground":
        ui._desktop_client_is_foreground = lambda _hwnd: False
    result = owner.apply_latest(now=11.01 if gate == "expired_observation" else 10.3)
    assert not result.visible and not ui._window_visible
    owner.close()


def test_callbacks_share_four_ms_slice_with_candidates(budget_ui):
    ui, owner, clock, _, built = budget_ui
    publish(owner, 100)
    for _ in range(10):
        ui._ui_callbacks.put(lambda: setattr(clock, "value", clock.value+.003))
    owner.apply_latest(now=10.2)
    assert built == []
    assert ui._ui_callbacks.qsize() == 8
    assert owner.status()["gui_tick_elapsed_ms"] == pytest.approx(6)
    # A single callback cannot be interrupted, but the next unit is deferred.
    assert owner.status()["gui_callback_max_ms"] == pytest.approx(3)
    owner.close()


@pytest.mark.parametrize("stage", ["native_window", "layout_measure", "layer_maintenance", "widget_create_update"])
def test_unpreemptible_slow_operation_is_attributed_and_retained(budget_ui, stage):
    ui, owner, clock, _, _ = budget_ui
    def delay(): clock.value += .350
    if stage == "native_window":
        move = ui._move_overlay_to
        ui._move_overlay_to = lambda *a, **kw: (delay(), move(*a, **kw))
    elif stage == "layout_measure":
        ui._apply_desktop_layout = lambda _layout: (delay(), 200)[1]
    elif stage == "layer_maintenance":
        ui._maintain_foreground_layer = lambda _hwnd, eligible: (delay(), {"desired_topmost": eligible})[1]
    else:
        build = ui._build_candidate_card
        ui._build_candidate_card = lambda *a, **kw: (delay(), build(*a, **kw))[1]
        publish(owner, 100)
    owner.apply_latest(now=10.2)
    status = owner.status()
    assert status["gui_tick_elapsed_ms"] >= 350
    assert status["gui_tick_phases_ms"][stage] >= 350
    assert status["last_slow_gui_tick"]["phases_ms"][stage] >= 350
    if stage == "widget_create_update":
        assert len(ui._candidate_application.rows) == 1
        assert ui._card_rows == {}
    owner.close()


def test_same_content_and_geometry_skip_rebuild_measure_and_native_operations(budget_ui):
    ui, owner, clock, _, built = budget_ui
    measured, moves = [], []
    ui._apply_desktop_layout = lambda _layout: measured.append(1) or 200
    move = ui._move_overlay_to
    ui._move_overlay_to = lambda *a, **kw: (moves.append(1), move(*a, **kw))
    publish(owner, 10)
    settle(ui, owner)
    baseline = (len(built), len(measured), len(moves))
    # Only irrelevant context metadata differs; rendered rows remain identical.
    owner.publish_candidates({"bench_champion_ids": [str(i) for i in range(10)], "noise": 2})
    request = owner.take_candidate_request()
    assert owner.publish_candidate_display(CandidateDisplay(request, "g1", request.groups,
        tuple({"id": str(i)} for i in range(10))))
    settle(ui, owner)
    assert (len(built), len(measured), len(moves)) == baseline
    assert ui.current_candidate_groups["noise"] == 2
    owner.close()


def test_close_destroys_partial_staging_and_releases_task_references(budget_ui):
    ui, owner, _, _, _ = budget_ui
    publish(owner, 100)
    owner.apply_latest(now=10.2)
    task = ui._candidate_application
    owner.close()
    assert task.frame.destroyed and not task.rows
    assert ui._candidate_application is None and ui._candidate_retired_application is None
    assert owner._candidate_display is None and owner.ui is None


def test_exception_during_construction_retires_partial_widgets_before_retry(budget_ui):
    ui, owner, _, _, _ = budget_ui
    publish(owner, 100)
    owner.apply_latest(now=10.2)
    task = ui._candidate_application
    build = ui._build_candidate_card
    ui._build_candidate_card = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("injected widget failure"))
    with pytest.raises(RuntimeError, match="injected"):
        owner.apply_latest(now=10.2)
    assert ui._candidate_application is None
    assert ui._candidate_retired_application is task
    ui._build_candidate_card = build
    settle(ui, owner)
    assert task.frame is None and len(ui._card_rows) == 100
    owner.close()


def test_late_avatar_cards_are_retried_in_bounded_round_robin_slices(budget_ui):
    ui, owner, clock, _, _ = budget_ui
    publish(owner, 40)
    settle(ui, owner)
    requests = []
    for key, row in ui._card_rows.items():
        row["img_label"] = SimpleNamespace(_hextech_avatar_loaded=key == "0")
    ui._request_avatar = lambda row: requests.append(row)
    for _ in range(5):
        before = len(requests)
        owner.apply_latest(now=10.2)
        assert len(requests)-before <= 8
    assert len(requests) == 39
    assert {id(row) for row in requests} == {id(row) for key, row in ui._card_rows.items() if key != "0"}
    owner.close()


def test_superseded_during_last_widget_call_never_publishes(budget_ui):
    ui, owner, clock, _, _ = budget_ui
    publish(owner, 1, prefix="obsolete-")
    old = ui.list_frame
    build = ui._build_candidate_card
    def supersede(*a, **kw):
        result = build(*a, **kw)
        publish(owner, 2, prefix="latest-")
        ui._build_candidate_card = build
        return result
    ui._build_candidate_card = supersede
    owner.apply_latest(now=10.2)
    assert ui.list_frame is old and ui._card_rows == {}
    settle(ui, owner)
    assert set(ui._card_rows) == {"latest-0", "latest-1"}
    owner.close()
