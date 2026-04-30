# -*- coding: utf-8 -*-
"""
QQQ/SPY 五年收益率基线 overlay 月度回测脚本。

复用逻辑：
- 使用 data_io.load_monthly_close_series() 读取本地月线，不联网、不触发数据同步。
- 使用 strategies.registry.build_strategy_registry() 逐月调用现有策略对象生成 Base 信号。

独立复刻/新增逻辑：
- 五年收益率基线高抛低吸 continuous/ladder overlay、T+1 月执行、组合回测、参数扫描与指标计算都在本文件内实现。
- 本脚本只写 output/ 下的回测 CSV，不写 position_history 或 decision_engine 日志。
"""

from __future__ import annotations

import math
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from config import ALLOCATION_WEIGHTS
from data_io import load_monthly_close_series
from strategies.registry import build_strategy_registry


START_DATE = pd.Timestamp("2008-01-01")
INITIAL_CAPITAL = 100000.0
ASSETS = ("QQQ", "SPY")
DEFAULT_OVERLAY_PARAMS = {
    "QQQ": {"beta": 0.20, "band": 0.12},
    "SPY": {"beta": 0.20, "band": 0.08},
}
DEFAULT_LADDER_PARAMS = {
    "QQQ": {
        "start_threshold": 0.10,
        "step_size": 0.05,
        "position_step": 0.05,
        "max_adjustment": 0.20,
        "base_zero_cap": 0.15,
    },
    "SPY": {
        "start_threshold": 0.10,
        "step_size": 0.05,
        "position_step": 0.05,
        "max_adjustment": 0.20,
        "base_zero_cap": 0.15,
    },
}
SCAN_GRID = {
    "QQQ": {
        "beta": [0.00, 0.05, 0.10, 0.15, 0.20],
        "band": [0.10, 0.12, 0.15, 0.20],
    },
    "SPY": {
        "beta": [0.00, 0.05, 0.10, 0.15, 0.20],
        "band": [0.08, 0.10, 0.12, 0.15],
    },
}
LADDER_SCAN_GRID = {
    "QQQ": {
        "start_threshold": [0.10, 0.15, 0.20],
        "step_size": [0.05],
        "position_step": [0.025, 0.05, 0.075],
        "max_adjustment": [0.10, 0.15, 0.20],
        "base_zero_cap": [0.10, 0.15, 0.20],
    },
    "SPY": {
        "start_threshold": [0.10, 0.15, 0.20],
        "step_size": [0.05],
        "position_step": [0.025, 0.05, 0.075],
        "max_adjustment": [0.10, 0.15, 0.20],
        "base_zero_cap": [0.10, 0.15, 0.20],
    },
}
QQQ_SELL_ONLY_SCAN_GRID = {
    "start_threshold": [0.25, 0.30, 0.35, 0.40],
    "step_size": [0.05, 0.10],
    "position_step": [0.025, 0.05],
    "max_adjustment": [0.05, 0.10, 0.15],
    "base_zero_cap": [0.0],
}
SPY_SELECTED_LADDER_PARAMS = {
    "start_threshold": 0.20,
    "step_size": 0.05,
    "position_step": 0.05,
    "max_adjustment": 0.10,
    "base_zero_cap": 0.10,
}
PORTFOLIO_MODES = {
    "config_weights": None,
    "equal_weight_full_capital": {"QQQ": 0.50, "SPY": 0.50},
}
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
SUMMARY_CSV = OUTPUT_DIR / "backtest_return_overlay_summary.csv"
MONTHLY_CSV = OUTPUT_DIR / "backtest_return_overlay_monthly.csv"
PARAM_SCAN_CSV = OUTPUT_DIR / "backtest_return_overlay_param_scan.csv"
CANDIDATES_CSV = OUTPUT_DIR / "backtest_return_overlay_candidates.csv"
LADDER_SCAN_CSV = OUTPUT_DIR / "backtest_return_overlay_ladder_scan.csv"
LADDER_CANDIDATES_CSV = OUTPUT_DIR / "backtest_return_overlay_ladder_candidates.csv"
QQQ_SELL_ONLY_SCAN_CSV = OUTPUT_DIR / "backtest_return_overlay_qqq_sell_only_scan.csv"
QQQ_SELL_ONLY_CANDIDATES_CSV = OUTPUT_DIR / "backtest_return_overlay_qqq_sell_only_candidates.csv"
QQQ_SELL_ONLY_SUMMARY_CSV = OUTPUT_DIR / "backtest_return_overlay_qqq_sell_only_summary.csv"
SPY_ONLY_FULL_SUMMARY_CSV = OUTPUT_DIR / "backtest_return_overlay_spy_only_full_summary.csv"
SPY_ONLY_SEGMENTS_CSV = OUTPUT_DIR / "backtest_return_overlay_spy_only_segments.csv"
SPY_ONLY_DELTA_CSV = OUTPUT_DIR / "backtest_return_overlay_spy_only_delta.csv"
SPY_ONLY_MONTHLY_CSV = OUTPUT_DIR / "backtest_return_overlay_spy_only_monthly.csv"
TRADE_EPSILON = 1e-6
CAGR_LOSS_LIMIT = 0.002
SHARPE_LOSS_LIMIT = 0.02
LADDER_MDD_IMPROVEMENT_MIN = 0.005
SELL_ONLY_MDD_IMPROVEMENT_MIN = 0.003
TURNOVER_MAX_MULTIPLIER = 1.20


def _require_monthly_close(asset: str) -> pd.Series:
    series = load_monthly_close_series(asset)
    if series is None or series.empty:
        raise RuntimeError(f"{asset} 月线读取失败：请检查本地 CSV、列名和 .sha256 校验。")

    series = pd.to_numeric(series, errors="coerce").dropna().sort_index()
    series = series[~series.index.duplicated(keep="last")]
    if series.empty:
        raise RuntimeError(f"{asset} 月线清洗后为空。")
    if series.index.max() < START_DATE:
        raise RuntimeError(f"{asset} 数据结束时间早于回测起点 {START_DATE.date()}。")
    return series


def get_base_signals(asset: str, monthly_close: pd.Series) -> pd.Series:
    """逐月复用当前项目策略对象，避免历史回测和当前信号实现分叉。"""
    registry = build_strategy_registry()
    if asset not in registry:
        raise RuntimeError(f"{asset} 未在 strategies.registry 中注册。")

    strategy = registry[asset]
    values: list[float] = []
    for signal_date in monthly_close.index:
        visible_history = monthly_close.loc[:signal_date]
        result = strategy.compute(visible_history)
        values.append(float(np.clip(result.signal_weight, 0.0, 1.0)))

    return pd.Series(values, index=monthly_close.index, name=f"{asset}_base_signal")


def _previous_year_last_close(monthly_close: pd.Series) -> pd.Series:
    year_last_close = monthly_close.groupby(monthly_close.index.year).last()
    values = []
    for signal_date in monthly_close.index:
        values.append(float(year_last_close.get(signal_date.year - 1, np.nan)))
    return pd.Series(values, index=monthly_close.index, name="prev_year_last_close")


def compute_return_overlay(
    asset: str,
    monthly_close: pd.Series,
    base_signal: pd.Series,
    beta: float,
    band: float,
) -> pd.DataFrame:
    if band <= 0:
        raise ValueError(f"{asset} band 必须大于 0。")
    if not 0 <= beta <= 1:
        raise ValueError(f"{asset} beta 必须在 [0, 1] 内。")

    aligned = pd.concat(
        [
            monthly_close.rename("close"),
            base_signal.reindex(monthly_close.index).rename("w_base"),
        ],
        axis=1,
    )
    aligned["w_base"] = aligned["w_base"].clip(0.0, 1.0)

    prev_year_close = _previous_year_last_close(aligned["close"])
    aligned["ytd_return"] = aligned["close"] / prev_year_close - 1.0

    # 五年基线只使用当前月之前的 60 个完整月，避免把当前月价格混入基准。
    prior_window_end = aligned["close"].shift(1)
    prior_window_start = aligned["close"].shift(61)
    valid_window = (prior_window_end > 0) & (prior_window_start > 0)
    aligned["five_year_cagr"] = np.nan
    aligned.loc[valid_window, "five_year_cagr"] = (
        (prior_window_end[valid_window] / prior_window_start[valid_window]) ** (1.0 / 5.0)
    ) - 1.0

    month_number = pd.Series(
        [idx.month for idx in aligned.index],
        index=aligned.index,
        dtype=float,
        name="month_number",
    )
    aligned["expected_ytd"] = ((1.0 + aligned["five_year_cagr"]) ** (month_number / 12.0)) - 1.0
    aligned["gap"] = aligned["ytd_return"] - aligned["expected_ytd"]
    aligned["w_mr"] = 0.5 - 0.5 * np.tanh(aligned["gap"] / band)

    w_final = ((1.0 - beta) * aligned["w_base"]) + (beta * aligned["w_mr"])
    w_final = w_final.where(aligned["w_mr"].notna(), aligned["w_base"])
    w_final = w_final.clip(0.0, 1.0)

    zero_base = aligned["w_base"].abs() <= TRADE_EPSILON
    w_final.loc[zero_base] = np.minimum(w_final.loc[zero_base], 0.25)
    aligned["w_final"] = w_final
    return aligned


