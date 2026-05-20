from __future__ import annotations

"""开发自检与手动验收入口。

这个模块是 `run/` 的统一验证入口：
- 默认模式执行离线、无外网、无浏览器依赖的结构与回归自检。
- `--bundle-manifest` 输出 bundle manifest 明细并校验关键字段。
- `--manual-web-synergy` 执行 Web/UI 详情页联动人工验收辅助检查。

它替代原先散落的 `run/tests/` 临时测试目录，以及独立的
`accept_web_synergy.py` / `verify_bundle_manifest.py` 工具入口。
"""

import argparse
import io
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from typing import Any
from unittest.mock import patch
from urllib.parse import quote

import requests
import pandas as pd

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

import processing.orchestrator as orchestrator
import processing.precomputed_cache as precomputed_cache
import processing.runtime_store as runtime_store
import scraping.full_synergy_scraper as synergy_scraper
import scraping.heal_worker as heal_worker
from display.web_api import _normalize_synergy_items, _synergy_item_to_compat_string
from processing.alias_search import load_manual_alias_index
from processing.view_adapter import process_hextechs_data
from scraping.full_hextech_scraper import extract_champion_stats
from scraping.full_synergy_scraper import (
    SYNERGY_REFRESH_META_VERSION,
    ApexSource,
    ChampionInfo,
    FetchedResource,
    SynergyEntry,
    SynergyExtractor,
    SynergyWriter,
    _load_json_file,
    _validate_publish_size,
    build_augment_name_map_from_static,
    build_champion_lookup,
    build_core_info,
    normalize_augment_name,
    normalize_slug,
    write_synergy_refresh_meta,
)
from tools.bundle_manifest import build_bundle_manifest
from tools.log_utils import install_summary_logging


TIER_IDS = ("Prismatic", "Gold", "Silver")


def check_root_entrypoints() -> None:
    root_scripts = {
        path.name
        for path in RUN_DIR.iterdir()
        if path.is_file() and path.suffix == ".py"
    }

    assert {"build.py", "hextech_ui.py", "web_server.py"}.issubset(root_scripts)
    assert (RUN_DIR / "display").exists()
    assert (RUN_DIR / "processing").exists()
    assert (RUN_DIR / "tools").exists()


