"""`dev_checks` 保留的手动验收与诊断实现。

自动化回归由 pytest 负责；本模块不导入测试代码，也不维护自动检查清单。
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping
from urllib.parse import quote

import requests

from hextech.scraping.synergy.scraper import (
    ApexSource, SynergyEntry, SynergyExtractor, _load_json_file,
    build_augment_name_map_from_static, build_champion_lookup, build_core_info,
)
from tools.bundle_manifest import build_bundle_manifest, manifest_contains_forbidden_path
from tools.package_rules import iter_package_data_entries

RUN_DIR = Path(__file__).resolve().parents[1]
HEXTECH_HEALTH_REQUIRED_COLUMNS = (
    "英雄ID", "英雄名称", "海克斯名称", "英雄胜率", "英雄出场率", "海克斯胜率", "海克斯出场率",
)
TIER_IDS = ("Prismatic", "Gold", "Silver")

def _read_json_object(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}

def _csv_health_summary(path: str, *, runtime_store_module: Any) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {"path": path, "exists": False, "valid": False}
    try:
        df = runtime_store_module.load_runtime_csv(path)
    except Exception as exc:
        return {"path": path, "exists": True, "valid": False, "error": f"{type(exc).__name__}: {exc}"}
    summary: dict[str, Any] = {
        "path": path,
        "filename": Path(path).name,
        "exists": True,
        "valid": not df.empty,
        "rows": int(len(df)),
        "unique_heroes": int(df["英雄ID"].astype(str).nunique()) if "英雄ID" in df.columns else 0,
        "missing_columns": [column for column in HEXTECH_HEALTH_REQUIRED_COLUMNS if column not in df.columns],
        "blank_required_cells": {},
    }
    for column in HEXTECH_HEALTH_REQUIRED_COLUMNS:
        if column in df.columns:
            summary["blank_required_cells"][column] = int(df[column].isna().sum() + (df[column].astype(str).str.strip() == "").sum())
    summary["meets_contract"] = (
        summary["valid"]
        and int(summary["rows"]) >= 300
        and int(summary["unique_heroes"]) >= 170
        and not summary["missing_columns"]
        and all(count == 0 for count in summary["blank_required_cells"].values())
    )
    return summary

def _raw_items_summary(path: str | Path) -> dict[str, Any]:
    payload = _read_json_object(path)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    return {
        "path": str(path),
        "exists": Path(path).exists(),
        "items": len(items),
    }

def _cleaned_synergy_summary(path: str | Path) -> dict[str, Any]:
    payload = _read_json_object(path)
    hero_count = 0
    item_count = 0
    mayhem_item_count = 0
    heroes_empty = 0
    if isinstance(payload, dict):
        hero_count = len(payload)
        for hero_payload in payload.values():
            if not isinstance(hero_payload, Mapping):
                continue
            items = hero_payload.get("synergy_items") if isinstance(hero_payload.get("synergy_items"), list) else []
            if not items and isinstance(hero_payload.get("synergies"), list):
                items = hero_payload.get("synergies")
            if not items:
                heroes_empty += 1
            item_count += len(items)
            mayhem_item_count += sum(
                1
                for item in items
                if isinstance(item, Mapping) and str(item.get("source") or "").lower() == "arammayhem"
            )
    return {
        "path": str(path),
        "exists": Path(path).exists(),
        "heroes": hero_count,
        "heroes_empty": heroes_empty,
        "items": item_count,
        "mayhem_items": mayhem_item_count,
    }

def _overlay_hint_summary(path: str | Path) -> dict[str, Any]:
    payload = _read_json_object(path)
    hints = payload.get("hints") if isinstance(payload.get("hints"), dict) else {}
    hints_with_synergies = 0
    arammayhem_synergy_hints = 0
    for hint in hints.values():
        if not isinstance(hint, Mapping):
            continue
        synergies = hint.get("synergies") if isinstance(hint.get("synergies"), list) else []
        if synergies:
            hints_with_synergies += 1
        if any(isinstance(item, Mapping) and str(item.get("source") or "").lower() == "arammayhem" for item in synergies):
            arammayhem_synergy_hints += 1
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    return {
        "path": str(path),
        "exists": Path(path).exists(),
        "hints": len(hints),
        "hints_with_synergies": hints_with_synergies,
        "arammayhem_synergy_hints": arammayhem_synergy_hints,
        "source": dict(source),
    }

def _error_log_summary(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {
            "path": str(target),
            "exists": False,
            "tail_lines": 0,
            "thread_pool_timeout_mentions": 0,
            "scrapling_timeout_mentions": 0,
            "mayhem_mentions": 0,
        }
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError as exc:
        return {"path": str(target), "exists": True, "error": f"{type(exc).__name__}: {exc}"}
    joined = "\n".join(lines).lower()
    return {
        "path": str(target),
        "exists": True,
        "tail_lines": len(lines),
        "thread_pool_timeout_mentions": joined.count("thread_pool_timeout"),
        "scrapling_timeout_mentions": joined.count("scrapling") + joined.count("curl: (28)"),
        "mayhem_mentions": joined.count("mayhem") + joined.count("联动"),
    }

def build_hextech_scrape_health_summary(
    *, runtime_store_module: Any, data_evidence_dir: Path
) -> dict[str, Any]:
    from hextech.overlay.hints import OVERLAY_HINT_CACHE_FILE
    from hextech.scraping.synergy.mayhem_refresh import (
        get_mayhem_raw_cache_path,
        get_mayhem_refresh_status_path,
        mayhem_refresh_due,
    )

    scraper_status_path = runtime_store_module.build_runtime_state_path("scraper_status.json")
    synergy_status_path = runtime_store_module.build_synergy_refresh_status_path()
    mayhem_status_path = get_mayhem_refresh_status_path()
    latest_csv = runtime_store_module.get_latest_valid_csv() or runtime_store_module.get_latest_csv() or ""
    scraper_status = _read_json_object(scraper_status_path)
    cleaned_path = runtime_store_module.build_synergy_cleaned_data_path()
    mayhem_raw_cache = get_mayhem_raw_cache_path()
    mayhem_evidence_raw = data_evidence_dir / "mayhem_combos.raw.json"
    error_log_path = runtime_store_module.get_runtime_root_dir() / "logs" / "hextech_error.log"

    csv_summary = _csv_health_summary(latest_csv, runtime_store_module=runtime_store_module)
    error_log_summary = _error_log_summary(error_log_path)
    mayhem_status = _read_json_object(mayhem_status_path)
    synergy_status = _read_json_object(synergy_status_path)
    mayhem_cache_summary = _raw_items_summary(mayhem_raw_cache)
    evidence_summary = _raw_items_summary(mayhem_evidence_raw)
    cleaned_summary = _cleaned_synergy_summary(cleaned_path)
    hint_summary = _overlay_hint_summary(OVERLAY_HINT_CACHE_FILE)

    hextech_issue = ""
    if csv_summary.get("meets_contract") and scraper_status.get("last_result") in {"fallback", "failed"}:
        hextech_issue = "data_available_refresh_failed"
    elif not csv_summary.get("meets_contract"):
        hextech_issue = "no_valid_hextech_csv"
    else:
        hextech_issue = "ok"

    mayhem_issue = "ok"
    if evidence_summary["items"] > 0 and mayhem_cache_summary["items"] <= 0:
        mayhem_issue = "raw_captured_not_published"
    elif mayhem_status.get("last_result") != "success":
        mayhem_issue = "no_successful_mayhem_refresh"

    synergy_issue = "ok"
    if cleaned_summary["items"] > 0 and cleaned_summary["mayhem_items"] <= 0 and hint_summary["hints_with_synergies"] > 0:
        synergy_issue = "old_synergy_visible_without_mayhem_source"
    elif hint_summary["hints_with_synergies"] > 0 and hint_summary["arammayhem_synergy_hints"] <= 0:
        synergy_issue = "old_synergy_visible_new_mayhem_not_in_overlay"
    elif hint_summary["hints_with_synergies"] <= 0:
        synergy_issue = "no_overlay_synergy_hints"

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hextech": {
            "issue": hextech_issue,
            "csv": csv_summary,
            "scraper_status_path": scraper_status_path,
            "scraper_status": {
                key: scraper_status.get(key)
                for key in (
                    "last_result",
                    "reason",
                    "failure_stage",
                    "active_csv",
                    "success_rows",
                    "fallback_used",
                    "last_attempt_at",
                )
            },
            "error_log": error_log_summary,
        },
        "mayhem": {
            "issue": mayhem_issue,
            "refresh_due": mayhem_refresh_due(),
            "status_path": mayhem_status_path,
            "status": mayhem_status,
            "runtime_raw": mayhem_cache_summary,
            "evidence_raw": evidence_summary,
        },
        "synergy": {
            "issue": synergy_issue,
            "refresh_status_path": synergy_status_path,
            "refresh_status": synergy_status,
            "cleaned": cleaned_summary,
            "overlay_hint_cache": hint_summary,
        },
    }

def print_hextech_scrape_health_summary(
    *, as_json: bool, runtime_store_module: Any, data_evidence_dir: Path
) -> None:
    summary = build_hextech_scrape_health_summary(
        runtime_store_module=runtime_store_module, data_evidence_dir=data_evidence_dir
    )
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(f"hextech: {summary['hextech']['issue']}")
    print(f"mayhem: {summary['mayhem']['issue']}")
    print(f"synergy: {summary['synergy']['issue']}")

def validate_bundle_manifest_contract() -> dict[str, Any]:
    """验证打包清单契约并返回清单；调用方决定如何展示结果。"""

    manifest = build_bundle_manifest(RUN_DIR)

    assert "hextech_snapshot_files" in manifest
    hextech_files = manifest["hextech_snapshot_files"]
    assert isinstance(hextech_files, list)
    assert all(str(item).replace("\\", "/").startswith("data/seed/startup/hextech/") for item in hextech_files)

    assert "synergy_data_file" in manifest
    assert manifest["synergy_data_file"]
    assert str(manifest["synergy_data_file"]).replace("\\", "/").startswith("data/seed/startup/synergy/")

    assert "synergy_data_files" in manifest
    synergy_files = manifest["synergy_data_files"]
    assert isinstance(synergy_files, list)
    assert all(str(item).replace("\\", "/").startswith("data/seed/startup/synergy/") for item in synergy_files)

    has_latest_pointer = any(Path(item).name == "Champion_Synergy_latest.v1.json" for item in synergy_files)
    has_timestamp_snapshot = any(
        Path(item).name.startswith("Champion_Synergy_")
        and Path(item).name != "Champion_Synergy_latest.v1.json"
        and Path(item).name.endswith(".json")
        for item in synergy_files
    )
    assert has_latest_pointer
    assert has_timestamp_snapshot
    assert "英雄目录.v1.json" in manifest.get("static_files", [])
    assert "海克斯资源目录.v1.json" in manifest.get("static_files", [])
    assert "augment.name-to-icon.v1.json" not in manifest.get("index_files", [])
    source_files = manifest.get("source_files", [])
    for required_source in (
        "hextech/__init__.py",
        "hextech/catalog/runtime_store.py",
        "hextech/core/refresh.py",
        "hextech/core/settings.py",
        "hextech/display/desktop/app.py",
        "hextech/display/web/app.py",
        "hextech/overlay/host.py",
        "hextech/overlay/lifecycle.py",
        "hextech/overlay/vision/sidecar.py",
        "hextech/scraping/hextech/scraper.py",
        "hextech/scraping/synergy/scraper.py",
        "hextech/scraping/transport/scrapling_client.py",
        "hextech/support/atomic_io.py",
        "hextech/support/log_utils.py",
        "hextech/support/user_diagnostics.py",
        "hextech_ui.py",
        "web_server.py",
        "tools/acceptance/overlay_performance_probe.py",
        "tools/acceptance/smoke_packaged_startup.py",
        "tools/acceptance/probe_official_overlay_provider.py",
    ):
        assert required_source in source_files
    legacy_source_prefixes = ("crawler/", "display/", "game_overlay/", "processing/", "scraping/")
    assert not any(str(item).startswith(legacy_source_prefixes) for item in source_files)
    assert not any("data/runtime" in str(item) for item in source_files)
    assert not any("data/raw" in str(item) for item in source_files)
    for forbidden_path in (
        "data/raw",
        "data/runtime",
        "data/processed",
        "runtime/reports",
        "runtime/report",
        "__pycache__",
        ".pyc",
        ".pyo",
    ):
        assert not manifest_contains_forbidden_path(manifest, forbidden_path)
    assert not any(str(item).startswith("run/tests/") or str(item).startswith("tests/") for item in source_files)
    assert not any(str(item).startswith(("tools/diagnostics/", "tools/maintenance/")) for item in source_files)
    assert "tools/collect_runtime_diagnostics.py" not in source_files
    assert "tools/cleanup_runtime.py" not in source_files
    assert "tools/dev_checks.py" not in source_files
    assert "tools/migrate_runtime_data.py" not in source_files
    assert not manifest_contains_forbidden_path(manifest, "overlay_anchor_calibration.v1.json")

    with TemporaryDirectory() as tmp_dir:
        fixture_root = Path(tmp_dir) / "fixture"
        fixture_index = fixture_root / "data" / "static" / "version"
        fixture_static = fixture_root / "hextech" / "display" / "web" / "static"
        fixture_assets = fixture_root / "data" / "static" / "assets"
        fixture_index.mkdir(parents=True)
        fixture_static.mkdir(parents=True)
        fixture_assets.mkdir(parents=True)
        (fixture_index / "海克斯资源目录.v1.json").write_text(
            '{"schema_version":1,"entries":[],"name_to_icon":{"尤里卡":"assets/1.png"},"apexlol_slug_map":{}}',
            encoding="utf-8",
        )
        (fixture_index / "英雄目录.v1.json").write_text('{"schema_version":1,"aliases":[]}', encoding="utf-8")
        (fixture_static / "index.html").write_text("<html></html>", encoding="utf-8")
        (fixture_assets / "1.png").write_bytes(b"png")
        manifest_path = Path(tmp_dir) / "bundle_manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        entries = iter_package_data_entries(fixture_root, manifest_path)
        entry_targets = {(entry.source.name, entry.target) for entry in entries}
        assert ("海克斯资源目录.v1.json", "data/static/version") in entry_targets
        assert ("英雄目录.v1.json", "data/static/version") in entry_targets
        assert ("static", "static") in entry_targets
        assert ("assets", "data/static/assets") in entry_targets
        assert ("bundle_manifest.json", ".") in entry_targets
        assert not (Path(tmp_dir) / "build" / "_bundle_runtime").exists()
        (fixture_assets / "debug.tmp").write_text("debug", encoding="utf-8")
        try:
            iter_package_data_entries(fixture_root, manifest_path)
        except ValueError as exc:
            assert "debug.tmp" in str(exc)
        else:
            raise AssertionError("assets 目录含非白名单文件时必须阻断打包规则生成")

    return manifest


def check_bundle_manifest(*, verbose: bool = False) -> None:
    manifest = validate_bundle_manifest_contract()
    if verbose:
        summary = {
            key: len(value) if isinstance(value, list) else value
            for key, value in manifest.items()
        }
        hextech_files = manifest["hextech_snapshot_files"]
        synergy_files = manifest["synergy_data_files"]
        has_latest_pointer = any(Path(item).name == "Champion_Synergy_latest.v1.json" for item in synergy_files)
        has_timestamp_snapshot = any(
            Path(item).name.startswith("Champion_Synergy_")
            and Path(item).name != "Champion_Synergy_latest.v1.json"
            and Path(item).name.endswith(".json")
            for item in synergy_files
        )
        print(summary)
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
