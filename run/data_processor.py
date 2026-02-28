import pandas as pd
import numpy as np
import logging
import os
import json
from typing import List, Dict, Any


def process_champions_data(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    计算全英雄大盘 T 度列表（微量贝叶斯 + Z-Score）

    算法逻辑：
    1. 应用微量贝叶斯平滑公式（及格出场率阈值=0.005）
    2. 计算 Z-Score 进行 80/20 赋分（英雄总分 = Z_贝叶斯胜率 * 0.80 + Z_出场率 * 0.20）
    3. 按分数降序排序
    """
    if df.empty:
        return []

    try:
        # 创建副本避免修改原始数据
        data = df.copy()
        # ========== 数据降维去重 ==========
        # CSV 是“英雄-海克斯”粒度，计算英雄大盘前必须去重
        data = data[['英雄名称', '英雄胜率', '英雄出场率']].drop_duplicates(subset=['英雄名称']).copy()
        # 确保必要列存在
        required_cols = ['英雄名称', '英雄胜率', '英雄出场率']
        if not all(col in data.columns for col in required_cols):
            return []

        # ========== 微量贝叶斯平滑 ==========
        # 及格出场率阈值 = 0.005 (0.5%)
        min_pick_rate = 0.005
        avg_winrate = data['英雄胜率'].mean()

        # 贝叶斯平滑公式：平滑胜率 = (实际场次 * 实际胜率 + 先验场次 * 先验胜率) / (实际场次 + 先验场次)
        # 出场率可视为场次的代理变量，将阈值作为先验权重
        # 贝叶斯胜率 = (英雄胜率 * 英雄出场率 + 平均胜率 * 阈值) / (英雄出场率 + 阈值)
        data['贝叶斯胜率'] = (
            data['英雄胜率'] * data['英雄出场率'] + avg_winrate * min_pick_rate
        ) / (data['英雄出场率'] + min_pick_rate)

        # ========== Z-Score 标准化 ==========
        # 计算贝叶斯胜率的 Z-Score
        bayes_mean = data['贝叶斯胜率'].mean()
        bayes_std = data['贝叶斯胜率'].std()
        if bayes_std == 0 or np.isnan(bayes_std):
            bayes_std = 1  # 防除零
        data['Z_贝叶斯胜率'] = (data['贝叶斯胜率'] - bayes_mean) / bayes_std

        # 计算出场率的 Z-Score
        pick_mean = data['英雄出场率'].mean()
        pick_std = data['英雄出场率'].std()
        if pick_std == 0 or np.isnan(pick_std):
            pick_std = 1  # 防除零
        data['Z_出场率'] = (data['英雄出场率'] - pick_mean) / pick_std

        # ========== 80/20 综合赋分 ==========
        # 英雄总分 = Z_贝叶斯胜率 * 0.80 + Z_出场率 * 0.20
        data['综合分数'] = data['Z_贝叶斯胜率'] * 0.80 + data['Z_出场率'] * 0.20

        # ========== 排序并输出 ==========
        data = data.sort_values(by='综合分数', ascending=False)

        # 构建输出结果
        result = []
        for _, row in data.iterrows():
            result.append({
                '英雄名称': str(row['英雄名称']),
                '英雄胜率': float(row['英雄胜率']) if pd.notna(row['英雄胜率']) else 0.0,
                '英雄出场率': float(row['英雄出场率']) if pd.notna(row['英雄出场率']) else 0.0,
                '贝叶斯胜率': float(row['贝叶斯胜率']) if pd.notna(row['贝叶斯胜率']) else 0.0,
                '综合分数': float(row['综合分数']) if pd.notna(row['综合分数']) else 0.0,
                'Z_贝叶斯胜率': float(row['Z_贝叶斯胜率']) if pd.notna(row['Z_贝叶斯胜率']) else 0.0,
                'Z_出场率': float(row['Z_出场率']) if pd.notna(row['Z_出场率']) else 0.0
            })

        return result

    except Exception:
        # 安全降级，返回空列表
        return []


def _generate_hextech_icon_url(hextech_name: str, tier: str) -> str:
    """
    生成海克斯官方 CDN 图片 URL

    优先读取 config/Augment_Icon_Map.json 获取图标路径。
    若匹配到路径且以 /lol-game-data/assets/ 开头，返回 CommunityDragon CDN URL。
    如果找不到，继续使用原有的 fallback 逻辑。
    """
    # 尝试读取图标映射文件
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_dir = os.path.join(base_dir, "config")
    icon_map_file = os.path.join(config_dir, "Augment_Icon_Map.json")

    icon_path = None
    if os.path.exists(icon_map_file):
        try:
            with open(icon_map_file, "r", encoding="utf-8") as f:
                icon_map = json.load(f)
            icon_path = icon_map.get(hextech_name)
        except (Exception):
            pass

    # 如果找到图标路径且符合格式，转换为 CommunityDragon URL
    if icon_path and icon_path.startswith("/lol-game-data/assets/"):
        # 去掉 /lol-game-data/assets/ 前缀，转小写
        relative_path = icon_path[len("/lol-game-data/assets/"):]
        return f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/{relative_path.lower()}"

    # Fallback 逻辑：生成默认 URL
    tier_map = {
        '棱彩': 'prismatic',
        '彩色': 'prismatic',
        '银色': 'silver',
        '金色': 'gold',
    }
    tier_en = tier_map.get(str(tier), 'prismatic')
    clean_name = ''.join(c.lower() for c in str(hextech_name) if c.isalnum())
    return f"https://raw.communitydragon.org/latest/game/assets/ux/cherry/augments/icons/cherry_{tier_en}_{clean_name}.png"


def process_hextechs_data(df: pd.DataFrame, name: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    计算单英雄专属海克斯，返回包含四个独立数组 + 排序视图的字典

    算法逻辑：
    1. 过滤掉出场率低于 0.0005 的幽灵数据
    2. 综合 Z-Score 推荐：胜率差>=0 时 Z_胜率*0.85 + Z_出场*0.15；胜率差<0 时 Z_胜率*0.85 - Z_出场*0.15
    3. 纯胜率极值榜单（按海克斯胜率降序）
    4. 按阶级分离（Prismatic/Gold/Silver），每个阶级同时支持综合得分和纯胜率排序
    """
    if df.empty:
        return {
            'top_10_overall': [],
            'comprehensive': [],
            'winrate_only': [],
            'Prismatic': [],
            'Gold': [],
            'Silver': []
        }

    try:
        # 创建副本避免修改原始数据
        data = df.copy()

        # 确保必要列存在
        required_cols = ['英雄名称', '海克斯名称', '海克斯胜率', '海克斯出场率', '胜率差']
        if not all(col in data.columns for col in required_cols):
            return {
                'top_10_overall': [],
                'comprehensive': [],
                'winrate_only': [],
                'Prismatic': [],
                'Gold': [],
                'Silver': []
            }

        # ========== 过滤指定英雄 ==========
        hero_data = data[data['英雄名称'] == name].copy()
        if hero_data.empty:
            return {
                'top_10_overall': [],
                'comprehensive': [],
                'winrate_only': [],
                'Prismatic': [],
                'Gold': [],
                'Silver': []
            }

        # ========== 过滤幽灵数据（出场率 < 0.0005） ==========
        ghost_threshold = 0.0005
        hero_data = hero_data[hero_data['海克斯出场率'] >= ghost_threshold].copy()
        if hero_data.empty:
            return {
                'top_10_overall': [],
                'comprehensive': [],
                'winrate_only': [],
                'Prismatic': [],
                'Gold': [],
                'Silver': []
            }

        # ========== Z-Score 计算 ==========
        # 胜率差 Z-Score
        wr_diff_mean = hero_data['胜率差'].mean()
        wr_diff_std = hero_data['胜率差'].std()
        if wr_diff_std == 0 or np.isnan(wr_diff_std):
            wr_diff_std = 1
        hero_data['Z_胜率差'] = (hero_data['胜率差'] - wr_diff_mean) / wr_diff_std

        # 出场率 Z-Score
        pick_mean = hero_data['海克斯出场率'].mean()
        pick_std = hero_data['海克斯出场率'].std()
        if pick_std == 0 or np.isnan(pick_std):
            pick_std = 1
        hero_data['Z_出场率'] = (hero_data['海克斯出场率'] - pick_mean) / pick_std

        # ========== 综合得分计算 ==========
        # 胜率差>=0: Z_胜率*0.85 + Z_出场*0.15
        # 胜率差<0: Z_胜率*0.85 - Z_出场*0.15
        def calc_comprehensive_score(row):
            z_wr = row['Z_胜率差']
            z_pr = row['Z_出场率']
            wr_diff = row['胜率差']
            if wr_diff >= 0:
                return z_wr * 0.85 + z_pr * 0.15
            else:
                return z_wr * 0.85 - z_pr * 0.15

        hero_data['综合得分'] = hero_data.apply(calc_comprehensive_score, axis=1)

        # ========== 辅助函数：生成海克斯卡片 ==========
        def build_hextech_card(row, include_score=True):
            card = {
                '海克斯名称': str(row['海克斯名称']),
                '海克斯阶级': str(row.get('海克斯阶级', '棱彩')),
                '海克斯胜率': float(row['海克斯胜率']) if pd.notna(row['海克斯胜率']) else 0.0,
                '海克斯出场率': float(row['海克斯出场率']) if pd.notna(row['海克斯出场率']) else 0.0,
                '胜率差': float(row['胜率差']) if pd.notna(row['胜率差']) else 0.0,
                'icon': _generate_hextech_icon_url(row['海克斯名称'], row.get('海克斯阶级', '棱彩'))
            }
            if include_score:
                card['综合得分'] = float(row['综合得分']) if pd.notna(row['综合得分']) else 0.0
            return card

        # ========== top_10_overall：不计阶级，按综合得分前 10 ==========
        top_10_data = hero_data.sort_values(by='综合得分', ascending=False).head(10)
        top_10_overall = []
        for _, row in top_10_data.iterrows():
            top_10_overall.append(build_hextech_card(row, include_score=True))

        # ========== 综合榜单（向后兼容） ==========
        comp_data = hero_data.sort_values(by='综合得分', ascending=False)
        comprehensive_list = []
        for _, row in comp_data.iterrows():
            comprehensive_list.append(build_hextech_card(row, include_score=True))

        # ========== 纯胜率榜单（向后兼容） ==========
        winrate_data = hero_data.sort_values(by='海克斯胜率', ascending=False)
        winrate_list = []
        for _, row in winrate_data.iterrows():
            winrate_list.append(build_hextech_card(row, include_score=False))

        # ========== 分阶级数组 ==========
        def build_tier_array(tier_name):
            """为指定阶级生成数组"""
            # 兼容多种阶级名称
            tier_variants = {
                'Prismatic': ['棱彩', '彩色'],
                'Gold': ['金色', '黄金'],
                'Silver': ['银色', '白银']
            }

            variants = tier_variants.get(tier_name, [])
            tier_data = hero_data[hero_data['海克斯阶级'].isin(variants)].copy()

            # 按综合得分排序
            tier_data_by_score = tier_data.sort_values(by='综合得分', ascending=False)
            result = []
            for _, row in tier_data_by_score.iterrows():
                result.append(build_hextech_card(row, include_score=True))

            return result

        prismatic_list = build_tier_array('Prismatic')
        gold_list = build_tier_array('Gold')
        silver_list = build_tier_array('Silver')

        return {
            'top_10_overall': top_10_overall,
            'comprehensive': comprehensive_list,
            'winrate_only': winrate_list,
            'Prismatic': prismatic_list,
            'Gold': gold_list,
            'Silver': silver_list
        }

    except Exception as e:
        logging.error(f"处理海克斯数据异常: {e}")
        # 安全降级，返回空列表
        return {
            'top_10_overall': [],
            'comprehensive': [],
            'winrate_only': [],
            'Prismatic': [],
            'Gold': [],
            'Silver': []
        }
