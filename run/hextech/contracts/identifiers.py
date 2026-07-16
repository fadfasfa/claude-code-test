"""领域 ID 的唯一规范化入口。

外部 adapter 可以传入 LCU/Pandas 常见的整数、数字字符串或整数浮点；进入核心后
一律使用无前导零的十进制字符串，避免 ``24``、``24.0`` 和 ``"024"`` 分叉。
"""

from __future__ import annotations

import math
import re
from typing import NewType


ChampionId = NewType("ChampionId", str)
AugmentId = NewType("AugmentId", str)
ItemId = NewType("ItemId", str)
GenerationId = NewType("GenerationId", str)
GameSessionId = NewType("GameSessionId", str)
VisionEpoch = NewType("VisionEpoch", int)

_VISION_AUGMENT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,128}$")


class InvalidIdentifierError(ValueError):
    """外部 ID 无法安全规范化。"""


def normalize_decimal_identifier(value: object) -> str:
    """把正十进制 ID 规范化为字符串；拒绝模糊和有损输入。"""

    if isinstance(value, bool) or value is None:
        raise InvalidIdentifierError("identifier_type_invalid")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise InvalidIdentifierError("identifier_not_integer")
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.isascii() or not text.isdecimal():
            raise InvalidIdentifierError("identifier_format_invalid")
        number = int(text, 10)
    else:
        raise InvalidIdentifierError("identifier_type_invalid")
    if number <= 0:
        raise InvalidIdentifierError("identifier_not_positive")
    return str(number)


def champion_id(value: object) -> ChampionId:
    return ChampionId(normalize_decimal_identifier(value))


def augment_id(value: object) -> AugmentId:
    return AugmentId(normalize_decimal_identifier(value))


def item_id(value: object) -> ItemId:
    return ItemId(normalize_decimal_identifier(value))


def optional_champion_id(value: object) -> ChampionId | None:
    try:
        return champion_id(value)
    except InvalidIdentifierError:
        return None


def optional_augment_id(value: object) -> AugmentId | None:
    """规范化数字统计 ID，并允许 Vision 输出的受限稳定标识。"""

    try:
        return augment_id(value)
    except InvalidIdentifierError:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text or not _VISION_AUGMENT_ID_RE.fullmatch(text):
            return None
        return AugmentId(text)
