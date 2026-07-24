"""overlay 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    Any,
    Path,
    RUN_DIR,
    TemporaryDirectory,
    hextech_scraper,
    json,
    load_augment_manifest_entries,
    patch,
    pd,
    runtime_store,
    time,
)

pytestmark = [pytest.mark.dev_gate, pytest.mark.overlay]

def test_overlay_performance_probe_contract() -> None:
    """验证游戏内显示性能记录结构可用于阶段 5 手动验收。"""
    import tooling.acceptance.overlay_performance_probe as overlay_performance_probe

    sample = overlay_performance_probe.build_overlay_performance_report(
        service_samples={
            "all_off": {"rss_mb": 90.0, "cpu_percent": 0.2},
            "web_only": {"rss_mb": 145.0, "cpu_percent": 1.4},
            "game_overlay_only": {"rss_mb": 130.0, "cpu_percent": 2.0},
            "web_and_overlay": {"rss_mb": 185.0, "cpu_percent": 3.2},
        },
        latency_samples_ms=[120.0, 240.0, 510.0],
        source_tag="dev-check",
    )
    assert sample["source"]["tag"] == "dev-check"
    assert set(sample["service_states"]) == {
        "all_off",
        "web_only",
        "game_overlay_only",
        "web_and_overlay",
    }
    assert sample["latency"]["p50_ms"] == 240.0
    assert sample["latency"]["p95_ms"] == 510.0
    assert sample["targets"]["recognition_p95_ms"] == 300.0
    assert sample["targets"]["overlay_p95_ms"] == 500.0
    assert sample["manual_acceptance_required"] is True

    module_text = (RUN_DIR / "tooling" / "acceptance" / "overlay_performance_probe.py").read_text(encoding="utf-8").lower()
    assert "requests" not in module_text
    assert "data" + "/runtime" not in module_text

def test_game_overlay_documentation_contract() -> None:
    """验证当前系统设计入口、正式路线和启动降级口径。"""

    readme_text = (RUN_DIR / "README.md").read_text(encoding="utf-8")
    project_text = (RUN_DIR / "docs" / "system-design.md").read_text(encoding="utf-8")
    docs_index_text = (RUN_DIR / "docs" / "README.md").read_text(encoding="utf-8")
    design_text = (RUN_DIR / "docs" / "system-design.md").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe -m hextech.infrastructure.vision.sidecar --once --preset auto --write-event" in readme_text
    assert ".venv\\Scripts\\python.exe -m hextech.infrastructure.vision.sidecar --loop --preset auto --write-event" in readme_text
    assert "# Hextech 伴生系统设计" in project_text
    assert "src/hextech" in project_text
    assert "var/snapshots" in project_text
    assert "DataService" in project_text
    assert "system-design.md" in docs_index_text
    assert "data-layout.md" in docs_index_text
    assert "generation" in design_text

def test_overlay_hint_cache_contract() -> None:
    """验证 overlay hint cache 可直接查询，且默认不暴露私用统计字段。"""
    import hextech.modules.recommendation.hints as overlay_hint_cache
    import hextech.modules.vision.events as overlay_event_channel
    import hextech.interfaces.overlay.renderer as overlay_renderer
    import hextech.modules.data.catalog.precomputed_cache as precomputed_cache
    import hextech.infrastructure.sources.catalog as augment_catalog

    host_parser = __import__("hextech.interfaces.overlay.host", fromlist=["build_parser"]).build_parser()
    acceptance_args = host_parser.parse_args(["--acceptance-screenshot", "overlay.png"])
    assert acceptance_args.acceptance_screenshot == Path("overlay.png")

    sample_payload = {
        "德玛西亚之力": {
            "comprehensive": [
                {
                    "英雄 ID": "86",
                    "英雄名称": "德玛西亚之力",
                    "海克斯ID": "augment_001",
                    "海克斯名称": "珠光护手",
                    "海克斯阶级": "Gold",
                    "tooltip_plain": "技能可以暴击。",
                    "源站排名": 2,
                    "综合得分": 1.25,
                    "海克斯胜率": 0.551,
                    "海克斯出场率": 0.082,
                }
            ]
        },
        "时间刺客": {
            "comprehensive": [
                {
                    "英雄 ID": "245",
                    "英雄名称": "时间刺客",
                    "海克斯ID": "augment_001",
                    "海克斯名称": "珠光护手",
                    "海克斯阶级": "Gold",
                    "tooltip_plain": "技能可以暴击。",
                    "源站排名": 8,
                    "综合得分": 0.84,
                    "海克斯胜率": 0.612,
                    "海克斯出场率": 0.044,
                }
            ]
        }
    }
    sample_synergy = {
        "珠光：护手": [
            {
                "hero_id": "266",
                "hero_name": "暗裔剑魔",
                "rating": "S",
                "tag": "强力联动",
                "tier": "棱彩",
                "content": "伤害爆炸",
                "augment_names": ["珠光护手"],
            }
        ]
    }

    public_cache = overlay_hint_cache.build_overlay_hint_cache(
        sample_payload,
        include_private_stats=False,
        source_tag="dev-check",
        synergy_by_name=sample_synergy,
    )
    public_hint = overlay_hint_cache.query_overlay_hint(public_cache, "augment_001")

    assert public_cache["schema_version"] == 1
    assert public_cache["source"]["tag"] == "dev-check"
    assert public_hint["ok"] is True
    assert public_hint["hint"]["name"] == "珠光护手"
    assert public_hint["hint"]["summary"] == "技能可以暴击。"
    # public 缓存严禁泄露私用统计字段
    for blocked_field in ("winrate", "pickrate", "rank", "score", "stats_by_champion_id", "stats_by_champion_name"):
        assert blocked_field not in public_hint["hint"], blocked_field
    # synergy 与私用统计无关，公共缓存也按 augment 名命中
    assert public_hint["hint"].get("synergies"), "公共缓存应保留按名命中的 synergy"
    assert public_hint["hint"]["synergies"][0]["hero_name"] == "暗裔剑魔"
    assert overlay_hint_cache.query_overlay_hint(public_cache, "珠光：护手")["ok"] is True
    normalized_event = overlay_event_channel.build_overlay_event(
        [{"slot": 0, "name": "珠光：护手", "state": "ready"}],
        hint_cache=public_cache,
    )
    assert normalized_event["slots"][0]["augment_id"] == "augment_001"
    render_model = overlay_renderer.build_render_model(normalized_event, hint_cache=public_cache, context=None)
    assert render_model["stats"][0]["name"] == "珠光护手"

    private_cache = overlay_hint_cache.build_overlay_hint_cache(
        sample_payload,
        include_private_stats=True,
        source_tag="dev-check",
        synergy_by_name=sample_synergy,
    )
    private_hint = overlay_hint_cache.query_overlay_hint(private_cache, "augment_001")
    assert private_hint["hint"]["winrate"] == 0.551
    assert private_hint["hint"]["pickrate"] == 0.082
    assert private_hint["hint"]["rank"] == 2
    assert private_hint["hint"]["score"] == 1.25
    assert private_hint["hint"]["source_heroes"] == ["德玛西亚之力", "时间刺客"]
    assert private_hint["hint"]["stats_by_champion_id"]["86"]["winrate"] == 0.551
    assert private_hint["hint"]["stats_by_champion_id"]["245"]["winrate"] == 0.612
    assert private_hint["hint"]["stats_by_champion_name"]["德玛西亚之力"]["pickrate"] == 0.082
    assert private_hint["hint"]["stats_by_champion_name"]["时间刺客"]["pickrate"] == 0.044
    assert private_hint["hint"]["synergies"][0]["augment_names"] == ["珠光护手"]

    # 没有 synergy 命中的 augment 不应出现 synergies 字段，避免 overlay 误判
    no_synergy_cache = overlay_hint_cache.build_overlay_hint_cache(
        sample_payload,
        include_private_stats=False,
        source_tag="dev-check",
        synergy_by_name={},
    )
    no_synergy_hint = overlay_hint_cache.query_overlay_hint(no_synergy_cache, "augment_001")
    assert "synergies" not in no_synergy_hint["hint"]

    missing = overlay_hint_cache.query_overlay_hint({}, "augment_404")
    assert missing == {"ok": False, "error": "cache_missing", "augment_id": "augment_404"}
    expired_cache = dict(public_cache)
    expired_cache["generated_at"] = time.time() - overlay_hint_cache.CACHE_MAX_AGE_SECONDS - 1
    expired = overlay_hint_cache.query_overlay_hint(expired_cache, "augment_001")
    assert expired == {"ok": False, "error": "cache_expired", "augment_id": "augment_001"}

    with TemporaryDirectory() as tmp_dir:
        missing_path = Path(tmp_dir) / "missing.json"
        damaged_path = Path(tmp_dir) / "damaged.json"
        damaged_path.write_text("{bad-json", encoding="utf-8")
        missing_payload = overlay_hint_cache.load_overlay_hint_cache(missing_path)
        damaged_payload = overlay_hint_cache.load_overlay_hint_cache(damaged_path)
        assert overlay_hint_cache.query_overlay_hint(missing_payload, "augment_404")["error"] == "cache_missing"
        assert overlay_hint_cache.query_overlay_hint(damaged_payload, "augment_404")["error"] == "cache_damaged"

    # synergy 加载器也只读本地快照，缺失/损坏时静默给空 dict，不抛异常
    with TemporaryDirectory() as tmp_dir:
        good_path = Path(tmp_dir) / "syn.json"
        good_path.write_text(
            json.dumps(
                {
                    "266": {
                        "name": "暗裔剑魔",
                        "synergy_items": [
                            {
                                "augment_names": ["珠光护手"],
                                "tier": "棱彩",
                                "rating": "S",
                                "tag": "强力联动",
                                "content": "伤害爆炸",
                            }
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        index = overlay_hint_cache._load_synergy_by_augment_name(good_path)
        assert "珠光护手" in index and index["珠光护手"][0]["hero_name"] == "暗裔剑魔"
        with (
            patch.object(overlay_hint_cache, "build_synergy_data_path", return_value=str(good_path)),
            patch.object(
                overlay_hint_cache,
                "get_latest_synergy_snapshot_path",
                side_effect=AssertionError("cleaned 默认路径可用时不应回退 raw latest"),
            ),
        ):
            default_index = overlay_hint_cache._load_synergy_by_augment_name()
        assert "珠光护手" in default_index

        damaged_syn = Path(tmp_dir) / "bad.json"
        damaged_syn.write_text("not-json", encoding="utf-8")
        assert overlay_hint_cache._load_synergy_by_augment_name(damaged_syn) == {}
        assert overlay_hint_cache._load_synergy_by_augment_name(Path(tmp_dir) / "missing.json") == {}

    assert not hasattr(hextech_scraper, "rebuild_runtime_caches")

    module_text = (RUN_DIR / "src" / "hextech" / "modules" / "recommendation" / "hints.py").read_text(encoding="utf-8")
    assert "requests" not in module_text
    assert "full_hextech_scraper" not in module_text

    # Overlay 启动只允许读取稳定清单；不得因 freshness 检查改写稳定版本数据。
    with TemporaryDirectory() as tmp_dir:
        manifest_path = Path(tmp_dir) / "Augment_Icon_Manifest.json"
        manifest_path.write_text(
            json.dumps(
                [
                    {
                        "schema_version": 2,
                        "name": "测试海克斯",
                        "tier": "黄金",
                        "filename": "test_small.png",
                        "local_path": "assets/test_small.png",
                        "icon_url": "/assets/augments/test_small.png",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        before = manifest_path.read_bytes()
        lookup = augment_catalog.load_augment_catalog_lookup_read_only(tmp_dir)
        assert lookup["测试海克斯"]["icon_url"] == "/assets/augments/test_small.png"
        assert manifest_path.read_bytes() == before

        debug_manifest_path = Path(tmp_dir) / "runtime" / "debug" / "augment_catalog" / "Augment_Icon_Manifest.debug.json"
        with (
            patch.object(augment_catalog, "AUGMENT_ICON_MANIFEST_FILE", str(manifest_path)),
            patch.object(
                augment_catalog,
                "AUGMENT_ICON_DEBUG_MANIFEST_FILE",
                str(debug_manifest_path),
                create=True,
            ),
        ):
            augment_catalog._write_augment_icon_manifest(
                [{"schema_version": 2, "name": "调试海克斯", "filename": "debug_small.png"}]
            )
        assert manifest_path.read_bytes() == before
        assert json.loads(debug_manifest_path.read_text(encoding="utf-8"))[0]["name"] == "调试海克斯"

        def valid_manifest(prefix: str) -> list[dict[str, Any]]:
            return [
                {
                    "schema_version": 2,
                    "name": f"{prefix}海克斯{i}",
                    "tier": "黄金",
                    "filename": f"{prefix.lower()}_{i}.png",
                    "local_path": f"assets/{prefix.lower()}_{i}.png",
                    "icon_url": f"/assets/augments/{prefix.lower()}_{i}.png",
                    "description": "说明",
                    "tooltip": "说明",
                    "tooltip_plain": "说明",
                    "spell_values": {},
                    "status": "ready",
                    "updated_at": "2026-01-01T00:00:00+0000",
                }
                for i in range(55)
            ]

        manifest_path.write_text(json.dumps(valid_manifest("Static"), ensure_ascii=False), encoding="utf-8")
        runtime_manifest_path = Path(tmp_dir) / "Augment_Icon_Manifest.debug.json"
        runtime_manifest_path.write_text(
            json.dumps(valid_manifest("Runtime"), ensure_ascii=False),
            encoding="utf-8",
        )
        with (
            patch.object(augment_catalog, "_manifest_is_stale", return_value=False),
            patch.object(augment_catalog, "_AUGMENT_ICON_MANIFEST_CACHE", ("", 0.0, [])),
            patch.object(augment_catalog, "_AUGMENT_LOOKUP_CACHE", ("", 0.0, {})),
        ):
            manifest = augment_catalog.load_augment_icon_manifest(config_dir=tmp_dir)
            lookup = augment_catalog.build_augment_catalog_lookup(config_dir=tmp_dir)
        assert manifest[0]["name"] == "Runtime海克斯0"
        assert "Runtime海克斯0" in lookup and "Static海克斯0" not in lookup

        with patch.object(runtime_store, "get_runtime_root_dir", return_value=Path(tmp_dir) / "runtime-root"):
            resolved_debug_path = Path(
                runtime_store.build_runtime_debug_path("augment_catalog/manifest.json")
            )
            assert resolved_debug_path == Path(tmp_dir) / "runtime-root" / "reports" / "augment_catalog" / "manifest.json"
            try:
                runtime_store.build_runtime_debug_path("../escaped.json")
            except ValueError:
                pass
            else:
                raise AssertionError("runtime debug 路径不得逃逸 debug 根目录")

    stable_manifest = load_augment_manifest_entries()
    assert stable_manifest
    assert all(
        not Path(str(item.get("local_path") or "")).is_absolute()
        for item in stable_manifest
        if isinstance(item, dict)
    )

    latest_df = pd.DataFrame(
        [
            {
                "英雄 ID": "432",
                "英雄名称": "星界游神",
                "英雄评级": 1,
                "英雄胜率": 0.51,
                "英雄出场率": 0.02,
                "海克斯ID": "1314",
                "源站排名": 1,
                "源站层级": "T1",
                "海克斯阶级": "Gold",
                "海克斯名称": "自然即是治愈",
                "海克斯胜率": 0.613,
                "海克斯出场率": 0.041,
                "胜率差": 0.08,
                "综合得分": 2.1,
            },
            {
                "英雄 ID": float("nan"),
                "英雄名称": "缺失ID英雄",
                "英雄评级": 1,
                "英雄胜率": 0.49,
                "英雄出场率": 0.01,
                "海克斯ID": "nan-id",
                "源站排名": 2,
                "源站层级": "T2",
                "海克斯阶级": "Gold",
                "海克斯名称": "缺失ID海克斯",
                "海克斯胜率": 0.502,
                "海克斯出场率": 0.012,
                "胜率差": 0.01,
                "综合得分": 1.1,
            },
            {
                "英雄 ID": "999",
                "英雄名称": float("nan"),
                "英雄评级": 1,
                "英雄胜率": 0.5,
                "英雄出场率": 0.02,
                "海克斯ID": "nan-hero",
                "源站排名": 3,
                "源站层级": "T3",
                "海克斯阶级": "Gold",
                "海克斯名称": "污染英雄名",
                "海克斯胜率": 0.5,
                "海克斯出场率": 0.02,
                "胜率差": 0.0,
                "综合得分": 0.5,
            },
            {
                "英雄 ID": "998",
                "英雄名称": "污染海克斯名",
                "英雄评级": 1,
                "英雄胜率": 0.5,
                "英雄出场率": 0.02,
                "海克斯ID": "nan-augment",
                "源站排名": 4,
                "源站层级": "T3",
                "海克斯阶级": "Gold",
                "海克斯名称": float("nan"),
                "海克斯胜率": 0.5,
                "海克斯出场率": 0.02,
                "胜率差": 0.0,
                "综合得分": 0.5,
            },
        ]
    )
    with (
        patch.object(runtime_store, "get_latest_csv", return_value=str(RUN_DIR / "data" / "raw" / "hextech" / "Hextech_Data_2099-01-01.csv")),
        patch.object(runtime_store, "load_runtime_csv", return_value=latest_df),
        patch("hextech.modules.data.catalog.augment_lookup.load_augment_catalog_lookup_read_only", return_value={}),
    ):
        latest_cache = overlay_hint_cache.build_overlay_hint_cache_from_precomputed(
            include_private_stats=True,
            source_tag="dev-check",
        )
    latest_hint = overlay_hint_cache.query_overlay_hint(latest_cache, "自然即是治愈")
    assert latest_hint["ok"] is True
    assert latest_hint["hint"]["winrate"] == 0.613
    assert latest_hint["hint"]["stats_by_champion_name"]["星界游神"]["pickrate"] == 0.041
    assert latest_cache["source"]["data_source"] == "runtime-csv"
    assert latest_cache["source"]["runtime_csv"] == "Hextech_Data_2099-01-01.csv"
    nan_id_hint = overlay_hint_cache.query_overlay_hint(latest_cache, "缺失ID海克斯")
    assert nan_id_hint["ok"] is True
    assert "nan" not in nan_id_hint["hint"].get("stats_by_champion_id", {})
    assert latest_cache["source"]["hero_count"] == 2
    assert overlay_hint_cache.query_overlay_hint(latest_cache, "污染英雄名")["ok"] is False
    assert overlay_hint_cache.query_overlay_hint(latest_cache, "nan-augment")["ok"] is False

    manifest_gap_df = pd.DataFrame(
        [
            {
                "英雄 ID": "266",
                "英雄名称": "暗裔剑魔",
                "海克斯ID": "1322",
                "海克斯名称": "罪恶快感",
                "海克斯阶级": "Gold",
                "海克斯胜率": 0.51,
                "海克斯出场率": 0.07,
                "源站排名": 1,
                "综合得分": 1.0,
            }
        ]
    )
    with (
        patch.object(runtime_store, "get_latest_csv", return_value=str(RUN_DIR / "data" / "raw" / "hextech" / "Hextech_Data_2099-01-02.csv")),
        patch.object(runtime_store, "load_runtime_csv", return_value=manifest_gap_df),
        patch(
            "hextech.modules.data.catalog.augment_lookup.load_augment_catalog_lookup_read_only",
            return_value={
                "冰雪爆裂": {
                    "name": "冰雪爆裂",
                    "tier": "黄金",
                    "cdragon_id": 2080,
                    "augment_name_id": "Snowbomb",
                    "icon_url": "/assets/augments/snowbomb_small.png",
                    "tooltip_plain": "在目标位置引爆雪球。",
                },
                "占位强化 A": {
                    "name": "占位强化 A",
                    "tier": "白银",
                    "cdragon_id": -1,
                    "augment_name_id": "PlaceholderA",
                    "icon_url": "/assets/augments/placeholdera_small.png",
                },
                "占位强化 B": {
                    "name": "占位强化 B",
                    "tier": "白银",
                    "cdragon_id": -1,
                    "augment_name_id": "PlaceholderB",
                    "icon_url": "/assets/augments/placeholderb_small.png",
                }
            },
        ),
    ):
        manifest_gap_cache = overlay_hint_cache.build_overlay_hint_cache_from_precomputed(
            include_private_stats=True,
            source_tag="dev-check",
        )
    snowbomb_hint = overlay_hint_cache.query_overlay_hint(manifest_gap_cache, "snowbomb")
    assert snowbomb_hint["ok"] is True
    assert snowbomb_hint["hint"]["augment_id"] == "2080"
    assert snowbomb_hint["hint"]["name"] == "冰雪爆裂"
    assert "stats_by_champion_id" not in snowbomb_hint["hint"]
    assert overlay_hint_cache.query_overlay_hint(manifest_gap_cache, "placeholdera")["hint"]["name"] == "占位强化 A"
    assert overlay_hint_cache.query_overlay_hint(manifest_gap_cache, "placeholderb")["hint"]["name"] == "占位强化 B"

    with TemporaryDirectory() as tmp_dir:
        champion_cache = Path(tmp_dir) / "Champion_List_Cache.json"
        hextech_cache = Path(tmp_dir) / "Champion_Hextech_Cache.json"
        champion_cache.write_text(
            json.dumps({"meta": {"source": "stale.csv"}, "data": [{"英雄名称": "德玛西亚之力"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        hextech_cache.write_text(
            json.dumps({"meta": {"source": "stale.csv"}, "data": sample_payload}, ensure_ascii=False),
            encoding="utf-8",
        )
        with (
            patch.object(runtime_store, "get_latest_csv", return_value=None),
            patch.object(precomputed_cache, "warm_precomputed_hextech_cache", return_value=False),
            patch.object(precomputed_cache, "CHAMPION_LIST_CACHE_FILE", str(champion_cache)),
            patch.object(precomputed_cache, "HEXTECH_DETAIL_CACHE_FILE", str(hextech_cache)),
        ):
            stale_cache = overlay_hint_cache.build_overlay_hint_cache_from_precomputed(
                include_private_stats=True,
                source_tag="dev-check",
            )
    stale_hint = overlay_hint_cache.query_overlay_hint(stale_cache, "augment_001")
    assert stale_hint["ok"] is True
    assert stale_hint["hint"]["stats_by_champion_id"]["86"]["pickrate"] == 0.082

def test_overlay_runtime_paths_contract() -> None:
    """验证 overlay event/context 共享的轻量运行态路径规则。"""
    import hextech.modules.vision.runtime_paths as overlay_runtime_paths

    with TemporaryDirectory() as tmp_dir:
        var_root = Path(tmp_dir) / "var"
        with patch.object(overlay_runtime_paths, "get_var_dir", return_value=var_root):
            resolved = Path(overlay_runtime_paths.overlay_runtime_state_path("probe.json"))
            assert resolved == (var_root / "state" / "probe.json").resolve()
            try:
                overlay_runtime_paths.overlay_runtime_state_path("../escaped.json")
            except ValueError as exc:
                assert "escaped state dir" in str(exc)
            else:
                raise AssertionError("overlay runtime state 路径不得逃逸 state 根目录")
