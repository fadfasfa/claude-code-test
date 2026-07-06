"""测试 运行态日志。

调用方: pytest; 关键依赖: hextech.support.log_utils。
"""
from __future__ import annotations

import json
import logging
import importlib
from pathlib import Path


def _reset_hextech_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_hextech_runtime_logging", False):
            root.removeHandler(handler)
            handler.close()
    if hasattr(root, "_hextech_runtime_logging_profile"):
        delattr(root, "_hextech_runtime_logging_profile")


def test_dev_profile_installs_full_summary_error_logs(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.install_runtime_logging(profile="dev")
    paths = log_utils.get_runtime_log_paths()
    logging.getLogger("hextech.test").info(
        "诊断事件 token=secret-token",
        extra={"component": "unit", "event": "logging.dev", "correlation_id": "corr-1"},
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert paths["summary"].is_file()
    assert paths["error"].is_file()
    assert paths["full"].is_file()

    payload = json.loads(paths["full"].read_text(encoding="utf-8").splitlines()[-1])
    assert payload["component"] == "unit"
    assert payload["event"] == "logging.dev"
    assert payload["correlation_id"] == "corr-1"
    assert payload["profile"] == "dev"
    assert payload["message"] == "诊断事件 token=<redacted>"
    assert "run_id" in payload
    assert "pid" in payload

    full_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_hextech_handler_name", "") == "dev_full_jsonl"
    ]
    assert len(full_handlers) == 1
    assert getattr(full_handlers[0], "maxBytes", 0) == 10 * 1024 * 1024
    assert getattr(full_handlers[0], "backupCount", 0) == 5

    _reset_hextech_logging()


def test_packaged_profile_skips_full_debug_jsonl(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.install_runtime_logging(profile="packaged")
    paths = log_utils.get_runtime_log_paths()
    logging.getLogger("hextech.test").warning("packaged warning")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert paths["summary"].is_file()
    assert paths["error"].is_file()
    assert not paths["full"].exists()
    assert not any(
        getattr(handler, "_hextech_handler_name", "") == "dev_full_jsonl"
        for handler in logging.getLogger().handlers
    )

    _reset_hextech_logging()


def test_runtime_logging_install_is_idempotent(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.install_runtime_logging(profile="dev")
    log_utils.install_runtime_logging(profile="dev")
    logging.getLogger("hextech.test").info("one event", extra={"component": "unit", "event": "once"})
    for handler in logging.getLogger().handlers:
        handler.flush()

    paths = log_utils.get_runtime_log_paths()
    lines = [line for line in paths["full"].read_text(encoding="utf-8").splitlines() if "one event" in line]
    assert len(lines) == 1

    _reset_hextech_logging()


def test_runtime_logging_replaces_legacy_summary_error_handlers(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)
    paths = log_utils.get_runtime_log_paths()
    root = logging.getLogger()
    legacy_summary = logging.FileHandler(paths["summary"], encoding="utf-8")
    legacy_error = logging.FileHandler(paths["error"], encoding="utf-8")
    root.addHandler(legacy_summary)
    root.addHandler(legacy_error)

    log_utils.install_runtime_logging(profile="dev")

    assert legacy_summary not in root.handlers
    assert legacy_error not in root.handlers
    assert any(getattr(handler, "_hextech_handler_name", "") == "runtime_summary" for handler in root.handlers)
    assert any(getattr(handler, "_hextech_handler_name", "") == "runtime_error" for handler in root.handlers)

    _reset_hextech_logging()


def test_runtime_logging_installs_warning_stream_handler(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.install_runtime_logging(profile="dev")

    stream_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_hextech_handler_name", "") == "runtime_stream"
    ]
    assert len(stream_handlers) == 1
    assert stream_handlers[0].level == logging.WARNING

    _reset_hextech_logging()


def test_runtime_rotating_handler_keeps_logging_when_rollover_is_locked(tmp_path):
    from hextech.support import log_utils

    path = tmp_path / "busy.log"
    path.write_text("existing\n", encoding="utf-8")
    handler = log_utils.RuntimeRotatingFileHandler(path, maxBytes=1, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))

    def fail_rollover():
        raise PermissionError("locked")

    handler.doRollover = fail_rollover
    record = logging.makeLogRecord({"name": "hextech.test", "levelno": logging.INFO, "levelname": "INFO", "msg": "new line"})

    handler.emit(record)
    handler.close()

    assert "new line" in path.read_text(encoding="utf-8")


def test_legacy_summary_logging_does_not_downgrade_runtime_handlers(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.install_runtime_logging(profile="dev")
    before = {
        getattr(handler, "_hextech_handler_name", ""): (handler.level, handler.formatter.__class__.__name__)
        for handler in logging.getLogger().handlers
        if getattr(handler, "_hextech_runtime_logging", False)
    }

    log_utils.install_summary_logging(level=logging.INFO, fmt="%(levelname)s:%(message)s")

    after = {
        getattr(handler, "_hextech_handler_name", ""): (handler.level, handler.formatter.__class__.__name__)
        for handler in logging.getLogger().handlers
        if getattr(handler, "_hextech_runtime_logging", False)
    }
    assert after == before
    assert after["runtime_summary"][0] == logging.INFO
    assert after["runtime_error"][0] == logging.WARNING
    assert after["dev_full_jsonl"][0] == logging.DEBUG

    _reset_hextech_logging()


def test_runtime_logging_recovers_after_legacy_forced_handlers(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.install_runtime_logging(profile="dev")
    legacy_handler = logging.FileHandler(tmp_path / "legacy.log", encoding="utf-8")

    try:
        log_utils.install_summary_logging(handlers=[legacy_handler])
        assert not any(getattr(handler, "_hextech_runtime_logging", False) for handler in logging.getLogger().handlers)
        assert not hasattr(logging.getLogger(), "_hextech_runtime_logging_profile")

        log_utils.install_runtime_logging(profile="dev")

        names = {
            getattr(handler, "_hextech_handler_name", "")
            for handler in logging.getLogger().handlers
            if getattr(handler, "_hextech_runtime_logging", False)
        }
        assert {"runtime_summary", "runtime_error", "runtime_stream", "dev_full_jsonl"}.issubset(names)
    finally:
        if legacy_handler in logging.getLogger().handlers:
            logging.getLogger().removeHandler(legacy_handler)
        legacy_handler.close()
        _reset_hextech_logging()


def test_importing_synergy_scraper_keeps_runtime_handler_levels(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.install_runtime_logging(profile="dev")
    import hextech.scraping.synergy.scraper as scraper

    importlib.reload(scraper)

    levels = {
        getattr(handler, "_hextech_handler_name", ""): handler.level
        for handler in logging.getLogger().handlers
        if getattr(handler, "_hextech_runtime_logging", False)
    }
    assert levels["runtime_summary"] == logging.INFO
    assert levels["runtime_error"] == logging.WARNING
    assert levels["dev_full_jsonl"] == logging.DEBUG
    assert levels["runtime_stream"] == logging.WARNING

    _reset_hextech_logging()


def test_runtime_logging_redacts_exception_traceback(tmp_path, monkeypatch):
    from hextech.support import log_utils

    _reset_hextech_logging()
    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.install_runtime_logging(profile="dev")
    logger = logging.getLogger("hextech.test")
    try:
        raise RuntimeError("cookie=secret-cookie")
    except RuntimeError:
        logger.exception("failed token=secret-token")
    for handler in logging.getLogger().handlers:
        handler.flush()

    error_text = log_utils.get_runtime_log_paths()["error"].read_text(encoding="utf-8")
    assert "secret-token" not in error_text
    assert "secret-cookie" not in error_text
    assert "token=<redacted>" in error_text
    assert "cookie=<redacted>" in error_text

    _reset_hextech_logging()


def test_write_structured_event_redacts_and_is_parseable(tmp_path, monkeypatch):
    from hextech.support import log_utils

    monkeypatch.setattr(log_utils, "_runtime_root_dir", lambda: tmp_path)

    log_utils.write_structured_event(
        "unit",
        "event.started",
        level="WARNING",
        token="secret",
        url="https://example.test/path?token=abc",
    )

    event_path = tmp_path / "state" / "runtime_events.v1.jsonl"
    payload = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["component"] == "unit"
    assert payload["event"] == "event.started"
    assert payload["level"] == "WARNING"
    assert payload["token"] == "<redacted>"
    assert payload["url"] == "https://example.test/path?<redacted>"


def test_write_structured_event_target_path_avoids_default_runtime_paths(tmp_path, monkeypatch):
    from hextech.support import log_utils

    target = tmp_path / "custom" / "events.jsonl"
    monkeypatch.setattr(
        log_utils,
        "get_runtime_log_paths",
        lambda: (_ for _ in ()).throw(AssertionError("default paths should not be resolved")),
    )

    log_utils.write_structured_event("unit", "custom.path", target_path=target)

    payload = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert payload["component"] == "unit"
    assert payload["event"] == "custom.path"


def test_entrypoints_install_runtime_logging():
    run_dir = Path(__file__).resolve().parents[1]
    web_server_source = (run_dir / "web_server.py").read_text(encoding="utf-8")
    assert "install_runtime_logging()" in (run_dir / "hextech_ui.py").read_text(encoding="utf-8")
    assert "install_runtime_logging()\n    run_web_server()" in web_server_source
    assert "\ninstall_runtime_logging()\n\nfrom hextech.display.web.app" not in web_server_source
    assert "install_runtime_logging()" in (
        run_dir / "hextech" / "scraping" / "version_sync.py"
    ).read_text(encoding="utf-8")
    assert "write_structured_event(" in (run_dir / "hextech" / "core" / "refresh.py").read_text(encoding="utf-8")