def compute_ladder_overlay_adjustment(
    gap: float,
    start_threshold: float,
    step_size: float,
    position_step: float,
    max_adjustment: float,
) -> float:
    if pd.isna(gap):
        return 0.0
    if start_threshold < 0 or step_size <= 0 or position_step <= 0 or max_adjustment < 0:
        raise ValueError("ladder 参数必须满足 threshold>=0, step/position_step>0, max_adjustment>=0。")

    gap_abs = abs(float(gap))
    if gap_abs < start_threshold:
        adjustment = 0.0
    else:
        step_count = math.floor((gap_abs - start_threshold) / step_size) + 1
        adjustment = min(step_count * position_step, max_adjustment)

    if gap > 0:
        return -adjustment
    if gap < 0:
        return adjustment
    return 0.0


def compute_ladder_overlay(
    asset: str,
    monthly_close: pd.Series,
    base_signal: pd.Series,
    start_threshold: float,
    step_size: float,
    position_step: float,
    max_adjustment: float,
    base_zero_cap: float,
) -> pd.DataFrame:
    if base_zero_cap < 0:
        raise ValueError(f"{asset} base_zero_cap 必须大于等于 0。")

    # 复用 continuous overlay 的基线字段，只用其中的 ytd/gap，不使用 tanh 仓位。
    aligned = compute_return_overlay(
        asset,
        monthly_close,
        base_signal,
        beta=0.0,
        band=max(DEFAULT_OVERLAY_PARAMS[asset]["band"], 1e-9),
    )
    adjustments = aligned["gap"].map(
        lambda gap: compute_ladder_overlay_adjustment(
            gap,
            start_threshold=start_threshold,
            step_size=step_size,
            position_step=position_step,
            max_adjustment=max_adjustment,
        )
    )
    aligned["ladder_adjustment"] = adjustments

    w_base = aligned["w_base"].fillna(0.0).clip(0.0, 1.0)
    w_final = (w_base + adjustments).clip(0.0, 1.0)
    zero_base = w_base.abs() <= TRADE_EPSILON
    positive_adjustment = adjustments > 0
    w_final.loc[zero_base & positive_adjustment] = np.minimum(
        adjustments.loc[zero_base & positive_adjustment],
        base_zero_cap,
    )
    w_final.loc[zero_base & ~positive_adjustment] = 0.0
    aligned["w_ladder"] = w_final.clip(0.0, 1.0)
    return aligned


def compute_qqq_sell_only_ladder_adjustment(
    gap: float,
    start_threshold: float,
    step_size: float,
    position_step: float,
    max_adjustment: float,
) -> float:
    if pd.isna(gap) or gap <= start_threshold:
        return 0.0
    if start_threshold < 0 or step_size <= 0 or position_step <= 0 or max_adjustment < 0:
        raise ValueError("sell-only ladder 参数必须满足 threshold>=0, step/position_step>0, max_adjustment>=0。")

    step_count = math.floor((float(gap) - start_threshold) / step_size) + 1
    adjustment = min(step_count * position_step, max_adjustment)
    return -adjustment


def compute_qqq_sell_only_ladder_overlay(
    monthly_close: pd.Series,
    base_signal: pd.Series,
    start_threshold: float,
    step_size: float,
    position_step: float,
    max_adjustment: float,
    base_zero_cap: float,
) -> pd.DataFrame:
    if base_zero_cap != 0:
        raise ValueError("QQQ sell-only 第一版要求 base_zero_cap=0。")

    # 只复用五年基线字段。sell-only 分支不做 gap<0 的低吸加仓。
    aligned = compute_return_overlay(
        "QQQ",
        monthly_close,
        base_signal,
        beta=0.0,
        band=max(DEFAULT_OVERLAY_PARAMS["QQQ"]["band"], 1e-9),
    )
    adjustments = aligned["gap"].map(
        lambda gap: compute_qqq_sell_only_ladder_adjustment(
            gap,
            start_threshold=start_threshold,
            step_size=step_size,
            position_step=position_step,
            max_adjustment=max_adjustment,
        )
    )
    aligned["sell_only_adjustment"] = adjustments
    aligned["w_sell_only"] = (aligned["w_base"].fillna(0.0) + adjustments).clip(0.0, 1.0)
    return aligned


def apply_t_plus_one(signal: pd.Series) -> pd.Series:
    return signal.shift(1).fillna(0.0).clip(0.0, 1.0)


def _position_changes(position: pd.Series) -> pd.Series:
    if position.empty:
        return position.copy()
    changes = position.diff().abs()
    changes.iloc[0] = abs(position.iloc[0])
    return changes.fillna(0.0)


def _annual_avg_max_drawdown(equity: pd.Series) -> float:
    drawdowns = []
    for _, year_equity in equity.groupby(equity.index.year):
        if year_equity.empty:
            continue
        year_drawdown = year_equity / year_equity.cummax() - 1.0
        drawdowns.append(float(year_drawdown.min()))
    return float(np.mean(drawdowns)) if drawdowns else np.nan


def calc_metrics(
    label: str,
    monthly_returns: pd.Series,
    position: pd.Series,
    equity: pd.Series,
    turnover_changes: pd.Series | None = None,
) -> dict[str, float | str]:
    monthly_returns = pd.to_numeric(monthly_returns, errors="coerce").dropna()
    position = pd.to_numeric(position.reindex(monthly_returns.index), errors="coerce").fillna(0.0)
    equity = pd.to_numeric(equity.reindex(monthly_returns.index), errors="coerce").dropna()

    if monthly_returns.empty or equity.empty:
        raise RuntimeError(f"{label} 没有可计算的月度回测数据。")

    start = monthly_returns.index[0]
    end = monthly_returns.index[-1]
    years = max((end - start).days / 365.25, 1e-12)
    total_return = float(equity.iloc[-1] / INITIAL_CAPITAL - 1.0)
    cagr = float((equity.iloc[-1] / INITIAL_CAPITAL) ** (1.0 / years) - 1.0)
    drawdown = equity / equity.cummax() - 1.0
    monthly_std = monthly_returns.std(ddof=1)
    volatility = float(monthly_std * math.sqrt(12.0))
    sharpe = np.nan
    if monthly_std > 0:
        sharpe = float(monthly_returns.mean() / monthly_std * math.sqrt(12.0))

    if turnover_changes is None:
        turnover_changes = _position_changes(position)
    else:
        turnover_changes = turnover_changes.reindex(monthly_returns.index).fillna(0.0)

    return {
        "Name": label,
        "Start": start.strftime("%Y-%m-%d"),
        "End": end.strftime("%Y-%m-%d"),
        "Total Return": total_return,
        "CAGR": cagr,
        "Max Drawdown": float(drawdown.min()),
        "Annual Avg Max Drawdown": _annual_avg_max_drawdown(equity),
        "Sharpe": sharpe,
        "Volatility": volatility,
        "Final Equity": float(equity.iloc[-1]),
        "Average Position": float(position.mean()),
        "Min Position": float(position.min()),
        "Max Position": float(position.max()),
        "Turnover": float(turnover_changes.sum()),
        "Trade Count": int((turnover_changes > TRADE_EPSILON).sum()),
    }


def _asset_frame_for_params(
    asset: str,
    monthly_close: pd.Series,
    base_signal: pd.Series,
    beta: float,
    band: float,
) -> pd.DataFrame:
    overlay_frame = compute_return_overlay(asset, monthly_close, base_signal, beta=beta, band=band)
    returns = monthly_close.pct_change().rename("asset_return")
    frame = pd.concat(
        [
            monthly_close.rename("close"),
            returns,
            base_signal.rename("base_signal"),
            overlay_frame[["w_mr", "w_final", "ytd_return", "five_year_cagr", "expected_ytd", "gap"]],
        ],
        axis=1,
    )
    frame["base_position"] = apply_t_plus_one(frame["base_signal"])
    frame["overlay_position"] = apply_t_plus_one(frame["w_final"])
    frame = frame.loc[frame.index >= START_DATE].dropna(subset=["asset_return"]).copy()
    if frame.empty:
        raise RuntimeError(f"{asset} 在 {START_DATE.date()} 之后没有可回测数据。")

    frame["base_return"] = frame["asset_return"] * frame["base_position"]
    frame["overlay_return"] = frame["asset_return"] * frame["overlay_position"]
    frame["base_equity"] = INITIAL_CAPITAL * (1.0 + frame["base_return"]).cumprod()
    frame["overlay_equity"] = INITIAL_CAPITAL * (1.0 + frame["overlay_return"]).cumprod()
    frame["beta"] = beta
    frame["band"] = band
    return frame


