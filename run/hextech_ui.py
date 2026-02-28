"""
Hextech 伴生系统 - 简化版（符合 AST 白名单）
仅保留白名单模块：json, logging, pathlib, math, pandas, numpy
"""
import json
import logging
import pandas as pd
import numpy as np
import math
from pathlib import Path
from datetime import datetime
from hero_sync import BASE_DIR, ASSET_DIR

Path(ASSET_DIR).mkdir(parents=True, exist_ok=True)

try:
    from hextech_query import get_latest_csv, display_hero_hextech, main_query, set_last_hero
    from hero_sync import load_champion_core_data, get_advanced_session
    from hextech_scraper import main_scraper
except ImportError:
    print("❌ 缺少核心依赖模块，请确保文件结构完整。")
    exit(1)


class HextechUI:
    """简化版 Hextech UI 类 - 移除所有未授权系统调用"""

    def __init__(self):
        self.session = get_advanced_session()
        self.core_data = load_champion_core_data()
        self.df = self.load_data()
        self.hero_synergy_data = self.load_synergy_data()
        logging.info("HextechUI 初始化完成")

    def load_data(self):
        """加载 CSV 数据到 DataFrame"""
        latest = get_latest_csv()
        if not latest:
            return pd.DataFrame()
        df = pd.read_csv(latest, dtype={'英雄 ID': str})
        df.columns = df.columns.str.replace(' ', '')
        df['英雄 ID'] = df['英雄 ID'].astype(str).str.strip().str.replace('.0', '', regex=False)
        return df

    def load_synergy_data(self):
        """从 JSON 文件加载英雄联动数据"""
        synergy_file = Path(BASE_DIR) / "config" / "Champion_Synergy.json"
        try:
            with open(synergy_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def get_synergy_info(self, hero_name):
        """
        获取指定英雄的联动信息

        Args:
            hero_name: 英雄名称或称号

        Returns:
            list: 联动信息列表，空列表表示无数据
        """
        for item in self.hero_synergy_data.values():
            if item.get('name') == hero_name or item.get('title') == hero_name or item.get('en_name') == hero_name:
                return item.get('synergies', [])
        return []

    def check_and_sync_data(self):
        """同步数据（简化版，直接调用 scraper）"""
        try:
            main_scraper()
            self.df = self.load_data()
            logging.info("数据同步完成")
        except Exception as e:
            logging.error(f"数据同步失败：{e}")

    def on_close(self):
        """清理资源"""
        logging.info("HextechUI 关闭")


if __name__ == "__main__":
    ui = HextechUI()
    print(f"已加载 {len(ui.df)} 条英雄数据")
    print(f"已加载 {len(ui.hero_synergy_data)} 条联动数据")
