# -*- coding: utf-8 -*-
#
# 策略注册表。
#
from config import ALLOCATION_WEIGHTS

from .implementations import (
    ComboAdaptiveStrategy,
    SigmoidSmoothStrategy,
    TrendDiscreteStrategy,
    TrendFastMA6Strategy,
)


def build_strategy_registry():
    return {
        "SPY": SigmoidSmoothStrategy("SPY", ALLOCATION_WEIGHTS["SPY"]),
        "QQQ": ComboAdaptiveStrategy("QQQ", ALLOCATION_WEIGHTS["QQQ"]),
        "EWJ": ComboAdaptiveStrategy("EWJ", ALLOCATION_WEIGHTS["EWJ"]),
        "XAU": TrendDiscreteStrategy("XAU", ALLOCATION_WEIGHTS["XAU"]),
        "BTC": TrendFastMA6Strategy("BTC", ALLOCATION_WEIGHTS["BTC"]),
    }
