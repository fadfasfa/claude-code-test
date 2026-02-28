import pandas as pd
import numpy as np
import logging
import os
import json
import hashlib
import time
from typing import List, Dict, Any, Optional, Tuple

# 导入英雄映射数据加载函数
from hero_sync import load_champion_core_data

# ========== 全局缓存池 ==========
# 海克斯计算结果缓存：{(hero_name, df_hash): result_dict}
_hextech_cache_pool: Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]] = {}
# 英雄大盘计算结果缓存：{df_hash: result_list}
_champion_cache_pool: Dict[str, List[Dict[str, Any]]] = {}
# 缓存元数据：{df_hash: {'row_count': int, 'timestamp': float}}
_cache_metadata: Dict[str, Dict[str, Any]] = {}

# 缓存配置
MAX_CACHE_SIZE = 100  # 最大缓存条目数
CACHE_TTL = 300.0  # 缓存生存时间（秒）

def _get_champion_maps():
    """获取英雄名称到 ID/英文名的映射缓存"""
    global _champion_core_cache
    if _champion_core_cache is None:
        try:
            _champion_core_cache = load_champion_core_data()
        except Exception as e:
            logging.warning(f"加载英雄核心数据失败：{e}")
            _champion_core_cache = {}

    # 构建 name_to_id 和 name_to_en 映射
    name_to_id = {}
    name_to_en = {}
    for key, value in _champion_core_cache.items():
        name = value.get('name', '')
        en_name = value.get('en_name', '')
        if name:
            name_to_id[name] = key
            name_to_en[name] = en_name
    return name_to_id, name_to_en


def _compute_df_hash(df: pd.DataFrame) -> str:
    """
    计算 DataFrame 的特征哈希值
    使用行数 + 列名 + 首尾行数据组合生成哈希，平衡性能与准确性
    """
    try:
        # 获取关键特征
        row_count = len(df)
        col_hash = hashlib.md5(str(tuple(df.columns)).encode()).hexdigest()[:8]

        # 采样首行数据（如果存在）
        sample_data = ""
        if row_count > 0:
            sample_data = str(df.iloc[0].tolist()) + str(df.iloc[-1].tolist())

        # 组合生成哈希
        hash_input = f"{row_count}|{col_hash}|{sample_data}"
        return hashlib.md5(hash_input.encode()).hexdigest()[:16]
    except Exception as e:
        logging.warning(f"计算 DataFrame 哈希失败：{e}")
        return str(id(df))


def _get_from_cache(cache_pool: dict, key) -> Optional[Any]:
    """从缓存池获取数据，检查 TTL"""
    if key in cache_pool:
        meta = _cache_metadata.get(key, {})
        # 检查缓存是否过期
        if CACHE_TTL <= 0 or (time.time() - meta.get('timestamp', 0)) < CACHE_TTL:
            return cache_pool[key]
        else:
            # 过期缓存清理
            cache_pool.pop(key, None)
            _cache_metadata.pop(key, None)
    return None


def _set_to_cache(cache_pool: dict, key, value: Any, df: pd.DataFrame) -> None:
    """设置缓存并更新元数据，实现 LRU 淘汰策略"""
    # 检查缓存大小，超出则淘汰最旧条目
    if len(cache_pool) >= MAX_CACHE_SIZE:
        oldest_key = next(iter(cache_pool))
        cache_pool.pop(oldest_key, None)
        _cache_metadata.pop(oldest_key, None)

    cache_pool[key] = value
    _cache_metadata[key] = {
        'row_count': len(df),
        'timestamp': time.time()
    }


def _invalidate_stale_caches(df: pd.DataFrame) -> None:
    """
    失效过时缓存
    当数据行数发生显著变化时，清理相关缓存
    """
    current_rows = len(df)
    stale_keys = []

    for key, meta in _cache_metadata.items():
        # 行数差异超过 10% 视为数据已更新
        if meta.get('row_count', 0) != current_rows:
            stale_keys.append(key)

    for key in stale_keys:
        _hextech_cache_pool.pop(key, None)
        _champion_cache_pool.pop(key, None)
        _cache_metadata.pop(key, None)


