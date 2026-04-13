# -*- coding: utf-8 -*-
#
# 各资产策略实现。
#
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .base import StrategyResult, is_finite_number, safe_last


@dataclass
class SigmoidSmoothStrategy:
    asset: str
    capital_weight: float
    ma_period: int = 12
    sensitivity: float = 50.0
    threshold: float = 0.1

    @property
    def min_points(self) -> int:
        return self.ma_period

    def compute(self, data: pd.Series | None) -> StrategyResult:
        if data is None or len(data) < self.min_points:
            return StrategyResult(self.asset, "Sigmoid", 0.0, np.nan, "数据不足", self.capital_weight)

        last = safe_last(data)
        ma_value = safe_last(data.rolling(self.ma_period, min_periods=self.ma_period).mean())
        if not (is_finite_number(last) and is_finite_number(ma_value)) or abs(ma_value) <= 1e-10:
            return StrategyResult(self.asset, "Sigmoid", 0.0, last, "数据无效", self.capital_weight)

        dev = (last / ma_value) - 1
        weight = np.clip(1 / (1 + np.exp(-self.sensitivity * dev)), 0, 1)
        if dev >= self.threshold:
            weight = 1.0
        elif dev <= -self.threshold:
            weight = 0.0
        return StrategyResult(self.asset, "Sigmoid", weight, last, f"偏离度: {dev:.2%}", self.capital_weight)


@dataclass
class ComboAdaptiveStrategy:
    asset: str
    capital_weight: float

    @property
    def min_points(self) -> int:
        return 12

    def compute(self, data: pd.Series | None) -> StrategyResult:
        if data is None or len(data) < self.min_points:
            return StrategyResult(self.asset, "Combo Adaptive", 0.0, np.nan, "数据不足", self.capital_weight)

        last = safe_last(data)
        averages = [safe_last(data.rolling(i, min_periods=i).mean()) for i in [3, 6, 9, 12]]
        m3, m6, m9, m12 = averages
        if not all(is_finite_number(v) for v in [last, m3, m6, m9, m12]):
            return StrategyResult(self.asset, "Combo Adaptive", 0.0, last, "数据无效", self.capital_weight)

        trend_weight = 1.0
        if last < m3:
            trend_weight = 0.0
        elif last < m6:
            trend_weight = 0.25
        elif last < m9:
            trend_weight = 0.50
        elif last < m12:
            trend_weight = 0.75

        if len(data) >= 2:
            prev = pd.to_numeric(data.iloc[-2:], errors="coerce")
            prev_m6 = data.rolling(6, min_periods=6).mean().iloc[-2:]
            if pd.notna(prev.iloc[-1]) and pd.notna(prev.iloc[-2]) and pd.notna(prev_m6.iloc[-1]) and pd.notna(prev_m6.iloc[-2]):
                if prev.iloc[-1] < prev_m6.iloc[-1] and prev.iloc[-2] < prev_m6.iloc[-2]:
                    trend_weight = min(trend_weight, 0.25)

        dev = (last / m12) - 1
        sigmoid_weight = np.clip(1 / (1 + np.exp(-50 * dev)), 0, 1) if abs(dev) < 0.1 else (1.0 if dev >= 0.1 else 0.0)
        vol = data.pct_change().iloc[-3:].std() * np.sqrt(12)
        alpha = np.clip((vol - 0.1) / 0.2, 0.2, 0.9) if is_finite_number(vol) else 0.2
        signal_weight = alpha * trend_weight + (1 - alpha) * sigmoid_weight
        return StrategyResult(
            self.asset,
            "Combo Adaptive",
            signal_weight,
            last,
            f"Vol:{vol:.1%} Alpha:{alpha:.2f}" if is_finite_number(vol) else f"Vol:N/A Alpha:{alpha:.2f}",
            self.capital_weight,
        )


@dataclass
class TrendDiscreteStrategy:
    asset: str
    capital_weight: float

    @property
    def min_points(self) -> int:
        return 15

    def compute(self, data: pd.Series | None) -> StrategyResult:
        if data is None or len(data) < self.min_points:
            return StrategyResult(self.asset, "Trend Discrete", 0.0, np.nan, "数据不足", self.capital_weight)

        last = safe_last(data)
        m6, m9, m12, m15 = [safe_last(data.rolling(i, min_periods=i).mean()) for i in [6, 9, 12, 15]]
        if not all(is_finite_number(v) for v in [last, m6, m9, m12, m15]):
            return StrategyResult(self.asset, "Trend Discrete", 0.0, last, "数据无效", self.capital_weight)

        weight = 1.0
        if last < m15:
            weight = 0.0
        elif last < m12:
            weight = 0.25
        elif last < m9:
            weight = 0.50
        elif last < m6:
            weight = 0.75
        return StrategyResult(self.asset, "Trend Discrete", weight, last, f"MA15底线: {m15:.2f}", self.capital_weight)


@dataclass
class TrendFastMA6Strategy:
    asset: str
    capital_weight: float

    @property
    def min_points(self) -> int:
        return 6

    def compute(self, data: pd.Series | None) -> StrategyResult:
        if data is None or len(data) < self.min_points:
            return StrategyResult(self.asset, "Trend Fast MA6", 0.0, np.nan, "数据不足", self.capital_weight)

        last = safe_last(data)
        ma6 = safe_last(data.rolling(6, min_periods=6).mean())
        if not all(is_finite_number(v) for v in [last, ma6]):
            return StrategyResult(self.asset, "Trend Fast MA6", 0.0, last, "数据无效", self.capital_weight)

        weight = 1.0 if last > ma6 else 0.0
        return StrategyResult(self.asset, "Trend Fast MA6", weight, last, f"MA6动量线: {ma6:.2f}", self.capital_weight)
