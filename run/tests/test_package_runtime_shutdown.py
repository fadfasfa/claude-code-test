from types import SimpleNamespace

import pytest

from tooling.build import runtime_shutdown as shutdown


def test_allowed_paths_are_exact_and_scoped(tmp_path):
    install = tmp_path / "HextechCompanion"
    artifacts = tmp_path / "artifacts"
    assert shutdown.allowed_executable(install / shutdown.APP_NAME, install, artifacts)
    assert shutdown.allowed_executable(artifacts / "staging" / "HextechCompanion-20260914-rf4" / shutdown.APP_NAME, install, artifacts)
    assert not shutdown.allowed_executable(tmp_path / "unknown" / shutdown.APP_NAME, install, artifacts)
    assert not shutdown.allowed_executable(install / "League of Legends.exe", install, artifacts)


def test_unknown_identity_blocks_before_any_termination(monkeypatch, tmp_path):
    terminated = []
    def process(pid, path):
        return SimpleNamespace(pid=pid, info={"name": shutdown.APP_NAME},
                               exe=lambda: str(path), create_time=lambda: 1,
                               terminate=lambda: terminated.append(pid))
    processes = [process(1, tmp_path / "HextechCompanion" / shutdown.APP_NAME),
                 process(2, tmp_path / "unknown" / shutdown.APP_NAME)]
    monkeypatch.setattr(shutdown.psutil, "process_iter", lambda _: processes)
    with pytest.raises(RuntimeError, match="允许范围"):
        shutdown.shutdown_for_package(tmp_path / "HextechCompanion", tmp_path / "artifacts")
    assert terminated == []


def test_closes_verified_hextech_but_leaves_client(monkeypatch, tmp_path):
    terminated = []
    app = SimpleNamespace(pid=1, info={"name": shutdown.APP_NAME},
                          exe=lambda: str(tmp_path / "HextechCompanion" / shutdown.APP_NAME),
                          create_time=lambda: 1, terminate=lambda: terminated.append(1))
    client = SimpleNamespace(pid=2, info={"name": "LeagueClient.exe"})
    scans = iter([[app, client], [client]])
    monkeypatch.setattr(shutdown.psutil, "process_iter", lambda _: next(scans))
    monkeypatch.setattr(shutdown.psutil, "wait_procs", lambda processes, timeout: (processes, []))
    assert shutdown.shutdown_for_package(tmp_path / "HextechCompanion", tmp_path / "artifacts") == (1,)
    assert terminated == [1]


def test_live_game_blocks_shutdown(monkeypatch, tmp_path):
    monkeypatch.setattr(shutdown.psutil, "process_iter", lambda _: [SimpleNamespace(info={"name": "League of Legends.exe"})])
    with pytest.raises(RuntimeError, match="real_game_active"):
        shutdown.shutdown_for_package(tmp_path / "HextechCompanion", tmp_path / "artifacts")
