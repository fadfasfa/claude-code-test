"""验证冻结 GUI 无 stdout 时的原子子进程握手。"""

from __future__ import annotations

import json
import sys

from hextech.modules.session.process_bootstrap import publish_process_bootstrap


def test_publish_process_bootstrap_without_stdout(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bootstrap.json"
    monkeypatch.setenv("HEXTECH_PROCESS_BOOTSTRAP_FILE", str(target))
    monkeypatch.setenv("HEXTECH_PROCESS_BOOTSTRAP_TOKEN", "test-token")
    monkeypatch.setattr(sys, "stdout", None)

    published = publish_process_bootstrap({"pid": 42, "port": 52001, "session_nonce": "nonce"})

    assert published["token"] == "test-token"
    assert json.loads(target.read_text(encoding="utf-8")) == published


def test_overlay_self_check_publishes_file_result_without_stdout(tmp_path, monkeypatch) -> None:
    from hextech.interfaces.overlay import host_runner

    target = tmp_path / "overlay-self-check.json"
    monkeypatch.setenv("HEXTECH_PROCESS_BOOTSTRAP_FILE", str(target))
    monkeypatch.setenv("HEXTECH_PROCESS_BOOTSTRAP_TOKEN", "overlay-token")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(host_runner, "run_self_check", lambda: {"ok": True})

    assert host_runner.main(["--self-check"]) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True, "token": "overlay-token"}
