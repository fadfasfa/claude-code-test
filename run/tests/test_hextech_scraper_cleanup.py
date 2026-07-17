"""Hextech 新抓取入口不再暴露旧 CSV retention API。"""

from __future__ import annotations


def test_hextech_service_has_no_legacy_csv_publisher() -> None:
    from hextech.infrastructure.sources.hextech import service

    for removed in ("cleanup_old_csvs", "build_daily_csv_path", "backup_active_csv_before_publish"):
        assert not hasattr(service, removed)


def test_expected_champions_come_from_catalog() -> None:
    from hextech.infrastructure.sources.hextech.source import build_expected_champions
    from hextech.modules.data.catalog.version_catalog import load_champion_core_data

    core = load_champion_core_data()
    remote = [{"championId": champion_id} for champion_id in core]
    champions = build_expected_champions(core, remote)
    assert len(champions) >= 170
    assert len({item["championId"] for item in champions}) == len(champions)
