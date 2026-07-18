"""Mayhem 刷新健康与 last-good 保留测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import json


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def test_recent_success_is_not_due(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    monkeypatch.setattr(service, "load_mayhem_refresh_status", lambda: {"last_success_at": _iso(1000)})
    assert service.mayhem_refresh_due(now=1100, stale_after_seconds=200) is False
    assert service.mayhem_refresh_due(now=1201, stale_after_seconds=200) is True


def test_failure_retry_uses_stable_jitter(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    status = {"last_attempt_at": _iso(1000), "last_result": "failed", "reason": "network_error"}
    monkeypatch.setattr(service, "load_mayhem_refresh_status", lambda: status)
    jitter = service._stable_failure_retry_jitter_seconds(status, 300)
    assert service.mayhem_refresh_due(
        now=1000 + 1800 + jitter - 1,
        failure_retry_seconds=1800,
        failure_retry_jitter_seconds=300,
    ) is False
    assert service.mayhem_refresh_due(
        now=1000 + 1800 + jitter,
        failure_retry_seconds=1800,
        failure_retry_jitter_seconds=300,
    ) is True


def test_skipped_refresh_preserves_last_success(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    previous = {"last_success_at": _iso(1000), "last_result": "success"}
    written: dict = {}
    monkeypatch.setattr(service, "load_mayhem_refresh_status", lambda: previous)
    monkeypatch.setattr(service, "get_mayhem_refresh_status_path", lambda: "unused.json")
    monkeypatch.setattr(service, "atomic_write_json", lambda _path, payload, **_kwargs: written.update(payload))
    payload = service.write_mayhem_refresh_status(result="skipped", reason="not_stale", now=1100)
    assert payload["last_success_at"] == previous["last_success_at"]
    assert written["last_result"] == "skipped"


def test_empty_online_result_does_not_publish(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    monkeypatch.setattr(service, "write_mayhem_refresh_status", lambda **kwargs: kwargs)
    payload = service.run_mayhem_refresh(force=True, scraper=lambda: {"items": [], "rejects": []})
    assert payload["result"] == "failed"
    assert payload["reason"] == "raw_empty"


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_mayhem_validation_does_not_require_apex_current(tmp_path) -> None:
    from hextech.modules.acquisition.mayhem.merge import merge_mayhem_combos

    raw_path = tmp_path / "mayhem.json"
    augment_path = tmp_path / "augments.json"
    champion_path = tmp_path / "champions.json"
    _write_json(
        raw_path,
        {
            "items": [
                {
                    "id": "combo-1",
                    "champion_id": "24",
                    "augment_names": ["测试海克斯"],
                    "body": "测试联动",
                }
            ],
            "rejects": [],
        },
    )
    _write_json(augment_path, [{"name": "测试海克斯", "tier": "黄金"}])
    _write_json(champion_path, {"24": {"name": "武器大师", "en_name": "Jax"}})

    summary = merge_mayhem_combos(
        mayhem_raw_path=raw_path,
        augment_manifest_path=augment_path,
        core_data_path=champion_path,
        validate_only=True,
    )

    assert summary["base_mode"] == "validation_only"
    assert summary["apex_path"] == ""
    assert summary["mayhem_valid_items"] == 1
    assert summary["written"] is False


def test_mayhem_merge_requires_explicit_apex_and_never_overrides_it(tmp_path) -> None:
    from hextech.modules.acquisition.mayhem.merge import merge_mayhem_combos

    raw_path = tmp_path / "mayhem.json"
    apex_path = tmp_path / "apex.json"
    augment_path = tmp_path / "augments.json"
    champion_path = tmp_path / "champions.json"
    combo = {
        "champion_id": "24",
        "augment_names": ["测试海克斯"],
        "body": "Mayhem 不得覆盖 Apex",
    }
    _write_json(raw_path, {"items": [combo], "rejects": []})
    _write_json(
        apex_path,
        {
            "24": {
                "id": "24",
                "name": "武器大师",
                "synergy_items": [{"augment_names": ["测试海克斯"], "source": "apex"}],
                "synergies": [],
            }
        },
    )
    _write_json(augment_path, [{"name": "测试海克斯", "tier": "黄金"}])
    _write_json(champion_path, {"24": {"name": "武器大师", "en_name": "Jax"}})

    summary = merge_mayhem_combos(
        apex_path=apex_path,
        mayhem_raw_path=raw_path,
        augment_manifest_path=augment_path,
        core_data_path=champion_path,
    )

    assert summary["base_mode"] == "apex_input"
    assert summary["mayhem_valid_items"] == 1
    assert summary["added_items"] == 0
    assert summary["skipped_duplicate_items"] == 1
    assert summary["merged_payload"]["24"]["synergy_items"][0]["source"] == "apex"
