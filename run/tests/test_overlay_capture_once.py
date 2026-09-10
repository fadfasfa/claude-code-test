from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import time
from pathlib import Path

from PIL import Image
import pytest

from hextech.infrastructure.vision import capture_once as capture
from tooling.diagnostics import overlay_capture_once as cli


def snapshot():
    return {
        "hwnd": 42, "foreground_hwnd": 42, "client_rect": [-1920, 30, -1280, 390],
        "process_id": 7, "process_started_at": 50.0, "game_instance_id": "game-7",
        "identity_quality": "process", "process_name": "League of Legends.exe", "dpi": 144,
        "display": {"status": "available", "monitor_device": "fixture-monitor", "dpi_scale": 1.5},
        "window_mode": {"status": "supported", "mode": "borderless"}, "observed_at": 100.0,
    }


def frame():
    result = Image.new("RGB", (640, 360), "#102030")
    result.putpixel((0, 0), (200, 200, 200))
    return result


def collect(**kwargs):
    arguments = dict(enabled=True, deadline=20.0, clock=lambda: 10.0, wall_clock=lambda: 100.0,
                     probe=snapshot, capture=lambda _: frame(), build_reader=lambda: {"build_id": "fixture"})
    arguments.update(kwargs)
    return capture.collect_once(**arguments)


def test_default_cli_and_collector_do_not_probe_capture_or_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "RUN_DIR", tmp_path)
    monkeypatch.setattr(cli, "run_bounded", lambda *_: pytest.fail("must not spawn"))
    assert cli.main([]) == 2
    assert "explicit_capture_required" in capsys.readouterr().out
    with pytest.raises(capture.CaptureRejected, match="explicit_capture_required"):
        collect(enabled=False, probe=lambda: pytest.fail("must not probe"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("foreground_hwnd", 9, "game_not_foreground"),
    ("identity_quality", "window_fallback", "identity_unverified"),
    ("process_name", "Other.exe", "identity_unverified"),
    ("game_instance_id", "", "identity_unverified"),
    ("process_started_at", float("nan"), "identity_unverified"),
    ("process_started_at", 101.0, "identity_unverified"),
    ("observed_at", 99.0, "observation_stale"),
    ("observed_at", 101.0, "observation_stale"),
    ("window_mode", {"status": "unsupported", "mode": "fullscreen"}, "mode_unsupported"),
    ("window_mode", {"status": "unknown", "mode": "unknown"}, "mode_unsupported"),
    ("dpi", 0, "dpi_unavailable"),
    ("client_rect", [0, 0, 0, 360], "size_budget"),
    ("client_rect", [0, 0, 10000, 10000], "size_budget"),
])
def test_rejected_before_capture(field, value, reason):
    before = snapshot()
    before[field] = value
    with pytest.raises(capture.CaptureRejected, match=reason):
        collect(probe=lambda: before, capture=lambda _: pytest.fail("must not capture"))


@pytest.mark.parametrize(("field", "value"), [
    ("hwnd", 43), ("process_id", 8), ("process_started_at", 51.0), ("game_instance_id", "new-game"),
    ("client_rect", [-1800, 30, -1160, 390]), ("dpi", 192),
    ("display", {"status": "available", "monitor_device": "other-monitor"}),
    ("foreground_hwnd", 1), ("window_mode", {"status": "supported", "mode": "windowed"}),
])
def test_after_capture_changes_discard_pixels(field, value):
    after = snapshot()
    after[field] = value
    states = iter((snapshot(), after))
    calls = []
    with pytest.raises(capture.CaptureRejected):
        collect(probe=lambda: next(states), capture=lambda s: calls.append(s) or frame())
    assert len(calls) == 1


def test_candidate_or_absent_is_saved_once_without_confirmation(tmp_path):
    calls = []
    png, metadata = collect(capture=lambda s: calls.append(s) or frame())
    assert len(calls) == 1
    assert metadata["selection_confirmed"] is False
    assert metadata["scene_observation"]["present"] is False
    assert metadata["requires_manual_truth"] is True
    assert metadata["automatic_exemplar_eligible"] is False
    assert metadata["before"]["client_rect"][0] == -1920
    assert metadata["before"]["dpi"] == 144
    assert metadata["before"] == metadata["after"]
    assert metadata["captured_at"] == 100.0
    assert metadata["build"]["build_id"] == "fixture"
    assert metadata["image"]["sha256"] == hashlib.sha256(png).hexdigest()
    with Image.open(io.BytesIO(png)) as saved:
        assert saved.size == (640, 360)
    destination = cli.new_output_directory(tmp_path)
    cli.write_evidence(destination, png, metadata, time.monotonic() + 10)
    assert json.loads((destination / "capture.json").read_text("utf-8")) == json.loads(json.dumps(metadata))
    assert (destination / "client.png").read_bytes() == png


