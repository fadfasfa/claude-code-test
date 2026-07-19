"""开发门禁测试共享依赖。

只保存跨域 imports、常量和辅助函数；测试实体由各域测试模块直接持有。
"""

from __future__ import annotations

# ruff: noqa: E402, F401, F403

import ast

from contextlib import ExitStack

import importlib

import inspect

import io

import json

import logging

import os

import re

import subprocess

import sys

import threading

import time

from datetime import datetime, timedelta, timezone

from pathlib import Path

from tempfile import TemporaryDirectory, mkstemp

from typing import Any, Mapping

from unittest.mock import patch

RUN_DIR = Path(__file__).resolve().parents[1]

import requests

import pandas as pd

WEB_STATIC_DIR = RUN_DIR / "src" / "hextech" / "interfaces" / "web" / "backend" / "static"

DATA_DIR = RUN_DIR / "resources"

DATA_STATIC_ASSET_DIR = DATA_DIR / "assets"

DATA_STATIC_VERSION_DIR = DATA_DIR / "catalog"

DATA_STARTUP_SEED_DIR = DATA_DIR / "seeds"

DATA_DIAGNOSTIC_DIR = RUN_DIR / "tests" / "fixtures" / "diagnostics"

DATA_EVIDENCE_DIR = DATA_DIR / "evidence"

HEXTECH_HEALTH_REQUIRED_COLUMNS = (
    "英雄ID",
    "英雄名称",
    "海克斯名称",
    "英雄胜率",
    "英雄出场率",
    "海克斯胜率",
    "海克斯出场率",
)

import hextech.modules.data.catalog.aliases as alias_search

import hextech.modules.data.catalog.precomputed_cache as precomputed_cache

import hextech.modules.data.catalog.runtime_store as runtime_store

import hextech.infrastructure.sources.hextech.service as hextech_scraper

import hextech.infrastructure.sources.apex.service as synergy_scraper

import hextech.modules.acquisition.common.icons as icon_resolver

import hextech.infrastructure.sources.version_sync as version_sync

import hextech.infrastructure.transport.scrapling_client as scrapling_client

import hextech.interfaces.web.backend.runtime as web_runtime

from hextech.interfaces.web.backend.synergy_api import (
    _build_synergy_api_payload,
    _normalize_synergy_items,
    _synergy_item_to_display_string,
)

from hextech.modules.data.catalog.version_catalog import (
    load_augment_manifest_entries,
    load_augment_name_to_icon_map,
)

from hextech.modules.data.catalog.view_adapter import process_hextechs_data

from hextech.infrastructure.sources.hextech.parsing import extract_champion_stats

from hextech.infrastructure.sources.apex.service import (
    SYNERGY_REFRESH_META_VERSION,
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

from tooling.build.manifest import build_bundle_manifest, manifest_contains_forbidden_path

from tooling.build.rules import iter_package_data_entries

from hextech.infrastructure.observability.logging import get_runtime_log_paths, install_runtime_logging, install_summary_logging

TIER_IDS = ("Prismatic", "Gold", "Silver")

ORIGINAL_SYNC_HERO_DATA = version_sync.sync_hero_data

VERSION_SYNC_WRITE_GUARDED_CHECKS = frozenset(
    {
        "test_hextech_scraper_fallback_contract",
        "test_hextech_cooldown_allows_forced_permission",
        "test_hextech_failed_refresh_never_overwrites_csv",
        "test_hextech_success_clears_fallback_state",
    }
)

def _write_runtime_csv(path: Path, row_count: int = 300) -> None:
    """生成满足运行态 schema 的最小离线样本。"""

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
        "胜率差": 0.01,
        "综合得分": 1.0,
    }
    pd.DataFrame([row] * row_count).to_csv(path, index=False, encoding=runtime_store.CSV_ENCODING)

def _top_level_import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            imports.add(f"{prefix}{node.module or ''}")
    return imports

def _probe_clean_import(script_name: str) -> set[str]:
    code = f"""
import json
import runpy
import sys
sys.path.insert(0, {str(RUN_DIR / "src")!r})
runpy.run_path({script_name!r}, run_name='__hextech_probe__')
watched = [
    'display',
    'display.web_server',
    'display.web_api',
    'fastapi',
    'uvicorn',
    'webbrowser',
    'requests',
]
print(json.dumps([name for name in watched if name in sys.modules]))
"""
    output = subprocess.check_output(
        [sys.executable, "-B", "-c", code],
        cwd=str(RUN_DIR),
        text=True,
        encoding="utf-8",
    )
    return set(json.loads(output))

def _probe_module_import(module_name: str) -> set[str]:
    code = f"""
import importlib
import json
import sys
sys.path.insert(0, {str(RUN_DIR / "src")!r})
importlib.import_module({module_name!r})
watched = [
    'display.web_server',
    'display.web_api',
    'fastapi',
    'uvicorn',
    'webbrowser',
    'requests',
]
print(json.dumps([name for name in watched if name in sys.modules]))
"""
    output = subprocess.check_output(
        [sys.executable, "-B", "-c", code],
        cwd=str(RUN_DIR),
        text=True,
        encoding="utf-8",
    )
    return set(json.loads(output))

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

def _write_json(path: Path, payload: dict, mtime: int = 1000) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path

def _snapshot(
    temp_dir: str,
    name: str = "synergy.json",
    *,
    mtime: float | None = None,
) -> Path:
    path = Path(temp_dir) / name
    path.write_text(json.dumps({"804": {"synergy_items": [{"content": "ok"}]}}), encoding="utf-8")
    timestamp = time.time() if mtime is None else mtime
    os.utime(path, (timestamp, timestamp))
    return path

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

def _collision_core_info() -> dict[str, ChampionInfo]:
    return {
        "254": ChampionInfo(
            id="254",
            name="皮城执法官",
            title="蔚",
            en_name="Vi",
            aliases=["蔚", "w"],
            slug=normalize_slug("Vi"),
        ),
        "234": ChampionInfo(
            id="234",
            name="破败之王",
            title="佛耶戈",
            en_name="Viego",
            aliases=["vi", "vie", "佛耶戈"],
            slug=normalize_slug("Viego"),
        ),
        "112": ChampionInfo(
            id="112",
            name="奥术先驱",
            title="维克托",
            en_name="Viktor",
            aliases=["vi", "vik", "维克托"],
            slug=normalize_slug("Viktor"),
        ),
    }

def _augment_map() -> dict[str, str]:
    return {
        "bladewaltz": "利刃华尔兹",
        normalize_augment_name("利刃华尔兹"): "利刃华尔兹",
        "利刃华尔兹": "利刃华尔兹",
    }

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and not name.startswith("test_")
]