def check_manual_alias_index() -> None:
    alias_file = RUN_DIR / "data" / "indexes" / "Champion_Alias_Index.json"
    payload = json.loads(alias_file.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert payload, "Champion_Alias_Index.json 应至少包含一条手工索引"
    first = payload[0]
    assert isinstance(first, dict)
    assert "heroName" in first
    assert load_manual_alias_index()


def check_heal_worker_contract() -> None:
    assert hasattr(heal_worker, "heal_missing_artifacts")
    assert hasattr(heal_worker, "detect_missing_artifacts")


def check_logging_contract() -> None:
    fd, tmp_name = mkstemp(prefix="hextech-dev-", suffix=".log")
    os.close(fd)
    file_handler = None
    try:
        file_handler = logging.FileHandler(tmp_name, encoding="utf-8")
        stream_buffer = io.StringIO()
        stream_handler = logging.StreamHandler(stream_buffer)

        install_summary_logging(handlers=[file_handler, stream_handler])

        assert file_handler.level == logging.ERROR
        assert stream_handler.level == logging.WARNING
    finally:
        if file_handler is not None:
            file_handler.close()
        try:
            os.remove(tmp_name)
        except OSError:
            pass


def check_packaging_config() -> None:
    build_script = (RUN_DIR / "tools" / "build_bundle.py").read_text(encoding="utf-8")
    spec_text = (RUN_DIR / "Hextech伴生终端.spec").read_text(encoding="utf-8")

    assert "--hidden-import\", \"filelock\"" in build_script
    assert "filelock" in spec_text
    assert "display" in (RUN_DIR / "tools" / "bundle_manifest.py").read_text(encoding="utf-8")


def check_no_legacy_imports() -> None:
    legacy_hits = []
    for path in RUN_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name == "dev_checks.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "from app." in text or "from services." in text or "import app." in text or "import services." in text:
            legacy_hits.append(path)
    assert not legacy_hits, f"仍存在旧导入: {legacy_hits}"


def check_bundle_manifest(*, verbose: bool = False) -> None:
    manifest = build_bundle_manifest(RUN_DIR)
    summary = {
        key: len(value) if isinstance(value, list) else value
        for key, value in manifest.items()
    }

    if verbose:
        print(summary)

    assert "hextech_snapshot_files" in manifest
    hextech_files = manifest["hextech_snapshot_files"]
    assert isinstance(hextech_files, list)

    assert "synergy_data_file" in manifest
    assert manifest["synergy_data_file"]

    assert "synergy_data_files" in manifest
    synergy_files = manifest["synergy_data_files"]
    assert isinstance(synergy_files, list)

    has_latest_pointer = any(Path(item).name == "Champion_Synergy_latest.v1.json" for item in synergy_files)
    has_timestamp_snapshot = any(
        Path(item).name.startswith("Champion_Synergy_")
        and Path(item).name != "Champion_Synergy_latest.v1.json"
        and Path(item).name.endswith(".json")
        for item in synergy_files
    )
    assert has_latest_pointer
    assert has_timestamp_snapshot

    if verbose:
        print("has_hextech_snapshot_files", True)
        print("hextech_snapshot_files_count", len(hextech_files))
        print("hextech_snapshot_files_sample", hextech_files[:5])
        print("has_synergy_data_file", True)
        print("synergy_data_file", manifest["synergy_data_file"])
        print("has_synergy_data_files", True)
        print("synergy_data_files_count", len(synergy_files))
        print("synergy_data_files_sample", synergy_files[:5])
        print("has_synergy_latest_pointer", has_latest_pointer)
        print("has_synergy_timestamp_snapshot", has_timestamp_snapshot)


def check_precomputed_cache_freshness() -> None:
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


def check_apex_source_snapshot_policy() -> None:
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
            "APEX_ALLOW_ONLINE_FETCH": "0",
            "APEX_ALLOW_BROWSER": "0",
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
    fetched = synergy_scraper.FetchedResource(url=source.base_url, text="<html></html>", source="selenium")
    try:
        with patch.dict(os.environ, {"APEX_ALLOW_BROWSER": "0"}):
            with patch.object(source, "fetch_requests", return_value=None):
                with patch.object(source, "fetch_browser", return_value=fetched) as fetch_browser:
                    assert source.fetch(source.base_url, allow_browser=True) is None
                    fetch_browser.assert_not_called()

        with patch.dict(os.environ, {"APEX_ALLOW_BROWSER": "1"}):
            with patch.object(source, "fetch_requests", return_value=None):
                with patch.object(source, "fetch_browser", return_value=fetched) as fetch_browser:
                    assert source.fetch(source.base_url, allow_browser=True) is fetched
                    fetch_browser.assert_called_once_with(source.base_url)
    finally:
        source.close()


def _flight_script(payload: str) -> str:
    return f"<script>self.__next_f.push([1,{json.dumps(payload, ensure_ascii=False)}])</script>"


def _morgana_maps() -> tuple[dict[str, str], dict[str, str]]:
    names = {
        "1373": "缩小引擎",
        "1420": "咏叹奏鸣",
        "1406": "祖母的辣椒油",
        "1052": "闪电打击",
        "1058": "秘术冲拳",
    }
    tiers = {
        "缩小引擎": "黄金",
        "咏叹奏鸣": "黄金",
        "祖母的辣椒油": "黄金",
        "闪电打击": "黄金",
        "秘术冲拳": "棱彩",
    }
    return names, tiers


def _morgana_html() -> str:
    ref_payload = (
        '27:[["$","$L28",null,{"championId":"25",'
        '"championAugmentsStats":{"25":[["25","$29","16.10","2026-05-14"]]}}]]\n'
        "29:T123,"
    )
    stats_payload = json.dumps(
        {
            "augments": {
                "1373": {"tier": "1", "win_rate": "0.5774767146486028", "pick_rate": "0.06533163688665154"},
                "1420": {"tier": "1", "win_rate": "0.5656274561173696", "pick_rate": "0.05278807324224152"},
                "1406": {"tier": "1", "win_rate": "0.5623392704067054", "pick_rate": "0.1451983183050285"},
                "1058": {"tier": "5", "win_rate": "0.42105263157894735", "pick_rate": "0.002627705297509843"},
            }
        },
        ensure_ascii=False,
    )
    noise = '<div>{"1052":{"winRate":0.62961,"pickRate":0.078373}}</div>'
    return noise + _flight_script(ref_payload) + _flight_script(stats_payload)


def check_hextech_source_parser() -> None:
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


def _write_json(path: Path, payload: dict, mtime: int = 1000) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _snapshot(
    temp_dir: str,
    name: str = "Champion_Synergy_20260519_223505.json",
    *,
    mtime: float | None = None,
) -> Path:
    path = Path(temp_dir) / name
    path.write_text(json.dumps({"804": {"synergy_items": [{"content": "ok"}]}}), encoding="utf-8")
    timestamp = time.time() if mtime is None else mtime
    os.utime(path, (timestamp, timestamp))
    return path


def _patch_synergy_dir(temp_dir: str, status: dict | None = None):
    payload = {} if status is None else status
    return (
        patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
        patch.object(heal_worker, "load_synergy_refresh_status", return_value=payload),
        patch.object(orchestrator, "load_synergy_refresh_status", return_value=payload),
    )


def check_synergy_refresh_freshness() -> None:
    with TemporaryDirectory() as temp_dir:
        _snapshot(temp_dir)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patches[2]:
            assert not heal_worker._synergy_data_fresh()
            assert orchestrator.should_refresh_synergy(False)

    with TemporaryDirectory() as temp_dir:
        synergy_path = _snapshot(temp_dir)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patches[2]:
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )

            assert heal_worker._synergy_data_fresh()
            assert not orchestrator.should_refresh_synergy(False)

            meta_path = Path(temp_dir) / "Champion_Synergy_latest.v1.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            assert meta["version"] == SYNERGY_REFRESH_META_VERSION
            assert meta["filename"] == synergy_path.name
            assert meta["non_empty_heroes"] == 1

    with TemporaryDirectory() as temp_dir:
        old_mtime = time.time() - (8 * 24 * 60 * 60)
        synergy_path = _snapshot(temp_dir, mtime=old_mtime)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patches[2]:
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )
            os.utime(synergy_path, (old_mtime, old_mtime))
            os.utime(Path(temp_dir) / "Champion_Synergy_latest.v1.json", (old_mtime, old_mtime))

            assert not heal_worker._synergy_data_fresh()
            assert orchestrator.should_refresh_synergy(False)

    blocked_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    status = {"last_result": "blocked", "blocked_until": blocked_until}
    with TemporaryDirectory() as temp_dir:
        old_mtime = time.time() - (8 * 24 * 60 * 60)
        synergy_path = _snapshot(temp_dir, mtime=old_mtime)

        patches = _patch_synergy_dir(temp_dir, status=status)
        with patches[0], patches[1], patches[2]:
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )
            os.utime(synergy_path, (old_mtime, old_mtime))
            os.utime(Path(temp_dir) / "Champion_Synergy_latest.v1.json", (old_mtime, old_mtime))

            assert heal_worker._synergy_data_fresh()
            assert not orchestrator.should_refresh_synergy(False)