def test_candidate_stage_does_not_require_tracker_confirmation(monkeypatch):
    observation = capture.detect_selection_scene(frame(), layout_id="fixture")
    candidate = replace(observation, present=True, scene_state="candidate")
    monkeypatch.setattr(capture, "detect_selection_scene", lambda *_args, **_kwargs: candidate)
    png, metadata = collect()
    assert png
    assert metadata["scene_observation"]["scene_state"] == "candidate"
    assert metadata["selection_confirmed"] is False


@pytest.mark.parametrize("image", [Image.new("RGB", (640, 360)), Image.new("RGB", (10, 10))])
def test_no_retry_on_black_frame_or_size_mismatch(image):
    calls = []
    with pytest.raises(capture.CaptureRejected, match="capture_flat_or_black|capture_size_mismatch"):
        collect(capture=lambda _: calls.append(1) or image.copy())
    assert calls == [1]


def test_budget_before_and_after_capture_and_byte_limit(monkeypatch):
    with pytest.raises(capture.CaptureRejected, match="budget_exceeded"):
        collect(deadline=10, probe=lambda: pytest.fail("no probe after deadline"))
    ticks = iter((10, 10, 21))
    with pytest.raises(capture.CaptureRejected, match="budget_exceeded"):
        collect(clock=lambda: next(ticks))
    monkeypatch.setattr(capture, "MAX_PNG_BYTES", 1)
    with pytest.raises(capture.CaptureRejected, match="byte_budget"):
        collect()


def test_isolated_outputs_never_overwrite_old_evidence(tmp_path):
    first = cli.new_output_directory(tmp_path)
    png, metadata = collect()
    cli.write_evidence(first, png, metadata, time.monotonic() + 10)
    second = cli.new_output_directory(tmp_path)
    assert first != second
    previous = {p.name: p.read_bytes() for p in first.iterdir()}
    with pytest.raises(FileExistsError):
        cli.write_evidence(first, b"changed", {}, time.monotonic() + 10)
    assert previous == {p.name: p.read_bytes() for p in first.iterdir()}
    assert not (tmp_path / "var").exists()


def test_reparse_output_root_rejected(tmp_path, monkeypatch):
    original = Path.lstat

    def redirected(path):
        result = original(path)
        if path.name == ".artifacts":
            from types import SimpleNamespace
            return SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(Path, "lstat", redirected)
    with pytest.raises(ValueError, match="reparse"):
        cli.new_output_directory(tmp_path)


@pytest.mark.parametrize("arguments", [
    ["--delay-seconds", "nan"], ["--budget-seconds", "inf"], ["--budget-seconds", "11"],
    ["--delay-seconds", "-1"], ["--output", "outside"],
])
def test_invalid_cli_never_starts_capture(arguments, monkeypatch):
    monkeypatch.setattr(cli, "run_bounded", lambda *_: pytest.fail("must not spawn"))
    with pytest.raises(SystemExit):
        cli.main(["--capture", *arguments])


def test_worker_source_drift_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.window, "configure_process_dpi_awareness", lambda: "per_monitor_v2")
    collected = collect()
    monkeypatch.setattr(capture, "collect_once", lambda **_: collected)
    sources = iter(({"a": "before"}, {"a": "after"}))
    monkeypatch.setattr(cli, "source_identity", lambda: next(sources))
    messages = []
    from types import SimpleNamespace
    cli._worker(str(tmp_path), time.monotonic() + 10,
                SimpleNamespace(send=messages.append, close=lambda: None))
    assert messages[0]["status"] == "rejected"
    assert messages[0]["reason"] == "capture_source_changed"
    assert list(tmp_path.iterdir()) == []


