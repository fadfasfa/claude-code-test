"""把 Codex goal 重抓结果合并进 0519 Champion Synergy 基线。

本脚本只负责合并和严格验证，不抓网页、不读取浏览器 profile、不处理登录态。

输入优先级：
  1. run/data/raw/synergy/codex_goal_delta/*.json
  2. run/data/raw/synergy/Champion_Synergy_20260527_061038_browser_ground_truth_oracle.json 中的 validation_status=pass 结果
  3. Champion_Synergy_20260519_223505.json 既有内容

严格 pass 条件：
  - declared_count == actual_card_count == len(synergy_items)
  - 每张卡 author/content/augment_names/tier 必填且非空
  - 每张卡必须包含 upvotes/downvotes，且值不是 null

用法示例：
  python run/scripts/merge_codex_goal_into_baseline.py --check
  python run/scripts/merge_codex_goal_into_baseline.py

只有 172/172 全部严格通过时，非 --check 模式才会原子写回
run/data/raw/synergy/Champion_Synergy_20260519_223505.json。

调用方: 见 import 此模块的代码; 关键依赖: 见 imports。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


BASELINE_NAME = "Champion_Synergy_20260519_223505.json"
ORACLE_NAME = "Champion_Synergy_20260527_061038_browser_ground_truth_oracle.json"
DELTA_DIR_NAME = "codex_goal_delta"
EXPECTED_HERO_COUNT = 172
REQUIRED_TEXT_FIELDS = ("author", "content", "augment_names", "tier")
VOTE_FIELDS = ("upvotes", "downvotes")


class RejectedHero(Exception):
    """单个 hero 未通过严格门禁时抛出，错误消息直接用于 stderr。"""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def as_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def int_or_reject(value: Any, *, hero_id: str, index: int, field: str) -> int:
    if value is None:
        raise RejectedHero(f"{hero_id}: card[{index}].{field} is null")
    try:
        return max(0, int(value))
    except (TypeError, ValueError) as exc:
        raise RejectedHero(f"{hero_id}: card[{index}].{field} is not int-like: {value!r}") from exc


def compat_string(item: dict[str, Any]) -> str:
    augment_text = ", ".join(dict.fromkeys(str(name).strip() for name in item["augment_names"] if str(name).strip()))
    rating = str(item.get("rating") or "未知").strip()
    if rating and not rating.startswith("评分"):
        rating = f"评分 {rating}"
    return " | ".join(
        [
            augment_text or "未知联动",
            str(item.get("tier") or "未知"),
            rating or "评分 未知",
            str(item.get("tag") or "强力联动"),
            str(int_or_reject(item.get("upvotes"), hero_id=str(item.get("_hero_id", "?")), index=-1, field="upvotes")),
            str(int_or_reject(item.get("downvotes"), hero_id=str(item.get("_hero_id", "?")), index=-1, field="downvotes")),
            f"作者：{item.get('author') or 'ApexLoL'}",
            "原创" if item.get("is_original") else "非原创",
            str(item.get("content") or ""),
        ]
    )


def normalize_item(raw: dict[str, Any], *, hero_id: str, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RejectedHero(f"{hero_id}: card[{index}] is not an object")

    augment_names = raw.get("augment_names")
    if not isinstance(augment_names, list):
        augment_names = [augment_names] if augment_names else []
    augment_names = [str(name).strip() for name in augment_names if str(name).strip()]

    item = {
        "augment_names": augment_names,
        "tier": str(raw.get("tier") or "").strip(),
        "rating": str(raw.get("rating") or "未知").strip() or "未知",
        "tag": str(raw.get("tag") or "强力联动").strip() or "强力联动",
        "author": str(raw.get("author") or "").strip(),
        "is_original": bool(raw.get("is_original")),
        "content": str(raw.get("content") or "").strip(),
        "upvotes": int_or_reject(raw.get("upvotes"), hero_id=hero_id, index=index, field="upvotes"),
        "downvotes": int_or_reject(raw.get("downvotes"), hero_id=hero_id, index=index, field="downvotes"),
    }

    for field in REQUIRED_TEXT_FIELDS:
        if not as_non_empty(item.get(field)):
            raise RejectedHero(f"{hero_id}: card[{index}].{field} is empty")
    return item


def candidate_from_oracle_result(result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    hero = result.get("hero")
    if not isinstance(hero, dict) or not hero.get("id"):
        raise RejectedHero("oracle result missing hero.id")
    hero_id = str(hero["id"])
    cards = result.get("cards")
    if not isinstance(cards, list):
        raise RejectedHero(f"{hero_id}: oracle cards is not a list")
    return hero_id, {
        "hero": hero,
        "declared_count": result.get("declared_count"),
        "actual_card_count": result.get("actual_card_count"),
        "synergy_items": cards,
    }


def candidate_from_delta(path: Path) -> tuple[str, dict[str, Any]]:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise RejectedHero(f"{path.name}: delta root is not an object")

    if "hero" in payload and "cards" in payload:
        return candidate_from_oracle_result(payload)

    hero_id = str(payload.get("id") or payload.get("hero_id") or path.stem.split("_", 1)[0])
    if not hero_id:
        raise RejectedHero(f"{path.name}: missing hero id")
    return hero_id, payload


def strict_payload(
    hero_id: str,
    candidate: dict[str, Any],
    baseline_hero: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    raw_items = candidate.get("synergy_items")
    if raw_items is None:
        raw_items = candidate.get("cards")
    if not isinstance(raw_items, list):
        raise RejectedHero(f"{hero_id}: {source} synergy_items/cards is not a list")

    declared_count = candidate.get("declared_count")
    actual_card_count = candidate.get("actual_card_count")
    # baseline 条目只有 synergy_items/synergies，没有计数字段；干净 checkout 缺少
    # 本地 oracle/delta 时全部英雄都会走 baseline 来源，按现有条目数补齐计数
    if source == "baseline" and declared_count is None and actual_card_count is None:
        declared_count = len(raw_items)
        actual_card_count = len(raw_items)
    if declared_count != actual_card_count or actual_card_count != len(raw_items):
        raise RejectedHero(
            f"{hero_id}: {source} count mismatch declared={declared_count!r} "
            f"actual={actual_card_count!r} len={len(raw_items)}"
        )

    normalized_items = [
        normalize_item(item, hero_id=hero_id, index=index)
        for index, item in enumerate(raw_items)
    ]
    payload = copy.deepcopy(baseline_hero)
    payload["id"] = str(payload.get("id") or hero_id)

    hero_meta = candidate.get("hero")
    if isinstance(hero_meta, dict):
        for field in ("name", "title", "en_name", "aliases"):
            if field in hero_meta and hero_meta[field]:
                payload[field] = hero_meta[field]

    payload["declared_count"] = int(declared_count)
    payload["actual_card_count"] = int(actual_card_count)
    payload["synergy_items"] = normalized_items
    payload["synergies"] = [compat_string(dict(item, _hero_id=hero_id)) for item in normalized_items]
    return payload


def load_oracle_passes(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    oracle = read_json(path)
    results = oracle.get("results") if isinstance(oracle, dict) else None
    if not isinstance(results, list):
        raise ValueError(f"{path} missing results[]")

    passes: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict) or result.get("validation_status") != "pass":
            continue
        hero_id, candidate = candidate_from_oracle_result(result)
        passes[hero_id] = candidate
    return passes


def load_oracle_failed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    oracle = read_json(path)
    results = oracle.get("results") if isinstance(oracle, dict) else None
    if not isinstance(results, list):
        raise ValueError(f"{path} missing results[]")

    failed_ids: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or result.get("validation_status") != "fail":
            continue
        hero = result.get("hero")
        if isinstance(hero, dict) and hero.get("id"):
            failed_ids.add(str(hero["id"]))
    return failed_ids


def load_deltas(delta_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    deltas: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if not delta_dir.exists():
        return deltas, errors

    for path in sorted(delta_dir.glob("*.json")):
        try:
            hero_id, candidate = candidate_from_delta(path)
        except (OSError, json.JSONDecodeError, RejectedHero) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        deltas[hero_id] = candidate
    return deltas, errors


def build_merged_payload(root: Path) -> tuple[dict[str, Any] | None, list[str], int]:
    synergy_dir = root / "run" / "data" / "raw" / "synergy"
    baseline_path = synergy_dir / BASELINE_NAME
    oracle_path = synergy_dir / ORACLE_NAME
    delta_dir = synergy_dir / DELTA_DIR_NAME

    baseline = read_json(baseline_path)
    if not isinstance(baseline, dict):
        raise ValueError(f"{baseline_path} root is not an object")

    oracle_passes = load_oracle_passes(oracle_path)
    oracle_failed_ids = load_oracle_failed_ids(oracle_path)
    deltas, rejected = load_deltas(delta_dir)
    merged: dict[str, Any] = {}
    accepted = 0

    if len(baseline) != EXPECTED_HERO_COUNT:
        rejected.append(f"baseline hero count is {len(baseline)}, expected {EXPECTED_HERO_COUNT}")

    for hero_id, baseline_hero in baseline.items():
        if not isinstance(baseline_hero, dict):
            rejected.append(f"{hero_id}: baseline hero is not an object")
            continue

        source = "baseline"
        candidate = baseline_hero
        if hero_id in oracle_passes:
            source = "oracle"
            candidate = oracle_passes[hero_id]
        if hero_id in oracle_failed_ids and hero_id not in deltas:
            rejected.append(f"{hero_id}: missing codex_goal_delta for oracle failed target")
            continue
        if hero_id in deltas:
            source = "codex_goal_delta"
            candidate = deltas[hero_id]

        try:
            merged[hero_id] = strict_payload(hero_id, candidate, baseline_hero, source=source)
            accepted += 1
        except RejectedHero as exc:
            rejected.append(str(exc))

    unknown_delta_ids = sorted(set(deltas) - set(baseline))
    for hero_id in unknown_delta_ids:
        rejected.append(f"{hero_id}: delta hero not found in baseline")

    if rejected:
        return None, rejected, accepted
    return merged, rejected, accepted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合并 Codex goal delta、oracle pass 和 0519 baseline。")
    parser.add_argument("--check", action="store_true", help="只做严格校验，不写回 baseline。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    baseline_path = root / "run" / "data" / "raw" / "synergy" / BASELINE_NAME

    try:
        merged, rejected, accepted = build_merged_payload(root)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2

    total = EXPECTED_HERO_COUNT
    for reason in rejected:
        print(f"reject: {reason}", file=sys.stderr)

    print(f"strict-pass: {accepted}/{total}; rejected: {len(rejected)}")

    if rejected or merged is None:
        return 1

    if args.check:
        print("check-only: baseline 未写入")
        return 0

    write_json_atomic(baseline_path, merged)
    print(f"wrote: {baseline_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
