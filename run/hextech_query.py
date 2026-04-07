import os
import glob
import json
import sys
import unicodedata
import pandas as pd
from champion_aliases import load_champion_alias_map, resolve_champion_name
from hero_sync import CONFIG_DIR, CORE_DATA_FILE
from alias_utils import normalize_alias_token, unique_alias_tokens
from runtime_data import normalize_runtime_df

if os.name == 'nt': os.system('')  # 启用 Windows 终端颜色输出。
RESET = "\033[0m"

# 延迟加载基础数据，降低启动耗时。
CORE_DATA = None
CHAMP_NAME_MAP = {}

def init_core_data():
    global CORE_DATA, CHAMP_NAME_MAP
    if CORE_DATA is None:
        from hero_sync import load_champion_core_data
        try:
            CORE_DATA = load_champion_core_data()
            CHAMP_NAME_MAP = {v["name"]: v["title"] for k, v in CORE_DATA.items()}
        except (json.JSONDecodeError, KeyError, ValueError):
            CORE_DATA = {}
            CHAMP_NAME_MAP = {}
        except Exception:
            CORE_DATA = {}
            CHAMP_NAME_MAP = {}

GLOBAL_LAST_HERO = None
_alias_cache = None

def set_last_hero(name):
    global GLOBAL_LAST_HERO
    GLOBAL_LAST_HERO = name

def _normalize_query_df(shared_df=None):
    if shared_df is None:
        latest_csv = get_latest_csv()
        if not latest_csv:
            return pd.DataFrame(), None
        df = normalize_runtime_df(pd.read_csv(latest_csv))
        source = latest_csv
    elif isinstance(shared_df, pd.DataFrame):
        df = normalize_runtime_df(shared_df.copy())
        source = "shared_df"
    else:
        df = normalize_runtime_df(pd.DataFrame(shared_df).copy())
        source = "shared_df"
    return df, source

def get_highlight_color(row):
    wr_diff = row['胜率差']
    if wr_diff < 0:
        diff_val = abs(wr_diff)
        if diff_val <= 0.03: return "\033[38;5;214m" 
        if diff_val <= 0.07: return "\033[38;5;196m" 
        if diff_val <= 0.12: return "\033[38;5;160m" 
        return "\033[38;5;129m"                      
    else:
        score = row['海克斯胜率'] + (row['海克斯出场率'] * 0.3)
        if score >= 0.56: return "\033[38;5;51m"   
        if score >= 0.53: return "\033[38;5;46m"   
        if score >= 0.505: return "\033[38;5;118m" 
        return ""

def get_latest_csv():
    files = glob.glob(os.path.join(CONFIG_DIR, "Hextech_Data_*.csv"))
    if not files: return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]

def get_char_width(char):
    # 全角和宽字符按 2 计算，其余按 1 计算。
    return 2 if unicodedata.east_asian_width(char) in ('F', 'W') else 1

def align_text(text, width):
    text = str(text)
    cur_len = 0
    res = ""
    for char in text:
        char_w = get_char_width(char)
        if cur_len + char_w > width: break
        res += char
        cur_len += char_w
    return res + ' ' * (width - cur_len)

def print_side_by_side_table(df_source, title, limit=None):
    df_all = df_source.copy()
    if limit: df_all = df_all[df_all['胜率差'] >= 0] 
    
    df_comp = df_all.sort_values(by='综合得分', ascending=False).reset_index(drop=True)
    df_win = df_all.sort_values(by=['海克斯胜率', '海克斯出场率'], ascending=[False, False]).reset_index(drop=True)
    if limit: df_comp, df_win = df_comp.head(limit), df_win.head(limit)
    
    NAME_W, VAL_W = 24, 8
    print("\n" + "="*110 + f"\n {title}\n" + "="*110)
    print(align_text("海克斯(综合推荐)", NAME_W) + align_text("胜率", VAL_W) + align_text("出场", VAL_W) + "  ||  " + 
          align_text("海克斯(纯胜率)", NAME_W) + align_text("胜率", VAL_W) + align_text("出场", VAL_W))
    print("-" * 110)
    
    for i in range(len(df_comp)):
        rc = df_comp.iloc[i] if i < len(df_comp) else None
        rw = df_win.iloc[i] if i < len(df_win) else None
        l_content, r_content = " "*NAME_W + " "*VAL_W*2, " "*NAME_W + " "*VAL_W*2
        l_color, r_color = "", ""
        
        if rc is not None:
            l_color = get_highlight_color(rc)
            tier_prefix = rc['海克斯阶级'][0] if isinstance(rc['海克斯阶级'], str) and rc['海克斯阶级'] else "?"
            l_content = align_text(f"{i+1}.[{tier_prefix}]{rc['海克斯名称']}", NAME_W) + align_text(f"{rc['海克斯胜率']:.1%}", VAL_W) + align_text(f"{rc['海克斯出场率']:.1%}", VAL_W)
        if rw is not None:
            r_color = get_highlight_color(rw)
            tier_prefix = rw['海克斯阶级'][0] if isinstance(rw['海克斯阶级'], str) and rw['海克斯阶级'] else "?"
            r_content = align_text(f"{i+1}.[{tier_prefix}]{rw['海克斯名称']}", NAME_W) + align_text(f"{rw['海克斯胜率']:.1%}", VAL_W) + align_text(f"{rw['海克斯出场率']:.1%}", VAL_W)
            
        print(f"{l_color}{l_content}{RESET if l_color else ''}  ||  {r_color}{r_content}{RESET if r_color else ''}")