def check_synergy_snapshot_store() -> None:
    with TemporaryDirectory() as temp_dir:
        snapshot = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"1": {}}, 1000)
        _write_json(
            Path(temp_dir) / "Champion_Synergy_latest.v1.json",
            {"version": 1, "filename": snapshot.name, "non_empty_heroes": 1},
            1001,
        )

        with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
            assert runtime_store.get_latest_synergy_snapshot_path() == str(snapshot.resolve())
            assert runtime_store.build_synergy_data_path() == str(snapshot.resolve())

    with TemporaryDirectory() as temp_dir:
        older = _write_json(Path(temp_dir) / "Champion_Synergy_20260518_010101.json", {"1": {}}, 1000)
        newer = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"2": {}}, 2000)
        (Path(temp_dir) / "Champion_Synergy_latest.v1.json").write_text("{bad", encoding="utf-8")

        with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
            assert runtime_store.get_latest_synergy_snapshot_path() == str(newer)
            assert runtime_store.get_latest_synergy_snapshot_path() != str(older)

    with TemporaryDirectory() as temp_dir:
        legacy = _write_json(Path(temp_dir) / "Champion_Synergy.json", {"1": {}}, 1000)

        with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
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


def _core_info() -> dict[str, ChampionInfo]:
    return {
        "55": ChampionInfo(
            id="55",
            name="卡特琳娜",
            title="不祥之刃",
            en_name="Katarina",
            aliases=["卡特"],
            slug=normalize_slug("Katarina"),
        )
    }