def _asset_frame_for_ladder_params(
    asset: str,
    monthly_close: pd.Series,
    base_signal: pd.Series,
    start_threshold: float,
    step_size: float,
    position_step: float,
    max_adjustment: float,
    base_zero_cap: float,
) -> pd.DataFrame:
    overlay_frame = compute_ladder_overlay(
        asset,
        monthly_close,
        base_signal,
        start_threshold=start_threshold,
        step_size=step_size,
        position_step=position_step,
        max_adjustment=max_adjustment,
        base_zero_cap=base_zero_cap,
    )
    returns = monthly_close.pct_change().rename("asset_return")
    frame = pd.concat(
        [
            monthly_close.rename("close"),
            returns,
            base_signal.rename("base_signal"),
            overlay_frame[
                [
                    "w_mr",
                    "w_final",
                    "w_ladder",
                    "ladder_adjustment",
                    "ytd_return",
                    "five_year_cagr",
                    "expected_ytd",
                    "gap",
                ]
            ],
        ],
        axis=1,
    )
    frame["base_position"] = apply_t_plus_one(frame["base_signal"])
    frame["ladder_position"] = apply_t_plus_one(frame["w_ladder"])
    frame = frame.loc[frame.index >= START_DATE].dropna(subset=["asset_return"]).copy()
    if frame.empty:
        raise RuntimeError(f"{asset} 在 {START_DATE.date()} 之后没有可回测数据。")

    frame["base_return"] = frame["asset_return"] * frame["base_position"]
    frame["ladder_return"] = frame["asset_return"] * frame["ladder_position"]
    frame["base_equity"] = INITIAL_CAPITAL * (1.0 + frame["base_return"]).cumprod()
    frame["ladder_equity"] = INITIAL_CAPITAL * (1.0 + frame["ladder_return"]).cumprod()
    frame["start_threshold"] = start_threshold
    frame["step_size"] = step_size
    frame["position_step"] = position_step
    frame["max_adjustment"] = max_adjustment
    frame["base_zero_cap"] = base_zero_cap
    return frame


def _asset_frame_for_qqq_sell_only_params(
    monthly_close: pd.Series,
    base_signal: pd.Series,
    start_threshold: float,
    step_size: float,
    position_step: float,
    max_adjustment: float,
    base_zero_cap: float,
) -> pd.DataFrame:
    overlay_frame = compute_qqq_sell_only_ladder_overlay(
        monthly_close,
        base_signal,
        start_threshold=start_threshold,
        step_size=step_size,
        position_step=position_step,
        max_adjustment=max_adjustment,
        base_zero_cap=base_zero_cap,
    )
    returns = monthly_close.pct_change().rename("asset_return")
    frame = pd.concat(
        [
            monthly_close.rename("close"),
            returns,
            base_signal.rename("base_signal"),
            overlay_frame[
                [
                    "w_sell_only",
                    "sell_only_adjustment",
                    "ytd_return",
                    "five_year_cagr",
                    "expected_ytd",
                    "gap",
                ]
            ],
        ],
        axis=1,
    )
    frame["base_position"] = apply_t_plus_one(frame["base_signal"])
    frame["sell_only_position"] = apply_t_plus_one(frame["w_sell_only"])
    frame = frame.loc[frame.index >= START_DATE].dropna(subset=["asset_return"]).copy()
    if frame.empty:
        raise RuntimeError(f"QQQ 在 {START_DATE.date()} 之后没有可回测数据。")

    frame["base_return"] = frame["asset_return"] * frame["base_position"]
    frame["sell_only_return"] = frame["asset_return"] * frame["sell_only_position"]
    frame["base_equity"] = INITIAL_CAPITAL * (1.0 + frame["base_return"]).cumprod()
    frame["sell_only_equity"] = INITIAL_CAPITAL * (1.0 + frame["sell_only_return"]).cumprod()
    frame["start_threshold"] = start_threshold
    frame["step_size"] = step_size
    frame["position_step"] = position_step
    frame["max_adjustment"] = max_adjustment
    frame["base_zero_cap"] = base_zero_cap
    return frame


def backtest_single_asset(
    asset: str,
    monthly_close: pd.Series,
    base_signal: pd.Series,
    beta: float,
    band: float,
) -> tuple[list[dict[str, float | str]], pd.DataFrame]:
    frame = _asset_frame_for_params(asset, monthly_close, base_signal, beta, band)
    metrics = [
        calc_metrics(f"{asset} Base", frame["base_return"], frame["base_position"], frame["base_equity"]),
        calc_metrics(
            f"{asset} Continuous Overlay",
            frame["overlay_return"],
            frame["overlay_position"],
            frame["overlay_equity"],
        ),
    ]
    return metrics, frame


def backtest_single_asset_ladder(
    asset: str,
    monthly_close: pd.Series,
    base_signal: pd.Series,
    params: dict[str, float],
) -> tuple[dict[str, float | str], pd.DataFrame]:
    frame = _asset_frame_for_ladder_params(asset, monthly_close, base_signal, **params)
    metrics = calc_metrics(
        f"{asset} Ladder Overlay",
        frame["ladder_return"],
        frame["ladder_position"],
        frame["ladder_equity"],
    )
    return metrics, frame


def _config_weights() -> tuple[dict[str, float], str]:
    try:
        qqq_weight = float(ALLOCATION_WEIGHTS["QQQ"])
        spy_weight = float(ALLOCATION_WEIGHTS["SPY"])
    except Exception:
        return {"QQQ": 0.5, "SPY": 0.5}, "config 权重不可用，使用 QQQ/SPY 50%/50%"

    if qqq_weight < 0 or spy_weight < 0 or qqq_weight + spy_weight <= 0:
        return {"QQQ": 0.5, "SPY": 0.5}, "config 权重无效，使用 QQQ/SPY 50%/50%"
    if qqq_weight + spy_weight > 1:
        total = qqq_weight + spy_weight
        return {"QQQ": qqq_weight / total, "SPY": spy_weight / total}, "config QQQ/SPY 权重和超过 100%，已归一化"
    return {"QQQ": qqq_weight, "SPY": spy_weight}, "使用 config.ALLOCATION_WEIGHTS；未分配部分保留现金"


def _portfolio_weights(mode: str) -> tuple[dict[str, float], str]:
    if mode == "config_weights":
        return _config_weights()
    if mode == "equal_weight_full_capital":
        return {"QQQ": 0.50, "SPY": 0.50}, "QQQ/SPY 50%/50%；未投入部分保留现金"
    raise ValueError(f"未知组合模式：{mode}")


def _portfolio_frame_for_assets(
    asset_frames: dict[str, pd.DataFrame],
    mode: str,
    overlay_kind: str = "continuous",
) -> tuple[pd.DataFrame, str]:
    if overlay_kind == "continuous":
        overlay_position_col = "overlay_position"
        overlay_return_col = "overlay_return"
    elif overlay_kind == "ladder":
        overlay_position_col = "ladder_position"
        overlay_return_col = "ladder_return"
    else:
        raise ValueError(f"未知 overlay_kind：{overlay_kind}")

    weights, weight_note = _portfolio_weights(mode)
    common_index = None
    for asset in ASSETS:
        index = asset_frames[asset].index
        common_index = index if common_index is None else common_index.intersection(index)
    if common_index is None or common_index.empty:
        raise RuntimeError("QQQ/SPY 没有共同回测月份，无法计算组合。")

    frame = pd.DataFrame(index=common_index.sort_values())
    base_returns = []
    overlay_returns = []
    base_positions = []
    overlay_positions = []
    base_turnover_changes = []
    overlay_turnover_changes = []

    for asset in ASSETS:
        asset_frame = asset_frames[asset].reindex(frame.index)
        weight = weights[asset]
        frame[f"{asset}_weight"] = weight
        frame[f"{asset}_asset_return"] = asset_frame["asset_return"]
        frame[f"{asset}_base_position"] = asset_frame["base_position"]
        frame[f"{asset}_overlay_position"] = asset_frame[overlay_position_col]

        base_returns.append(weight * asset_frame["base_return"])
        overlay_returns.append(weight * asset_frame[overlay_return_col])
        base_positions.append(weight * asset_frame["base_position"])
        overlay_positions.append(weight * asset_frame[overlay_position_col])
        base_turnover_changes.append(weight * _position_changes(asset_frame["base_position"]))
        overlay_turnover_changes.append(weight * _position_changes(asset_frame[overlay_position_col]))

    frame["base_return"] = pd.concat(base_returns, axis=1).sum(axis=1)
    frame["overlay_return"] = pd.concat(overlay_returns, axis=1).sum(axis=1)
    frame["base_position"] = pd.concat(base_positions, axis=1).sum(axis=1)
    frame["overlay_position"] = pd.concat(overlay_positions, axis=1).sum(axis=1)
    frame["base_turnover_change"] = pd.concat(base_turnover_changes, axis=1).sum(axis=1)
    frame["overlay_turnover_change"] = pd.concat(overlay_turnover_changes, axis=1).sum(axis=1)
    frame["base_equity"] = INITIAL_CAPITAL * (1.0 + frame["base_return"]).cumprod()
    frame["overlay_equity"] = INITIAL_CAPITAL * (1.0 + frame["overlay_return"]).cumprod()
    return frame, weight_note