def process_champions_data(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    计算全英雄大盘 T 度列表（微量贝叶斯 + Z-Score）

    算法逻辑：
    1. 应用微量贝叶斯平滑公式（及格出场率阈值=0.005）
    2. 计算 Z-Score 进行 80/20 赋分（英雄总分 = Z_贝叶斯胜率 * 0.80 + Z_出场率 * 0.20）
    3. 按分数降序排序

    缓存策略：
    - 使用 DataFrame 特征哈希作为缓存键
    - 若数据未变化，直接返回缓存结果（O(1) 复杂度）
    """
    if df.empty:
        return []

    # ========== 缓存检查 ==========
    df_hash = _compute_df_hash(df)
    cached_result = _get_from_cache(_champion_cache_pool, df_hash)
    if cached_result is not None:
        logging.debug(f"命中英雄大盘缓存，哈希={df_hash}")
        return cached_result

    # 获取英雄映射数据
    name_to_id, name_to_en = _get_champion_maps()

    try:
        # ========== 数据降维去重 ==========
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
                '英雄 ID': name_to_id.get(str(row['英雄名称']), ''),
                '英文名': name_to_en.get(str(row['英雄名称']), ''),
                '英雄胜率': float(row['英雄胜率']) if pd.notna(row['英雄胜率']) else 0.0,
                '英雄出场率': float(row['英雄出场率']) if pd.notna(row['英雄出场率']) else 0.0,
                '贝叶斯胜率': float(row['贝叶斯胜率']) if pd.notna(row['贝叶斯胜率']) else 0.0,
                '综合分数': float(row['综合分数']) if pd.notna(row['综合分数']) else 0.0,
                'Z_贝叶斯胜率': float(row['Z_贝叶斯胜率']) if pd.notna(row['Z_贝叶斯胜率']) else 0.0,
                'Z_出场率': float(row['Z_出场率']) if pd.notna(row['Z_出场率']) else 0.0
            })

        return result

    except Exception as e:
        logging.error(f"处理英雄大盘数据异常：{e}")
        # 安全降级，返回空列表
        return []


def _clear_champion_cache():
    """手动清空英雄大盘缓存（用于强制刷新）"""
    global _champion_cache_pool, _cache_metadata
    _champion_cache_pool.clear()
    _cache_metadata.clear()


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
    1. 动态置信度衰减惩罚：弃用硬编码的 0.0005 幽灵数据过滤，改用基于出场率的平滑衰减
    2. 综合 Z-Score 推荐：胜率差>=0 时 Z_胜率*0.85 + Z_出场*0.15；胜率差<0 时 Z_胜率*0.85 - Z_出场*0.15
    3. 纯胜率极值榜单（按海克斯胜率降序）
    4. 按阶级分离（Prismatic/Gold/Silver），每个阶级同时支持综合得分和纯胜率排序

    缓存策略：
    - 使用 (英雄名，df_hash) 作为缓存键
    - 同一英雄数据未变化时直接返回缓存（O(1) 复杂度）
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

    # ========== 缓存检查 ==========
    df_hash = _compute_df_hash(df)
    cache_key = (name, df_hash)
    cached_result = _get_from_cache(_hextech_cache_pool, cache_key)
    if cached_result is not None:
        logging.debug(f"命中海克斯缓存，英雄={name}, 哈希={df_hash}")
        return cached_result

    try:
        # 创建副本避免修改原始数据
        data = df.copy()

        # 确保必要列存在
        required_cols = ['英雄名称', '海克斯名称', '海克斯胜率', '海克斯出场率', '胜率差']
        if not all(col in data.columns for col in required_cols):
            logging.warning(f"缺少必要列，当前列：{data.columns.tolist()}")
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
            logging.warning(f"英雄 '{name}' 无数据")
            return {
                'top_10_overall': [],
                'comprehensive': [],
                'winrate_only': [],
                'Prismatic': [],
                'Gold': [],
                'Silver': []
            }

        # ========== 动态置信度衰减惩罚（替代硬编码的 0.0005 过滤） ==========
        # 旧逻辑：ghost_threshold = 0.0005; hero_data = hero_data[hero_data['海克斯出场率'] >= ghost_threshold]
        # 新逻辑：使用平滑衰减因子，让低样本高胜率的海克斯分数自然下降，避免错杀
        # 衰减因子 = 1 / (1 + (threshold / pick_rate)^2)
        # 当出场率远低于阈值时，因子趋近于 0；当出场率高于阈值时，因子趋近于 1
        confidence_threshold = 0.001  # 置信度阈值（0.1%）

        def apply_confidence_penalty(row):
            """计算动态置信度惩罚因子"""
            pick_rate = row['海克斯出场率']
            if pick_rate <= 0:
                return 0.0
            # 使用 Sigmoid 型衰减曲线
            penalty = 1.0 / (1.0 + (confidence_threshold / pick_rate) ** 2)
            return penalty

        hero_data['置信度因子'] = hero_data.apply(apply_confidence_penalty, axis=1)

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

        # ========== 综合得分计算（带置信度衰减） ==========
        # 基础逻辑：胜率差>=0 时 Z_胜率*0.85 + Z_出场*0.15；胜率差<0 时 Z_胜率*0.85 - Z_出场*0.15
        # 新增：乘以置信度因子，让低样本数据自然衰减
        def calc_comprehensive_score(row):
            z_wr = row['Z_胜率差']
            z_pr = row['Z_出场率']
            wr_diff = row['胜率差']
            confidence = row['置信度因子']

            if wr_diff >= 0:
                base_score = z_wr * 0.85 + z_pr * 0.15
            else:
                base_score = z_wr * 0.85 - z_pr * 0.15

            # 应用置信度衰减
            return base_score * confidence

        hero_data['综合得分'] = hero_data.apply(calc_comprehensive_score, axis=1)

        # ========== 综合得分防爆盾（NaN 填充） ==========
        hero_data['综合得分'] = hero_data['综合得分'].fillna(0.0)

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

            # 按综合得分排序并截取前 10
            tier_data_by_score = tier_data.sort_values(by='综合得分', ascending=False).head(10)
            result = []
            for _, row in tier_data_by_score.iterrows():
                result.append(build_hextech_card(row, include_score=True))

            return result

        prismatic_list = build_tier_array('Prismatic')
        gold_list = build_tier_array('Gold')
        silver_list = build_tier_array('Silver')

        result = {
            'top_10_overall': top_10_overall,
            'comprehensive': comprehensive_list,
            'winrate_only': winrate_list,
            'Prismatic': prismatic_list,
            'Gold': gold_list,
            'Silver': silver_list
        }

        # ========== 缓存结果 ==========
        _set_to_cache(_hextech_cache_pool, cache_key, result, df)
        return result

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

def clear_hextech_cache():
    """手动清空海克斯缓存（用于强制刷新）"""
    global _hextech_cache_pool, _cache_metadata
    _hextech_cache_pool.clear()
    _cache_metadata.clear()
