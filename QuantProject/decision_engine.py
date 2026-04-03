# -*- coding: utf-8 -*-
#
# 全资产仓位决策引擎。
# 读取本地数据，计算策略信号，输出终端报告。
#
import logging
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
import warnings

from config import DATA_DIR, LOG_FILE, FILES, ALLOCATION_WEIGHTS

# 调试日志。
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(Path(DATA_DIR).parent / 'decision_engine.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 全局容错设置。
warnings.filterwarnings('ignore')
try:
    pd.set_option('future.no_silent_downcasting', True)
except Exception:
    pass

def load_stooq_data(asset_name):
    # 读取并重采样本地数据，支持模糊列名。
    path = Path(DATA_DIR) / FILES[asset_name]
    logger.debug(f"[{asset_name}] 加载数据文件：{path}")

    if not path.exists():
        logger.warning(f"[{asset_name}] 数据文件不存在：{path}")
        return None

    try:
        df = pd.read_csv(path, low_memory=True)
        logger.debug(f"[{asset_name}] 读取完成，行数：{len(df)}, 列数：{len(df.columns)}")

        if df.empty:
            logger.warning(f"[{asset_name}] 数据文件为空")
            return None

        d_col = None
        c_col = None
        for col in df.columns:
            col_lower = col.lower()
            if col_lower in ['data', 'date', 'index']:
                d_col = col
            if col_lower in ['zamkniecie', 'close']:
                c_col = col

        if d_col is None or c_col is None:
            logger.error(f"[{asset_name}] 列名匹配失败：{list(df.columns)}")
            return None

        df[d_col] = pd.to_datetime(df[d_col], errors='coerce')
        df = df.dropna(subset=[d_col])

        if df.empty:
            logger.warning(f"[{asset_name}] 日期转换后数据为空")
            return None

        df = df.set_index(d_col).sort_index()
        m = df[c_col].resample('ME').last()
        logger.debug(f"[{asset_name}] 重采样完成，数据点数：{len(m)}, 最新日期：{m.index[-1]}")
        return m
    except (FileNotFoundError, pd.errors.EmptyDataError) as e:
        logger.warning(f"[{asset_name}] 文件读取异常：{type(e).__name__}")
        return None
    except Exception as e:
        logger.error(f"[{asset_name}] 数据加载失败：{type(e).__name__} - {e}")
        return None

def main():
    logger.info("=" * 60)
    logger.info("启动全资产仓位决策引擎")
    logger.info("=" * 60)

    print("\n" + "="*115)
    raw_input = input(">>> [资金管理] 请输入您的实盘总资金 (按回车默认纯百分比模式): ")
    raw_input = raw_input.strip().replace(',', '')

    try:
        total_capital = float(raw_input) if raw_input else 0.0
        logger.info(f"用户输入总资金：${total_capital:,.2f}")
    except ValueError:
        logger.warning(f"用户输入格式错误：'{raw_input}'，使用默认值 0.0")
        print("[-] 输入格式错误，系统退回纯百分比模式。")
        total_capital = 0.0

    total_deployed_cash = 0.0
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    report_lines = []
    
    report_lines.append("\n" + "="*115)
    report_lines.append(f"[{now_str}] 全资产量化仓位决策系统")
    report_lines.append("="*115)
    report_lines.append(f"{'资产':<6} | {'策略模型':<16} | {'资金占比':<8} | {'最新价':>8} | {'信号仓位':>8} | {'建议分配金额($)':>16} | {'核心指标状态'}")
    report_lines.append("-" * 115)

    # 1. SPY: Sigmoid Smooth
    logger.info("开始处理 SPY (Sigmoid Smooth 策略)...")
    spy = load_stooq_data('SPY')
    if spy is not None and len(spy) >= 12:
        last, ma12 = spy.iloc[-1], spy.rolling(12).mean().iloc[-1]
        dev = (last / ma12) - 1
        w = np.clip(1 / (1 + np.exp(-50 * dev)), 0, 1)
        if dev >= 0.1:
            w = 1.0
            logger.debug(f"[SPY] 触发强多头条件：偏离度 {dev:.2%} >= 10%")
        elif dev <= -0.1:
            w = 0.0
            logger.debug(f"[SPY] 触发强空头条件：偏离度 {dev:.2%} <= -10%")
        else:
            logger.debug(f"[SPY] Sigmoid 平滑：偏离度={dev:.2%}, 权重={w:.2%}")

        cap_limit = total_capital * ALLOCATION_WEIGHTS['SPY']
        target_amt = w * cap_limit
        total_deployed_cash += target_amt
        amt_str = f"{target_amt:,.2f}" if total_capital > 0 else "N/A"
        report_lines.append(f"{'SPY':<6} | {'Sigmoid':<16} | {ALLOCATION_WEIGHTS['SPY']:<8.2%} | {last:>8.2f} | {w:>8.1%} | {amt_str:>16} | 偏离度: {dev:.2%}")

    # 2. QQQ & 3. EWJ: Combo Adaptive
    logger.info("开始处理 QQQ 和 EWJ (Combo Adaptive 策略)...")
    for asset in ['QQQ', 'EWJ']:
        data = load_stooq_data(asset)
        if data is not None and len(data) >= 12:
            last = data.iloc[-1]
            m3, m6, m9, m12 = [data.rolling(i).mean().iloc[-1] for i in [3, 6, 9, 12]]
            
            t_pos = 1.0
            if last < m12: t_pos = 0.25
            if last < m9: t_pos = 0.50
            if last < m6: t_pos = 0.75
            if last < m3: t_pos = 0.0
            if data.iloc[-1] < data.rolling(6).mean().iloc[-1] and data.iloc[-2] < data.rolling(6).mean().iloc[-2]:
                t_pos = min(t_pos, 0.25)
                
            dev_q = (last / m12) - 1
            s_pos = np.clip(1/(1+np.exp(-50*dev_q)), 0, 1) if abs(dev_q) < 0.1 else (1.0 if dev_q >= 0.1 else 0.0)
            
            vol = data.pct_change().iloc[-3:].std() * np.sqrt(12)
            alpha = np.clip((vol - 0.1) / 0.2, 0.2, 0.9)
            w_combo = alpha * t_pos + (1 - alpha) * s_pos
            
            cap_limit = total_capital * ALLOCATION_WEIGHTS[asset]
            target_amt = w_combo * cap_limit
            total_deployed_cash += target_amt
            amt_str = f"{target_amt:,.2f}" if total_capital > 0 else "N/A"
            report_lines.append(f"{asset:<6} | {'Combo Adaptive':<16} | {ALLOCATION_WEIGHTS[asset]:<8.2%} | {last:>8.2f} | {w_combo:>8.1%} | {amt_str:>16} | Vol:{vol:.1%} Alpha:{alpha:.2f}")

    # 4. XAU: Trend Discrete
    logger.info("开始处理 XAU (Trend Discrete 策略)...")
    xau = load_stooq_data('XAU')
    if xau is not None and len(xau) >= 15:
        last = xau.iloc[-1]
        m6, m9, m12, m15 = [xau.rolling(i).mean().iloc[-1] for i in [6, 9, 12, 15]]
        w = 1.0
        if last < m15: w = 0.0
        elif last < m12: w = 0.25
        elif last < m9: w = 0.50
        elif last < m6: w = 0.75
        
        cap_limit = total_capital * ALLOCATION_WEIGHTS['XAU']
        target_amt = w * cap_limit
        total_deployed_cash += target_amt
        amt_str = f"{target_amt:,.2f}" if total_capital > 0 else "N/A"
        report_lines.append(f"{'XAU':<6} | {'Trend Discrete':<16} | {ALLOCATION_WEIGHTS['XAU']:<8.2%} | {last:>8.2f} | {w:>8.1%} | {amt_str:>16} | MA15底线: {m15:.2f}")

    # 5. BTC: Trend Discrete Fast (MA6)
    logger.info("开始处理 BTC (Trend Fast MA6 策略)...")
    btc = load_stooq_data('BTC')
    if btc is not None and len(btc) >= 6:
        last, ma6_b = btc.iloc[-1], btc.rolling(6).mean().iloc[-1]
        w_btc = 1.0 if last > ma6_b else 0.0
        
        cap_limit = total_capital * ALLOCATION_WEIGHTS['BTC']
        target_amt = w_btc * cap_limit
        total_deployed_cash += target_amt
        amt_str = f"{target_amt:,.2f}" if total_capital > 0 else "N/A"
        report_lines.append(f"{'BTC':<6} | {'Trend Fast MA6':<16} | {ALLOCATION_WEIGHTS['BTC']:<8.2%} | {last:>8.2f} | {w_btc:>8.1%} | {amt_str:>16} | MA6动量线: {ma6_b:.2f}")

    report_lines.append("-" * 115)
    
    if total_capital > 0:
        cash_reserved = total_capital - total_deployed_cash
        report_lines.append(f">>> [资金总控] 实盘总本金: ${total_capital:,.2f}")
        report_lines.append(f">>> [执行摘要] 系统建议投入总额: ${total_deployed_cash:,.2f} | 建议保留现金避险: ${cash_reserved:,.2f}")
    
    report_lines.append("=" * 115 + "\n")

    full_report = "\n".join(report_lines)
    print(full_report)
    
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(full_report)
        print(f"[成功] 本次决议及调仓金额已自动归档至：{LOG_FILE}")
    except Exception as e:
        print(f"[-] 警告：日志保存失败 -> {str(e)}")

if __name__ == "__main__":
    main()
