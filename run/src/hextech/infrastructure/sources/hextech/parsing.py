"""Hextech 英雄详情 payload 与 HTML 的纯解析。"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback

from bs4 import BeautifulSoup

from hextech.modules.acquisition.hextech.contracts import HextechStatRecord

DEFAULT_HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL = "https://cdn.dtodo.cn/hextech/champion-details"
HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL = (
    os.getenv("HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL", DEFAULT_HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL).strip()
    or DEFAULT_HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL
)
def _clean_augment_text(value) -> str:
    # 统一清洗文本字段，避免空白干扰后续拼接。
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _extract_augment_meta(raw_item: dict) -> dict:
    # 提取增强符文描述信息；tooltip 缺失时回退 description。
    description = _clean_augment_text(
        raw_item.get("description")
        or raw_item.get("desc")
    )
    tooltip = _clean_augment_text(
        raw_item.get("tooltip")
        or raw_item.get("toolTip")
        or raw_item.get("tips")
    )
    if not tooltip:
        tooltip = description
    spell_values = _extract_spell_values(raw_item)
    return {
        "description": description,
        "tooltip": tooltip,
        "spell_values": spell_values,
    }


def _extract_spell_values(raw_item: dict) -> dict:
    # 提取增强符文中的可替换数值，用于后续 tooltip_plain 占位符解析。
    values = {}

    def append_value(name, value):
        key = _clean_augment_text(name)
        if not key:
            return
        try:
            values[key] = float(value)
        except (TypeError, ValueError):
            return

    def consume_mapping(mapping):
        if not isinstance(mapping, dict):
            return
        for key, val in mapping.items():
            if isinstance(val, (int, float)):
                append_value(key, val)
            elif isinstance(val, list):
                # 兼容 [100, 120, ...] 这种多等级数组，取首个有效数值。
                for item in val:
                    if isinstance(item, (int, float)):
                        append_value(key, item)
                        break

    consume_mapping(raw_item.get("spellDataValues"))
    consume_mapping(raw_item.get("DataValues"))
    consume_mapping(raw_item.get("dataValues"))
    consume_mapping(raw_item.get("mDataValues"))

    effects = raw_item.get("mEffects")
    if isinstance(effects, dict):
        consume_mapping(effects)
    elif isinstance(effects, list):
        for entry in effects:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("key") or entry.get("id")
                val = entry.get("value") or entry.get("values") or entry.get("amount")
                if isinstance(val, list):
                    val = next((x for x in val if isinstance(x, (int, float))), None)
                if isinstance(val, (int, float)):
                    append_value(name, val)

    return values


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percent_text_to_rate(value: str) -> float:
    text = str(value or "").strip().replace(" ", "")
    if not text.endswith("%"):
        return 0.0
    return _to_float(text.removesuffix("%")) / 100.0


def _source_tier_value(value) -> int:
    text = str(value or "").strip().upper().removeprefix("T")
    try:
        return int(text)
    except ValueError:
        return 99


def _source_tier_label(value) -> str:
    tier = _source_tier_value(value)
    return f"T{tier}" if tier != 99 else str(value or "").strip()


def _metadata_tier_from_rarity(value) -> str:
    rarity_to_tier = {
        0: "白银",
        1: "黄金",
        2: "棱彩",
        3: "棱彩",
        "0": "白银",
        "1": "黄金",
        "2": "棱彩",
        "3": "棱彩",
        "silver": "白银",
        "gold": "黄金",
        "prismatic": "棱彩",
        "白银": "白银",
        "黄金": "黄金",
        "棱彩": "棱彩",
    }
    return rarity_to_tier.get(value, rarity_to_tier.get(str(value or "").strip().lower(), ""))


def _decode_next_flight_payloads(html: str) -> list[str]:
    # Next/React Flight 会把长文本切成多段 push 字符串；这里仅做字符串反转义，不解释页面其它文本。
    payloads = []
    pattern = re.compile(
        r"<script>self\.__next_f\.push\(\[1,\"(.*?)\"\]\)</script>",
        re.DOTALL,
    )
    for match in pattern.finditer(html or ""):
        raw_payload = match.group(1)
        try:
            payloads.append(json.loads(f'"{raw_payload}"'))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logging.debug("React Flight payload 反转义失败：%s", exc)
    return payloads


def _extract_champion_augments_ref_id(payloads: list[str], champ_id: str) -> str:
    escaped_id = re.escape(str(champ_id))
    ref_pattern = re.compile(
        rf'"championAugmentsStats"\s*:\s*\{{\s*"{escaped_id}"\s*:\s*\[\s*\[\s*"{escaped_id}"\s*,\s*"\$([0-9A-Za-z]+)"',
        re.DOTALL,
    )
    for payload in payloads:
        match = ref_pattern.search(payload)
        if match:
            return match.group(1)
    return ""


def _extract_flight_text_blocks(payloads: list[str]) -> dict[str, str]:
    # Flight 文本块形如 `29:T9159,`，下一段 push 字符串才是这个 ID 对应的正文。
    blocks = {}
    pending_ref = ""
    ref_pattern = re.compile(r"(?:^|\n)([0-9A-Za-z]+):T[0-9A-Fa-f]+,\s*$")
    for payload in payloads:
        if pending_ref and payload.lstrip().startswith("{"):
            blocks[pending_ref] = payload.strip()
            pending_ref = ""

        match = ref_pattern.search(payload)
        if match:
            pending_ref = match.group(1)
    return blocks


def _parse_augments_payload(payload: str) -> dict:
    text = str(payload or "").strip()
    if not text:
        return {}

    start = text.find('{"augments"')
    if start > 0:
        text = text[start:]

    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    augments = parsed.get("augments") if isinstance(parsed, dict) else None
    if not isinstance(augments, dict):
        return {}
    return augments


def _looks_like_augment_stats(augments: dict) -> bool:
    """校验 dict 是否为 id→统计数据 结构（排除 SEO 元数据块）。

    SEO 块如 {"augments":{"title":"...","description":"..."}} 的 value 是纯字符串；
    真实 augment stats 块 value 是含 win_rate/winRate/pick_rate/pickRate/tier 的字典。
    """
    if not isinstance(augments, dict):
        return False
    for value in augments.values():
        if isinstance(value, dict) and (
            "win_rate" in value or "winRate" in value
            or "pick_rate" in value or "pickRate" in value
            or "tier" in value
        ):
            return True
    return False


def _extract_react_flight_augments(html: str, champ_id: str) -> dict:
    payloads = _decode_next_flight_payloads(html)
    if not payloads:
        return {}

    ref_id = _extract_champion_augments_ref_id(payloads, champ_id)
    if ref_id:
        augments = _parse_augments_payload(_extract_flight_text_blocks(payloads).get(ref_id, ""))
        if augments:
            return augments

    # 兼容极简测试快照或站点 Flight 形态变化：仍只读取 React Flight 内唯一的 augments JSON 块。
    # 加 _looks_like_augment_stats 校验排除 SEO 元数据块（其 value 为纯字符串，不含统计字段）。
    candidates = [_parse_augments_payload(payload) for payload in payloads if '{"augments"' in payload]
    candidates = [item for item in candidates if item and _looks_like_augment_stats(item)]
    if len(candidates) == 1:
        logging.warning("[%s] 未找到 championAugmentsStats 文本块，使用唯一 React Flight augments 块兜底。", champ_id)
        return candidates[0]
    return {}


def _sort_source_augments(augments: dict) -> list[tuple[str, dict]]:
    def sort_key(entry):
        aug_id, raw_stats = entry
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        rank = int(_to_float(stats.get("rank"), default=0.0))
        return (
            rank if rank > 0 else 9999,
            _source_tier_value(stats.get("tier")),
            -_to_float(stats.get("win_rate", stats.get("winRate"))),
            -_to_float(stats.get("pick_rate", stats.get("pickRate"))),
            str(aug_id),
        )

    return sorted(augments.items(), key=sort_key)


def build_hextech_champion_detail_json_url(champ_id: str) -> str:
    return f"{HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL.rstrip('/')}/{str(champ_id).strip()}.json"


def _extract_detail_json_augments(payload: dict, champ_id: str) -> dict:
    if not isinstance(payload, dict):
        return {}
    candidates = payload.get("championAugments")
    if not isinstance(candidates, list):
        return {}
    expected_id = str(champ_id)
    for item in candidates:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        item_champ_id = str(item[0])
        if item_champ_id != expected_id:
            continue
        raw_stats = item[1]
        if isinstance(raw_stats, str):
            try:
                parsed = json.loads(raw_stats)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        elif isinstance(raw_stats, dict):
            parsed = raw_stats
        else:
            continue
        augments = parsed.get("augments") if isinstance(parsed, dict) else None
        if isinstance(augments, dict) and _looks_like_augment_stats(augments):
            return augments
    return {}


def _source_total_from_augments(augments: dict) -> int:
    totals = []
    for raw_stats in augments.values():
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        total = _to_float(stats.get("total"), default=0.0)
        if total > 0:
            totals.append(int(total))
    return max(totals, default=0)


def _rows_from_source_augments(
    source_augments: dict,
    *,
    aug_id_map: dict,
    truth_dict: dict,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_tier_map: dict | None = None,
) -> list[dict]:
    rows = []
    for fallback_rank, (raw_id, raw_stats) in enumerate(_sort_source_augments(source_augments), start=1):
        mid = str(raw_id)
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        try:
            win = _to_float(stats.get("win_rate", stats.get("winRate")))
            pick = _to_float(stats.get("pick_rate", stats.get("pickRate")))

            if pick > 1.0:
                pick = pick / 100.0
                logging.debug(f"[量纲转换] 海克斯 ID={mid}，出场率从百分数转换为小数：{pick*100:.1f}% -> {pick:.4f}")
            pick = min(1.0, max(0.0, pick))

            web_name = aug_id_map.get(mid, "")
            local_tier = truth_dict.get(web_name) or (aug_tier_map or {}).get(mid) or "未知"
            source_rank = int(_to_float(stats.get("rank"), default=float(fallback_rank))) or fallback_rank
            if web_name and win > 0:
                rows.append(
                    _build_row(
                        champ_id=champ_id,
                        champ_name=champ_name,
                        champ_data=champ_data,
                        augment_id=mid,
                        augment_name=web_name,
                        source_rank=source_rank,
                        source_tier=stats.get("tier"),
                        local_tier=local_tier,
                        winrate=win,
                        pickrate=pick,
                    )
                )
        except (ValueError, IndexError, AttributeError) as e:
            logging.warning(
                f"[{champ_name}] 海克斯 ID={mid} 解析失败：{e} | "
                f"源站字段：{stats} | 堆栈：{traceback.format_exc().strip()}"
            )
            continue
    return sorted(rows, key=lambda item: item["源站排名"])


def extract_champion_detail_json_stats(
    payload: dict,
    aug_id_map: dict,
    truth_dict: dict,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_tier_map: dict | None = None,
) -> list[dict]:
    """解析 CDN champion-details JSON；这是全量海克斯统计的快速链路。"""

    source_augments = _extract_detail_json_augments(payload, champ_id)
    if not source_augments:
        return []
    return _rows_from_source_augments(
        source_augments,
        aug_id_map=aug_id_map,
        truth_dict=truth_dict,
        champ_id=champ_id,
        champ_name=champ_name,
        champ_data=champ_data,
        aug_tier_map=aug_tier_map,
    )


def _build_row(
    *,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    augment_id: str,
    augment_name: str,
    source_rank: int,
    source_tier: str,
    local_tier: str,
    winrate: float,
    pickrate: float,
) -> dict:
    record = HextechStatRecord(
        champion_id=str(champ_id),
        champion_name=str(champ_name),
        champion_tier=str(champ_data.get("tier", "T3")),
        champion_win_rate=float(champ_data.get("winRate", 0)),
        champion_pick_rate=float(champ_data.get("pickRate", 0)),
        augment_id=str(augment_id),
        source_rank=int(source_rank),
        source_tier=_source_tier_label(source_tier),
        augment_tier=str(local_tier),
        augment_name=str(augment_name),
        augment_win_rate=float(winrate),
        augment_pick_rate=min(1.0, max(0.0, float(pickrate))),
    )
    return record.to_csv_row()


def _extract_rendered_table_stats(
    html: str,
    aug_id_map: dict,
    truth_dict: dict,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_tier_map: dict | None = None,
) -> list[dict]:
    """解析渲染后的隐藏全量表格；静态 React Flight 只包含折叠窗口。"""

    id_by_name = {str(name): str(augment_id) for augment_id, name in aug_id_map.items() if str(name or "").strip()}
    if not id_by_name:
        return []

    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    seen_ids = set()
    for tr in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"], recursive=False)]
        if len(cells) != 5:
            continue
        rank_text, augment_name, source_tier, winrate_text, pickrate_text = cells
        if not rank_text.isdigit() or not source_tier.replace(" ", "").upper().startswith("T"):
            continue
        if not winrate_text.strip().endswith("%") or not pickrate_text.strip().endswith("%"):
            continue
        augment_name = _clean_augment_text(augment_name)
        augment_id = id_by_name.get(augment_name, "")
        if not augment_id or augment_id in seen_ids:
            continue
        winrate = _percent_text_to_rate(winrate_text)
        pickrate = _percent_text_to_rate(pickrate_text)
        if winrate <= 0:
            continue
        seen_ids.add(augment_id)
        local_tier = truth_dict.get(augment_name) or (aug_tier_map or {}).get(augment_id) or "未知"
        rows.append(
            _build_row(
                champ_id=champ_id,
                champ_name=champ_name,
                champ_data=champ_data,
                augment_id=augment_id,
                augment_name=augment_name,
                source_rank=int(rank_text),
                source_tier=source_tier,
                local_tier=local_tier,
                winrate=winrate,
                pickrate=pickrate,
            )
        )
    return sorted(rows, key=lambda item: item["源站排名"])


def extract_champion_stats(
    html: str,
    aug_id_map: dict,
    truth_dict: dict,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_tier_map: dict | None = None,
) -> list:
    rendered_rows = _extract_rendered_table_stats(
        html,
        aug_id_map,
        truth_dict,
        champ_id,
        champ_name,
        champ_data,
        aug_tier_map,
    )
    if rendered_rows:
        return rendered_rows

    # 静态响应只解析当前英雄组件引用的 React Flight augments 数据块，避免整页统计串源。
    rows = []
    source_augments = _extract_react_flight_augments(html, champ_id)
    if not source_augments:
        logging.warning("[%s] 未解析到当前英雄 React Flight augments 数据块", champ_name)
        return rows

    rows = _rows_from_source_augments(
        source_augments,
        aug_id_map=aug_id_map,
        truth_dict=truth_dict,
        champ_id=champ_id,
        champ_name=champ_name,
        champ_data=champ_data,
        aug_tier_map=aug_tier_map,
    )
    return rows
