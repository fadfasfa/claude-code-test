"""runtime 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    Any,
    ExitStack,
    Path,
    RUN_DIR,
    TemporaryDirectory,
    WEB_STATIC_DIR,
    _probe_clean_import,
    _probe_module_import,
    _top_level_import_names,
    _write_runtime_csv,
    alias_search,
    datetime,
    heal_worker,
    hextech_scraper,
    json,
    orchestrator,
    os,
    patch,
    pd,
    precomputed_cache,
    runtime_store,
    threading,
    time,
    timezone,
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

        with (
            patch.object(orchestrator, "get_latest_csv", side_effect=AssertionError("refresh must use valid csv")),
            patch.object(orchestrator, "get_latest_valid_csv", return_value=str(valid)),
            patch.object(orchestrator, "hextech_refresh_blocked", return_value=False),
            patch.object(orchestrator, "_file_is_fresh", return_value=True),
        ):
            assert orchestrator.should_refresh_hextech(False) is False

        with (
            patch.object(heal_worker, "get_latest_csv", side_effect=AssertionError("freshness must use valid csv")),
            patch.object(heal_worker, "get_latest_valid_csv", return_value=str(valid)),
            patch.object(heal_worker, "_file_is_fresh", return_value=True),
        ):
            assert heal_worker._latest_csv_ready() is True
            assert heal_worker._latest_csv_fresh() is True

def test_hextech_failed_refresh_never_overwrites_csv() -> None:
    """低行数与 force 超时都只能回退，不能覆盖已有快照。"""

    def fake_result(status_code: int | None, payload: Any = None, text: str = "ok", error: str = ""):
        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False)
        return hextech_scraper.ScraplingFetchResult(
            url="https://example.test",
            text=text,
            status_code=status_code,
            fetched_at="2026-06-23T00:00:00+00:00",
            error=error,
        )

    class SequenceFetcher:
        def __init__(self, responses: list[Any]) -> None:
            self.responses = list(responses)
            self.calls = 0

        def __call__(self, *_args, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    one_row = {
        "英雄ID": "1",
        "英雄名称": "测试英雄",
        "英雄评级": "S",
        "英雄胜率": 0.5,
        "英雄出场率": 0.1,
        "海克斯阶级": "Gold",
        "海克斯名称": "测试海克斯",
        "海克斯胜率": 0.51,
        "海克斯出场率": 0.02,
        "源站排名": 1,
    }
    metadata = fake_result(200, {"100": {"displayName": "测试海克斯"}})
    stats = fake_result(200, [{"championId": "1"}])

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fallback_csv = root / "Hextech_Data_2026-06-19.csv"
        output_csv = root / "Hextech_Data_2026-06-21.csv"
        status_file = root / "scraper_status.json"
        _write_runtime_csv(fallback_csv, 300)
        output_csv.write_text("do-not-overwrite", encoding="utf-8")
        low_row_fetcher = SequenceFetcher([metadata, stats, fake_result(200, text="detail")])

        common_patches = (
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch.object(hextech_scraper, "get_latest_valid_csv", return_value=str(fallback_csv)),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_daily_csv_path", return_value=str(output_csv)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(hextech_scraper, "extract_champion_stats", return_value=[one_row]),
            patch.object(hextech_scraper.time, "sleep"),
        )
        with ExitStack() as stack:
            for context_manager in common_patches:
                stack.enter_context(context_manager)
            stack.enter_context(patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")))
            stack.enter_context(patch.object(hextech_scraper, "fetch_text", side_effect=low_row_fetcher))
            stack.enter_context(
                patch.object(hextech_scraper, "atomic_write_csv", side_effect=AssertionError("低行数不得覆盖 CSV"))
            )
            assert hextech_scraper.main_scraper() is True
        assert output_csv.read_text(encoding="utf-8") == "do-not-overwrite"
        assert json.loads(status_file.read_text(encoding="utf-8"))["reason"] == "insufficient_rows_1"

        status_file.unlink()
        future_block = {
            "last_result": "fallback",
            "reason": "http_403",
            "blocked_until": datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat(),
        }
        timeout_fetcher = SequenceFetcher(
            [
                fake_result(200, {"100": {"displayName": "测试海克斯"}}),
                fake_result(200, [{"championId": "1"}]),
                fake_result(None, text="", error="simulated timeout"),
                fake_result(None, text="", error="simulated timeout"),
            ]
        )
        with (
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch.object(hextech_scraper, "get_latest_valid_csv", return_value=str(fallback_csv)),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(hextech_scraper, "load_scraper_status", return_value=future_block),
            patch.object(hextech_scraper, "fetch_text", side_effect=timeout_fetcher),
            patch.object(
                hextech_scraper.DETAIL_PASS_RUNNER,
                "run",
                side_effect=AssertionError("预检超时不得启动英雄详情并发"),
            ),
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper(force=True) is True
        timeout_status = json.loads(status_file.read_text(encoding="utf-8"))
        assert timeout_fetcher.calls == 4
        assert timeout_status["reason"] == "timeout"
        assert timeout_status["next_retry_at"] == timeout_status["blocked_until"]

def test_hextech_success_clears_fallback_state() -> None:
    def fake_result(payload: Any = None, text: str = "ok"):
        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False)
        return hextech_scraper.ScraplingFetchResult(
            url="https://example.test",
            text=text,
            status_code=200,
            fetched_at="2026-06-23T00:00:00+00:00",
            error="",
        )

    class SequenceFetcher:
        def __init__(self) -> None:
            self.responses = [
                fake_result({"100": {"displayName": "测试海克斯"}}),
                fake_result([{"championId": "1"}]),
                fake_result(text="detail"),
            ]

        def __call__(self, *_args, **_kwargs):
            return self.responses.pop(0)

    row = {
        "英雄ID": "1",
        "英雄名称": "测试英雄",
        "英雄评级": "S",
        "英雄胜率": 0.5,
        "英雄出场率": 0.1,
        "海克斯阶级": "Gold",
        "海克斯名称": "测试海克斯",
        "海克斯胜率": 0.51,
        "海克斯出场率": 0.02,
        "源站排名": 1,
    }
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        output_csv = root / "Hextech_Data_2026-06-21.csv"
        status_file = root / "scraper_status.json"
        stale_status = {
            "last_result": "fallback",
            "reason": "http_403",
            "blocked_until": datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat(),
            "last_success_time": 1,
        }
        with (
            patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")),
            patch.object(hextech_scraper, "load_scraper_status", return_value=stale_status),
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch.object(hextech_scraper, "fetch_text", side_effect=SequenceFetcher()),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_daily_csv_path", return_value=str(output_csv)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(hextech_scraper, "extract_champion_stats", return_value=[row] * 300),
            patch.object(hextech_scraper, "backup_active_csv_before_publish"),
            patch.object(hextech_scraper, "cleanup_old_csvs") as cleanup,
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper() is True

        status = json.loads(status_file.read_text(encoding="utf-8"))
        assert status["last_result"] == "success"
        assert status["reason"] == ""
        assert status["blocked_until"] == ""
        assert status["active_csv"] == str(output_csv)
        assert status["last_success_time"] > 1
        assert len(pd.read_csv(output_csv, encoding=runtime_store.CSV_ENCODING)) == 300
        cleanup.assert_called_once()

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
    from hextech.catalog.runtime_store import CachedDataFrameLoader
    from hextech.catalog import view_adapter

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
    web_server_text = (RUN_DIR / "hextech" / "display" / "web" / "app.py").read_text(encoding="utf-8")
    frontend_package = (RUN_DIR / "frontend" / "package.json").read_text(encoding="utf-8")
    tailwind_config = (RUN_DIR / "frontend" / "tailwind.config.js").read_text(encoding="utf-8")

    assert 'href="/static/css/hextech-theme.css"' in index_text
    assert 'href="/static/css/hextech-theme.css"' in detail_text
    assert 'app.mount("/css"' not in web_server_text
    assert "../display/static" not in frontend_package
    assert "../display/static" not in tailwind_config
    assert "../hextech/display/web/static/css/tailwind-compiled.css" in frontend_package
    assert "../hextech/display/web/static/**/*.html" in tailwind_config
    assert "../hextech/display/web/static/**/*.js" in tailwind_config

def test_ui_feature_flags_contract() -> None:
    """验证双开关运行态配置的默认值、持久化和未知字段收口。"""
    import hextech.core.settings as ui_feature_flags

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

def test_desktop_ui_feature_switch_contract() -> None:
    """验证桌面 UI 不再初始化时无条件启动 Web 服务。"""
    import hextech.display.desktop.runtime as ui_runtime
    import hextech.display.desktop.app as desktop_app

    ui_text = (RUN_DIR / "hextech" / "display" / "desktop" / "app.py").read_text(encoding="utf-8")
    runtime_text = (RUN_DIR / "hextech" / "display" / "desktop" / "runtime.py").read_text(encoding="utf-8")
    assert "ui._df_lock" not in runtime_text
    assert "ui.df" not in runtime_text
    init_start = ui_text.index("    def __init__(self):")
    init_end = ui_text.index("    def _mark_first_idle_visible", init_start)
    init_body = ui_text[init_start:init_end]

    assert "ServiceManager" in ui_text
    assert "Web 前端" in ui_text
    assert "游戏内显示" in ui_text
    assert "低频监听" not in ui_text
    assert "tk.Checkbutton" not in ui_text
    assert "feature_status_label" not in ui_text
    assert "Web:" not in ui_text
    assert " / sidecar" not in ui_text
    assert 'root.attributes("-alpha", 1.0' in ui_text
    assert "_build_feature_toggle" in ui_text
    assert 'sticky="ew"' in ui_text
    assert "self._refresh_feature_toggle_styles()\n            command()" in ui_text
    assert "_feature_toggle_busy" in ui_text
    assert "_closing" in ui_text
    assert "_overlay_operation_lock" in ui_text
    assert "_game_overlay_desired_enabled" in ui_text
    assert "StartupTimingProbe" in ui_text
    assert "_schedule_post_visible_bootstrap" in ui_text
    assert "hextech-post-visible-bootstrap" in ui_text
    assert "self.root.after(50, self._schedule_post_visible_bootstrap)" in ui_text
    assert "self._start_runtime_supervisor()" not in init_body
    assert "ServiceManager(" not in init_body
    assert "start_low_frequency_listener()" not in init_body
    assert "_start_tracked_thread" in ui_text
    assert 'self._feature_toggle_is_busy("游戏内显示")' in ui_text
    assert "set_game_overlay_enabled" in ui_text
    assert "components.get(\"game_overlay\")" in ui_text
    assert "hextech-toggle-web" in ui_text
    assert "hextech-toggle-overlay" in ui_text
    assert "hextech-overlay-watchdog" in ui_text
    assert "hextech-toggle-private-stats" in ui_text
    assert "正在切换 Web 前端" in ui_text
    assert "_set_overlay_status_summary" in ui_text
    assert "游戏内显示: 正在提交启动请求" in ui_text
    assert "游戏内显示启动请求已提交" in ui_text
    assert "WINDOW_EXPANDED_GEOMETRY = \"320x740\"" in ui_text
    assert "manage_overlay_runtime=False" in ui_text
    assert "overlay_controller=GameOverlayController(" not in ui_text
    assert "start_vision_sidecar_process" not in ui_text
    assert "self._start_web_server()" not in init_body

    root_entry_imports = _top_level_import_names(RUN_DIR / "hextech_ui.py")
    assert not any(name.startswith("display") for name in root_entry_imports)

    assert _probe_clean_import("hextech_ui.py") == set()
    assert _probe_module_import("hextech.overlay.host") == set()

    captured: dict[str, Any] = {}

    class DummyProcess:
        def poll(self):
            return None

    def fake_popen(command, startupinfo=None, cwd=None, env=None):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})
        return DummyProcess()

    with (
        patch.object(ui_runtime.subprocess, "Popen", side_effect=fake_popen),
        patch.object(ui_runtime, "_clear_web_readiness_files", return_value=None),
        patch.object(ui_runtime, "_wait_for_web_startup", return_value=None),
    ):
        ui_runtime.start_web_server_process("unused-port-file.txt", auto_open_browser=True)
    assert captured["env"]["HEXTECH_OPEN_BROWSER"] == "0"

    assert hasattr(ui_runtime, "open_companion_browser")
    assert hasattr(ui_runtime, "close_companion_browser")
    private_toggle_body = ui_text.split("    def _toggle_private_policy_stats", 1)[1].split("    def _toggle_low_frequency_listener", 1)[0]
    assert "self.data_service.set_private_stats" in private_toggle_body
    assert "build_overlay_hint_cache_from_precomputed" not in private_toggle_body
    assert "desired_private_stats = bool(self.private_stats_var.get())" in private_toggle_body
    assert "self._persist_feature_flags_from_controls()" in private_toggle_body
    assert "save_ui_feature_flags(desired_flags)" not in private_toggle_body
    assert "if not _web_frontend_available(ui):\n            time.sleep(3)\n            continue" not in runtime_text

    class DummyBoolVar:
        def __init__(self, value: bool):
            self.value = value

        def get(self) -> bool:
            return self.value

        def set(self, value: bool) -> None:
            self.value = bool(value)

    class OverlayStartProbe:
        def __init__(self):
            self.start_calls = 0

        def start_game_overlay(self) -> None:
            self.start_calls += 1

        def stop_game_overlay(self) -> None:
            return None

        def is_game_overlay_running(self) -> bool:
            return False

    # 退出标记可能在 toggle 等待 operation lock 时发生；释放后不得再启动 overlay。
    lifecycle_ui = object.__new__(desktop_app.HextechUI)
    lifecycle_ui.game_overlay_var = DummyBoolVar(True)
    lifecycle_ui.service_manager = OverlayStartProbe()
    lifecycle_ui._overlay_operation_lock = threading.Lock()
    lifecycle_ui._overlay_operation_lock.acquire()
    lifecycle_ui._closing = False
    lifecycle_ui._game_overlay_desired_enabled = False
    lifecycle_ui._set_feature_toggle_busy = lambda *_args: None
    lifecycle_ui._set_status = lambda *_args: None
    lifecycle_ui._persist_feature_flags_from_controls = lambda: None
    lifecycle_ui._try_persist_feature_flags_from_controls = lambda: None
    lifecycle_ui._run_on_ui_thread = lambda command: command()
    lifecycle_ui._raise_if_service_error = lambda _name: None
    toggle_threads: list[threading.Thread] = []

    def start_toggle_thread(target, *, name: str):
        thread = threading.Thread(target=target, name=name, daemon=True)
        toggle_threads.append(thread)
        thread.start()
        return thread

    lifecycle_ui._start_tracked_thread = start_toggle_thread
    lifecycle_ui._toggle_game_overlay()
    assert toggle_threads and toggle_threads[0].is_alive()
    lifecycle_ui._closing = True
    lifecycle_ui._overlay_operation_lock.release()
    toggle_threads[0].join(timeout=2)
    assert not toggle_threads[0].is_alive()
    assert lifecycle_ui.service_manager.start_calls == 0

    candidate_groups = {
        "selected_champion_ids": ["1", "2", "3", "4", "5"],
        "bench_champion_ids": ["6", "7", "8", "9", "10"],
        "local_champion_id": "2",
        "teammate_champion_ids": ["1", "3", "4", "5"],
    }
    candidate_champions = [
        {"id": str(index), "name": f"英雄{index}", "英雄评级": f"T{index}", "英雄胜率": win, "英雄出场率": 0.01}
        for index, win in (
            (1, 0.41),
            (2, 0.55),
            (3, 0.49),
            (4, 0.52),
            (5, 0.47),
            (6, 0.60),
            (7, 0.46),
            (8, 0.58),
            (9, 0.50),
            (10, 0.44),
        )
    ]
    dummy_ui = type("DummyDesktopUI", (), {})()
    dummy_ui._candidate_groups_from_input = desktop_app.HextechUI._candidate_groups_from_input.__get__(dummy_ui)
    display_list = desktop_app.HextechUI._build_candidate_display_list(dummy_ui, candidate_groups, candidate_champions)
    assert [item["id"] for item in display_list] == ["6", "8", "2", "4", "9", "3", "5", "7", "10", "1"]
    assert next(item for item in display_list if item["id"] == "2")["selection_role"] == "self"
    assert next(item for item in display_list if item["id"] == "4")["selection_role"] == "teammate"
    assert next(item for item in display_list if item["id"] == "6")["selection_role"] == "bench"
    assert "_group_rank" not in display_list[0]
    equal_win_champions = [
        {"id": "2", "name": "英雄2", "英雄评级": "T1", "英雄胜率": 0.5, "英雄出场率": 0.01},
        {"id": "1", "name": "英雄1", "英雄评级": "T1", "英雄胜率": 0.5, "英雄出场率": 0.01},
    ]
    equal_win_groups = {
        "selected_champion_ids": ["2", "1"],
        "bench_champion_ids": [],
        "local_champion_id": "1",
        "teammate_champion_ids": ["2"],
    }
    equal_win_list = desktop_app.HextechUI._build_candidate_display_list(dummy_ui, equal_win_groups, equal_win_champions)
    assert [item["id"] for item in equal_win_list] == ["2", "1"]


def test_desktop_self_role_has_stronger_visual_priority_than_teammate() -> None:
    from hextech.display.desktop import app as desktop_app

    self_style = desktop_app._selection_role_style("self")
    teammate_style = desktop_app._selection_role_style("teammate")
    bench_style = desktop_app._selection_role_style("bench")

    assert self_style["border_width"] == 3
    assert self_style["marker_width"] > teammate_style["marker_width"] > bench_style["marker_width"]
    assert self_style["accent"] != teammate_style["accent"]
    assert self_style["surface"] != bench_style["surface"]
