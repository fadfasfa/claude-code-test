"""scraping 域 pytest 开发门禁。"""

import pytest
import hextech.infrastructure.sources.hextech.refresh_support as hextech_refresh_support

from tests._dev_gate_support import (
    Any,
    ORIGINAL_SYNC_HERO_DATA,
    Path,
    TemporaryDirectory,
    _morgana_html,
    _morgana_maps,
    _write_runtime_csv,
    datetime,
    extract_champion_stats,
    heal_worker,
    hextech_scraper,
    json,
    os,
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


def test_hextech_rate_normalization_accepts_percentages_and_rejects_invalid_values() -> None:
    from hextech.infrastructure.sources.hextech.parsing import _build_row, _normalize_rate

    row = _build_row(
        champ_id="25",
        champ_name="堕落天使",
        champ_data={"tier": "1", "winRate": 53, "pickRate": 10},
        augment_id="1373",
        augment_name="缩小引擎",
        source_rank=1,
        source_tier="1",
        local_tier="黄金",
        winrate=53,
        pickrate=10,
    )

    assert row["英雄胜率"] == pytest.approx(0.53)
    assert row["英雄出场率"] == pytest.approx(0.10)
    assert row["海克斯胜率"] == pytest.approx(0.53)
    assert row["海克斯出场率"] == pytest.approx(0.10)
    for invalid in (-1, float("nan"), float("inf"), 101):
        with pytest.raises(ValueError):
            _normalize_rate(invalid, field_name="fixture_rate")


@pytest.mark.parametrize("failure", (OSError("disk"), KeyError("dataframe"), RuntimeError("publisher")))
def test_hextech_unexpected_failure_writes_diagnostics_and_preserves_last_good(tmp_path: Path, failure: Exception) -> None:
    last_good = tmp_path / "last-good.csv"
    last_good.write_text("stable", encoding="utf-8")
    diagnostic = tmp_path / "failed-run.json"

    def write_failure(reason: str, **kwargs) -> bool:
        diagnostic.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "stage": kwargs["failure_stage"],
                    "samples": kwargs["attempt"]["failure_samples"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return True

    with (
        patch.object(hextech_scraper, "_main_scraper_impl", side_effect=failure),
        patch.object(hextech_scraper, "_finish_refresh_failure", side_effect=write_failure),
        pytest.raises(type(failure)),
    ):
        hextech_scraper.main_scraper(force=True)

    payload = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert payload["reason"] == "unexpected_exception"
    assert payload["stage"] == "unexpected_exception"
    assert payload["samples"][-1]["error_type"] == type(failure).__name__
    assert last_good.read_text(encoding="utf-8") == "stable"

def test_cdragon_force_refresh_semantics() -> None:
    """CDragon minimal manifest 不应被旧完整描述字段规则强制重建。"""
    import hextech.infrastructure.sources.catalog as augment_catalog

    def cdragon_entry(index: int) -> dict[str, Any]:
        return {
            "schema_version": augment_catalog.MANIFEST_SCHEMA_VERSION,
            "name": f"Augment{index}",
            "tier": "Silver",
            "filename": f"augment{index}.png",
            "icon_url": f"/assets/augments/augment{index}.png",
            "source_icon_url": f"{augment_catalog._CDRAGON_SOURCE_PREFIX}game/assets/augments/augment{index}.png",
        }

    minimal_manifest = [cdragon_entry(index) for index in range(augment_catalog._MIN_VALID_MANIFEST_ENTRIES)]
    assert augment_catalog._is_cdragon_minimal_manifest(minimal_manifest)
    assert augment_catalog._is_cdragon_minimal_manifest([cdragon_entry(0)]) is False
    non_cdragon = [
        {**cdragon_entry(index), "source_icon_url": "https://apexlol.info/x.png"}
        for index in range(augment_catalog._MIN_VALID_MANIFEST_ENTRIES)
    ]
    assert augment_catalog._is_cdragon_minimal_manifest(non_cdragon) is False

    calls: dict[str, int] = {"icon_map": 0}

    def fake_load_icon_map(config_dir=None, force_refresh=False):
        calls["icon_map"] += 1
        return {}

    with (
        patch.object(augment_catalog, "_read_manifest_file", return_value=minimal_manifest),
        patch.object(augment_catalog, "load_augment_icon_map", side_effect=fake_load_icon_map),
        patch.object(augment_catalog, "_load_full_map", return_value={}),
        patch.object(augment_catalog, "_fetch_remote_augment_metadata", return_value={}),
        patch.object(augment_catalog, "_write_augment_icon_manifest"),
    ):
        result = augment_catalog.build_augment_icon_manifest(force_refresh=False)
        assert result is minimal_manifest
        assert calls["icon_map"] == 0
        augment_catalog.build_augment_icon_manifest(force_refresh=True)
        assert calls["icon_map"] == 1

def test_cdragon_source_schema_marker() -> None:
    """CDragon 条目优先使用显式 source_schema 标记，旧数据仍按前缀兼容。"""
    import hextech.infrastructure.sources.catalog as augment_catalog

    raw_entry = {
        "schema_version": augment_catalog.MANIFEST_SCHEMA_VERSION,
        "name": "缩小引擎",
        "tier": "棱彩",
        "filename": "shrinkengine.png",
        "icon_url": "/assets/augments/shrinkengine.png",
        "source_icon_url": f"{augment_catalog._CDRAGON_SOURCE_PREFIX}game/assets/augments/shrinkengine.png",
        "source_schema": augment_catalog._CDRAGON_SOURCE_SCHEMA,
    }
    normalized = augment_catalog._normalize_cdragon_manifest_entry(raw_entry)
    assert normalized["source_schema"] == augment_catalog._CDRAGON_SOURCE_SCHEMA

    explicit_only = {**raw_entry, "source_icon_url": "https://apexlol.info/x.png"}
    assert augment_catalog._is_cdragon_source_item(explicit_only) is True

    legacy = {key: value for key, value in raw_entry.items() if key != "source_schema"}
    assert augment_catalog._is_cdragon_source_item(legacy) is True

    foreign = {**raw_entry, "source_schema": "", "source_icon_url": "https://apexlol.info/x.png"}
    assert augment_catalog._is_cdragon_source_item(foreign) is False

def test_heal_worker_contract() -> None:
    assert hasattr(heal_worker, "heal_missing_artifacts")
    assert hasattr(heal_worker, "detect_missing_artifacts")
    with TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
        with (
            patch.object(heal_worker, "LOCK_FILE", Path(temp_dir) / "heal.lock"),
            patch.object(heal_worker, "_write_startup_status"),
            patch.object(heal_worker, "_latest_csv_fresh", return_value=True),
            patch.object(heal_worker, "_core_data_ready", return_value=True),
            patch.object(heal_worker, "_augment_manifest_ready", return_value=True),
            patch.object(heal_worker, "_file_is_fresh", return_value=True),
            patch.object(heal_worker, "_image_assets_ready", return_value=True),
            patch.object(heal_worker, "is_augment_icon_prefetch_ready", return_value=True),
            patch.object(heal_worker, "get_latest_csv", return_value="Hextech_Data_2026-06-30.csv"),
            patch.object(heal_worker, "get_latest_valid_csv", return_value="Hextech_Data_2026-06-30.csv"),
            patch.object(heal_worker, "hextech_refresh_blocked", return_value=False),
            patch.object(heal_worker, "_heal_champion_core", return_value=True),
            patch.object(heal_worker, "_heal_hero_rankings", return_value=True),
            patch.object(heal_worker, "_heal_augment_catalog", return_value=True),
            patch.object(heal_worker, "_heal_images", return_value=True),
        ):
            missing = heal_worker.detect_missing_artifacts()
            assert "synergy_data" not in missing
            report = heal_worker.heal_missing_artifacts(force=True)
            for field in ("requested", "repaired", "failed"):
                assert "synergy_data" not in report[field]

def test_hextech_scraper_fallback_contract() -> None:
    """403 必须在英雄并发前熔断；有本地数据则降级可用。"""

    def fake_result(status_code: int | None, payload: Any = None, text: str = "", error: str = ""):
        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False)
        return scrapling_client.ScraplingFetchResult(
            url="https://example.test",
            text=text,
            status_code=status_code,
            fetched_at="2026-06-23T00:00:00+00:00",
            error=error,
        )

    class SequenceFetcher:
        def __init__(self) -> None:
            self.responses = [
                fake_result(200, {"100": {"displayName": "测试海克斯"}}),
                fake_result(200, [{"championId": "1"}]),
                fake_result(403, {}),
            ]
            self.calls = 0

        def __call__(self, *_args, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fallback_csv = root / "Hextech_Data_2026-06-19.csv"
        status_file = root / "scraper_status.json"
        _write_runtime_csv(fallback_csv, 300)
        fetcher = SequenceFetcher()
        started_at = time.time()

        with (
            patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")),
            patch.object(hextech_scraper, "load_augment_tier_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch("hextech.infrastructure.sources.hextech.refresh_support.fetch_text", side_effect=fetcher),
            patch.object(hextech_scraper, "get_latest_valid_csv", return_value=str(fallback_csv)),
            patch("hextech.infrastructure.sources.hextech.refresh_support.get_latest_valid_csv", return_value=str(fallback_csv)),
            patch("hextech.infrastructure.sources.hextech.refresh_support.build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(
                hextech_scraper.DETAIL_PASS_RUNNER,
                "run",
                side_effect=AssertionError("403 后不得启动英雄详情并发"),
            ),
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper() is True

        status = json.loads(status_file.read_text(encoding="utf-8"))
        assert fetcher.calls == 3
        assert status["last_result"] == "fallback"
        assert status["reason"] == "http_403"
        assert status["active_csv"] == str(fallback_csv)
        blocked_until = datetime.fromisoformat(status["blocked_until"])
        assert 29 * 60 <= blocked_until.timestamp() - started_at <= 31 * 60

        status_file.unlink()
        failed_fetcher = SequenceFetcher()
        with (
            patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")),
            patch.object(hextech_scraper, "load_augment_tier_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch("hextech.infrastructure.sources.hextech.refresh_support.fetch_text", side_effect=failed_fetcher),
            patch.object(hextech_scraper, "get_latest_valid_csv", return_value=None),
            patch("hextech.infrastructure.sources.hextech.refresh_support.get_latest_valid_csv", return_value=None),
            patch("hextech.infrastructure.sources.hextech.refresh_support.build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(
                hextech_scraper.DETAIL_PASS_RUNNER,
                "run",
                side_effect=AssertionError("403 后不得启动英雄详情并发"),
            ),
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper() is False

        failed_status = json.loads(status_file.read_text(encoding="utf-8"))
        assert failed_status["last_result"] == "failed"
        assert failed_status["active_csv"] == ""

def test_hextech_remote_failure_cooldown_and_escalation() -> None:
    """403/429/timeout 暂停 30 分钟；连续 3 次后给出升级诊断而不是盲重试。"""

    started_at = time.time()
    previous = {
        "last_result": "fallback",
        "reason": "timeout",
        "consecutive_remote_failures": 2,
    }
    with TemporaryDirectory() as temp_dir:
        status_file = Path(temp_dir) / "scraper_status.json"
        with (
            patch("hextech.infrastructure.sources.hextech.refresh_support.load_scraper_status", return_value=previous),
            patch("hextech.infrastructure.sources.hextech.refresh_support.build_runtime_state_path", return_value=str(status_file)),
            patch("hextech.infrastructure.sources.hextech.refresh_support.get_latest_valid_csv", return_value="valid.csv"),
        ):
            payload = hextech_refresh_support._write_scraper_status("fallback", "timeout", active_csv="valid.csv")

    blocked_until = datetime.fromisoformat(payload["blocked_until"])
    assert 29 * 60 <= blocked_until.timestamp() - started_at <= 31 * 60
    assert payload["next_retry_at"] == payload["blocked_until"]
    assert payload["consecutive_remote_failures"] == 3
    assert payload["remote_failure_escalated"] is True
    assert "last-good" in payload["bypass_evaluation_hint"]

    mixed_previous = {
        "last_result": "fallback",
        "reason": "http_403",
        "consecutive_remote_failures": 2,
    }
    with TemporaryDirectory() as temp_dir:
        status_file = Path(temp_dir) / "scraper_status.json"
        with (
            patch("hextech.infrastructure.sources.hextech.refresh_support.load_scraper_status", return_value=mixed_previous),
            patch("hextech.infrastructure.sources.hextech.refresh_support.build_runtime_state_path", return_value=str(status_file)),
            patch("hextech.infrastructure.sources.hextech.refresh_support.get_latest_valid_csv", return_value="valid.csv"),
        ):
            mixed_payload = hextech_refresh_support._write_scraper_status("fallback", "http_429", active_csv="valid.csv")

    assert mixed_payload["consecutive_remote_failures"] == 3
    assert mixed_payload["remote_failure_escalated"] is True

def test_hextech_champion_detail_json_extracts_full_rows() -> None:
    """CDN champion-details JSON 是 30 秒级快链路，必须优先于 browser-mode 全量刷新。"""

    payload = {
        "championId": "910",
        "championAugments": [
            [
                "910",
                json.dumps(
                    {
                        "augments": {
                            "1001": {
                                "tier": "1",
                                "rank": "2",
                                "win_rate": "0.61",
                                "pick_rate": "0.02",
                                "total": "3",
                            },
                            "1002": {
                                "tier": "1",
                                "rank": "1",
                                "win_rate": "0.62",
                                "pick_rate": "0.03",
                                "total": "3",
                            },
                            "1003": {
                                "tier": "3",
                                "rank": "3",
                                "win_rate": "0.55",
                                "pick_rate": "0.04",
                                "total": "3",
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
            ]
        ],
    }
    rows = hextech_scraper.extract_champion_detail_json_stats(
        payload,
        {"1001": "慢链路A", "1002": "快链路B", "1003": "快链路C"},
        {"快链路B": "黄金"},
        "910",
        "异画师",
        {"tier": "T2", "winRate": 0.5, "pickRate": 0.1},
        {"1003": "棱彩"},
    )

    assert [row["海克斯ID"] for row in rows] == ["1002", "1001", "1003"]
    assert rows[0]["源站排名"] == 1
    assert rows[0]["海克斯名称"] == "快链路B"
    assert rows[0]["海克斯胜率"] == 0.62
    assert rows[-1]["海克斯阶级"] == "棱彩"

def test_hextech_detail_fetch_prefers_cdn_json_without_browser() -> None:
    """详情抓取先走 CDN JSON；成功时不得触发 browser-mode 兜底。"""

    payload = {
        "championId": "910",
        "championAugments": [
            [
                "910",
                json.dumps(
                    {
                        "augments": {
                            str(1000 + rank): {
                                "tier": "1",
                                "rank": str(rank),
                                "win_rate": str(0.6 - rank / 1000),
                                "pick_rate": str(rank / 10000),
                                "total": "65",
                            }
                            for rank in range(1, 66)
                        }
                    }
                ),
            ]
        ],
    }
    calls = []

    def fake_fetch(url: str, *_args, **_kwargs):
        calls.append(url)
        return scrapling_client.ScraplingFetchResult(
            url=url,
            text=json.dumps(payload),
            status_code=200,
            fetched_at="2026-07-02T00:00:00+00:00",
            error="",
        )

    with (
        patch("hextech.infrastructure.sources.hextech.refresh_support.fetch_text", side_effect=fake_fetch),
        patch.object(hextech_scraper, "fetch_page", side_effect=AssertionError("快链路成功不得使用 browser-mode")),
    ):
        result = hextech_scraper.fetch_champion_detail_stats_fast(
            {"championId": "910", "tier": "T2", "winRate": 0.5, "pickRate": 0.1},
            core_data={"910": {"name": "异画师"}},
            aug_id_map={str(1000 + rank): f"快链路{rank}" for rank in range(1, 66)},
            truth_dict={},
            aug_tier_map={},
            timeout=6,
        )

    assert len(result["rows"]) == 65
    assert "champion-details/910.json" in calls[0]
    assert result["reason"] == ""

def test_scrapling_tls_error_contract() -> None:
    """curl TLS 错误必须被分类为 tls_error，并带上下文向上抛出。"""

    error_text = "curl: (35) TLS connect error: error:00000000:OPENSSL_internal:invalid library (0)"
    assert scrapling_client.classify_fetch_error(error_text) == "tls_error"

    class BadFetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            raise RuntimeError(error_text)

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = BadFetcher
    fetchers_module.DynamicFetcher = BadFetcher
    fetchers_module.StealthyFetcher = BadFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
    ):
        page_result = scrapling_client.fetch_page("https://example.test")
    assert page_result.error_kind == "tls_error"

    result = scrapling_client.ScraplingFetchResult(
        url="https://example.test/detail/1",
        text="",
        status_code=None,
        fetched_at="2026-06-25T00:00:00+00:00",
        error=error_text,
        error_kind="tls_error",
        attempts=2,
    )
    assert hextech_refresh_support._scrapling_failure_reason(result) == ("tls_error", None)

    with patch("hextech.infrastructure.sources.hextech.refresh_support.fetch_text", return_value=result):
        try:
            hextech_scraper.fetch_with_retry(
                "https://example.test/detail/1",
                quiet=True,
                raise_on_failure=True,
                caller="hextech_detail",
                context="championId=1;champion=测试英雄",
            )
        except hextech_scraper.RemoteFetchError as exc:
            assert exc.reason == "tls_error"
            assert exc.url == "https://example.test/detail/1"
            assert exc.context == "championId=1;champion=测试英雄"
        else:
            raise AssertionError("tls_error 必须向上抛出 RemoteFetchError")

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

def test_hextech_cooldown_and_heal_fallback() -> None:
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

    missing = {
        "hextech_rankings": True,
        "augment_catalog": False,
        "champion_core": False,
        "images": False,
        "latest_csv": "valid.csv",
        "augment_icons_prefetched": True,
    }
    with TemporaryDirectory() as temp_dir:
        with (
            patch.object(heal_worker, "LOCK_FILE", Path(temp_dir) / "heal.lock"),
            patch.object(heal_worker, "detect_missing_artifacts", return_value=missing),
            patch.object(heal_worker, "_write_startup_status"),
            patch.object(heal_worker, "_heal_hero_rankings", return_value=True),
            patch.object(heal_worker, "load_scraper_status", return_value=fallback_status),
        ):
            report = heal_worker.heal_missing_artifacts()
        assert report["fallback"] == ["hextech_rankings"]
        assert report["failed"] == []

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