def backtest_portfolio(
    asset_frames: dict[str, pd.DataFrame],
    mode: str,
    overlay_kind: str = "continuous",
) -> tuple[list[dict[str, float | str]], pd.DataFrame, str]:
    frame, weight_note = _portfolio_frame_for_assets(asset_frames, mode, overlay_kind=overlay_kind)
    if mode == "config_weights":
        base_name = "Portfolio Base config weights"
        overlay_name = (
            "Portfolio Continuous Overlay config weights"
            if overlay_kind == "continuous"
            else "Portfolio Ladder Overlay config weights"
        )
    else:
        base_name = "Portfolio Base 50/50"
        overlay_name = (
            "Portfolio Continuous Overlay 50/50"
            if overlay_kind == "continuous"
            else "Portfolio Ladder Overlay 50/50"
        )

    metrics = [
        calc_metrics(
            base_name,
            frame["base_return"],
            frame["base_position"],
            frame["base_equity"],
            frame["base_turnover_change"],
        ),
        calc_metrics(
            overlay_name,
            frame["overlay_return"],
            frame["overlay_position"],
            frame["overlay_equity"],
            frame["overlay_turnover_change"],
        ),
    ]
    return metrics, frame, weight_note


def _metric_value(metrics: dict[str, float | str], key: str) -> float:
    value = metrics.get(key)
    return float(value) if value is not None and pd.notna(value) else np.nan


def _scan_row(
    scope: str,
    base_metrics: dict[str, float | str],
    test_metrics: dict[str, float | str],
    qqq_beta: float | None = None,
    qqq_band: float | None = None,
    spy_beta: float | None = None,
    spy_band: float | None = None,
    mode: str = "",
) -> dict[str, float | str | bool]:
    base_cagr = _metric_value(base_metrics, "CAGR")
    base_sharpe = _metric_value(base_metrics, "Sharpe")
    base_mdd = _metric_value(base_metrics, "Max Drawdown")
    test_cagr = _metric_value(test_metrics, "CAGR")
    test_sharpe = _metric_value(test_metrics, "Sharpe")
    test_mdd = _metric_value(test_metrics, "Max Drawdown")
    cagr_loss = base_cagr - test_cagr
    sharpe_loss = base_sharpe - test_sharpe
    mdd_improvement = test_mdd - base_mdd
    return {
        "Scope": scope,
        "Mode": mode,
        "QQQ beta": qqq_beta,
        "QQQ band": qqq_band,
        "SPY beta": spy_beta,
        "SPY band": spy_band,
        "Name": test_metrics["Name"],
        "Base CAGR": base_cagr,
        "CAGR": test_cagr,
        "CAGR Loss": cagr_loss,
        "Base Sharpe": base_sharpe,
        "Sharpe": test_sharpe,
        "Sharpe Loss": sharpe_loss,
        "Base Max Drawdown": base_mdd,
        "Max Drawdown": test_mdd,
        "Max Drawdown Improvement": mdd_improvement,
        "Turnover": _metric_value(test_metrics, "Turnover"),
        "Trade Count": int(test_metrics["Trade Count"]),
        "Final Equity": _metric_value(test_metrics, "Final Equity"),
        "Pass Sharpe": bool(sharpe_loss <= SHARPE_LOSS_LIMIT),
        "Pass CAGR": bool(cagr_loss <= CAGR_LOSS_LIMIT),
        "Candidate": bool(
            mdd_improvement > 0
            and cagr_loss <= CAGR_LOSS_LIMIT
            and sharpe_loss <= SHARPE_LOSS_LIMIT
        ),
    }


def _sort_scan(scan: pd.DataFrame) -> pd.DataFrame:
    if scan.empty:
        return scan
    sorted_scan = scan.copy()
    sorted_scan["Pass Sharpe Sort"] = sorted_scan["Pass Sharpe"].astype(int)
    sorted_scan["Pass CAGR Sort"] = sorted_scan["Pass CAGR"].astype(int)
    sorted_scan = sorted_scan.sort_values(
        by=[
            "Pass Sharpe Sort",
            "Pass CAGR Sort",
            "Max Drawdown Improvement",
            "Turnover",
            "CAGR",
            "Sharpe",
        ],
        ascending=[False, False, False, True, False, False],
        kind="mergesort",
    )
    return sorted_scan.drop(columns=["Pass Sharpe Sort", "Pass CAGR Sort"])


def scan_single_asset(
    asset: str,
    monthly_close: pd.Series,
    base_signal: pd.Series,
    base_metrics: dict[str, float | str],
) -> pd.DataFrame:
    rows = []
    for beta, band in product(SCAN_GRID[asset]["beta"], SCAN_GRID[asset]["band"]):
        frame = _asset_frame_for_params(asset, monthly_close, base_signal, beta, band)
        test_metrics = calc_metrics(
            f"{asset} beta={beta:.2f} band={band:.2f}",
            frame["overlay_return"],
            frame["overlay_position"],
            frame["overlay_equity"],
        )
        rows.append(
            _scan_row(
                scope=asset,
                base_metrics=base_metrics,
                test_metrics=test_metrics,
                qqq_beta=beta if asset == "QQQ" else None,
                qqq_band=band if asset == "QQQ" else None,
                spy_beta=beta if asset == "SPY" else None,
                spy_band=band if asset == "SPY" else None,
            )
        )
    return _sort_scan(pd.DataFrame(rows))


def scan_portfolio(
    monthly_data: dict[str, pd.Series],
    base_signals: dict[str, pd.Series],
    base_metrics_by_mode: dict[str, dict[str, float | str]],
) -> pd.DataFrame:
    rows = []
    qqq_grid = list(product(SCAN_GRID["QQQ"]["beta"], SCAN_GRID["QQQ"]["band"]))
    spy_grid = list(product(SCAN_GRID["SPY"]["beta"], SCAN_GRID["SPY"]["band"]))
    for qqq_params, spy_params in product(qqq_grid, spy_grid):
        qqq_beta, qqq_band = qqq_params
        spy_beta, spy_band = spy_params
        asset_frames = {
            "QQQ": _asset_frame_for_params("QQQ", monthly_data["QQQ"], base_signals["QQQ"], qqq_beta, qqq_band),
            "SPY": _asset_frame_for_params("SPY", monthly_data["SPY"], base_signals["SPY"], spy_beta, spy_band),
        }
        for mode in PORTFOLIO_MODES:
            portfolio_frame, _ = _portfolio_frame_for_assets(asset_frames, mode)
            test_metrics = calc_metrics(
                f"Portfolio {mode} qqq={qqq_beta:.2f}/{qqq_band:.2f} spy={spy_beta:.2f}/{spy_band:.2f}",
                portfolio_frame["overlay_return"],
                portfolio_frame["overlay_position"],
                portfolio_frame["overlay_equity"],
                portfolio_frame["overlay_turnover_change"],
            )
            rows.append(
                _scan_row(
                    scope="Portfolio",
                    mode=mode,
                    base_metrics=base_metrics_by_mode[mode],
                    test_metrics=test_metrics,
                    qqq_beta=qqq_beta,
                    qqq_band=qqq_band,
                    spy_beta=spy_beta,
                    spy_band=spy_band,
                )
            )
    return _sort_scan(pd.DataFrame(rows))


def _ladder_param_grid(asset: str) -> list[dict[str, float]]:
    grid = LADDER_SCAN_GRID[asset]
    params = []
    for start_threshold, step_size, position_step, max_adjustment, base_zero_cap in product(
        grid["start_threshold"],
        grid["step_size"],
        grid["position_step"],
        grid["max_adjustment"],
        grid["base_zero_cap"],
    ):
        params.append(
            {
                "start_threshold": start_threshold,
                "step_size": step_size,
                "position_step": position_step,
                "max_adjustment": max_adjustment,
                "base_zero_cap": base_zero_cap,
            }
        )
    return params