def _augment_map() -> dict[str, str]:
    return {
        "bladewaltz": "利刃华尔兹",
        normalize_augment_name("利刃华尔兹"): "利刃华尔兹",
        "利刃华尔兹": "利刃华尔兹",
    }


def check_synergy_structured_payloads() -> None:
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


def _normalize_text(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _normalize_tier(value: Any) -> str:
    text = str(value or "").strip()
    if "棱彩" in text or "彩色" in text or text == "Prismatic":
        return "Prismatic"
    if "黄金" in text or "金" in text or text == "Gold":
        return "Gold"
    if "白银" in text or "银" in text or text == "Silver":
        return "Silver"
    return text


def _entry_to_expected(entry: SynergyEntry) -> dict:
    return {
        "name": ", ".join(entry.augment_names),
        "names": list(entry.augment_names),
        "tier": entry.tier,
        "rating": entry.rating,
        "tag": entry.tag,
        "author": entry.author,
        "content": entry.content,
    }


def _make_local_driver():
    errors = []
    headless = os.getenv("ACCEPT_HEADLESS", "1").strip() != "0"
    browser = os.getenv("ACCEPT_BROWSER", "auto").strip().lower() or "auto"
    if browser in {"auto", "edge"}:
        try:
            from selenium import webdriver
            from selenium.webdriver.edge.options import Options as EdgeOptions

            options = EdgeOptions()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions")
            options.add_argument("--window-size=1365,900")
            driver = webdriver.Edge(options=options)
            driver.set_page_load_timeout(20)
            return driver
        except Exception as exc:  # pragma: no cover - browser availability is machine local
            errors.append(f"edge={exc.__class__.__name__}:{str(exc)[:120]}")
    if browser in {"auto", "chrome"}:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions

            options = ChromeOptions()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions")
            options.add_argument("--window-size=1365,900")
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            return driver
        except Exception as exc:  # pragma: no cover - browser availability is machine local
            errors.append(f"chrome={exc.__class__.__name__}:{str(exc)[:120]}")
    raise RuntimeError("无法启动验收浏览器：" + ", ".join(errors))


def _safe_get(driver, url: str) -> None:
    try:
        driver.get(url)
    except Exception:
        # 源站广告/长连接偶尔拖住 load；后续用 DOM 轮询判断页面是否可用。
        pass


def _wait_local_cards(driver, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            const container = document.querySelector('#synergyArticleScroll');
            return {
              cards: document.querySelectorAll('#synergyArticleScroll .hextech-card').length,
              text: container ? container.textContent : ''
            };
            """
        )
        text = state.get("text", "")
        if state.get("cards", 0) > 0 or "暂无联动" in text or "该阶级无联动" in text:
            return
        time.sleep(0.5)
    raise TimeoutError("本地详情页联动区域等待超时")


def _extract_local_cards(driver) -> list[dict]:
    return driver.execute_script(
        """
        return Array.from(document.querySelectorAll('#synergyArticleScroll .hextech-card')).map(card => {
          const titleBlock = card.querySelector('.hextech-article-title-block');
          const row = titleBlock && titleBlock.children ? titleBlock.children[0] : null;
          const spans = Array.from(row ? row.querySelectorAll('span') : [])
            .map(s => (s.textContent || '').trim()).filter(Boolean);
          const title = spans[0] || '';
          const rating = spans[1] || '';
          const tier = titleBlock && titleBlock.children && titleBlock.children[1]
            ? (titleBlock.children[1].textContent || '').trim()
            : '';
          const content = (card.querySelector('.hextech-article-content')?.textContent || '').trim();
          const headTexts = Array.from(row ? row.children : [])
            .map(el => (el.textContent || '').trim()).filter(Boolean);
          const tags = headTexts.filter(text => (
            text && text !== title && text !== rating && text !== '?' && !text.includes(`${rating}\\n`)
          ));
          const img = card.querySelector('img[data-hextech-name]');
          return {
            title,
            rating,
            tier,
            tags,
            content,
            resolvedName: img ? (img.getAttribute('data-hextech-name') || '') : '',
            resolvedTier: img ? (img.getAttribute('data-hextech-tier') || '') : ''
          };
        });
        """
    )


def _click_tier(driver, tier: str) -> None:
    driver.execute_script(
        """
        const tier = arguments[0];
        const button = document.getElementById(`tab-${tier}`);
        if (!button) throw new Error(`missing tier button ${tier}`);
        button.click();
        """,
        tier,
    )
    _wait_local_cards(driver)


def _compare_expected_to_local(expected: dict, local: dict) -> dict:
    tag_ok = not expected["tag"] or any(
        _normalize_text(expected["tag"]) in _normalize_text(tag)
        for tag in local.get("tags", [])
    )
    possible_names = {_normalize_text(name) for name in expected["names"]}
    resolved_name = _normalize_text(local.get("resolvedName") or local.get("title"))
    return {
        "name": _normalize_text(local.get("title")) == _normalize_text(expected["name"]),
        "tier": _normalize_tier(local.get("tier")) == _normalize_tier(expected["tier"]),
        "rating": _normalize_text(local.get("rating")) == _normalize_text(expected["rating"]),
        "tag": tag_ok,
        "content": _normalize_text(local.get("content")).find(_normalize_text(expected["content"])[:24]) >= 0,
        "catalog": resolved_name in possible_names,
    }


def _source_visible_matches(driver, expected: list[dict]) -> list[dict]:
    text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    clean_text = _normalize_text(text)
    results = []
    for item in expected:
        results.append(
            {
                "name": all(_normalize_text(name) in clean_text for name in item["names"]),
                "tier": _normalize_text(item["tier"]) in clean_text,
                "rating": _normalize_text(item["rating"]) in clean_text,
                "tag": _normalize_text(item["tag"]) in clean_text,
                "content": _normalize_text(item["content"])[:24] in clean_text,
            }
        )
    return results


def _source_page_blocked(driver) -> bool:
    text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    normalized = _normalize_text(text)
    return (
        "deploymentpaused" in normalized
        or "service_unavailable" in normalized
        or "503" in normalized
    )


def _all_match(rows: list[dict]) -> bool:
    return bool(rows) and all(all(row.values()) for row in rows)


def _wait_source_visible(driver, source_url: str, expected: list[dict]) -> list[dict]:
    """等待源站详情页真正把右侧联动文本渲染出来。"""

    last_matches: list[dict] = []
    for attempt in range(3):
        _safe_get(driver, source_url)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if _source_page_blocked(driver):
                break
            last_matches = _source_visible_matches(driver, expected)
            if _all_match(last_matches):
                return last_matches
            time.sleep(0.75)
        if attempt < 2:
            time.sleep(2)
    return last_matches


def _resolve_base_url(args) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    if args.port_file:
        port = Path(args.port_file).read_text(encoding="utf-8").strip()
        return f"http://127.0.0.1:{port}"
    return "http://127.0.0.1:8000"


def _load_local_champions(base_url: str) -> list[dict]:
    response = requests.get(f"{base_url}/api/champions", timeout=15)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"{base_url}/api/champions 未返回英雄列表")
    return payload


def _build_source_extractor() -> SynergyExtractor:
    core_info = build_core_info(_load_json_file("Champion_Core_Data.json", "core_data"))
    return SynergyExtractor(
        champion_lookup=build_champion_lookup(core_info),
        augment_name_map=build_augment_name_map_from_static(),
    )


def run_manual_web_synergy(args) -> dict:
    base_url = _resolve_base_url(args)
    source_base = args.source_base.rstrip("/")
    champions = _load_local_champions(base_url)
    rng = random.Random(args.seed)
    shuffled = list(champions)
    rng.shuffle(shuffled)

    source = ApexSource()
    extractor = _build_source_extractor()
    local_driver = _make_local_driver()
    screenshots = Path(args.screenshot_dir).resolve()
    screenshots.mkdir(parents=True, exist_ok=True)
    selected = []
    attempts = []

    try:
        for champion in shuffled:
            champ_id = str(champion.get("英雄 ID") or champion.get("英雄ID") or "").strip()
            hero_name = str(champion.get("英雄名称") or "").strip()
            en_name = str(champion.get("英文名") or "").strip()
            if not champ_id or not hero_name or not en_name:
                continue

            source_url = f"{source_base}/champions/{en_name}"
            resource = source.fetch(source_url, allow_browser=True)
            entries = extractor._extract_from_resource(resource) if resource else []
            attempts.append({"id": champ_id, "name": hero_name, "en": en_name, "source_entries": len(entries)})
            if not entries:
                continue

            expected = [_entry_to_expected(entry) for entry in entries[: args.first_n]]
            local_url = (
                f"{base_url}/detail.html?hero={quote(hero_name)}&id={quote(champ_id)}&en={quote(en_name)}"
                f"&acceptance={args.seed}"
            )
            _safe_get(local_driver, local_url)
            _wait_local_cards(local_driver)
            _click_tier(local_driver, "all")
            local_cards = _extract_local_cards(local_driver)[: len(expected)]
            comparisons = [
                _compare_expected_to_local(item, local_cards[index] if index < len(local_cards) else {})
                for index, item in enumerate(expected)
            ]

            tier_checks = []
            for tier in TIER_IDS:
                _click_tier(local_driver, tier)
                cards = _extract_local_cards(local_driver)
                violations = [
                    {"title": card.get("title", ""), "tier": card.get("tier", ""), "resolvedTier": card.get("resolvedTier", "")}
                    for card in cards
                    if _normalize_tier(card.get("tier") or card.get("resolvedTier")) != tier
                ]
                tier_checks.append({"tier": tier, "count": len(cards), "violations": violations})

            source_matches = _wait_source_visible(local_driver, source_url, expected)

            result = {
                "id": champ_id,
                "name": hero_name,
                "en": en_name,
                "local_url": local_url,
                "source_url": source_url,
                "source_entries": len(entries),
                "local_count": len(local_cards),
                "field_ok": len(local_cards) == len(expected) and _all_match(comparisons),
                "tier_ok": all(not check["violations"] for check in tier_checks),
                "source_visible_ok": _all_match(source_matches),
                "comparisons": comparisons,
                "tier_checks": tier_checks,
                "source_matches": source_matches,
            }
            if not (result["field_ok"] and result["tier_ok"] and result["source_visible_ok"]):
                screenshot = screenshots / f"{args.label}-{champ_id}-{en_name}.png"
                try:
                    local_driver.save_screenshot(str(screenshot))
                    result["screenshot"] = str(screenshot)
                except Exception as exc:
                    result["screenshot_error"] = exc.__class__.__name__
            selected.append(result)
            if len(selected) >= args.sample_size:
                break
    finally:
        source.close()
        try:
            local_driver.quit()
        except Exception:
            pass

    passed = len(selected) == args.sample_size and all(
        item["field_ok"] and item["tier_ok"] and item["source_visible_ok"]
        for item in selected
    )
    return {
        "label": args.label,
        "base_url": base_url,
        "source_base": source_base,
        "seed": args.seed,
        "sample_size": args.sample_size,
        "first_n": args.first_n,
        "passed": passed,
        "selected": selected,
        "attempts": attempts,
    }


def run_default_checks() -> None:
    check_root_entrypoints()
    check_manual_alias_index()
    check_heal_worker_contract()
    check_logging_contract()
    check_packaging_config()
    check_bundle_manifest()
    check_precomputed_cache_freshness()
    check_apex_source_snapshot_policy()
    check_hextech_source_parser()
    check_synergy_refresh_freshness()
    check_synergy_snapshot_store()
    check_synergy_structured_payloads()
    check_no_legacy_imports()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech 开发自检与手动验收入口。")
    parser.add_argument("--bundle-manifest", action="store_true", help="只输出并校验 bundle manifest 明细。")
    parser.add_argument("--manual-web-synergy", action="store_true", help="执行 Web/UI 详情页联动人工验收辅助检查。")
    parser.add_argument("--base-url", default="", help="本地 Web 地址，例如 http://127.0.0.1:8000")
    parser.add_argument("--port-file", default="", help="UI/Web 写出的 web_server_port.txt；未传 base-url 时使用。")
    parser.add_argument("--source-base", default="https://apexlol.info/zh")
    parser.add_argument("--seed", type=int, default=20260518)
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--first-n", type=int, default=5)
    parser.add_argument("--label", default="web", help="输出标签，例如 web 或 ui。")
    parser.add_argument("--screenshot-dir", default=os.path.join("data", "runtime", "acceptance", "synergy"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.manual_web_synergy:
        result = run_manual_web_synergy(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1

    if args.bundle_manifest:
        check_bundle_manifest(verbose=True)
        return 0

    run_default_checks()
    print("所有开发自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
