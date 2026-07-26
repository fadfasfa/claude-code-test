"""离线量化 overlay 识别通道的真值命中率与分数分离度。

读取真机自动产出的 `overlay_vision_timelines/*.jsonl` 与人工真值 JSON，统计
text/text_alt/icon 三通道 Top-1 的命中率，以及命中组与未命中组的 confidence/
margin 分布差距。它不截图、不加载模板、不修改任何判定规则——产出的分离度
报告是后续调整识别阈值（或改指纹/ROI）的唯一依据。

用法：
  python tooling/diagnostics/vision_separation.py --list
      列出可标注的 (session, epoch) 与各槽最终名称，辅助人工写真值。
  python tooling/diagnostics/vision_separation.py --truth truth.json [--json]
      truth 结构：{"epochs": [{"session_id": "...", "selection_epoch": 5,
                    "expected_slots": ["卡名A", "卡名B", "卡名C"]}]}
      expected_slots 允许 null/"" 表示该槽无真值，跳过。

调用方: 人工诊断; 关键依赖: overlay_vision_timelines JSONL、recommendation.hints。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.infrastructure.vision.sidecar_common import (  # noqa: E402
    OVERLAY_VISION_TRACE_FILE,
    VISION_TIMELINE_DIRNAME,
)
from hextech.modules.recommendation.hints import normalize_augment_id  # noqa: E402


DEFAULT_TIMELINE_DIR = OVERLAY_VISION_TRACE_FILE.parent / VISION_TIMELINE_DIRNAME
CHANNELS = ("text", "text_alt", "icon")


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _identity(value: Any) -> str:
    return normalize_augment_id(_clean_text(value))


def _load_timeline_entries(timeline_dir: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(timeline_dir.glob("selection-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, Mapping):
                entries.append(dict(entry))
    return entries


def _recognition_entries(entries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """只保留真实截图识别 observation；暂停/失败探针不含通道分数。"""

    return [
        entry
        for entry in entries
        if str(entry.get("observation_kind") or "recognition") == "recognition"
        and str(entry.get("capture_status") or "captured") == "captured"
    ]


def _load_truth(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    epochs = payload.get("epochs") if isinstance(payload, Mapping) else None
    if not isinstance(epochs, Sequence) or isinstance(epochs, (str, bytes)):
        raise ValueError(f"truth payload epochs must be a list: {path}")
    result: list[dict[str, Any]] = []
    for item in epochs:
        if not isinstance(item, Mapping):
            continue
        session_id = _clean_text(item.get("session_id"))
        try:
            epoch = int(item.get("selection_epoch") or 0)
        except (TypeError, ValueError):
            continue
        expected = item.get("expected_slots")
        if not session_id or epoch <= 0 or not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
            continue
        result.append(
            {
                "session_id": session_id,
                "selection_epoch": epoch,
                "expected_slots": [_clean_text(value) for value in list(expected)[:3]],
            }
        )
    return result


def _percentile(ordered: list[float], ratio: float) -> float | None:
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return round(ordered[index], 6)


def _distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    count = len(ordered)
    mean = sum(ordered) / count if count else None
    return {
        "count": count,
        "min": round(ordered[0], 6) if count else None,
        "p50": _percentile(ordered, 0.5),
        "p95": _percentile(ordered, 0.95),
        "mean": round(mean, 6) if mean is not None else None,
    }


def _pooled_std(hits: list[float], misses: list[float]) -> float | None:
    def variance(values: list[float]) -> float | None:
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        return sum((value - mean) ** 2 for value in values) / (len(values) - 1)

    var_hit, var_miss = variance(hits), variance(misses)
    parts = [
        (len(values) - 1) * var
        for values, var in ((hits, var_hit), (misses, var_miss))
        if var is not None
    ]
    freedom = sum(len(values) - 1 for values, var in ((hits, var_hit), (misses, var_miss)) if var is not None)
    if not parts or freedom <= 0:
        return None
    return (sum(parts) / freedom) ** 0.5


def _channel_report(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """observations: [{hit: bool, confidence: float|None, margin: float|None}]"""

    hits = [item for item in observations if item["hit"]]
    misses = [item for item in observations if not item["hit"]]

    def metric_values(items: list[dict[str, Any]], key: str) -> list[float]:
        return [float(item[key]) for item in items if isinstance(item.get(key), (int, float))]

    hit_margins = metric_values(hits, "margin")
    miss_margins = metric_values(misses, "margin")
    pooled = _pooled_std(hit_margins, miss_margins)
    mean_gap = None
    cohen_d = None
    if hit_margins and miss_margins:
        mean_gap = round(sum(hit_margins) / len(hit_margins) - sum(miss_margins) / len(miss_margins), 6)
        if pooled:
            cohen_d = round((sum(hit_margins) / len(hit_margins) - sum(miss_margins) / len(miss_margins)) / pooled, 3)
    return {
        "observations": len(observations),
        "top1_hit_rate": round(len(hits) / len(observations), 4) if observations else None,
        "hit_margin": _distribution(hit_margins),
        "miss_margin": _distribution(miss_margins),
        "hit_confidence": _distribution(metric_values(hits, "confidence")),
        "miss_confidence": _distribution(metric_values(misses, "confidence")),
        # margin 均值差与 Cohen's d：d 明显（≥0.8）说明 margin 有信息量，可做
        # 阈值/归一化；d 很小说明问题在指纹本身，调阈值只是平移错误。
        "margin_mean_gap": mean_gap,
        "margin_cohen_d": cohen_d,
    }


def analyze_separation(
    entries: Sequence[Mapping[str, Any]],
    truth: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    truth_index = {
        (item["session_id"], int(item["selection_epoch"])): [
            _identity(value) for value in item["expected_slots"]
        ]
        for item in truth
    }
    per_channel: dict[str, list[dict[str, Any]]] = {channel: [] for channel in CHANNELS}
    matched_epochs: set[tuple[str, int]] = set()
    for entry in _recognition_entries(entries):
        key = (str(entry.get("session_id") or ""), int(entry.get("selection_epoch") or 0))
        expected = truth_index.get(key)
        if expected is None:
            continue
        matched_epochs.add(key)
        slots = entry.get("slots") if isinstance(entry.get("slots"), list) else []
        for index, slot in enumerate(slots[:3]):
            if not isinstance(slot, Mapping):
                continue
            expected_identity = expected[index] if index < len(expected) else ""
            if not expected_identity:
                continue
            for channel in CHANNELS:
                payload = slot.get(channel) if isinstance(slot.get(channel), Mapping) else {}
                top_identity = _identity(payload.get("name")) or _identity(payload.get("augment_id"))
                if not top_identity:
                    continue
                per_channel[channel].append(
                    {
                        "hit": top_identity == expected_identity,
                        "confidence": payload.get("confidence"),
                        "margin": payload.get("margin"),
                    }
                )
    return {
        "truth_epochs": len(truth_index),
        "matched_epochs": len(matched_epochs),
        "unmatched_epochs": sorted(
            f"{session_id}#e{epoch}" for session_id, epoch in set(truth_index) - matched_epochs
        ),
        "channels": {channel: _channel_report(items) for channel, items in per_channel.items()},
    }


def list_labelable_epochs(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 (session, epoch) 汇总可标注对象与最终槽名，辅助人工写真值。"""

    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for entry in _recognition_entries(entries):
        key = (str(entry.get("session_id") or ""), int(entry.get("selection_epoch") or 0))
        grouped.setdefault(key, []).append(entry)
    result: list[dict[str, Any]] = []
    for (session_id, epoch), items in sorted(grouped.items()):
        slot_names: list[str] = []
        for index in range(3):
            names = Counter(
                _clean_text(entry_slots[index].get("name"))
                for entry in items
                for entry_slots in [entry.get("slots") or []]
                if index < len(entry_slots)
                and isinstance(entry_slots[index], Mapping)
                and _clean_text(entry_slots[index].get("name"))
            )
            slot_names.append(names.most_common(1)[0][0] if names else "")
        result.append(
            {
                "session_id": session_id,
                "selection_epoch": epoch,
                "recognition_observations": len(items),
                "final_slot_names": slot_names,
            }
        )
    return result


