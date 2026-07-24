"""runtime 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    Path,
    RUN_DIR,
    TemporaryDirectory,
    WEB_STATIC_DIR,
    _write_runtime_csv,
    alias_search,
    json,
    os,
    patch,
    precomputed_cache,
    runtime_store,
    threading,
)

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

def test_latest_valid_runtime_csv_fallback() -> None:
    """最新快照保留兼容读取；健康判断必须回退到上一份有效版本。"""

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        valid = root / "Hextech_Data_2026-06-19.csv"
        too_small = root / "Hextech_Data_2026-06-20.csv"
        broken = root / "Hextech_Data_2026-06-21.csv"
        _write_runtime_csv(valid, 300)
        _write_runtime_csv(too_small, 299)
        broken.write_text("unexpected\nvalue\n", encoding="utf-8")
        os.utime(valid, (1000, 1000))
        os.utime(too_small, (2000, 2000))
        os.utime(broken, (3000, 3000))

        with patch.object(
            runtime_store,
            "iter_runtime_csv_files",
            return_value=[str(valid), str(too_small), str(broken)],
        ):
            assert runtime_store.get_latest_valid_csv() == str(valid)
            assert runtime_store.get_latest_csv() == str(broken)

def test_precomputed_cache_freshness() -> None:
    with TemporaryDirectory() as temp_dir:
        latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
        latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
        os.utime(latest_csv, (1000, 1000))

        cache_file = Path(temp_dir) / "Champion_Hextech_Cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "meta": {
                        "source": latest_csv.name,
                        "source_mtime": 999,
                    },
                    "data": {"酒桶": {"comprehensive": [{"海克斯名称": "旧数据"}]}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)):
            assert not precomputed_cache._cache_matches_latest_csv(str(cache_file))

    with TemporaryDirectory() as temp_dir:
        latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
        latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
        os.utime(latest_csv, (2000, 2000))

        cache_file = Path(temp_dir) / "Champion_Hextech_Cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "meta": {
                        "source": latest_csv.name,
                        "source_mtime": 1000,
                    },
                    "data": {"酒桶": {"comprehensive": [{"海克斯名称": "旧数据"}]}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        precomputed_cache._hextech_cache_state.update({"path": "", "mtime": 0.0, "data": {}})
        with (
            patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)),
            patch.object(precomputed_cache, "_resolve_cache_file", return_value=str(cache_file)),
        ):
            assert precomputed_cache.load_precomputed_hextech_for_hero("酒桶") is None

    with TemporaryDirectory() as temp_dir:
        latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
        latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
        os.utime(latest_csv, (3000, 3000))

        cache_file = Path(temp_dir) / "Champion_Hextech_Cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "meta": {
                        "source": latest_csv.name,
                        "source_mtime": 3000,
                    },
                    "data": {"酒桶": {"comprehensive": [{"海克斯名称": "可用数据"}]}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        precomputed_cache._cache_match_state.pop(str(cache_file), None)
        read_count = {"value": 0}
        original_read_cache_payload = precomputed_cache._read_cache_payload

        def counted_read_cache_payload(path: str) -> dict:
            read_count["value"] += 1
            return original_read_cache_payload(path)

        with (
            patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)),
            patch.object(precomputed_cache, "_read_cache_payload", side_effect=counted_read_cache_payload),
        ):
            assert precomputed_cache._cache_matches_latest_csv(str(cache_file))
            assert precomputed_cache._cache_matches_latest_csv(str(cache_file))
            assert read_count["value"] == 1
            os.utime(cache_file, (4000, 4000))
            assert precomputed_cache._cache_matches_latest_csv(str(cache_file))
            assert read_count["value"] == 2

def test_precomputed_cache_returns_unpollutable_copies() -> None:
    with TemporaryDirectory() as temp_dir:
        latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
        latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
        os.utime(latest_csv, (5000, 5000))

        champion_cache = Path(temp_dir) / "Champion_List_Cache.json"
        champion_cache.write_text(
            json.dumps(
                {
                    "meta": {"source": latest_csv.name, "source_mtime": 5000},
                    "data": [{"name": "酒桶", "stats": {"wins": 1}}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        hextech_cache = Path(temp_dir) / "Champion_Hextech_Cache.json"
        hextech_cache.write_text(
            json.dumps(
                {
                    "meta": {"source": latest_csv.name, "source_mtime": 5000},
                    "data": {"酒桶": {"comprehensive": [{"海克斯名称": "A", "score": {"value": 1}}]}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        precomputed_cache._champion_cache_state.update({"path": "", "mtime": 0.0, "data": []})
        precomputed_cache._hextech_cache_state.update({"path": "", "mtime": 0.0, "data": {}})
        precomputed_cache._cache_match_state.clear()

        def resolve_cache_file(path: str, _legacy_name: str) -> str:
            if path == precomputed_cache.CHAMPION_LIST_CACHE_FILE:
                return str(champion_cache)
            return str(hextech_cache)

        with (
            patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)),
            patch.object(precomputed_cache, "_resolve_cache_file", side_effect=resolve_cache_file),
        ):
            champions = precomputed_cache.load_precomputed_champion_list()
            champions[0]["stats"]["wins"] = 999
            assert precomputed_cache.load_precomputed_champion_list()[0]["stats"]["wins"] == 1

            hextech_map = precomputed_cache.load_precomputed_hextech_map()
            hextech_map["酒桶"]["comprehensive"][0]["score"]["value"] = 999
            assert precomputed_cache.load_precomputed_hextech_map()["酒桶"]["comprehensive"][0]["score"]["value"] == 1

            hero_payload = precomputed_cache.load_precomputed_hextech_for_hero("酒桶")
            hero_payload["comprehensive"][0]["score"]["value"] = 999
            fresh_payload = precomputed_cache.load_precomputed_hextech_for_hero("酒桶")
            assert fresh_payload["comprehensive"][0]["score"]["value"] == 1

def test_cached_dataframe_loader_returns_copy_and_hash_samples_middle() -> None:
    import pandas as pd
    from hextech.modules.data.catalog.runtime_store import CachedDataFrameLoader
    from hextech.modules.data.catalog import view_adapter

    with TemporaryDirectory() as temp_dir:
        csv_path = Path(temp_dir) / "Hextech_Data_20260518.csv"
        csv_path.write_text(
            "\n".join(
                [
                    "英雄ID,英雄名称,英雄评级,英雄胜率,英雄出场率,海克斯名称,海克斯阶级,海克斯胜率,海克斯出场率",
                    "1,酒桶,A,50.0,10.0,A,Gold,55.0,2.0",
                    "2,盖伦,B,49.0,8.0,B,Silver,51.0,1.5",
                    "3,拉克丝,S,52.0,12.0,C,Prismatic,57.0,1.0",
                    "",
                ]
            ),
            encoding="utf-8-sig",
        )
        os.utime(csv_path, (6000, 6000))

        loader = CachedDataFrameLoader(lambda: str(csv_path))
        first = loader.get_df()
        first.loc[0, "英雄名称"] = "污染"
        second = loader.get_df()
        assert second.loc[0, "英雄名称"] == "酒桶"

    left = pd.DataFrame({"英雄名称": ["A", "B", "C", "D", "E"], "score": [1, 2, 3, 4, 5]})
    right = pd.DataFrame({"英雄名称": ["A", "B", "C", "D", "E"], "score": [1, 99, 3, 4, 5]})
    assert view_adapter._compute_df_hash(left) != view_adapter._compute_df_hash(right)

    cache_df = pd.DataFrame(
        {
            "英雄ID": [1, 2],
            "英雄名称": ["酒桶", "盖伦"],
            "英雄评级": ["A", "B"],
            "英雄胜率": [50.0, 49.0],
            "英雄出场率": [10.0, 8.0],
            "海克斯名称": ["A", "B"],
            "海克斯阶级": ["Gold", "Silver"],
            "海克斯胜率": [55.0, 51.0],
            "海克斯出场率": [2.0, 1.5],
            "胜率差": [5.0, 2.0],
            "综合得分": [1.0, 0.5],
        }
    )
    view_adapter._champion_cache_pool.clear()
    view_adapter._hextech_cache_pool.clear()
    view_adapter._cache_metadata.clear()

    champions = view_adapter.process_champions_data(cache_df, use_runtime_cache=True, log_columns=False)
    champions[0]["英雄名称"] = "污染"
    fresh_champions = view_adapter.process_champions_data(cache_df, use_runtime_cache=True, log_columns=False)
    assert fresh_champions[0]["英雄名称"] != "污染"

    hextechs = view_adapter.process_hextechs_data(
        cache_df,
        "酒桶",
        catalog_lookup={},
        use_runtime_cache=True,
        log_columns=False,
    )
    hextechs["comprehensive"][0]["海克斯名称"] = "污染"
    fresh_hextechs = view_adapter.process_hextechs_data(
        cache_df,
        "酒桶",
        catalog_lookup={},
        use_runtime_cache=True,
        log_columns=False,
    )
    assert fresh_hextechs["comprehensive"][0]["海克斯名称"] != "污染"

def test_precomputed_atomic_write_uses_unique_temp_files() -> None:
    with TemporaryDirectory() as temp_dir:
        target = Path(temp_dir) / "cache.json"
        replace_sources: list[str] = []
        original_replace = precomputed_cache.atomic_write_json.__globals__["os"].replace
        replace_lock = threading.Lock()

        def tracking_replace(src, dst):
            with replace_lock:
                replace_sources.append(str(src))
            original_replace(src, dst)

        errors: list[BaseException] = []

        def write_payload(index: int) -> None:
            try:
                precomputed_cache._atomic_write_json(str(target), {"value": index})
            except BaseException as exc:
                errors.append(exc)

        with patch.object(precomputed_cache.atomic_write_json.__globals__["os"], "replace", side_effect=tracking_replace):
            threads = [
                threading.Thread(target=write_payload, args=(index,))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        assert not errors
        assert json.loads(target.read_text(encoding="utf-8"))["value"] in {0, 1}
        assert len(replace_sources) == 2
        assert len(set(replace_sources)) == 2
        assert not target.with_suffix(target.suffix + ".tmp").exists()

def test_static_css_single_mount_contract() -> None:
    index_text = (WEB_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    detail_text = (WEB_STATIC_DIR / "detail.html").read_text(encoding="utf-8")
    web_server_text = (RUN_DIR / "src" / "hextech" / "interfaces" / "web" / "backend" / "app.py").read_text(encoding="utf-8")
    frontend_package = (RUN_DIR / "src" / "hextech" / "interfaces" / "web" / "frontend" / "package.json").read_text(encoding="utf-8")
    tailwind_config = (
        RUN_DIR / "src" / "hextech" / "interfaces" / "web" / "frontend" / "tailwind.config.js"
    ).read_text(encoding="utf-8")

    assert 'href="/static/css/hextech-theme.css"' in index_text
    assert 'href="/static/css/hextech-theme.css"' in detail_text
    assert 'app.mount("/css"' not in web_server_text
    assert "../display/static" not in frontend_package
    assert "../display/static" not in tailwind_config
    assert "../backend/static/css/tailwind-compiled.css" in frontend_package
    assert "../backend/static/**/*.html" in tailwind_config
    assert "../backend/static/**/*.js" in tailwind_config

def test_ui_feature_flags_contract() -> None:
    """验证双开关运行态配置的默认值、持久化和未知字段收口。"""
    import hextech.modules.session.settings as ui_feature_flags

    with TemporaryDirectory() as tmp_dir:
        flags_path = Path(tmp_dir) / "ui_feature_flags.json"
        defaults = ui_feature_flags.load_ui_feature_flags(flags_path)

        assert defaults["web_frontend_enabled"] is False
        assert defaults["game_overlay_enabled"] is True
        assert defaults["auto_open_browser"] is True
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

        assert loaded["web_frontend_enabled"] is True
        assert loaded["game_overlay_enabled"] is True
        assert loaded["auto_open_browser"] is True
        assert loaded["private_policy_stats_enabled"] is True
        assert "unknown" not in loaded

        parsed = ui_feature_flags.normalize_ui_feature_flags(
            {"game_overlay_enabled": "false", "web_frontend_enabled": "true", "auto_open_browser": "invalid"}
        )
        assert parsed["game_overlay_enabled"] is False
        assert parsed["web_frontend_enabled"] is True
        assert parsed["auto_open_browser"] is True

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
            assert migrated["web_frontend_enabled"] is False
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