def test_foreground_non_game_rejected_without_reading_geometry_or_pixels(monkeypatch):
    monkeypatch.setattr(capture.window, "foreground_root_hwnd", lambda: 7)
    monkeypatch.setattr(capture.window, "_window_process_name", lambda _: "other.exe")
    monkeypatch.setattr(capture.window, "game_window_identity", lambda _: pytest.fail("not game"))
    with pytest.raises(capture.CaptureRejected, match="verified_foreground_game_missing"):
        capture.probe_foreground_game()


def test_worker_success_binds_preserved_layout_and_source_hashes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(capture.window, "configure_process_dpi_awareness", lambda: "per_monitor_v2")
    collected = collect()
    monkeypatch.setattr(capture, "collect_once", lambda **_: collected)
    monkeypatch.setattr(cli, "source_identity", lambda: {"fixture.py": "sha256"})
    messages = []
    cli._worker(str(tmp_path), time.monotonic() + 10,
                SimpleNamespace(send=messages.append, close=lambda: None))
    assert messages[0]["status"] == "captured"
    metadata = json.loads((tmp_path / "capture.json").read_text("utf-8"))
    assert metadata["layout"]["anchor_qualification"] == "pending_real_device"
    assert metadata["collector_source_sha256"] == {"fixture.py": "sha256"}
    assert metadata["runtime_build_verified"] is False


@pytest.mark.parametrize("native_result,changed", [(True, False), (False, False), (True, True)])
def test_native_capture_is_client_only_once_without_screen_fallback(monkeypatch, native_result, changed):
    import sys
    from types import SimpleNamespace
    calls = []

    class DC:
        def CreateCompatibleDC(self):
            return self

        def SelectObject(self, obj):
            calls.append("select")
            return 1

        def PatBlt(self, origin, size, flag):
            assert origin == (0, 0) and size == (640, 360) and flag == 0x42

        def GetSafeHdc(self):
            return 33

        def DeleteDC(self):
            calls.append("delete_dc")

    class Bitmap:
        def CreateCompatibleBitmap(self, source, width, height):
            assert (width, height) == (640, 360)

        def GetBitmapBits(self, _):
            return bytes(640 * 360 * 4)

        def GetHandle(self):
            return 44

    class PrintWindow:
        def __call__(self, hwnd, hdc, flags):
            calls.append("capture")
            assert (hwnd, hdc, flags) == (42, 33, 3)
            return native_result

    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetDC=lambda hwnd: calls.append(("get_dc", hwnd)) or 11,
        ReleaseDC=lambda hwnd, dc: calls.append(("release", hwnd, dc)),
        DeleteObject=lambda _: calls.append("delete_bitmap"),
    ))
    monkeypatch.setitem(sys.modules, "win32ui", SimpleNamespace(
        CreateDCFromHandle=lambda _: DC(), CreateBitmap=Bitmap,
    ))
    monkeypatch.setattr(capture.ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(PrintWindow=PrintWindow())))
    fresh = snapshot()
    fresh["observed_at"] = time.time()
    if changed:
        fresh["dpi"] = 192
    monkeypatch.setattr(capture, "probe_foreground_game", lambda: fresh)
    if not native_result or changed:
        with pytest.raises(capture.CaptureRejected):
            capture.capture_client(snapshot())
    else:
        with capture.capture_client(snapshot()) as result:
            assert result.size == (640, 360)
    assert calls.count("capture") == (0 if changed else 1)
    assert ("get_dc", 42) in calls  # never GetDC(0)
    assert calls.count("delete_dc") == 2
    assert calls[-1] == ("release", 42, 11)


def _blocked_worker(*_):
    time.sleep(30)


def test_actual_spawn_hard_timeout_stops_only_diagnostic_worker(tmp_path):
    # 独立临时输出 + 无窗口无截图子进程，验证原生调用卡住时的终止边界。
    import multiprocessing
    actual = multiprocessing.get_context("spawn")
    processes = []

    class Context:
        Pipe = staticmethod(actual.Pipe)

        @staticmethod
        def Process(**kwargs):
            kwargs["target"] = _blocked_worker
            process = actual.Process(**kwargs)
            processes.append(process)
            return process

    started = time.monotonic()
    result = cli.run_bounded(tmp_path, 0.2, context=Context())
    assert result["reason"] == "capture_budget_exceeded"
    assert result["worker_stopped"] is True
    assert not processes[0].is_alive()
    assert time.monotonic() - started < 4
    assert list(tmp_path.iterdir()) == []
