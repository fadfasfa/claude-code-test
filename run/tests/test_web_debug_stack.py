"""Web 调试入口的默认进程边界与回收顺序。"""

from __future__ import annotations

from types import SimpleNamespace

from tooling.dev import web_stack


class _Process:
    returncode = 0

    def __init__(self) -> None:
        self.running = True
        self.stopped = False

    def poll(self):
        return None if self.running else self.returncode

    def terminate(self) -> None:
        self.running = False
        self.stopped = True

    def kill(self) -> None:
        self.running = False
        self.stopped = True

    def wait(self, timeout=None):
        del timeout
        self.running = False
        return self.returncode


def test_probe_starts_data_and_web_without_overlay_then_cleans_up(tmp_path, monkeypatch) -> None:
    port_file = tmp_path / "web_server_port.txt"
    port_file.write_text("8123", encoding="utf-8")
    monkeypatch.setattr(web_stack, "build_runtime_state_path", lambda _name: str(port_file))
    process = _Process()
    stopped: list[object] = []
    data_handle = SimpleNamespace(
        get_status=lambda: {"state": "ready", "generation_id": "g1"},
    )

    result = web_stack.run_web_debug_stack(
        probe_only=True,
        start_data_service=lambda **_kwargs: data_handle,
        stop_data_service=stopped.append,
        start_web=lambda *_args, **_kwargs: process,
        overlay_factory=lambda: (_ for _ in ()).throw(AssertionError("默认不得启动 Overlay")),
        probe_web=lambda url: 200 if url == "http://127.0.0.1:8123" else 500,
        reuse_existing_web=False,
    )

    assert result == 0
    assert process.stopped is True
    assert stopped == [data_handle]


def test_with_overlay_is_explicit_and_stopped(tmp_path, monkeypatch) -> None:
    port_file = tmp_path / "web_server_port.txt"
    port_file.write_text("8124", encoding="utf-8")
    monkeypatch.setattr(web_stack, "build_runtime_state_path", lambda _name: str(port_file))
    process = _Process()
    calls: list[str] = []
    overlay = SimpleNamespace(
        start=lambda: calls.append("overlay_start"),
        stop=lambda: calls.append("overlay_stop"),
    )

    web_stack.run_web_debug_stack(
        probe_only=True,
        with_overlay=True,
        start_data_service=lambda **_kwargs: SimpleNamespace(get_status=lambda: {"generation_id": "g2"}),
        stop_data_service=lambda _handle: calls.append("data_stop"),
        start_web=lambda *_args, **_kwargs: process,
        overlay_factory=lambda: overlay,
        probe_web=lambda _url: 200,
        reuse_existing_web=False,
    )

    assert calls == ["overlay_start", "overlay_stop", "data_stop"]


def test_existing_data_service_and_web_are_reused_without_being_stopped(tmp_path, monkeypatch) -> None:
    port_file = tmp_path / "web_server_port.txt"
    port_file.write_text("8125", encoding="utf-8")
    monkeypatch.setattr(web_stack, "build_runtime_state_path", lambda _name: str(port_file))
    calls: list[str] = []

    result = web_stack.run_web_debug_stack(
        probe_only=True,
        start_data_service=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("code=3")),
        stop_data_service=lambda _handle: calls.append("data_stop"),
        start_web=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不得重复启动 Web")),
        probe_web=lambda _url: 200,
        snapshot_status=lambda: {"state": "ready", "generation_id": "g-existing"},
    )

    assert result == 0
    assert calls == []
