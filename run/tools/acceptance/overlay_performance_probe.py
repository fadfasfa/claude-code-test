"""游戏内显示性能验收摘要工具。

本工具只生成结构化性能报告，供阶段 5 人工记录四种服务状态和延迟样本。默认不写
运行态文件、不启动服务、不访问网络。

调用方: dev_checks; 关键依赖: 见 imports。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from typing import Any, Mapping, Sequence


SERVICE_STATE_KEYS = ("all_off", "web_only", "game_overlay_only", "web_and_overlay")


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(len(ordered) * max(0.0, min(100.0, percentile)) / 100.0))
    return round(ordered[min(len(ordered) - 1, rank - 1)], 3)


def _normalize_service_sample(sample: Mapping[str, Any] | None) -> dict[str, float]:
    source = sample if isinstance(sample, Mapping) else {}
    return {
        "rss_mb": round(_coerce_float(source.get("rss_mb")), 3),
        "cpu_percent": round(_coerce_float(source.get("cpu_percent")), 3),
    }


def build_overlay_performance_report(
    *,
    service_samples: Mapping[str, Mapping[str, Any]] | None = None,
    latency_samples_ms: Sequence[float] | None = None,
    recognition_samples_ms: Sequence[float] | None = None,
    render_samples_ms: Sequence[float] | None = None,
    source_tag: str = "manual",
) -> dict[str, Any]:
    """生成阶段 5 性能记录结构；真实采样由人工或后续自动探针填入。"""

    services = service_samples if isinstance(service_samples, Mapping) else {}
    latency_samples = [float(value) for value in (latency_samples_ms or [])]
    recognition_samples = [float(value) for value in (recognition_samples_ms or [])]
    render_samples = [float(value) for value in (render_samples_ms or [])]
    overlay_p95_ms = _percentile(latency_samples, 95)
    return {
        "generated_at": time.time(),
        "source": {"tag": str(source_tag or "manual")},
        "service_states": {
            key: _normalize_service_sample(services.get(key))
            for key in SERVICE_STATE_KEYS
        },
        "latency": {
            "samples_ms": [round(value, 3) for value in latency_samples],
            "count": len(latency_samples),
            "avg_ms": round(statistics.fmean(latency_samples), 3) if latency_samples else 0.0,
            "p50_ms": _percentile(latency_samples, 50),
            "p95_ms": overlay_p95_ms,
            "pass_p95": bool(latency_samples and overlay_p95_ms <= 500.0),
            "segments": {
                "recognition": _latency_summary(recognition_samples, target_p95_ms=300.0),
                "render": _latency_summary(render_samples, target_p95_ms=200.0),
            },
        },
        "targets": {
            "recognition_p95_ms": 300.0,
            "overlay_p95_ms": 500.0,
        },
        "warm_path_only": True,
        "manual_acceptance_required": True,
    }


def _latency_summary(samples: Sequence[float], *, target_p95_ms: float) -> dict[str, Any]:
    p95_ms = _percentile(samples, 95)
    return {
        "samples_ms": [round(float(value), 3) for value in samples],
        "count": len(samples),
        "p50_ms": _percentile(samples, 50),
        "p95_ms": p95_ms,
        "target_p95_ms": float(target_p95_ms),
        "pass_p95": bool(samples and p95_ms <= float(target_p95_ms)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Hextech 游戏内显示性能验收摘要。")
    parser.add_argument("--latency-ms", nargs="*", type=float, default=[], help="手工录入的端到端延迟样本。")
    parser.add_argument("--recognition-ms", nargs="*", type=float, default=[], help="识别事件写入前的延迟样本。")
    parser.add_argument("--render-ms", nargs="*", type=float, default=[], help="事件写入到 overlay 渲染的延迟样本。")
    parser.add_argument("--source-tag", default="manual")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_overlay_performance_report(
        latency_samples_ms=args.latency_ms,
        recognition_samples_ms=args.recognition_ms,
        render_samples_ms=args.render_ms,
        source_tag=args.source_tag,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