def _print_separation_summary(summary: Mapping[str, Any]) -> None:
    print(f"真值 epoch：{summary['truth_epochs']}，匹配到时间线：{summary['matched_epochs']}")
    for missing in summary["unmatched_epochs"]:
        print(f"  [警告] 真值未匹配任何时间线条目：{missing}")
    channels = summary["channels"]
    for channel in CHANNELS:
        report = channels[channel]
        print(f"[{channel}] observations={report['observations']} top1命中率={report['top1_hit_rate']}")
        print(
            f"  命中 margin  {report['hit_margin']} | 未命中 margin {report['miss_margin']}"
        )
        print(
            f"  命中 conf    {report['hit_confidence']} | 未命中 conf   {report['miss_confidence']}"
        )
        print(
            f"  margin 均值差={report['margin_mean_gap']}  Cohen's d={report['margin_cohen_d']}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量化 overlay 识别通道真值命中率与 margin 分离度。")
    parser.add_argument("--timeline-dir", type=Path, default=DEFAULT_TIMELINE_DIR)
    parser.add_argument("--truth", type=Path, help="人工真值 JSON；缺省时配合 --list 使用。")
    parser.add_argument("--list", action="store_true", help="列出可标注的 (session, epoch) 与最终槽名。")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON。")
    args = parser.parse_args(argv)

    if not args.timeline_dir.is_dir():
        print(f"[错误] 时间线目录不存在：{args.timeline_dir}（需要新 Build 真机进入过选择阶段）")
        print(
            "  正式安装版数据请显式传入 --timeline-dir "
            "%LOCALAPPDATA%\\HextechNexus\\var\\state\\overlay_vision_timelines"
        )
        return 1
    entries = _load_timeline_entries(args.timeline_dir)
    if args.list:
        rows = list_labelable_epochs(entries)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                names = " / ".join(name or "?" for name in row["final_slot_names"])
                print(
                    f"session={row['session_id']} epoch={row['selection_epoch']} "
                    f"obs={row['recognition_observations']} slots={names}"
                )
            if not rows:
                print("时间线中没有可标注的识别 observation。")
        return 0
    if args.truth is None:
        parser.error("需要 --truth 或 --list 之一")
    summary = analyze_separation(entries, _load_truth(args.truth))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_separation_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
