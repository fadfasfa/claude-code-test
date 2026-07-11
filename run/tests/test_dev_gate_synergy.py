"""synergy 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    FetchedResource,
    Path,
    RUN_DIR,
    SYNERGY_REFRESH_META_VERSION,
    SynergyEntry,
    SynergyExtractor,
    SynergyWriter,
    TemporaryDirectory,
    _augment_map,
    _collision_core_info,
    _core_info,
    _normalize_synergy_items,
    _patch_synergy_dir,
    _snapshot,
    _synergy_item_to_compat_string,
    _validate_publish_size,
    _write_json,
    build_champion_lookup,
    datetime,
    heal_worker,
    icon_resolver,
    json,
    orchestrator,
    os,
    patch,
    runtime_store,
    synergy_scraper,
    time,
    timedelta,
    timezone,
    write_synergy_refresh_meta,
)

pytestmark = pytest.mark.dev_gate

def test_apexlol_hextech_map_size_limit() -> None:
    class OversizeResponse:
        headers = {"Content-Length": str(icon_resolver.MAX_APEXLOL_HEXTECH_MAP_BYTES + 1)}
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b""

        def close(self) -> None:
            return None

    original_cache = icon_resolver._APEXLOL_MAP_CACHE
    try:
        with TemporaryDirectory() as tmp_dir:
            cached = {"cached": "slug"}
            icon_resolver._APEXLOL_MAP_CACHE = ("cached-path", 1.0, cached)
            with patch("hextech.scraping.icon_resolver.requests.get", return_value=OversizeResponse()):
                result = icon_resolver.load_apexlol_hextech_map(config_dir=tmp_dir, force_refresh=True)
            assert result == cached
            assert not (Path(tmp_dir) / "Augment_Apexlol_Map.json").exists()
    finally:
        icon_resolver._APEXLOL_MAP_CACHE = original_cache

def test_apex_source_snapshot_policy() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "apex_snapshot"
        manual = root / "manual"
        manual.mkdir(parents=True)
        (manual / "sample.json").write_text('{"katarina": []}', encoding="utf-8")

        with (
            patch.object(synergy_scraper, "DEFAULT_APEX_SNAPSHOT_DIR", str(root)),
            patch.object(synergy_scraper, "DEFAULT_APEX_MANUAL_SNAPSHOT_DIR", str(manual)),
            patch.dict(os.environ, {"APEX_SNAPSHOT_DIR": ""}),
        ):
            source = synergy_scraper.ApexSource()
            resources = source._load_snapshot_resources()
            source.close()

        assert len(resources) == 1
        assert resources[0].source == "snapshot"
        assert "sample.json" in resources[0].url

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "apex_snapshot"
        manual = root / "manual"
        root.mkdir(parents=True)
        env = {
            "APEX_SNAPSHOT_DIR": "",
            "APEX_SYNERGY_JSON_URL": "",
        }

        with (
            patch.object(synergy_scraper, "DEFAULT_APEX_SNAPSHOT_DIR", str(root)),
            patch.object(synergy_scraper, "DEFAULT_APEX_MANUAL_SNAPSHOT_DIR", str(manual)),
            patch.dict(os.environ, env, clear=True),
        ):
            source = synergy_scraper.ApexSource()
            with (
                patch.object(source, "fetch_configured_json_resource", side_effect=AssertionError("默认不得读取在线 JSON")),
                patch.object(source, "fetch", side_effect=AssertionError("默认不得联网抓取 Apex 页面")),
            ):
                assert source.discover_resources() == []
            source.close()

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "apex_snapshot"
        manual = root / "manual"
        root.mkdir(parents=True)
        env = {
            "APEX_SNAPSHOT_DIR": "",
            "APEX_ALLOW_ONLINE_FETCH": "0",
            "APEX_SYNERGY_JSON_URL": "",
        }

        with (
            patch.object(synergy_scraper, "DEFAULT_APEX_SNAPSHOT_DIR", str(root)),
            patch.object(synergy_scraper, "DEFAULT_APEX_MANUAL_SNAPSHOT_DIR", str(manual)),
            patch.dict(os.environ, env),
        ):
            source = synergy_scraper.ApexSource()
            with (
                patch.object(source, "fetch_configured_json_resource", side_effect=AssertionError),
                patch.object(source, "fetch", side_effect=AssertionError),
            ):
                assert source.discover_resources() == []
            source.close()

    source = synergy_scraper.ApexSource()
    detail_url = source.build_allowed_url("/zh/champions/Vi")
    origin_html = "<html><body>强力联动 作者 评分</body></html>"
    cf_html = "<html><script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>Just a moment</html>"

    def scrapling_result(status_code: int | None, text: str, error: str = "") -> synergy_scraper.ScraplingFetchResult:
        return synergy_scraper.ScraplingFetchResult(
            url=detail_url or source.base_url,
            text=text,
            status_code=status_code,
            fetched_at="2026-06-23T00:00:00+00:00",
            error=error,
        )

    try:
        assert detail_url
        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(200, origin_html)) as fetch_get,
            patch.object(source, "fetch_stealthy", side_effect=AssertionError("origin 页面不应启动 Stealthy")),
            patch.object(source, "fetch_cloakbrowser", side_effect=AssertionError("origin 页面不应启动 CloakBrowser")),
        ):
            fetched = source.fetch(detail_url)
            assert fetched is not None
            assert fetched.source == "scrapling-get"
            fetch_get.assert_called_once()

        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(403, cf_html)),
            patch.object(
                source,
                "fetch_stealthy",
                return_value=FetchedResource(url=detail_url, text=origin_html, source="scrapling-stealthy", status_code=200),
            ) as fetch_stealthy,
            patch.object(source, "fetch_cloakbrowser", side_effect=AssertionError("Stealthy 成功后不应启动 CloakBrowser")),
            patch.dict(os.environ, {"APEX_ALLOW_STEALTHY": "1"}),
        ):
            fetched = source.fetch(detail_url, allow_stealthy=True)
            assert fetched is not None
            assert fetched.source == "scrapling-stealthy"
            fetch_stealthy.assert_called_once_with(detail_url)

        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(403, cf_html)),
            patch.object(source, "fetch_stealthy", side_effect=AssertionError("默认不得启动 Stealthy")),
            patch.object(source, "fetch_cloakbrowser", side_effect=AssertionError("默认不得启动 CloakBrowser")),
            patch.dict(os.environ, {"APEX_ALLOW_STEALTHY": ""}, clear=False),
        ):
            fetched = source.fetch(detail_url)
            assert fetched is not None
            assert fetched.source == "scrapling-get"

        blocked_stealthy = FetchedResource(
            url=detail_url,
            text=cf_html,
            source="scrapling-stealthy",
            status_code=403,
            error="cloudflare_block",
        )
        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(403, cf_html)),
            patch.object(source, "fetch_stealthy", return_value=blocked_stealthy),
            patch.object(
                source,
                "fetch_cloakbrowser",
                return_value=FetchedResource(url=detail_url, text=origin_html, source="cloakbrowser", status_code=200),
            ) as fetch_cloakbrowser,
            patch.dict(os.environ, {"APEX_ALLOW_CLOAKBROWSER": "1"}),
        ):
            fetched = source.fetch(detail_url)
            assert fetched is not None
            assert fetched.source == "cloakbrowser"
            fetch_cloakbrowser.assert_called_once()

        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(403, cf_html)),
            patch.object(source, "fetch_stealthy", return_value=blocked_stealthy),
            patch.object(source, "fetch_cloakbrowser", side_effect=AssertionError("CloakBrowser 已禁用")),
            patch.dict(os.environ, {"APEX_ALLOW_STEALTHY": "1", "APEX_ALLOW_CLOAKBROWSER": "0"}),
        ):
            fetched = source.fetch(detail_url, allow_stealthy=True)
            assert fetched is not None
            assert fetched.source == "scrapling-stealthy"
    finally:
        source.close()

def test_synergy_refresh_freshness() -> None:
    with TemporaryDirectory() as temp_dir:
        _snapshot(temp_dir)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
            assert not orchestrator.auto_synergy_refresh_enabled()
            assert not orchestrator.should_refresh_synergy(False)
            assert "synergy_data" not in heal_worker.detect_missing_artifacts()

    with TemporaryDirectory() as temp_dir:
        synergy_path = _snapshot(temp_dir)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )

            assert not orchestrator.should_refresh_synergy(False)
            assert "synergy_data" not in heal_worker.detect_missing_artifacts()

            meta_path = Path(temp_dir) / "Champion_Synergy_latest.v1.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            assert meta["version"] == SYNERGY_REFRESH_META_VERSION
            assert meta["filename"] == synergy_path.name
            assert meta["non_empty_heroes"] == 1

    with TemporaryDirectory() as temp_dir:
        old_mtime = time.time() - (8 * 24 * 60 * 60)
        synergy_path = _snapshot(temp_dir, mtime=old_mtime)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )
            os.utime(synergy_path, (old_mtime, old_mtime))
            os.utime(Path(temp_dir) / "Champion_Synergy_latest.v1.json", (old_mtime, old_mtime))

            assert not orchestrator.should_refresh_synergy(False)
            assert "synergy_data" not in heal_worker.detect_missing_artifacts()

    blocked_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    status = {"last_result": "blocked", "blocked_until": blocked_until}
    with TemporaryDirectory() as temp_dir:
        old_mtime = time.time() - (8 * 24 * 60 * 60)
        synergy_path = _snapshot(temp_dir, mtime=old_mtime)

        patches = _patch_synergy_dir(temp_dir, status=status)
        with patches[0], patches[1], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )
            os.utime(synergy_path, (old_mtime, old_mtime))
            os.utime(Path(temp_dir) / "Champion_Synergy_latest.v1.json", (old_mtime, old_mtime))

            assert not orchestrator.should_refresh_synergy(False)

    with TemporaryDirectory() as temp_dir:
        _snapshot(temp_dir)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "0"}):
            assert not orchestrator.should_refresh_synergy(True)
            assert "synergy_data" not in heal_worker.detect_missing_artifacts()

    with TemporaryDirectory() as temp_dir:
        status_path = Path(temp_dir) / "synergy_refresh_status.json"
        started_at = time.time()
        with patch.object(orchestrator, "build_synergy_refresh_status_path", return_value=str(status_path)):
            orchestrator._write_synergy_refresh_status("blocked", "cloudflare_block")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        blocked_until = datetime.fromisoformat(status["blocked_until"])
        assert 5.9 * 60 * 60 <= blocked_until.timestamp() - started_at <= 6.1 * 60 * 60

def test_synergy_snapshot_store() -> None:
    with TemporaryDirectory() as temp_dir:
        snapshot = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"1": {}}, 1000)
        _write_json(
            Path(temp_dir) / "Champion_Synergy_latest.v1.json",
            {"version": 1, "filename": snapshot.name, "non_empty_heroes": 1},
            1001,
        )
        cleaned_missing = Path(temp_dir) / "Champion_Synergy_Cleaned.json"

        with (
            patch.object(runtime_store, "_runtime_raw_dirs", return_value=[Path(temp_dir)]),
            patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
            patch.object(runtime_store, "build_synergy_cleaned_data_path", return_value=str(cleaned_missing)),
        ):
            assert runtime_store.get_latest_synergy_snapshot_path() == str(snapshot.resolve())
            assert runtime_store.build_synergy_data_path() == str(snapshot.resolve())

    with TemporaryDirectory() as temp_dir:
        snapshot = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"1": {}}, 1000)
        cleaned = _write_json(Path(temp_dir) / "Champion_Synergy_Cleaned.json", {"cleaned": {}}, 1002)
        _write_json(
            Path(temp_dir) / "Champion_Synergy_latest.v1.json",
            {"version": 1, "filename": snapshot.name, "non_empty_heroes": 1},
            1001,
        )

        with (
            patch.object(runtime_store, "_runtime_raw_dirs", return_value=[Path(temp_dir)]),
            patch.object(runtime_store, "build_synergy_cleaned_data_path", return_value=str(cleaned)),
        ):
            assert runtime_store.build_synergy_data_path() == str(cleaned)

    with TemporaryDirectory() as temp_dir:
        older = _write_json(Path(temp_dir) / "Champion_Synergy_20260518_010101.json", {"1": {}}, 1000)
        newer = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"2": {}}, 2000)
        (Path(temp_dir) / "Champion_Synergy_latest.v1.json").write_text("{bad", encoding="utf-8")
        cleaned_missing = Path(temp_dir) / "Champion_Synergy_Cleaned.json"

        with (
            patch.object(runtime_store, "_runtime_raw_dirs", return_value=[Path(temp_dir)]),
            patch.object(runtime_store, "build_synergy_cleaned_data_path", return_value=str(cleaned_missing)),
        ):
            assert runtime_store.get_latest_synergy_snapshot_path() == str(newer)
            assert runtime_store.get_latest_synergy_snapshot_path() != str(older)

    with TemporaryDirectory() as temp_dir:
        legacy = _write_json(Path(temp_dir) / "Champion_Synergy.json", {"1": {}}, 1000)
        cleaned_missing = Path(temp_dir) / "Champion_Synergy_Cleaned.json"

        with (
            patch.object(runtime_store, "_runtime_raw_dirs", return_value=[Path(temp_dir)]),
            patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
            patch.object(runtime_store, "build_synergy_cleaned_data_path", return_value=str(cleaned_missing)),
        ):
            assert runtime_store.get_latest_synergy_snapshot_path() is None
            assert runtime_store.build_synergy_data_path() == str(legacy)

    try:
        _validate_publish_size(
            {"heroes": 172, "non_empty_heroes": 100, "synergy_entries": 700},
            {"heroes": 172, "non_empty_heroes": 136, "synergy_entries": 876},
        )
    except ValueError as exc:
        assert "协同数据熔断" in str(exc)
    else:
        raise AssertionError("过小协同快照应触发发布熔断")

def test_mayhem_combo_pipeline_contract() -> None:
    from hextech.scraping.synergy.mayhem_combo_scraper import parse_combo_manifest
    import hextech.scraping.synergy.mayhem_merge as mayhem_merge
    import hextech.scraping.synergy.mayhem_refresh as mayhem_refresh
    from tools.clean_mayhem_combos import merge_mayhem_combos

    manifest_items, manifest_rejects, manifest_meta = parse_combo_manifest(
        {
            "pageSize": 1,
            "totalCombos": 2,
            "cards": [
                {
                    "id": 1,
                    "championId": "Vayne",
                    "champName": "暗夜猎手",
                    "augmentId": "fan_the_hammer",
                    "augmentName": "连拨击锤",
                    "tier": "S+",
                    "typeBadges": [{"label": "神级"}],
                    "comboDescription": "每一发弩箭都可以触发 W 和攻击特效。",
                    "comboHref": "/zh-cn/combo/vayne-fan-the-hammer/",
                },
                {
                    "id": 2,
                    "championId": "Brand",
                    "champName": "复仇焰魂",
                    "augmentId": "infernal_conduit",
                    "augmentName": "炼狱导管",
                    "tier": "S",
                    "comboDescription": "技能灼烧不断缩减冷却。",
                    "comboHref": "/zh-cn/combo/brand-infernal-conduit/",
                },
            ],
        },
        "https://arammayhem.com/zh-cn/combo/",
        max_pages=1,
    )
    assert len(manifest_items) == 1
    assert not manifest_rejects
    assert manifest_meta["selected"] == 1
    assert manifest_items[0]["source_url"].endswith("/zh-cn/combo/vayne-fan-the-hammer/")

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        apex_path = root / "Champion_Synergy_20260519_223505.json"
        raw_path = root / "mayhem_combos.raw.json"
        augment_manifest_path = root / "Augment_Icon_Manifest.json"
        core_path = root / "Champion_Core_Data.json"
        output_path = root / "Champion_Synergy_Cleaned.json"

        _write_json(
            core_path,
            {
                "67": {"name": "暗夜猎手", "title": "薇恩", "en_name": "Vayne", "aliases": []},
                "63": {"name": "复仇焰魂", "title": "布兰德", "en_name": "Brand", "aliases": []},
            },
        )
        _write_json(
            augment_manifest_path,
            [
                {
                    "name": "连拨击锤",
                    "tier": "棱彩",
                    "filename": "fanthehammer_small.png",
                    "augment_name_id": "FanTheHammer",
                    "source_icon_path": "/lol-game-data/assets/ASSETS/UX/Cherry/Augments/Icons/FanTheHammer_small.png",
                },
                {
                    "name": "炼狱导管",
                    "tier": "棱彩",
                    "filename": "infernalconduit_small.png",
                    "augment_name_id": "InfernalConduit",
                    "source_icon_path": "/lol-game-data/assets/ASSETS/UX/Cherry/Augments/Icons/InfernalConduit_small.png",
                },
            ],
        )
        _write_json(
            apex_path,
            {
                "67": {
                    "id": "67",
                    "name": "暗夜猎手",
                    "title": "薇恩",
                    "en_name": "Vayne",
                    "aliases": [],
                    "synergies": [],
                    "synergy_items": [
                        {
                            "augment_names": ["连拨击锤"],
                            "tier": "棱彩",
                            "rating": "S",
                            "tag": "强力联动",
                            "author": "ApexLoL",
                            "content": "Apex 已有同组合。",
                        }
                    ],
                },
                "63": {
                    "id": "63",
                    "name": "复仇焰魂",
                    "title": "布兰德",
                    "en_name": "Brand",
                    "aliases": [],
                    "synergies": [],
                    "synergy_items": [],
                },
            },
        )
        _write_json(
            raw_path,
            {
                "schema_version": 1,
                "items": [
                    {
                        "champion": "暗夜猎手",
                        "champion_id": "Vayne",
                        "augment_names": ["Fan The Hammer"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "S+",
                        "body": "重复组合不应加入。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/vayne-fan-the-hammer/",
                    },
                    {
                        "champion": "复仇焰魂",
                        "champion_id": "Brand",
                        "augment_names": ["Infernal Conduit"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "S+",
                        "body": "技能灼烧不断缩减冷却。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/brand-infernal-conduit/",
                    },
                    {
                        "champion": "复仇焰魂",
                        "champion_id": "Brand",
                        "augment_names": ["Infernal Conduit"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "S",
                        "body": "Retired in live Mayhem。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/brand-retired/",
                    },
                    {
                        "champion": "复仇焰魂",
                        "champion_id": "Brand",
                        "augment_names": ["Infernal Conduit"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "A",
                        "body": "依赖旧 Trait / Augment Sets 的组合。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/brand-trait/",
                    },
                ],
                "rejects": [],
            },
        )

        summary = merge_mayhem_combos(
            apex_path=apex_path,
            mayhem_raw_path=raw_path,
            augment_manifest_path=augment_manifest_path,
            core_data_path=core_path,
            output_path=output_path,
        )

        assert summary["mayhem_raw_items"] == 4
        assert summary["mayhem_valid_items"] == 2
        assert summary["added_items"] == 1
        assert summary["skipped_duplicate_items"] == 1
        assert summary["clean_reject_items"] == 2
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        brand_items = payload["63"]["synergy_items"]
        assert brand_items[0]["augment_names"] == ["炼狱导管"]
        assert brand_items[0]["source"] == "arammayhem"
        assert brand_items[0]["source_rating"] == "S+"

        sentinel = {"sentinel": True}
        output_path.write_text(json.dumps(sentinel, ensure_ascii=False), encoding="utf-8")
        _write_json(raw_path, {"schema_version": 1, "items": [], "rejects": [{"reason": "empty"}]})
        empty_summary = merge_mayhem_combos(
            apex_path=apex_path,
            mayhem_raw_path=raw_path,
            augment_manifest_path=augment_manifest_path,
            core_data_path=core_path,
            output_path=output_path,
        )
        assert empty_summary["written"] is False
        assert json.loads(output_path.read_text(encoding="utf-8")) == sentinel

        raw_latest = root / "Champion_Synergy.raw-latest.json"
        cleaned_base = root / "Champion_Synergy_Cleaned.current.json"
        _write_json(
            raw_latest,
            {
                "63": {
                    "id": "63",
                    "name": "复仇焰魂",
                    "title": "布兰德",
                    "en_name": "Brand",
                    "aliases": [],
                    "synergies": [],
                    "synergy_items": [],
                },
            },
        )
        _write_json(
            cleaned_base,
            {
                "67": {
                    "id": "67",
                    "name": "暗夜猎手",
                    "title": "薇恩",
                    "en_name": "Vayne",
                    "aliases": [],
                    "synergies": [],
                    "synergy_items": [
                        {
                            "augment_names": ["连拨击锤"],
                            "tier": "棱彩",
                            "rating": "S",
                            "tag": "强力联动",
                            "author": "ApexLoL",
                            "content": "旧 cleaned 不应覆盖 raw latest。",
                        }
                    ],
                }
            },
        )
        with (
            patch.object(mayhem_merge, "build_raw_synergy_data_path", return_value=str(raw_latest)),
            patch.object(mayhem_merge, "build_synergy_cleaned_data_path", return_value=str(cleaned_base)),
        ):
            raw_first_summary = merge_mayhem_combos(
                mayhem_raw_path=raw_path,
                augment_manifest_path=augment_manifest_path,
                core_data_path=core_path,
                write_output=False,
            )
        assert raw_first_summary["base_mode"] == "raw_latest"
        assert raw_first_summary["apex_path"] == str(raw_latest)

        cleaned_base = root / "Champion_Synergy_Cleaned.current.json"
        _write_json(
            cleaned_base,
            {
                "63": {
                    "id": "63",
                    "name": "复仇焰魂",
                    "title": "布兰德",
                    "en_name": "Brand",
                    "aliases": [],
                    "synergies": [
                        "炼狱导管 | 棱彩 | 作者: ARAMMayhem | 旧 Mayhem 组合。",
                        "普通备注：可参考 ARAMMayhem 网站的公开讨论。",
                    ],
                    "synergy_items": [
                        {
                            "augment_names": ["炼狱导管"],
                            "tier": "棱彩",
                            "rating": "A",
                            "tag": "强力联动",
                            "author": "ARAMMayhem",
                            "is_original": False,
                            "content": "旧 Mayhem 组合。",
                            "upvotes": 0,
                            "downvotes": 0,
                            "source": "arammayhem",
                        }
                    ],
                },
                "64": {
                    "id": "64",
                    "name": "永恒梦魇",
                    "synergies": [
                        "强化组合 | 金色 | 作者: ARAMMayhem | 历史兼容项。",
                        "普通备注：可参考 ARAMMayhem 网站的公开讨论。",
                    ],
                },
            },
        )
        _write_json(
            raw_path,
            {
                "schema_version": 1,
                "items": [
                    {
                        "champion": "复仇焰魂",
                        "champion_id": "Brand",
                        "augment_names": ["Infernal Conduit"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "S+",
                        "body": "新版 Mayhem 组合。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/brand-new/",
                    },
                ],
                "rejects": [],
            },
        )
        with (
            patch.object(mayhem_merge, "build_synergy_cleaned_data_path", return_value=str(cleaned_base)),
            patch.object(mayhem_merge, "build_raw_synergy_data_path", return_value=str(root / "missing_legacy.json")),
        ):
            cleaned_summary = merge_mayhem_combos(
                mayhem_raw_path=raw_path,
                augment_manifest_path=augment_manifest_path,
                core_data_path=core_path,
            )
        assert cleaned_summary["base_mode"] == "cleaned_without_mayhem"
        assert cleaned_summary["removed_existing_mayhem_items"] == 1
        assert cleaned_summary["added_items"] == 1
        refreshed = json.loads(cleaned_base.read_text(encoding="utf-8"))
        refreshed_items = refreshed["63"]["synergy_items"]
        assert len(refreshed_items) == 1
        assert refreshed_items[0]["content"] == "新版 Mayhem 组合。"
        assert len(refreshed["63"]["synergies"]) == 2
        assert all("旧 Mayhem" not in item for item in refreshed["63"]["synergies"])
        assert "普通备注：可参考 ARAMMayhem 网站的公开讨论。" in refreshed["63"]["synergies"]
        assert refreshed["64"]["synergies"] == ["普通备注：可参考 ARAMMayhem 网站的公开讨论。"]

        status_path = root / "mayhem_refresh_status.json"
        raw_cache_path = root / "runtime_cache_mayhem.raw.json"
        now = 1_800_000_000.0
        with (
            patch.object(mayhem_refresh, "get_mayhem_refresh_status_path", return_value=str(status_path)),
            patch.object(mayhem_refresh, "get_mayhem_raw_cache_path", return_value=str(raw_cache_path)),
        ):
            mayhem_refresh.write_mayhem_refresh_status(result="success", now=now)
            skipped = mayhem_refresh.run_mayhem_refresh(
                now=now + 60,
                scraper=lambda: (_ for _ in ()).throw(AssertionError("未 stale 不应抓取 Mayhem")),
            )
            assert skipped["last_result"] == "skipped"
            assert skipped["reason"] == "not_stale"

            mayhem_refresh.write_mayhem_refresh_status(result="success", reason="old", now=now - 4 * 24 * 60 * 60)
            rebuilt: list[bool] = []
            success = mayhem_refresh.run_mayhem_refresh(
                now=now,
                scraper=lambda: {"schema_version": 1, "items": [{"ok": True}], "rejects": []},
                merge=lambda **_kwargs: {"written": True, "added_items": 3},
                rebuild_hint_cache=lambda: rebuilt.append(True),
            )
            assert success["last_result"] == "success"
            assert success["raw_items"] == 1
            assert success["added_items"] == 3
            assert rebuilt == [True]
            assert json.loads(raw_cache_path.read_text(encoding="utf-8"))["items"]

            failed = mayhem_refresh.run_mayhem_refresh(
                force=True,
                now=now + 1,
                scraper=lambda: {"schema_version": 1, "items": [], "rejects": [{"reason": "empty"}]},
                merge=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("空 raw 不应清洗发布")),
            )
            assert failed["last_result"] == "failed"
            assert failed["reason"] == "raw_empty"

def test_synergy_structured_payloads() -> None:
    payload = {
        "55": {
            "synergy_items": [
                {
                    "augment_names": ["利刃华尔兹"],
                    "tier": "黄金",
                    "rating": "A",
                    "tag": "强力联动",
                    "author": "ApexLoL",
                    "content": "卡特琳娜 R 可以触发这条联动。",
                }
            ]
        }
    }
    extractor = SynergyExtractor(
        champion_lookup=build_champion_lookup(_core_info()),
        augment_name_map=_augment_map(),
    )

    result = extractor.extract([
        FetchedResource(
            url="https://apexlol.info/zh/snapshot/data.json",
            text=json.dumps(payload, ensure_ascii=False),
            source="test",
        )
    ])

    assert "katarina" in result
    assert result["katarina"][0].augment_names == ["利刃华尔兹"]
    assert result["katarina"][0].tier == "黄金"

    entry = SynergyEntry(
        champion_slug="katarina",
        augment_names=["利刃华尔兹"],
        tier="黄金",
        rating="A",
        tag="强力联动",
        author="ApexLoL",
        is_original=True,
        content="卡特琳娜 R 可以触发这条联动。",
        upvotes=3,
        downvotes=1,
    )

    writer_payload = SynergyWriter(_core_info()).build_payload({"katarina": [entry]})
    assert writer_payload["55"]["synergy_items"][0]["augment_names"] == ["利刃华尔兹"]
    assert "利刃华尔兹 | 黄金 | 评分 A" in writer_payload["55"]["synergies"][0]

    legacy = "利刃华尔兹 | 黄金 | 评分 A | 强力联动 | A | B站晴转小雨Yy_ | 原创 | 卡特琳娜 R 可以触发这条联动。"
    items = _normalize_synergy_items([], [legacy])
    assert items[0]["augment_names"] == ["利刃华尔兹"]
    assert items[0]["rating"] == "A"
    assert items[0]["is_original"]
    assert _synergy_item_to_compat_string(items[0]).split(" | ")[4:6] == ["0", "0"]

    html_extractor = SynergyExtractor(
        champion_lookup=build_champion_lookup(_core_info()),
        augment_name_map=_augment_map(),
    )
    html = """
    <html><body>
    <div>利刃华尔兹</div>
    <div>黄金</div>
    <div>D 级</div>
    <div>陷阱</div>
    <div>0</div>
    <div>0</div>
    <div>作者</div>
    <div>ApexLoL</div>
    <p>卡特琳娜 R 在这个组合里会卡手。</p>
    </body></html>
    """

    parsed = html_extractor.extract([
        FetchedResource(
            url="https://apexlol.info/zh/champions/Katarina",
            text=html,
            source="test",
        )
    ])

    assert parsed["katarina"][0].rating == "D"
    assert parsed["katarina"][0].tag == "陷阱"

def test_synergy_alias_collision_guard() -> None:
    def entry(slug: str, content: str) -> SynergyEntry:
        return SynergyEntry(
            champion_slug=slug,
            augment_names=[content],
            tier="黄金",
            rating="A",
            tag="强力联动",
            author="ApexLoL",
            is_original=True,
            content=content,
            upvotes=0,
            downvotes=0,
        )

    writer = SynergyWriter(_collision_core_info())
    payload = writer.build_payload(
        {
            "vi": [entry("vi", "蔚专属联动")],
            "viego": [entry("viego", "佛耶戈专属联动")],
            "viktor": [entry("viktor", "维克托专属联动")],
        }
    )

    assert payload["254"]["synergy_items"][0]["content"] == "蔚专属联动"
    assert payload["234"]["synergy_items"][0]["content"] == "佛耶戈专属联动"
    assert payload["112"]["synergy_items"][0]["content"] == "维克托专属联动"

    payload_without_viktor = writer.build_payload({"vi": [entry("vi", "蔚专属联动")]})
    assert payload_without_viktor["112"]["synergy_items"] == []

def test_synergy_playwright_calibrator_contract() -> None:
    tool_path = RUN_DIR / "tools" / "calibrate_synergy_playwright.py"
    text = tool_path.read_text(encoding="utf-8")
    assert "sync_playwright" in text
    assert "只访问本地 Hextech Web/API" in text
    assert "apexlol.info" not in text.lower()
    assert "build_synergy_data_path" in text
    assert "api_quarantined" in text
    assert "if duplicate_with else []" in text