def _ladder_param_key(params: dict[str, float]) -> tuple[float, float, float, float, float]:
    return (
        params["start_threshold"],
        params["step_size"],
        params["position_step"],
        params["max_adjustment"],
        params["base_zero_cap"],
    )


def _expand_ladder_params(prefix: str, params: dict[str, float] | None) -> dict[str, float | None]:
    fields = ["start_threshold", "step_size", "position_step", "max_adjustment", "base_zero_cap"]
    return {f"{prefix} {field}": (None if params is None else params[field]) for field in fields}


def _ladder_scan_row(
    scope: str,
    base_metrics: dict[str, float | str],
    test_metrics: dict[str, float | str],
    qqq_params: dict[str, float] | None = None,
    spy_params: dict[str, float] | None = None,
    mode: str = "",
) -> dict[str, float | str | bool]:
    base_cagr = _metric_value(base_metrics, "CAGR")
    base_sharpe = _metric_value(base_metrics, "Sharpe")
    base_mdd = _metric_value(base_metrics, "Max Drawdown")
    base_turnover = _metric_value(base_metrics, "Turnover")
    test_cagr = _metric_value(test_metrics, "CAGR")
    test_sharpe = _metric_value(test_metrics, "Sharpe")
    test_mdd = _metric_value(test_metrics, "Max Drawdown")
    test_turnover = _metric_value(test_metrics, "Turnover")
    cagr_loss = base_cagr - test_cagr
    sharpe_loss = base_sharpe - test_sharpe
    mdd_improvement = test_mdd - base_mdd
    turnover_limit = base_turnover * TURNOVER_MAX_MULTIPLIER
    pass_cagr = cagr_loss <= CAGR_LOSS_LIMIT
    pass_sharpe = sharpe_loss <= SHARPE_LOSS_LIMIT
    pass_mdd = mdd_improvement >= LADDER_MDD_IMPROVEMENT_MIN
    pass_turnover = test_turnover <= turnover_limit
    row: dict[str, float | str | bool] = {
        "Scope": scope,
        "Mode": mode,
        "Name": test_metrics["Name"],
        "Base CAGR": base_cagr,
        "CAGR": test_cagr,
        "CAGR Loss": cagr_loss,
        "Base Sharpe": base_sharpe,
        "Sharpe": test_sharpe,
        "Sharpe Loss": sharpe_loss,
        "Base Max Drawdown": base_mdd,
        "Max Drawdown": test_mdd,
        "Max Drawdown Improvement": mdd_improvement,
        "Base Turnover": base_turnover,
        "Turnover": test_turnover,
        "Turnover Limit": turnover_limit,
        "Trade Count": int(test_metrics["Trade Count"]),
        "Final Equity": _metric_value(test_metrics, "Final Equity"),
        "Pass CAGR": bool(pass_cagr),
        "Pass Sharpe": bool(pass_sharpe),
        "Pass Max Drawdown": bool(pass_mdd),
        "Pass Turnover": bool(pass_turnover),
        "Candidate": bool(pass_cagr and pass_sharpe and pass_mdd and pass_turnover),
    }
    row.update(_expand_ladder_params("QQQ", qqq_params))
    row.update(_expand_ladder_params("SPY", spy_params))
    return row


def _sort_ladder_scan(scan: pd.DataFrame) -> pd.DataFrame:
    if scan.empty:
        return scan
    sorted_scan = scan.copy()
    for column in ["Pass CAGR", "Pass Sharpe", "Pass Max Drawdown", "Pass Turnover", "Candidate"]:
        sorted_scan[f"{column} Sort"] = sorted_scan[column].astype(int)
    sorted_scan = sorted_scan.sort_values(
        by=[
            "Candidate Sort",
            "Pass CAGR Sort",
            "Pass Sharpe Sort",
            "Pass Max Drawdown Sort",
            "Pass Turnover Sort",
            "Max Drawdown Improvement",
            "Turnover",
            "CAGR",
            "Sharpe",
        ],
        ascending=[False, False, False, False, False, False, True, False, False],
        kind="mergesort",
    )
    return sorted_scan.drop(
        columns=[
            "Candidate Sort",
            "Pass CAGR Sort",
            "Pass Sharpe Sort",
            "Pass Max Drawdown Sort",
            "Pass Turnover Sort",
        ]
    )


def build_ladder_asset_frames(
    monthly_data: dict[str, pd.Series],
    base_signals: dict[str, pd.Series],
) -> dict[str, dict[tuple[float, float, float, float, float], tuple[dict[str, float], pd.DataFrame]]]:
    cache: dict[str, dict[tuple[float, float, float, float, float], tuple[dict[str, float], pd.DataFrame]]] = {}
    for asset in ASSETS:
        cache[asset] = {}
        for params in _ladder_param_grid(asset):
            key = _ladder_param_key(params)
            cache[asset][key] = (
                params,
                _asset_frame_for_ladder_params(asset, monthly_data[asset], base_signals[asset], **params),
            )
    return cache


def scan_ladder_single_asset(
    asset: str,
    ladder_cache: dict[str, dict[tuple[float, float, float, float, float], tuple[dict[str, float], pd.DataFrame]]],
    base_metrics: dict[str, float | str],
) -> pd.DataFrame:
    rows = []
    for params, frame in ladder_cache[asset].values():
        test_metrics = calc_metrics(
            f"{asset} ladder {params}",
            frame["ladder_return"],
            frame["ladder_position"],
            frame["ladder_equity"],
        )
        rows.append(
            _ladder_scan_row(
                scope=asset,
                base_metrics=base_metrics,
                test_metrics=test_metrics,
                qqq_params=params if asset == "QQQ" else None,
                spy_params=params if asset == "SPY" else None,
            )
        )
    return _sort_ladder_scan(pd.DataFrame(rows))


def scan_ladder_portfolio(
    ladder_cache: dict[str, dict[tuple[float, float, float, float, float], tuple[dict[str, float], pd.DataFrame]]],
    base_metrics_by_mode: dict[str, dict[str, float | str]],
) -> pd.DataFrame:
    rows = []
    qqq_items = list(ladder_cache["QQQ"].values())
    spy_items = list(ladder_cache["SPY"].values())
    for (qqq_params, qqq_frame), (spy_params, spy_frame) in product(qqq_items, spy_items):
        asset_frames = {"QQQ": qqq_frame, "SPY": spy_frame}
        for mode in PORTFOLIO_MODES:
            portfolio_frame, _ = _portfolio_frame_for_assets(asset_frames, mode, overlay_kind="ladder")
            test_metrics = calc_metrics(
                f"Portfolio ladder {mode}",
                portfolio_frame["overlay_return"],
                portfolio_frame["overlay_position"],
                portfolio_frame["overlay_equity"],
                portfolio_frame["overlay_turnover_change"],
            )
            rows.append(
                _ladder_scan_row(
                    scope="Portfolio",
                    mode=mode,
                    base_metrics=base_metrics_by_mode[mode],
                    test_metrics=test_metrics,
                    qqq_params=qqq_params,
                    spy_params=spy_params,
                )
            )
    return _sort_ladder_scan(pd.DataFrame(rows))


def _comparison_row(
    name: str,
    base_metrics: dict[str, float | str],
    ladder_metrics: dict[str, float | str],
) -> dict[str, float | str]:
    return {
        "Name": name,
        "Base CAGR": _metric_value(base_metrics, "CAGR"),
        "Ladder CAGR": _metric_value(ladder_metrics, "CAGR"),
        "CAGR Loss": _metric_value(base_metrics, "CAGR") - _metric_value(ladder_metrics, "CAGR"),
        "Base Sharpe": _metric_value(base_metrics, "Sharpe"),
        "Ladder Sharpe": _metric_value(ladder_metrics, "Sharpe"),
        "Sharpe Loss": _metric_value(base_metrics, "Sharpe") - _metric_value(ladder_metrics, "Sharpe"),
        "Base Max Drawdown": _metric_value(base_metrics, "Max Drawdown"),
        "Ladder Max Drawdown": _metric_value(ladder_metrics, "Max Drawdown"),
        "Max Drawdown Improvement": _metric_value(ladder_metrics, "Max Drawdown") - _metric_value(base_metrics, "Max Drawdown"),
        "Base Turnover": _metric_value(base_metrics, "Turnover"),
        "Ladder Turnover": _metric_value(ladder_metrics, "Turnover"),
    }


