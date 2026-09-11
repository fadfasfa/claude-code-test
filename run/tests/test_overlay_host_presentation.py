"""验证 Overlay mapped/composed 分层与有限像素探针。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class _Root:
    def __init__(self) -> None:
        self.withdrawn = False
        self.geometry_value = ""
        self.expose_count = 0

    def geometry(self, value: str) -> None:
        self.geometry_value = value

    def deiconify(self) -> None:
        self.withdrawn = False

    def withdraw(self) -> None:
        self.withdrawn = True

    def attributes(self, *_args) -> None:
        return None

    def after_idle(self, callback):
        callback()
        return "idle"

    def after(self, _delay: int, callback):
        callback()
        return "after"

    def event_generate(self, *_args, **_kwargs) -> None:
        self.expose_count += 1


class _Canvas:
    def find_all(self):
        return (1,)

    def type(self, _item):
        return "polygon"

    def itemcget(self, _item, _key):
        return "#0A1428"

    def bbox(self, _item):
        return (0, 0, 100, 100)

    def find_overlapping(self, *_args):
        return (1,)

    def winfo_rgb(self, value: str):
        value = value.lstrip("#")
        return tuple(int(value[index : index + 2], 16) * 257 for index in (0, 2, 4))


def _healthy_probe() -> dict[str, object]:
    return {
        "overlay_hwnd": 123,
        "valid": True,
        "ws_visible": True,
        "iconic": False,
        "cloaked": False,
        "dwm_status": "ok",
        "expected_client_rect": [10, 20, 110, 120],
        "actual_rect": [10, 20, 110, 120],
        "rect_matches": True,
        "style_checks": {
            "topmost": True,
            "layered": True,
            "transparent": True,
            "no_activate": True,
            "toolwindow": True,
        },
        "style_ok": True,
        "error": "",
    }


def _visibility() -> dict[str, object]:
    return {
        "target_rect": (10, 20, 110, 120),
        "pending_geometry": "100x100+10+20",
    }


class _DeferredRoot(_Root):
    def __init__(self):
        super().__init__()
        self.callbacks = []

    def after_idle(self, callback):
        self.callbacks.append(callback)

    def after(self, _delay, callback):
        self.callbacks.append(callback)


@pytest.mark.parametrize("phase", ["mapping", "composition", "retry"])
@pytest.mark.parametrize("change", ["draw", "geometry"])
def test_queued_presentation_callbacks_cannot_present_a_superseded_version(phase, change):
    from hextech.interfaces.overlay import host_presentation as presentation

    visibility = _visibility()
    root = _DeferredRoot()
    presentation.mark_canvas_drawn(visibility)
    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=_healthy_probe()) as probe,
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd") as show,
        patch.object(presentation, "_run_composition_probe", return_value={"state": "mismatched"}) as composition,
    ):
        presentation.ensure_overlay_presentation(root, _Canvas(), {}, visibility)
        if phase in {"composition", "retry"}:
            root.callbacks.pop(0)()
        if phase == "retry":
            root.callbacks.pop(0)()
        stale = root.callbacks.pop(0)
        if change == "draw":
            presentation.mark_canvas_drawn(visibility, event={"timing": {"event_written_at": 200}})
        else:
            presentation.invalidate_overlay_presentation(visibility, reason="geometry_changed")
            visibility["target_rect"] = (110, 20, 210, 120)
        before = (probe.call_count, show.call_count, composition.call_count)
        stale()
        assert (probe.call_count, show.call_count, composition.call_count) == before
        assert visibility["last_presented_at"] == 0
        assert not visibility["presentation"]["_scheduled"]


def test_latest_draw_can_compose_while_old_callback_is_still_queued():
    from hextech.interfaces.overlay import host_presentation as presentation

    root = _DeferredRoot()
    visibility = _visibility()
    presentation.mark_canvas_drawn(visibility, event={"timing": {"event_written_at": 100}})
    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=_healthy_probe()),
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd"),
        patch.object(presentation, "_run_composition_probe", return_value={"state": "matched"}),
    ):
        presentation.ensure_overlay_presentation(root, _Canvas(), {}, visibility)
        stale = root.callbacks.pop(0)
        presentation.mark_canvas_drawn(visibility, event={"timing": {"event_written_at": 200}})
        presentation.ensure_overlay_presentation(root, _Canvas(), {}, visibility)
        stale()
        assert visibility["presentation"]["_scheduled"]
        root.callbacks.pop(0)()
        root.callbacks.pop(0)()
    status = presentation.presentation_status(visibility)
    assert status["state"] == "composed"
    assert status["event_written_at"] == 200


def test_canvas_draw_does_not_claim_presented_until_pixels_match() -> None:
    from hextech.interfaces.overlay import host_presentation as presentation

    visibility = _visibility()
    changed: list[bool] = []
    visibility["presentation_state_changed"] = lambda: changed.append(True)
    presentation.mark_canvas_drawn(
        visibility,
        completed_at=100.0,
        ready_frame=True,
        event={
            "source": {
                "session_id": "session-1",
                "selection_epoch": 7,
                "selection_revision": 3,
            },
            "timing": {"event_written_at": 99.5},
        },
    )
    assert visibility["last_presented_at"] == 0.0

    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=_healthy_probe()) as probe,
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd"),
        patch.object(
            presentation,
            "_sample_screen_pixels",
            return_value=[(10, 20, 40)] * 5,
        ),
    ):
        presentation.ensure_overlay_presentation(_Root(), _Canvas(), {}, visibility)

    status = presentation.presentation_status(visibility)
    assert status["state"] == "composed"
    assert status["composition_probe"]["state"] == "matched"
    assert status["presented_at"] > status["draw_completed_at"]
    assert status["event_written_at"] == 99.5
    assert status["event_session_id"] == "session-1"
    assert status["event_selection_epoch"] == 7
    assert status["event_selection_revision"] == 3
    assert status["ready_frame"] is True
    assert visibility["last_ready_frame_at"] == status["presented_at"]
    assert changed
    assert probe.call_count >= 2


def test_ws_visible_does_not_pass_when_window_is_cloaked() -> None:
    from hextech.interfaces.overlay import host_presentation as presentation

    visibility = _visibility()
    presentation.mark_canvas_drawn(visibility)
    probe = _healthy_probe()
    probe["cloaked"] = True

    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=probe),
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd"),
    ):
        presentation.ensure_overlay_presentation(_Root(), _Canvas(), {}, visibility)

    status = presentation.presentation_status(visibility)
    assert status["ws_visible"] is True
    assert status["state"] == "failed"
    assert status["failure_reason"] == "window_cloaked"
    assert status["presented_at"] == 0.0


def test_composition_mismatch_retries_once_then_fails() -> None:
    from hextech.interfaces.overlay import host_presentation as presentation

    visibility = _visibility()
    root = _Root()
    presentation.mark_canvas_drawn(visibility)

    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=_healthy_probe()),
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd") as show,
        patch.object(presentation, "_sample_screen_pixels", return_value=[(255, 255, 255)] * 5),
    ):
        presentation.ensure_overlay_presentation(root, _Canvas(), {}, visibility)

    status = presentation.presentation_status(visibility)
    assert status["state"] == "failed"
    assert status["failure_reason"] == "composition_not_observed"
    assert status["composition_probe"]["state"] == "mismatched"
    assert root.expose_count == 1
    assert show.call_count == 2


def test_probe_unavailable_stays_mapped_without_fabricating_presented_at() -> None:
    from hextech.interfaces.overlay import host_presentation as presentation

    visibility = _visibility()
    presentation.mark_canvas_drawn(visibility)
    canvas = _Canvas()
    canvas.find_all = lambda: ()

    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=_healthy_probe()),
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd"),
    ):
        presentation.ensure_overlay_presentation(_Root(), canvas, {}, visibility)

    status = presentation.presentation_status(visibility)
    assert status["state"] == "mapped"
    assert status["composition_probe"]["state"] == "unavailable"
    assert status["presented_at"] == 0.0


def test_composed_window_is_reprobed_even_when_request_cache_is_true() -> None:
    from hextech.interfaces.overlay import host_presentation as presentation

    visibility = _visibility()
    visibility["window_visible"] = True
    presentation.mark_canvas_drawn(visibility)
    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=_healthy_probe()) as probe,
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd"),
        patch.object(presentation, "_sample_screen_pixels", return_value=[(10, 20, 40)] * 5),
    ):
        presentation.ensure_overlay_presentation(_Root(), _Canvas(), {}, visibility)
        first_count = probe.call_count
        presentation.ensure_overlay_presentation(_Root(), _Canvas(), {}, visibility)

    assert probe.call_count == first_count + 1


def test_apply_overlay_rect_can_explicitly_show_window(monkeypatch) -> None:
    from hextech.interfaces.overlay import host_platform as platform
    from hextech.interfaces.overlay.host_common import SWP_NOACTIVATE, SWP_SHOWWINDOW

    calls: list[tuple[object, ...]] = []
    user32 = SimpleNamespace(SetWindowPos=lambda *args: calls.append(args) or 1)
    monkeypatch.setattr(platform.ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setattr(platform, "_root_hwnd", lambda _root: 321)

    assert platform._apply_overlay_rect(object(), (1, 2, 101, 202), show=True)
    assert calls[0][-1] == SWP_NOACTIVATE | SWP_SHOWWINDOW


def test_capture_exclusion_is_set_and_read_back_before_mapping(monkeypatch) -> None:
    from hextech.interfaces.overlay import host_platform as platform

    class User32:
        affinity = 0

        def SetWindowDisplayAffinity(self, hwnd: int, affinity: int) -> int:
            assert hwnd == 321
            self.affinity = affinity
            return 1

        def GetWindowDisplayAffinity(self, hwnd: int, pointer) -> int:
            assert hwnd == 321
            pointer._obj.value = self.affinity
            return 1

    user32 = User32()
    monkeypatch.setattr(platform, "_root_hwnd", lambda _root: 321)

    result = platform._ensure_overlay_capture_exclusion(object(), user32=user32)

    assert result["status"] == "applied"
    assert result["set_ok"] is True
    assert result["query_ok"] is True
    assert result["applied_affinity"] == platform.WDA_EXCLUDEFROMCAPTURE


def test_capture_exclusion_readback_composes_without_ambiguous_desktop_dc() -> None:
    from hextech.interfaces.overlay import host_presentation as presentation

    visibility = _visibility()
    presentation.mark_canvas_drawn(visibility, ready_frame=True)
    probe = _healthy_probe()
    probe["capture_exclusion"] = {
        "status": "applied",
        "requested_affinity": 0x11,
        "applied_affinity": 0x11,
        "query_ok": True,
    }
    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=probe),
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd"),
        patch.object(presentation, "_sample_screen_pixels") as desktop_dc,
    ):
        presentation.ensure_overlay_presentation(_Root(), _Canvas(), {}, visibility)

    status = presentation.presentation_status(visibility)
    assert status["state"] == "composed"
    assert status["host_surface_probe"]["state"] == "matched"
    assert status["composition_probe"]["state"] == "excluded"
    assert status["composition_probe"]["reason"] == "display_affinity_readback_applied"
    assert desktop_dc.call_count == 0


def test_packaged_capture_probe_distinguishes_excluded_and_leaked_marker() -> None:
    from PIL import Image

    from hextech.interfaces.overlay import host_presentation_smoke as smoke

    class Backend:
        def __init__(self, image: Image.Image) -> None:
            self.image = image
            self.closed = False

        def capture_rgb(self, rect):
            assert rect == (104, 104, 141, 141)
            return self.image

        def close(self) -> None:
            self.closed = True

    excluded_backend = Backend(Image.new("RGB", (37, 37), smoke.SMOKE_BACKGROUND))
    leaked_backend = Backend(Image.new("RGB", (37, 37), smoke.SMOKE_CAPTURE_MARKER))
    with patch.object(smoke, "MssCaptureBackend", return_value=excluded_backend):
        excluded = smoke._capture_exclusion_probe((80, 80, 1040, 680))
    with patch.object(smoke, "MssCaptureBackend", return_value=leaked_backend):
        leaked = smoke._capture_exclusion_probe((80, 80, 1040, 680))

    assert excluded["state"] == "excluded"
    assert excluded["matched_count"] == 0
    assert excluded["probe_contract"] == "mss_capture_exclusion_v1"
    assert leaked["state"] == "leaked"
    assert leaked["matched_count"] == 3
    assert excluded_backend.closed and leaked_backend.closed
    for invalid_color in ("black", "white"):
        invalid = Backend(Image.new("RGB", (37, 37), invalid_color))
        with patch.object(smoke, "MssCaptureBackend", return_value=invalid):
            result = smoke._capture_exclusion_probe((80, 80, 1040, 680))
        assert result["state"] == "unavailable"
        assert result["reason"] == "capture_background_mismatch"


def test_unconfirmed_capture_exclusion_never_maps_window() -> None:
    from hextech.interfaces.overlay import host_presentation as presentation

    visibility = _visibility()
    root = _Root()
    presentation.mark_canvas_drawn(visibility)
    probe = _healthy_probe()
    probe["capture_exclusion"] = {"status": "failed", "reason": "display_affinity_readback_mismatch"}
    with (
        patch.object(presentation, "_probe_overlay_hwnd", return_value=probe),
        patch.object(presentation, "_ensure_overlay_window_styles"),
        patch.object(presentation, "_show_overlay_hwnd") as show,
    ):
        presentation.ensure_overlay_presentation(root, _Canvas(), {}, visibility)

    status = presentation.presentation_status(visibility)
    assert root.withdrawn is True
    assert show.call_count == 0
    assert status["state"] == "failed"
    assert status["failure_reason"] == "capture_exclusion_unavailable"
