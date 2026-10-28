"""runtime 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (Path, TemporaryDirectory, alias_search, json, patch)

pytestmark = pytest.mark.dev_gate

def test_runtime_alias_persistence() -> None:
    original_runtime_alias_file = alias_search.RUNTIME_ALIAS_FILE
    original_alias_index_file = alias_search.CHAMPION_ALIAS_INDEX_FILE
    original_cache = alias_search._ALIAS_INDEX_CACHE
    try:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stable_alias_file = tmp_path / "Champion_Alias_Index.json"
            stable_alias_payload = [
                {
                    "heroName": "德玛西亚之力",
                    "title": "盖伦",
                    "enName": "Garen",
                    "heroId": "86",
                    "aliases": ["盖伦"],
                }
            ]
            stable_alias_file.write_text(json.dumps(stable_alias_payload, ensure_ascii=False), encoding="utf-8")
            core_file = tmp_path / "Champion_Core_Data.json"
            core_payload = {"86": {"name": "德玛西亚之力", "title": "盖伦", "en_name": "Garen", "aliases": ["盖伦"]}}
            core_file.write_text(json.dumps(core_payload, ensure_ascii=False), encoding="utf-8")
            stable_before = stable_alias_file.read_text(encoding="utf-8")
            core_before = core_file.read_text(encoding="utf-8")

            alias_search.CHAMPION_ALIAS_INDEX_FILE = str(stable_alias_file)
            alias_search.RUNTIME_ALIAS_FILE = str(tmp_path / "runtime" / "aliases.json")
            alias_search._ALIAS_INDEX_CACHE = ("", 0.0, [])
            added = alias_search.add_runtime_champion_alias(stable_alias_payload[0], "大宝剑")
            merged = alias_search.load_champion_alias_map(force_refresh=True)

            assert added
            assert "大宝剑" in merged["德玛西亚之力"]
            assert Path(alias_search.RUNTIME_ALIAS_FILE).exists()
            assert stable_alias_file.read_text(encoding="utf-8") == stable_before
            assert core_file.read_text(encoding="utf-8") == core_before
    finally:
        alias_search.RUNTIME_ALIAS_FILE = original_runtime_alias_file
        alias_search.CHAMPION_ALIAS_INDEX_FILE = original_alias_index_file
        alias_search._ALIAS_INDEX_CACHE = original_cache







def test_ui_feature_flags_contract() -> None:
    """验证双开关运行态配置的默认值、持久化和未知字段收口。"""
    import hextech.modules.session.settings as ui_feature_flags

    with TemporaryDirectory() as tmp_dir:
        flags_path = Path(tmp_dir) / "ui_feature_flags.json"
        defaults = ui_feature_flags.load_ui_feature_flags(flags_path)

        assert "web_frontend_enabled" not in defaults
        assert defaults["game_overlay_enabled"] is True
        assert "auto_open_browser" not in defaults
        assert defaults["private_policy_stats_enabled"] is True
        assert defaults["low_frequency_listener_enabled"] is True

        ui_feature_flags.save_ui_feature_flags(
            {
                "web_frontend_enabled": True,
                "game_overlay_enabled": True,
                "private_policy_stats_enabled": True,
                "unknown": "ignored",
            },
            flags_path,
        )
        loaded = ui_feature_flags.load_ui_feature_flags(flags_path)

        assert "web_frontend_enabled" not in loaded
        assert loaded["game_overlay_enabled"] is True
        assert "auto_open_browser" not in loaded
        assert loaded["private_policy_stats_enabled"] is True
        assert "unknown" not in loaded

        parsed = ui_feature_flags.normalize_ui_feature_flags(
            {"game_overlay_enabled": "false", "web_frontend_enabled": "true", "auto_open_browser": "invalid"}
        )
        assert parsed["game_overlay_enabled"] is False
        assert "web_frontend_enabled" not in parsed
        assert "auto_open_browser" not in parsed

    with TemporaryDirectory() as tmp_dir:
        flags_path = Path(tmp_dir) / "ui_feature_flags.json"
        marker_path = Path(tmp_dir) / "ui_feature_flags.defaults.v2.json"
        flags_path.write_text(
            json.dumps(
                {
                    "web_frontend_enabled": False,
                    "game_overlay_enabled": False,
                    "auto_open_browser": True,
                    "private_policy_stats_enabled": False,
                    "low_frequency_listener_enabled": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        with (
            patch.object(ui_feature_flags, "FEATURE_FLAGS_FILE", flags_path),
            patch.object(ui_feature_flags, "DEFAULT_ON_MIGRATION_MARKER_FILE", marker_path),
            patch.object(ui_feature_flags.sys, "frozen", False, create=True),
        ):
            migrated = ui_feature_flags.load_ui_feature_flags()
            assert "web_frontend_enabled" not in migrated
            assert migrated["game_overlay_enabled"] is True
            assert migrated["private_policy_stats_enabled"] is False
            assert migrated["low_frequency_listener_enabled"] is True
            assert marker_path.exists()

            ui_feature_flags.save_ui_feature_flags(
                {**migrated, "game_overlay_enabled": False, "private_policy_stats_enabled": False},
                flags_path,
            )
            preserved = ui_feature_flags.load_ui_feature_flags()
            assert preserved["game_overlay_enabled"] is False
            assert preserved["private_policy_stats_enabled"] is False