def _qqq_sell_only_param_grid() -> list[dict[str, float]]:
    params = []
    for start_threshold, step_size, position_step, max_adjustment, base_zero_cap in product(
        QQQ_SELL_ONLY_SCAN_GRID["start_threshold"],
        QQQ_SELL_ONLY_SCAN_GRID["step_size"],
        QQQ_SELL_ONLY_SCAN_GRID["position_step"],
        QQQ_SELL_ONLY_SCAN_GRID["max_adjustment"],
        QQQ_SELL_ONLY_SCAN_GRID["base_zero_cap"],
    ):
        params.append(
            {
                "start_threshold": start_threshold,
                "step_size": step_size,
                "position_step": position_step,
                "max_adjustment": max_adjustment,
                "base_zero_cap": base_zero_cap,
            }
        )
    return params


def _sell_only_scan_row(
    base_metrics: dict[str, float | str],
    test_metrics: dict[str, float | str],
    params: dict[str, float],
) -> dict[str, float | str | bool]:
    base_cagr = _metric_value(base_metrics, "CAGR")
    base_sharpe = _metric_value(base_metrics, "Sharpe")
    base_mdd = _metric_value(base_metrics, "Max Drawdown")
    base_turnover = _metric_value(base_metrics, "Turnover")
    test_cagr = _metric_value(test_metrics, "CAGR")
    test_sharpe = _metric_value(test_metrics, "Sharpe")
    test_mdd = _metric_value(test_metrics, "Max Drawdown")
    test_turnover = _metric_value(test_metrics, "Turnover")
    cagr_loss = base_cagr - test_cagr
    sharpe_loss = base_sharpe - test_sharpe
    mdd_improvement = test_mdd - base_mdd
    turnover_limit = base_turnover * TURNOVER_MAX_MULTIPLIER
    pass_cagr = cagr_loss <= CAGR_LOSS_LIMIT
    pass_sharpe = sharpe_loss <= SHARPE_LOSS_LIMIT
    pass_mdd = mdd_improvement >= SELL_ONLY_MDD_IMPROVEMENT_MIN
    pass_turnover = test_turnover <= turnover_limit
    return {
        "Scope": "QQQ",
        "Name": test_metrics["Name"],
        "start_threshold": params["start_threshold"],
        "step_size": params["step_size"],
        "position_step": params["position_step"],
        "max_adjustment": params["max_adjustment"],
        "base_zero_cap": params["base_zero_cap"],
        "Base CAGR": base_cagr,
        "CAGR": test_cagr,
        "CAGR Loss": cagr_loss,
        "Base Sharpe": base_sharpe,
        "Sharpe": test_sharpe,
        "Sharpe Loss": sharpe_loss,
        "Base Max Drawdown": base_mdd,
        "Max Drawdown": test_mdd,
        "Max Drawdown Improvement": mdd_improvement,
        "Base Turnover": base_turnover,
        "Turnover": test_turnover,
        "Turnover Limit": turnover_limit,
        "Trade Count": int(test_metrics["Trade Count"]),
        "Final Equity": _metric_value(test_metrics, "Final Equity"),
        "Pass CAGR": bool(pass_cagr),
        "Pass Sharpe": bool(pass_sharpe),
        "Pass Max Drawdown": bool(pass_mdd),
        "Pass Turnover": bool(pass_turnover),
        "Candidate": bool(pass_cagr and pass_sharpe and pass_mdd and pass_turnover),
    }


def _sort_sell_only_scan(scan: pd.DataFrame) -> pd.DataFrame:
    if scan.empty:
        return scan
    sorted_scan = scan.copy()
    for column in ["Pass CAGR", "Pass Sharpe", "Pass Max Drawdown", "Pass Turnover", "Candidate"]:
        sorted_scan[f"{column} Sort"] = sorted_scan[column].astype(int)
    sorted_scan = sorted_scan.sort_values(
        by=[
            "Candidate Sort",
            "Pass CAGR Sort",
            "Pass Sharpe Sort",
            "Pass Max Drawdown Sort",
            "Pass Turnover Sort",
            "Max Drawdown Improvement",
            "Turnover",
            "CAGR",
            "Sharpe",
        ],
        ascending=[False, False, False, False, False, False, True, False, False],
        kind="mergesort",
    )
    return sorted_scan.drop(
        columns=[
            "Candidate Sort",
            "Pass CAGR Sort",
            "Pass Sharpe Sort",
            "Pass Max Drawdown Sort",
            "Pass Turnover Sort",
        ]
    )


def scan_qqq_sell_only(
    monthly_close: pd.Series,
    base_signal: pd.Series,
    base_metrics: dict[str, float | str],
) -> tuple[pd.DataFrame, dict[tuple[float, float, float, float, float], tuple[dict[str, float], pd.DataFrame]]]:
    rows = []
    cache: dict[tuple[float, float, float, float, float], tuple[dict[str, float], pd.DataFrame]] = {}
    for params in _qqq_sell_only_param_grid():
        key = _ladder_param_key(params)
        frame = _asset_frame_for_qqq_sell_only_params(monthly_close, base_signal, **params)
        cache[key] = (params, frame)
        test_metrics = calc_metrics(
            f"QQQ sell-only {params}",
            frame["sell_only_return"],
            frame["sell_only_position"],
            frame["sell_only_equity"],
        )
        rows.append(_sell_only_scan_row(base_metrics, test_metrics, params))
    return _sort_sell_only_scan(pd.DataFrame(rows)), cache


def _selected_sell_only_result(
    scan: pd.DataFrame,
    cache: dict[tuple[float, float, float, float, float], tuple[dict[str, float], pd.DataFrame]],
) -> tuple[dict[str, float], pd.DataFrame, bool]:
    selected_row = scan.loc[scan["Candidate"]].head(1)
    selected_is_candidate = True
    if selected_row.empty:
        selected_row = scan.head(1)
        selected_is_candidate = False
    if selected_row.empty:
        raise RuntimeError("QQQ sell-only 扫描没有产生任何结果。")

    row = selected_row.iloc[0]
    params = {
        "start_threshold": float(row["start_threshold"]),
        "step_size": float(row["step_size"]),
        "position_step": float(row["position_step"]),
        "max_adjustment": float(row["max_adjustment"]),
        "base_zero_cap": float(row["base_zero_cap"]),
    }
    _, frame = cache[_ladder_param_key(params)]
    return params, frame, selected_is_candidate


def _custom_portfolio_50_50(
    name: str,
    qqq_frame: pd.DataFrame,
    qqq_return_col: str,
    qqq_position_col: str,
    spy_frame: pd.DataFrame,
    spy_return_col: str,
    spy_position_col: str,
) -> tuple[dict[str, float | str], pd.DataFrame]:
    common_index = qqq_frame.index.intersection(spy_frame.index).sort_values()
    if common_index.empty:
        raise RuntimeError(f"{name} 没有共同回测月份。")
    frame = pd.DataFrame(index=common_index)
    frame["QQQ_return"] = qqq_frame.reindex(common_index)[qqq_return_col]
    frame["QQQ_position"] = qqq_frame.reindex(common_index)[qqq_position_col]
    frame["SPY_return"] = spy_frame.reindex(common_index)[spy_return_col]
    frame["SPY_position"] = spy_frame.reindex(common_index)[spy_position_col]
    frame["return"] = 0.5 * frame["QQQ_return"] + 0.5 * frame["SPY_return"]
    frame["position"] = 0.5 * frame["QQQ_position"] + 0.5 * frame["SPY_position"]
    frame["turnover_change"] = (
        0.5 * _position_changes(frame["QQQ_position"])
        + 0.5 * _position_changes(frame["SPY_position"])
    )
    frame["equity"] = INITIAL_CAPITAL * (1.0 + frame["return"]).cumprod()
    metrics = calc_metrics(name, frame["return"], frame["position"], frame["equity"], frame["turnover_change"])
    return metrics, frame


def _format_summary(summary: pd.DataFrame) -> pd.DataFrame:
    formatted = summary.copy()
    percent_columns = [
        "Total Return",
        "CAGR",
        "Max Drawdown",
        "Annual Avg Max Drawdown",
        "Volatility",
        "Average Position",
        "Min Position",
        "Max Position",
        "Turnover",
    ]
    for column in percent_columns:
        formatted[column] = formatted[column].map(lambda value: "N/A" if pd.isna(value) else f"{value:.2%}")
    formatted["Sharpe"] = formatted["Sharpe"].map(lambda value: "N/A" if pd.isna(value) else f"{value:.2f}")
    formatted["Final Equity"] = formatted["Final Equity"].map(lambda value: f"{value:,.2f}")
    return formatted


def _format_pct_or_blank(value) -> str:
    numeric_value = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric_value):
        return "" if pd.isna(value) else str(value)
    return f"{numeric_value:.2%}"


