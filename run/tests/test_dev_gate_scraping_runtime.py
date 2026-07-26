"""scraping 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    ORIGINAL_SYNC_HERO_DATA,
    Path,
    TemporaryDirectory,
    _morgana_html,
    _morgana_maps,
    datetime,
    extract_champion_stats,
    hextech_scraper,
    json,
    patch,
    pd,
    process_hextechs_data,
    scrapling_client,
    sys,
    time,
    timezone,
    version_sync,
)

pytestmark = pytest.mark.dev_gate



def test_response_text_repairs_latin1_mislabeled_utf8() -> None:
    """回归：无 charset 资源被 Scrapling 误报 ISO-8859-1 时曾丢失中文强化名。"""

    class MislabeledResponse:
        body = "回归基本功".encode("utf-8")
        encoding = "ISO-8859-1"

    assert scrapling_client._response_text(MislabeledResponse()) == "回归基本功"
    assert scrapling_client._response_html(MislabeledResponse()) == "回归基本功"


def test_response_text_keeps_declared_encoding_for_real_latin1() -> None:
    """真 latin-1 字节（非法 UTF-8）仍按声明编码解码，正常路径不回归。"""

    class Latin1Response:
        body = b"caf\xe9"
        encoding = "ISO-8859-1"

    assert scrapling_client._response_text(Latin1Response()) == "café"
    assert scrapling_client._response_html(Latin1Response()) == "café"


def test_scrapling_fetch_text_keeps_internal_retry() -> None:
    """短文本抓取也必须保留一次 Scrapling 内部 retry，避免 session 被提前释放。"""

    calls = []

    class GoodResponse:
        body = "{}"
        status = 200

    class RecordingFetcher:
        @staticmethod
        def get(*_args, **kwargs):
            calls.append(kwargs)
            return GoodResponse()

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = RecordingFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
    ):
        result = scrapling_client.fetch_text("https://example.test/data.json", max_attempts=1)

    assert result.status_code == 200
    assert calls
    assert calls[0]["retries"] == 1

def test_scrapling_fetch_page_get_timeout_uses_seconds() -> None:
    """Scrapling Fetcher.get 的 timeout 必须使用秒，避免 30_000 被解释成 8 小时。"""

    calls = []

    class GoodResponse:
        html = "<html></html>"
        status = 200

        def css(self, _selector):
            raise AssertionError("未传 selector 时不应读取 css")

    class RecordingFetcher:
        @staticmethod
        def get(*_args, **kwargs):
            calls.append(kwargs)
            return GoodResponse()

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = RecordingFetcher
    fetchers_module.DynamicFetcher = RecordingFetcher
    fetchers_module.StealthyFetcher = RecordingFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
    ):
        result = scrapling_client.fetch_page("https://example.test", timeout_ms=30_000, max_attempts=1)

    assert result.status_code == 200
    assert calls
    assert calls[0]["timeout"] == 30.0
    assert calls[0]["retries"] == 1

def test_verify_data_source_integrity_offline_fixture_mode() -> None:
    """离线 fixture 模式必须跳过所有远端请求，保留本地数据一致性检查。"""

    import tooling.checks.source_integrity as verifier

    row = {
        "英雄ID": "1",
        "英雄名称": "测试英雄",
        "海克斯ID": "101",
        "海克斯名称": "测试海克斯",
        "海克斯阶级": "Gold",
        "源站层级": "Gold",
        "源站排名": 1,
        "海克斯胜率": 0.5,
        "海克斯出场率": 0.2,
        "英雄胜率": 0.51,
        "英雄出场率": 0.12,
    }
    core_data = {
        "1": {
            "name": "测试英雄",
            "title": "测试标题",
            "en_name": "TestHero",
            "aliases": [],
        }
    }
    fixture_payload = {
        "core_data": core_data,
        "augment_metadata": {},
        "champion_stats": [],
        "source_rows_by_champ_id": {"1": [row]},
    }
    cache_payload = {"comprehensive": [dict(row)]}
    df = pd.DataFrame([row])

    with TemporaryDirectory() as tmp_dir:
        fixture_path = Path(tmp_dir) / "source_fixture.json"
        fixture_path.write_text(json.dumps(fixture_payload, ensure_ascii=False), encoding="utf-8")
        csv_path = Path(tmp_dir) / "Hextech_Data_2026-07-07.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

        with (
            patch.object(verifier, "get_advanced_session", side_effect=AssertionError("offline fixture 不得创建远端 session")),
            patch.object(verifier.hex_scraper, "fetch_with_retry", side_effect=AssertionError("offline fixture 不得请求远端")),
            patch.object(verifier, "load_champion_core_data", return_value=core_data),
            patch.object(verifier, "get_latest_csv", return_value=str(csv_path)),
            patch.object(verifier, "load_runtime_csv", return_value=df),
            patch.object(verifier, "load_precomputed_champion_list", return_value=[{"英雄ID": "1", "英雄胜率": 0.51, "英雄出场率": 0.12}]),
            patch.object(verifier, "load_precomputed_hextech_for_hero", return_value=cache_payload),
            patch.object(verifier, "_build_api_client", return_value=object()),
            patch.object(verifier, "_load_api_payload", return_value=cache_payload),
        ):
            code, report = verifier.run(verifier.parse_args(["--offline-fixture", str(fixture_path), "--heroes", "1"]))

    assert code == 0
    assert report["passed"] is True
    assert report["source_mode"] == "offline-fixture"
    assert report["heroes"][0]["checks"]["csv"]["source_count"] == 1

def test_version_sync_startup_resource_guard() -> None:
    """普通启动已有稳定资源时不得无条件访问远端或写 resources。"""

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        core_file = root / "英雄目录.v1.json"
        manifest_file = root / "海克斯资源目录.v1.json"
        version_file = root / "hero_version.txt"
        core_file.write_text("{}", encoding="utf-8")
        manifest_file.write_text("{}", encoding="utf-8")
        version_file.write_text("15.13.1", encoding="utf-8")
        with (
            patch.object(version_sync, "CORE_DATA_FILE", str(core_file)),
            patch.object(version_sync, "AUGMENT_MAP_FILE", str(root / "missing-augment-map.json")),
            patch.object(version_sync, "AUGMENT_ICON_FILE", str(root / "missing-augment-icon.json")),
            patch.object(version_sync, "AUGMENT_MANIFEST_FILE", str(manifest_file)),
            patch.object(version_sync, "VERSION_FILE", str(version_file)),
            patch.object(version_sync, "get_advanced_session", side_effect=AssertionError("普通启动不得查远端版本")),
        ):
            version_sync._last_sync_time = 0
            assert ORIGINAL_SYNC_HERO_DATA() is True

    with patch.object(
        version_sync,
        "get_advanced_session",
        side_effect=AssertionError("Catalog 强制检查也必须由 DataService coordinator 执行"),
    ):
        version_sync._last_sync_time = 0
        assert ORIGINAL_SYNC_HERO_DATA(allow_remote_check=True) is True

def test_hextech_cooldown_allows_forced_permission() -> None:
    fallback_status = {
        "last_result": "fallback",
        "reason": "http_403",
        "blocked_until": datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat(),
    }
    with (
        patch("hextech.infrastructure.sources.hextech.refresh_support.load_scraper_status", return_value=fallback_status),
        patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
        patch.object(hextech_scraper, "get_latest_valid_csv", return_value="valid.csv"),
        patch("hextech.infrastructure.sources.hextech.refresh_support.get_latest_valid_csv", return_value="valid.csv"),
        patch("hextech.infrastructure.sources.hextech.refresh_support.fetch_text", side_effect=AssertionError("冷却期不得发起网络请求")),
    ):
        assert hextech_scraper.main_scraper() is True
        assert hextech_scraper.check_execution_permission(force=True)[0] is True

def test_hextech_source_parser() -> None:
    aug_id_map, truth_dict = _morgana_maps()

    rows = extract_champion_stats(
        _morgana_html(),
        aug_id_map,
        truth_dict,
        "25",
        "堕落天使",
        {"tier": "1", "winRate": 0.5255849975106489, "pickRate": 0.011419658717905307},
    )

    names = [row["海克斯名称"] for row in rows]
    assert names[:3] == ["缩小引擎", "咏叹奏鸣", "祖母的辣椒油"]
    assert "闪电打击" not in names
    assert rows[0]["海克斯ID"] == "1373"
    assert rows[0]["源站排名"] == 1
    assert rows[0]["源站层级"] == "T1"
    assert abs(rows[0]["海克斯胜率"] - 0.5774767146486028) < 1e-12
    assert abs(rows[0]["海克斯出场率"] - 0.06533163688665154) < 1e-12

    html = _morgana_html() + '<script>{"9999":{"win_rate":0.99,"pick_rate":0.99}}</script>'
    noisy_rows = extract_champion_stats(
        html,
        aug_id_map,
        truth_dict,
        "25",
        "堕落天使",
        {"tier": "1", "winRate": 0.52, "pickRate": 0.01},
    )
    assert {row["海克斯ID"] for row in noisy_rows} == {"1373", "1420", "1406", "1058"}

    df = pd.DataFrame(
        [
            {
                "英雄ID": "25",
                "英雄名称": "堕落天使",
                "英雄评级": "T1",
                "英雄胜率": 0.52,
                "英雄出场率": 0.01,
                "海克斯ID": "1",
                "源站排名": 2,
                "源站层级": "T1",
                "海克斯阶级": "黄金",
                "海克斯名称": "高胜率后排",
                "海克斯胜率": 0.9,
                "海克斯出场率": 0.01,
                "胜率差": 0.38,
                "综合得分": 100,
            },
            {
                "英雄ID": "25",
                "英雄名称": "堕落天使",
                "英雄评级": "T1",
                "英雄胜率": 0.52,
                "英雄出场率": 0.01,
                "海克斯ID": "2",
                "源站排名": 1,
                "源站层级": "T1",
                "海克斯阶级": "黄金",
                "海克斯名称": "源站第一",
                "海克斯胜率": 0.5,
                "海克斯出场率": 0.5,
                "胜率差": -0.02,
                "综合得分": -100,
            },
        ]
    )

    result = process_hextechs_data(df, "堕落天使", catalog_lookup={}, use_runtime_cache=False)
    assert result["comprehensive"][0]["海克斯名称"] == "源站第一"
    assert result["top_10_overall"][0]["源站排名"] == 1
    assert result["winrate_only"][0]["海克斯名称"] == "高胜率后排"

    missing_derived_df = df.drop(columns=["胜率差", "综合得分"]).rename(columns={"英雄ID": "英雄 ID"})
    missing_result = process_hextechs_data(
        missing_derived_df,
        "堕落天使",
        catalog_lookup={},
        use_runtime_cache=False,
    )
    assert missing_result["comprehensive"]
    assert missing_result["comprehensive"][0]["海克斯名称"] == "源站第一"
