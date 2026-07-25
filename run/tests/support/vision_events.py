"""构造稳定的 Vision 选择事件，避免主题测试各自复制快照结构。"""

from __future__ import annotations

from typing import Any


def ready_slot(slot: int, augment_id: str, name: str) -> dict[str, Any]:
    """返回有图标佐证的 strong 槽位；每次调用都构造独立嵌套对象。"""

    candidate = {
        "augment_id": augment_id,
        "name": name,
        "tier": "Gold",
        "confidence": 0.91,
    }
    return {
        "slot": slot,
        "channels": {
            "text": {"margin": 0.05, "top_candidates": [candidate]},
            "text_alt": {"margin": 0.05, "top_candidates": [candidate]},
            "icon": {"margin": 0.03, "top_candidates": [{**candidate, "confidence": 0.86}]},
        },
    }


def medium_slot(slot: int, augment_id: str, name: str) -> dict[str, Any]:
    """返回没有独立图标佐证的 medium 双字体槽位。"""

    candidate = {
        "augment_id": augment_id,
        "name": name,
        "tier": "Gold",
        "confidence": 0.91,
    }
    return {
        "slot": slot,
        "channels": {
            "text": {"margin": 0.05, "top_candidates": [candidate]},
            "text_alt": {"margin": 0.05, "top_candidates": [candidate]},
        },
    }


def weak_slot(slot: int, augment_id: str, name: str) -> dict[str, Any]:
    """返回低于判定阈值的槽位，供状态机超时与恢复场景使用。"""

    candidate = {
        "augment_id": augment_id,
        "name": name,
        "tier": "Gold",
        "confidence": 0.69,
    }
    return {
        "slot": slot,
        "channels": {"text": {"margin": 0.015, "top_candidates": [candidate]}},
    }


def selection_event() -> dict[str, Any]:
    """返回三槽位、无悬停遮挡的标准海克斯选择快照。"""

    return {
        "source": {
            "scene_present": True,
            "scene_kind": "hextech",
            "selection_button_present": True,
            "cursor_over_cards": False,
            "card_residue": False,
            "name_residue": [False, False, False],
        },
        "_raw_slots": [
            ready_slot(0, "augment_a", "强化 A"),
            ready_slot(1, "augment_b", "强化 B"),
            ready_slot(2, "augment_c", "强化 C"),
        ],
    }
