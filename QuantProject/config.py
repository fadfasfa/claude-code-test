# -*- coding: utf-8 -*-
#
# 核心配置模块：全系统统一配置中心 (config.py)
# 提供 BASE_DIR、DATA_DIR、ASSETS_MAPPING、ALLOCATION_WEIGHTS 真值定义
#
from pathlib import Path

# 动态生成 BASE_DIR 路径
BASE_DIR = Path(__file__).resolve().parent

# 数据目录与日志文件路径
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "position_history.txt"
STRUCTURED_LOG_FILE = BASE_DIR / "position_history.jsonl"
SYNC_STATUS_FILE = DATA_DIR / "sync_status.json"

# 数据同步策略
# 以交易日为单位判断本地数据是否足够新鲜，避免每次启动都触发联网抓取。
DATA_FRESHNESS_BUSINESS_DAYS = 2

# 发生增量同步时，默认回看最近多少天的数据，避免全量拉取。
UPDATE_LOOKBACK_DAYS = 45

# 外部数据源网络容错参数
YFINANCE_TIMEOUT_SECONDS = 12
STOOQ_TIMEOUT_SECONDS = 10
STOOQ_MAX_RETRIES = 2
MAX_DOWNLOAD_WORKERS = 5

# 输入与数据安全边界
MAX_TOTAL_CAPITAL = 1e12
MAX_REASONABLE_PRICE = 1e8

# 全资产映射配置 (Stooq 代码与本地文件名)
ASSETS_MAPPING = {
    'SPY': {'stooq_code': 'spy.us', 'yf_code': 'SPY', 'file': 'spy_us_d.csv'},
    'QQQ': {'stooq_code': 'qqq.us', 'yf_code': 'QQQ', 'file': 'qqq_us_d.csv'},
    'EWJ': {'stooq_code': 'ewj.us', 'yf_code': 'EWJ', 'file': 'ewj_us_d.csv'},
    'XAU': {'stooq_code': 'xauusd', 'yf_code': 'GC=F', 'file': 'xau_usd_d.csv'},
    'BTC': {'stooq_code': 'btc.v', 'yf_code': 'BTC-USD', 'file': 'btc_us_d.csv'}
}

# 简化文件名字典 (供 decision_engine.py 使用)
FILES = {k: v['file'] for k, v in ASSETS_MAPPING.items()}

# 资金分配权重配置 (BTC 15%, 其余各 21.25%)
ALLOCATION_WEIGHTS = {
    'SPY': 0.2125,
    'QQQ': 0.2125,
    'EWJ': 0.2125,
    'XAU': 0.2125,
    'BTC': 0.1500
}

_weights_total = sum(ALLOCATION_WEIGHTS.values())
if abs(_weights_total - 1.0) >= 1e-9:
    raise ValueError(f"ALLOCATION_WEIGHTS 之和应为 1.0，当前为 {_weights_total}")
