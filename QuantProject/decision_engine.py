# -*- coding: utf-8 -*-
#
# 全资产仓位决策引擎。
# 调度数据加载、策略计算与报告归档。
#
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
import warnings

import pandas as pd

from config import DATA_DIR, LOG_FILE, MAX_TOTAL_CAPITAL, STRUCTURED_LOG_FILE
from data_io import load_monthly_close_series
from strategies import StrategyResult, build_strategy_registry

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers.clear()
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler = RotatingFileHandler(
    Path(DATA_DIR).parent / "decision_engine.log",
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(stream_handler)
logger.propagate = False

warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        pd.set_option("future.no_silent_downcasting", True)
    except Exception:
        pass


def _is_finite_number(value) -> bool:
    try:
        return value is not None and pd.notna(value) and float(value) == float(value)
    except Exception:
        return False


def _fmt_price(value):
    return f"{value:,.2f}" if _is_finite_number(value) else "N/A"


def _fmt_pct(value):
    return f"{value:.1%}" if _is_finite_number(value) else "N/A"


def _fmt_amount(value, capital_enabled):
    if not capital_enabled:
        return "N/A"
    return f"{value:,.2f}" if _is_finite_number(value) else "N/A"


def _parse_total_capital(raw_input: str) -> float:
    cleaned = raw_input.strip().replace(",", "")
    try:
        total_capital = float(cleaned) if cleaned else 0.0
        if total_capital < 0:
            raise ValueError("资金不能为负数")
        if total_capital > MAX_TOTAL_CAPITAL:
            raise ValueError("输入金额超出合理范围")
        if cleaned:
            logger.info("用户输入总资金：$***（已脱敏）")
        return total_capital
    except ValueError as exc:
        logger.warning("用户输入无效：'%s'，原因：%s", cleaned, exc)
        print(f"[-] 输入无效：{exc}，系统退回纯百分比模式。")
        return 0.0


def _build_report(results: list[StrategyResult], total_capital: float, capital_enabled: bool, now_str: str):
    report_lines = []
    total_deployed_cash = 0.0

    report_lines.append("\n" + "=" * 115)
    report_lines.append(f"[{now_str}] 全资产量化仓位决策系统")
    report_lines.append("=" * 115)
    report_lines.append(f"{'资产':<6} | {'策略模型':<16} | {'资金占比':<8} | {'最新价':>8} | {'信号仓位':>8} | {'建议分配金额($)':>16} | {'核心指标状态'}")
    report_lines.append("-" * 115)

    structured_positions = []
    for result in results:
        target_amount = result.target_amount(total_capital)
        total_deployed_cash += target_amount
        report_lines.append(
            f"{result.asset:<6} | {result.model:<16} | {result.capital_weight:<8.2%} | {_fmt_price(result.last_price):>8} | "
            f"{_fmt_pct(result.signal_weight):>8} | {_fmt_amount(target_amount, capital_enabled):>16} | {result.status}"
        )
        structured_positions.append(
            {
                "asset": result.asset,
                "model": result.model,
                "capital_weight": result.capital_weight,
                "last_price": result.last_price if _is_finite_number(result.last_price) else None,
                "signal_weight": result.signal_weight,
                "target_amount": target_amount if capital_enabled else None,
                "status": result.status,
            }
        )

    report_lines.append("-" * 115)
    if capital_enabled:
        cash_reserved = total_capital - total_deployed_cash
        report_lines.append(f">>> [资金总控] 实盘总本金: ${total_capital:,.2f}")
        report_lines.append(f">>> [执行摘要] 系统建议投入总额: ${total_deployed_cash:,.2f} | 建议保留现金避险: ${cash_reserved:,.2f}")
    report_lines.append("=" * 115 + "\n")

    return "\n".join(report_lines), {
        "timestamp": now_str,
        "capital_enabled": capital_enabled,
        "total_capital": total_capital if capital_enabled else None,
        "total_deployed_cash": total_deployed_cash if capital_enabled else None,
        "positions": structured_positions,
    }


def _persist_reports(full_report: str, structured_record: dict):
    with open(LOG_FILE, "a", encoding="utf-8") as text_file:
        text_file.write(full_report)
    with open(STRUCTURED_LOG_FILE, "a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(json.dumps(structured_record, ensure_ascii=False) + "\n")


def run_engine(total_capital: float) -> tuple[str, dict]:
    registry = build_strategy_registry()
    results: list[StrategyResult] = []
    for asset, strategy in registry.items():
        logger.info("开始处理 %s (%s 策略)...", asset, strategy.__class__.__name__)
        data = load_monthly_close_series(asset, logger=logger)
        results.append(strategy.compute(data))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _build_report(results, total_capital, total_capital > 0, now_str)


def main():
    logger.info("=" * 60)
    logger.info("启动全资产仓位决策引擎")
    logger.info("=" * 60)

    print("\n" + "=" * 115)
    raw_input = input(">>> [资金管理] 请输入您的实盘总资金 (按回车默认纯百分比模式): ")
    total_capital = _parse_total_capital(raw_input)

    full_report, structured_record = run_engine(total_capital)
    print(full_report)

    try:
        _persist_reports(full_report, structured_record)
        print(f"[成功] 本次决议及调仓金额已自动归档至：{LOG_FILE}")
    except Exception as exc:
        print(f"[-] 警告：日志保存失败 -> {exc}")


if __name__ == "__main__":
    main()
