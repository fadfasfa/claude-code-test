# -*- coding: utf-8 -*-
"""
核心配置模块：全系统统一配置中心 (config.py)
提供 BASE_DIR、DATA_DIR、ASSETS_MAPPING、ALLOCATION_WEIGHTS 真值定义
"""
from pathlib import Path

# 动态生成 BASE_DIR 路径 (基于物理路径 C:\QuantProject)
BASE_DIR = Path(r"C:\Users\apple\claude code test\QuantProject")

# 数据目录与日志文件路径
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "position_history.txt"

# 全资产映射配置 (Stooq 代码与本地文件名)
ASSETS_MAPPING = {
    'SPY': {'stooq_code': 'spy.us', 'file': 'spy_us_d.csv'},
    'QQQ': {'stooq_code': 'qqq.us', 'file': 'qqq_us_d.csv'},
    'EWJ': {'stooq_code': 'ewj.us', 'file': 'ewj_us_d.csv'},
    'XAU': {'stooq_code': 'xauusd', 'file': 'xau_usd_d.csv'},
    'BTC': {'stooq_code': 'btc.v', 'file': 'btc_us_d.csv'}
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