def _format_float_or_blank(value) -> str:
    numeric_value = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric_value):
        return "" if pd.isna(value) else str(value)
    return f"{numeric_value:.3f}"


def _format_scan(scan: pd.DataFrame) -> pd.DataFrame:
    formatted = scan.copy()
    percent_columns = [
        "QQQ beta",
        "QQQ band",
        "SPY beta",
        "SPY band",
        "QQQ start_threshold",
        "QQQ step_size",
        "QQQ position_step",
        "QQQ max_adjustment",
        "QQQ base_zero_cap",
        "SPY start_threshold",
        "SPY step_size",
        "SPY position_step",
        "SPY max_adjustment",
        "SPY base_zero_cap",
        "start_threshold",
        "step_size",
        "position_step",
        "max_adjustment",
        "base_zero_cap",
        "Base CAGR",
        "CAGR",
        "CAGR Loss",
        "Base Max Drawdown",
        "Max Drawdown",
        "Max Drawdown Improvement",
        "Base Turnover",
        "Turnover",
        "Turnover Limit",
        "Ladder CAGR",
        "Ladder Max Drawdown",
        "Base Turnover",
        "Ladder Turnover",
    ]
    for column in percent_columns:
        if column in formatted.columns:
            formatted[column] = formatted[column].map(_format_pct_or_blank)
    for column in ["Base Sharpe", "Sharpe", "Sharpe Loss", "Ladder Sharpe"]:
        if column in formatted.columns:
            formatted[column] = formatted[column].map(_format_float_or_blank)
    if "Final Equity" in formatted.columns:
        formatted["Final Equity"] = formatted["Final Equity"].map(lambda value: f"{value:,.2f}")
    return formatted