def add_new_alias(new_alias, official_names):
    print(f"\n错误 未匹配到对应英雄: \"{new_alias}\"")
    print("请选择您的操作：\n [任意键] 只是打错了，重新输入\n [2] 我要将该词添加为某个英雄的新外号")
    try:
        choice = input("请 请选择 (2/任意键): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice != '2':
        return None

    target_input = input("请 请输入该英雄的官方名称或系统中已有的外号 (例如: 皇子): ").strip()
    target_hero = get_official_hero_name(target_input, official_names)

    if not target_hero:
        return None

    confirm = input(f"请 确认要将 \"{new_alias}\" 永久添加为（{target_hero}）的外号吗？(y/n): ").strip().lower()
    if confirm == 'y':
        global CORE_DATA, CHAMP_NAME_MAP, _alias_cache
        from hero_sync import load_champion_core_data

        try:
            core_data = load_champion_core_data()
        except Exception:
            return None

        target_key = None
        for champ_id, champ_info in core_data.items():
            if str(champ_info.get("name", "")).strip() == target_hero:
                target_key = champ_id
                break

        if not target_key:
            return None

        target_entry = dict(core_data.get(target_key, {}))
        aliases = target_entry.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        if new_alias not in aliases:
            aliases.append(new_alias)
        target_entry["aliases"] = aliases
        core_data[target_key] = target_entry

        tmp_path = CORE_DATA_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(core_data, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, CORE_DATA_FILE)

        CORE_DATA = None
        CHAMP_NAME_MAP = {}
        _alias_cache = None
        print("成功 添加成功！")
        return target_hero
    return None


def build_default_aliases():
    print("\n警告 正在加载统一英雄别名索引...")
    aliases = {}
    try:
        from hero_sync import load_champion_core_data
        core_data = load_champion_core_data()
        for _, v in core_data.items():
            name = v.get("name")
            if not name:
                continue
            title = v.get("title")
            en = v.get("en_name", "")
            aliases[name] = unique_alias_tokens(
                [name, title, en],
                v.get("aliases", []),
            )
    except Exception as e:
        print(f"警告 核心数据提取失败: {e}")

    for hero_name, index_aliases in load_champion_alias_map().items():
        aliases.setdefault(hero_name, [])
        aliases[hero_name] = unique_alias_tokens(aliases[hero_name], index_aliases)
    return aliases


def load_hero_aliases():
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache
    _alias_cache = build_default_aliases()
    return _alias_cache


def get_official_hero_name(user_input, official_names):
    init_core_data()
    u_in = normalize_alias_token(user_input)
    resolved_name = resolve_champion_name(user_input)
    if resolved_name and resolved_name in official_names:
        return resolved_name

    hero_aliases = load_hero_aliases()
    potential = set()
    for official_name, aliases in hero_aliases.items():
        normalized_aliases = [normalize_alias_token(alias) for alias in aliases]
        if any(u_in == alias or u_in in alias or alias in u_in for alias in normalized_aliases if alias):
            if official_name in official_names:
                potential.add(official_name)
    for name in official_names:
        title = CHAMP_NAME_MAP.get(name, "")
        normalized_name = normalize_alias_token(name)
        normalized_title = normalize_alias_token(title)
        if (
            u_in in normalized_name
            or u_in in normalized_title
            or normalized_name in u_in
            or normalized_title in u_in
        ):
            potential.add(name)
    results = sorted(list(potential))
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    print("\n[?] 匹配到多个英雄:")
    for i, res in enumerate(results, 1):
        print(f" [{i}] {res}")
    try:
        idx = int(input("请 请输入序号选择: ")) - 1
        return results[idx]
    except (ValueError, IndexError):
        return None

def display_hero_hextech(df, hero_name, target_tier=None, is_from_ui=False):
    global GLOBAL_LAST_HERO
    GLOBAL_LAST_HERO = hero_name
    
    hero_data = df[df['英雄名称'] == hero_name].copy()
    if hero_data.empty: 
        print(f"错误 未在最新战报中找到 {hero_name} 的数据。")
        return
        
    h_win = hero_data.iloc[0]['英雄胜率']
    h_tier = hero_data.iloc[0]['英雄评级']
    stats_str = f"[评级:{h_tier} | 胜率:{h_win:.1%}]"

    if target_tier:
        tier_map = {"1":"白银", "2":"黄金", "3":"棱彩", "白银":"白银", "黄金":"黄金", "棱彩":"棱彩"}
        t_name = tier_map.get(str(target_tier))
        if t_name:
            tier_data = hero_data[hero_data['海克斯阶级'] == t_name]
            if not tier_data.empty: print_side_by_side_table(tier_data, f"综合推荐 （{hero_name}）- {t_name}阶级战报")
    else:
        print_side_by_side_table(hero_data, f"尊享 （{hero_name}）{stats_str} 全阶级 Top 25", limit=25)

    if is_from_ui:
        prompt = "\n请 （输入）称号/别名"
        if GLOBAL_LAST_HERO: prompt += f" | 快捷: 1/2/3查（{GLOBAL_LAST_HERO}）"
        prompt += " (q退出, u悬浮窗): "
        print(prompt, end="", flush=True)

def main_query(shared_df=None, ui_instance=None):
    df, source = _normalize_query_df(shared_df)
    payload = {
        "source": source,
        "row_count": int(len(df)),
        "column_names": list(df.columns),
        "last_hero": GLOBAL_LAST_HERO,
        "has_data": not df.empty,
    }

    if ui_instance is not None:
        try:
            setattr(ui_instance, "backend_query_snapshot", payload)
        except Exception:
            pass

    return payload

if __name__ == "__main__":
    sys.exit(0)
