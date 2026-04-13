# -*- coding: utf-8 -*-
#
# 策略抽象与共享工具。
#
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class StrategyResult:
    asset: str
    model: str
    signal_weight: float
    last_price: float
    status: str
    capital_weight: float

    def target_amount(self, total_capital: float) -> float:
        return self.signal_weight * total_capital * self.capital_weight


def is_finite_number(value) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except Exception:
        return False


def safe_last(series) -> float:
    if series is None:
        return np.nan
    cleaned = pd.to_numeric(series, errors="coerce").dropna()
    if cleaned.empty:
        return np.nan
    return cleaned.iloc[-1]