def _build_monthly_output(asset_frames: dict[str, pd.DataFrame], portfolio_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for asset, frame in asset_frames.items():
        parts.append(frame.add_prefix(f"{asset}_"))
    for mode, frame in portfolio_frames.items():
        parts.append(frame.add_prefix(f"Portfolio_{mode}_"))
    monthly = pd.concat(parts, axis=1).sort_index()
    monthly.index.name = "Date"
    return monthly.reset_index()


def _print_table(title: str, table: pd.DataFrame, max_rows: int | None = None) -> None:
    print(f"=== {title} ===")
    if table.empty:
        print("(empty)")
    else:
        shown = table if max_rows is None else table.head(max_rows)
        print(shown.to_string(index=False))
    print()


def _weighted_two_asset_portfolio(
    name: str,
    qqq_frame: pd.DataFrame,
    qqq_return_col: str,
    qqq_position_col: str,
    spy_frame: pd.DataFrame,
    spy_return_col: str,
    spy_position_col: str,
    weights: dict[str, float],
) -> tuple[dict[str, float | str], pd.DataFrame]:
    common_index = qqq_frame.index.intersection(spy_frame.index).sort_values()
    if common_index.empty:
        raise RuntimeError(f"{name} 没有共同回测月份。")

    frame = pd.DataFrame(index=common_index)
    frame["QQQ_weight"] = weights["QQQ"]
    frame["SPY_weight"] = weights["SPY"]
    frame["QQQ_return"] = qqq_frame.reindex(common_index)[qqq_return_col]
    frame["QQQ_position"] = qqq_frame.reindex(common_index)[qqq_position_col]
    frame["SPY_return"] = spy_frame.reindex(common_index)[spy_return_col]
    frame["SPY_position"] = spy_frame.reindex(common_index)[spy_position_col]
    frame["return"] = weights["QQQ"] * frame["QQQ_return"] + weights["SPY"] * frame["SPY_return"]
    frame["position"] = weights["QQQ"] * frame["QQQ_position"] + weights["SPY"] * frame["SPY_position"]
    frame["turnover_change"] = (
        weights["QQQ"] * _position_changes(frame["QQQ_position"])
        + weights["SPY"] * _position_changes(frame["SPY_position"])
    )
    frame["equity"] = INITIAL_CAPITAL * (1.0 + frame["return"]).cumprod()
    metrics = calc_metrics(name, frame["return"], frame["position"], frame["equity"], frame["turnover_change"])
    return metrics, frame


def _metrics_from_returns(
    name: str,
    monthly_returns: pd.Series,
    position: pd.Series,
    turnover_changes: pd.Series | None = None,
) -> dict[str, float | str]:
    clean_returns = pd.to_numeric(monthly_returns, errors="coerce").dropna()
    if clean_returns.empty:
        raise RuntimeError(f"{name} 没有可计算的区间收益。")
    equity = INITIAL_CAPITAL * (1.0 + clean_returns).cumprod()
    return calc_metrics(name, clean_returns, position.reindex(clean_returns.index), equity, turnover_changes)


def _segment_ranges(latest_date: pd.Timestamp) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    return [
        ("Full", pd.Timestamp("2008-01-01"), latest_date),
        ("2008-2013", pd.Timestamp("2008-01-01"), pd.Timestamp("2013-12-31")),
        ("2014-2019", pd.Timestamp("2014-01-01"), pd.Timestamp("2019-12-31")),
        ("2020-2022", pd.Timestamp("2020-01-01"), pd.Timestamp("2022-12-31")),
        ("2023-End", pd.Timestamp("2023-01-01"), latest_date),
    ]


def _slice_frame(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return frame.loc[(frame.index >= start) & (frame.index <= end)].copy()


def _segment_metric_row(
    segment: str,
    name: str,
    frame: pd.DataFrame,
    return_col: str,
    position_col: str,
    turnover_col: str | None = None,
) -> dict[str, float | str]:
    turnover_changes = frame[turnover_col] if turnover_col is not None and turnover_col in frame.columns else None
    metrics = _metrics_from_returns(name, frame[return_col], frame[position_col], turnover_changes)
    return {"Segment": segment, **metrics}


def _delta_row(
    segment: str,
    comparison: str,
    base_metrics: dict[str, float | str],
    test_metrics: dict[str, float | str],
) -> dict[str, float | str]:
    return {
        "Segment": segment,
        "Comparison": comparison,
        "CAGR Delta": _metric_value(test_metrics, "CAGR") - _metric_value(base_metrics, "CAGR"),
        "MaxDD Improvement": _metric_value(test_metrics, "Max Drawdown") - _metric_value(base_metrics, "Max Drawdown"),
        "Sharpe Delta": _metric_value(test_metrics, "Sharpe") - _metric_value(base_metrics, "Sharpe"),
        "Volatility Delta": _metric_value(test_metrics, "Volatility") - _metric_value(base_metrics, "Volatility"),
        "Turnover Delta": _metric_value(test_metrics, "Turnover") - _metric_value(base_metrics, "Turnover"),
        "Trade Count Delta": int(test_metrics["Trade Count"]) - int(base_metrics["Trade Count"]),
    }


def _format_spy_only_delta(delta: pd.DataFrame) -> pd.DataFrame:
    formatted = delta.copy()
    for column in ["CAGR Delta", "MaxDD Improvement", "Volatility Delta", "Turnover Delta"]:
        formatted[column] = formatted[column].map(_format_pct_or_blank)
    formatted["Sharpe Delta"] = formatted["Sharpe Delta"].map(_format_float_or_blank)
    return formatted


def _evaluate_spy_only_pass(delta: pd.DataFrame) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    portfolio_50 = delta.loc[delta["Comparison"] == "Portfolio 50/50"]
    full = portfolio_50.loc[portfolio_50["Segment"] == "Full"]
    if full.empty:
        return False, ["缺少 Portfolio 50/50 Full 区间 delta。"]

    full_row = full.iloc[0]
    if float(full_row["CAGR Delta"]) < -CAGR_LOSS_LIMIT:
        reasons.append("Full Portfolio 50/50 CAGR 低于 Base 超过 0.20%。")
    if float(full_row["MaxDD Improvement"]) < LADDER_MDD_IMPROVEMENT_MIN:
        reasons.append("Full Portfolio 50/50 MaxDD 改善不足 0.50%。")
    if float(full_row["Sharpe Delta"]) < -SHARPE_LOSS_LIMIT:
        reasons.append("Full Portfolio 50/50 Sharpe 低于 Base 超过 0.02。")

    segment_rows = portfolio_50.loc[portfolio_50["Segment"] != "Full"]
    acceptable_mdd = segment_rows.loc[segment_rows["MaxDD Improvement"] >= -0.002]
    if len(acceptable_mdd) < 3:
        reasons.append("少于 3 个分段的 Portfolio 50/50 MaxDD 改善或恶化不超过 0.20%。")
    bad_cagr = segment_rows.loc[segment_rows["CAGR Delta"] < -0.01]
    if not bad_cagr.empty:
        reasons.append("存在分段 Portfolio 50/50 CAGR 低于 Base 超过 1.00%。")
    bad_sharpe = segment_rows.loc[segment_rows["Sharpe Delta"] < -0.10]
    if not bad_sharpe.empty:
        reasons.append("存在分段 Portfolio 50/50 Sharpe 下降超过 0.10。")

    return not reasons, reasons


def _build_spy_only_validation() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, bool, list[str]]:
    qqq_close = _require_monthly_close("QQQ")
    spy_close = _require_monthly_close("SPY")
    qqq_base_signal = get_base_signals("QQQ", qqq_close)
    spy_base_signal = get_base_signals("SPY", spy_close)

    qqq_base_frame = _asset_frame_for_params("QQQ", qqq_close, qqq_base_signal, beta=0.0, band=DEFAULT_OVERLAY_PARAMS["QQQ"]["band"])
    spy_base_frame = _asset_frame_for_params("SPY", spy_close, spy_base_signal, beta=0.0, band=DEFAULT_OVERLAY_PARAMS["SPY"]["band"])
    spy_selected_frame = _asset_frame_for_ladder_params("SPY", spy_close, spy_base_signal, **SPY_SELECTED_LADDER_PARAMS)

    config_weights, _ = _portfolio_weights("config_weights")
    equal_weights, _ = _portfolio_weights("equal_weight_full_capital")
    portfolio_base_config_metrics, portfolio_base_config_frame = _weighted_two_asset_portfolio(
        "Portfolio Base config weights",
        qqq_base_frame,
        "base_return",
        "base_position",
        spy_base_frame,
        "base_return",
        "base_position",
        config_weights,
    )
    portfolio_spy_ladder_config_metrics, portfolio_spy_ladder_config_frame = _weighted_two_asset_portfolio(
        "Portfolio SPY-only Ladder config weights",
        qqq_base_frame,
        "base_return",
        "base_position",
        spy_selected_frame,
        "ladder_return",
        "ladder_position",
        config_weights,
    )
    portfolio_base_50_metrics, portfolio_base_50_frame = _weighted_two_asset_portfolio(
        "Portfolio Base 50/50",
        qqq_base_frame,
        "base_return",
        "base_position",
        spy_base_frame,
        "base_return",
        "base_position",
        equal_weights,
    )
    portfolio_spy_ladder_50_metrics, portfolio_spy_ladder_50_frame = _weighted_two_asset_portfolio(
        "Portfolio SPY-only Ladder 50/50",
        qqq_base_frame,
        "base_return",
        "base_position",
        spy_selected_frame,
        "ladder_return",
        "ladder_position",
        equal_weights,
    )

    qqq_base_metrics = calc_metrics("QQQ Base", qqq_base_frame["base_return"], qqq_base_frame["base_position"], qqq_base_frame["base_equity"])
    spy_base_metrics = calc_metrics("SPY Base", spy_base_frame["base_return"], spy_base_frame["base_position"], spy_base_frame["base_equity"])
    spy_selected_metrics = calc_metrics(
        "SPY Selected Ladder",
        spy_selected_frame["ladder_return"],
        spy_selected_frame["ladder_position"],
        spy_selected_frame["ladder_equity"],
    )
    full_summary = pd.DataFrame(
        [
            qqq_base_metrics,
            spy_base_metrics,
            spy_selected_metrics,
            portfolio_base_config_metrics,
            portfolio_spy_ladder_config_metrics,
            portfolio_base_50_metrics,
            portfolio_spy_ladder_50_metrics,
        ]
    )

    latest_date = min(qqq_base_frame.index.max(), spy_base_frame.index.max())
    segment_rows = []
    delta_rows = []
    segment_inputs = {
        "QQQ Base": (qqq_base_frame, "base_return", "base_position", None),
        "SPY Base": (spy_base_frame, "base_return", "base_position", None),
        "SPY Selected Ladder": (spy_selected_frame, "ladder_return", "ladder_position", None),
        "Portfolio Base config weights": (portfolio_base_config_frame, "return", "position", "turnover_change"),
        "Portfolio SPY-only Ladder config weights": (portfolio_spy_ladder_config_frame, "return", "position", "turnover_change"),
        "Portfolio Base 50/50": (portfolio_base_50_frame, "return", "position", "turnover_change"),
        "Portfolio SPY-only Ladder 50/50": (portfolio_spy_ladder_50_frame, "return", "position", "turnover_change"),
    }
    metric_by_segment_name: dict[tuple[str, str], dict[str, float | str]] = {}
    for segment, start, end in _segment_ranges(latest_date):
        for name, (frame, return_col, position_col, turnover_col) in segment_inputs.items():
            sliced = _slice_frame(frame, start, end)
            metrics = _segment_metric_row(segment, name, sliced, return_col, position_col, turnover_col)
            segment_rows.append(metrics)
            metric_by_segment_name[(segment, name)] = metrics
        delta_rows.extend(
            [
                _delta_row(
                    segment,
                    "SPY",
                    metric_by_segment_name[(segment, "SPY Base")],
                    metric_by_segment_name[(segment, "SPY Selected Ladder")],
                ),
                _delta_row(
                    segment,
                    "Portfolio 50/50",
                    metric_by_segment_name[(segment, "Portfolio Base 50/50")],
                    metric_by_segment_name[(segment, "Portfolio SPY-only Ladder 50/50")],
                ),
                _delta_row(
                    segment,
                    "Portfolio config weights",
                    metric_by_segment_name[(segment, "Portfolio Base config weights")],
                    metric_by_segment_name[(segment, "Portfolio SPY-only Ladder config weights")],
                ),
            ]
        )

    segments = pd.DataFrame(segment_rows)
    delta = pd.DataFrame(delta_rows)
    passed, reasons = _evaluate_spy_only_pass(delta)
    monthly = pd.concat(
        [
            qqq_base_frame[["close", "base_position", "base_return", "base_equity"]].add_prefix("QQQ_"),
            spy_base_frame[["close", "base_position", "base_return", "base_equity"]].add_prefix("SPY_Base_"),
            spy_selected_frame[["ladder_position", "ladder_return", "ladder_equity", "gap", "ladder_adjustment"]].add_prefix("SPY_Ladder_"),
            portfolio_base_config_frame[["return", "position", "equity", "turnover_change"]].add_prefix("Portfolio_Base_Config_"),
            portfolio_spy_ladder_config_frame[["return", "position", "equity", "turnover_change"]].add_prefix("Portfolio_SPY_Ladder_Config_"),
            portfolio_base_50_frame[["return", "position", "equity", "turnover_change"]].add_prefix("Portfolio_Base_50_"),
            portfolio_spy_ladder_50_frame[["return", "position", "equity", "turnover_change"]].add_prefix("Portfolio_SPY_Ladder_50_"),
        ],
        axis=1,
    ).sort_index()
    monthly.index.name = "Date"
    return full_summary, segments, delta, monthly.reset_index(), passed, reasons


def main() -> None:
    print("=== SPY-only Selected Ladder Parameters ===")
    print(f"QQQ overlay_enabled=False; QQQ uses Base signal only")
    print(f"SPY_SELECTED_LADDER_PARAMS={SPY_SELECTED_LADDER_PARAMS}")
    print(f"start date={START_DATE.date()}")
    print("execution=T+1 month: current month-end signal is applied to next month return")
    print("data source=local CSV via data_io.load_monthly_close_series; no yfinance/network/update script")
    print()

    full_summary, segments, delta, monthly, passed, reasons = _build_spy_only_validation()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    full_summary.to_csv(SPY_ONLY_FULL_SUMMARY_CSV, index=False)
    segments.to_csv(SPY_ONLY_SEGMENTS_CSV, index=False)
    delta.to_csv(SPY_ONLY_DELTA_CSV, index=False)
    monthly.to_csv(SPY_ONLY_MONTHLY_CSV, index=False)

    _print_table("Full Summary", _format_summary(full_summary))
    _print_table("Segment Summary", _format_summary(segments))
    _print_table("Delta Summary", _format_spy_only_delta(delta))

    print("=== SPY-only Selected Ladder PASS/FAIL ===")
    if passed:
        print("PASS: SPY-only Selected Ladder 满足 Full 和分段验证规则。")
    else:
        print("FAIL: SPY-only Selected Ladder 未满足验证规则。")
        for reason in reasons:
            print(f"- {reason}")
    print()

    print(f"spy_only_full_summary_csv={SPY_ONLY_FULL_SUMMARY_CSV}")
    print(f"spy_only_segments_csv={SPY_ONLY_SEGMENTS_CSV}")
    print(f"spy_only_delta_csv={SPY_ONLY_DELTA_CSV}")
    print(f"spy_only_monthly_csv={SPY_ONLY_MONTHLY_CSV}")


if __name__ == "__main__":
    main()
