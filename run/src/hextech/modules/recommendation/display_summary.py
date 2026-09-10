"""为 Overlay 生成不改写原文身份的有界展示摘要。

本模块只处理已经完成来源验证、身份关联、原文去重和覆盖率统计的联动条目。
它不访问网络或磁盘，也不把派生文字写回 ``content``。调用方必须在后台准备
线程传入具体显示规格；Canvas 仍需用实际 Tk 字体对最终文字复核。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypedDict


RULE_VERSION = "overlay-display-summary-v1"
SUMMARY_READY = "ready"
SUMMARY_REVIEW_REQUIRED = "review_required"
REVIEW_FALLBACK_TEXT = "内容较长，已保留完整原文待审"

MeasureText = Callable[[str, int], float]


class DisplaySummarySpec(TypedDict):
    id: str
    max_width_px: int
    font_px: int
    max_lines: int
    prefix: str


class DisplaySummary(TypedDict):
    text: str
    source_text: str
    source_sha256: str
    rule_version: str
    display_spec: DisplaySummarySpec
    status: str
    omission_reason: str
    fallback_text: str
    original_char_count: int
    summary_char_count: int
    line_count: int
    removed_duplicate_sentences: int
    removed_boilerplate_sentences: int
    truncated: bool


@dataclass(frozen=True)
class CleanedDisplayContent:
    text: str
    removed_duplicate_sentences: int
    removed_boilerplate_sentences: int


# 只删除完整、精确命中的无信息句。这里故意不收录“推荐”“很强”等可能承载
# 来源判断的措辞，避免把语义压成主观短标签或误删适用条件。
_PROVEN_BOILERPLATE = frozenset(
    {
        "点击查看详情。",
        "更多内容请查看详情页。",
    }
)
_PUNCTUATION_TRANSLATION = str.maketrans({"｡": "。", "﹐": "，", "﹕": "：", "﹔": "；"})
_CLOSING_PUNCTUATION = frozenset("”’」』】）》）]")


def _source_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        encoded = f"{type(value).__module__}.{type(value).__qualname__}:{value!r}"
    return encoded.encode("utf-8", errors="surrogatepass")


def content_source_sha256(value: Any) -> str:
    """对原始 content 的值和类型取稳定身份，不使用清洗后的派生文字。"""

    return hashlib.sha256(_source_bytes(value)).hexdigest()


def _normalize_source_text(value: str) -> str:
    text = unicodedata.normalize("NFC", value).translate(_PUNCTUATION_TRANSLATION)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def _sentence_units(text: str) -> list[str]:
    """只按明确的中英文终止符切分，并把右引号留在原句。

    URL、版本号、小数点和冒号/分号都不会被当作句界；无法可靠分析的复合文本
    保持为一个整体，宁可进入待审也不进行猜测式摘要。
    """

    if not text:
        return []
    units: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in "。！？!?":
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] in _CLOSING_PUNCTUATION:
            end += 1
        unit = text[start:end].strip()
        if unit:
            units.append(unit)
        start = end
        index = end
    tail = text[start:].strip()
    if tail:
        units.append(tail)
    return units


def clean_display_content(value: str) -> CleanedDisplayContent:
    """规范空白/等价标点，并只删除完全重复句和白名单套话。"""

    normalized = _normalize_source_text(value)
    units = _sentence_units(normalized)
    kept: list[str] = []
    previous_unit = ""
    duplicate_count = 0
    boilerplate_count = 0
    for unit in units:
        # 只去相邻的完整重复句。跨段重复可能分别从属于不同条件，不能仅按
        # 文本相同就合并（例如“对 A”与“对 B”后各自的“优先选择”）。
        if unit == previous_unit:
            duplicate_count += 1
            continue
        previous_unit = unit
        if unit in _PROVEN_BOILERPLATE:
            boilerplate_count += 1
            continue
        kept.append(unit)
    joined = ""
    for unit in kept:
        if joined and unit and unit[0].isascii() and unit[0].isalnum() and joined[-1] in "!?！？":
            joined += " "
        joined += unit
    return CleanedDisplayContent(
        text=joined,
        removed_duplicate_sentences=duplicate_count,
        removed_boilerplate_sentences=boilerplate_count,
    )


def _normalized_spec(value: Mapping[str, Any]) -> DisplaySummarySpec:
    try:
        width = int(value.get("max_width_px") or 0)
        font = int(value.get("font_px") or 0)
        lines = int(value.get("max_lines") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_display_summary_spec") from exc
    spec_id = str(value.get("id") or "").strip()
    if not spec_id or width <= 0 or font <= 0 or lines <= 0:
        raise ValueError("invalid_display_summary_spec")
    return {
        "id": spec_id,
        "max_width_px": width,
        "font_px": font,
        "max_lines": lines,
        "prefix": str(value.get("prefix") or ""),
    }


def _estimated_width(text: str, font_px: int) -> float:
    total = 0.0
    for char in text:
        if unicodedata.combining(char):
            continue
        if char.isspace():
            factor = 0.35
        elif unicodedata.east_asian_width(char) in {"W", "F", "A"}:
            factor = 1.0
        elif char in "ilI.,:;|!'`":
            factor = 0.36
        else:
            factor = 0.62
        total += max(1, font_px) * factor
    return total


def _wrapped_line_count(text: str, *, spec: DisplaySummarySpec, measure: MeasureText) -> int:
    combined = f"{spec['prefix']}{text}"
    if not combined:
        return 0
    width = spec["max_width_px"]
    font = spec["font_px"]
    lines = 1
    current = ""
    for char in combined:
        if char.isspace() and not current:
            continue
        candidate = current + char
        if current and measure(candidate, font) > width:
            lines += 1
            current = "" if char.isspace() else char
        else:
            current = candidate
    return lines


def build_display_summary(
    content: Any,
    *,
    display_spec: Mapping[str, Any],
    measure: MeasureText | None = None,
) -> DisplaySummary:
    """按具体像素预算生成派生摘要；超预算时不返回片段。"""

    spec = _normalized_spec(display_spec)
    source_hash = content_source_sha256(content)
    fallback = REVIEW_FALLBACK_TEXT
    if not isinstance(content, str):
        return {
            "text": "",
            "source_text": "",
            "source_sha256": source_hash,
            "rule_version": RULE_VERSION,
            "display_spec": spec,
            "status": SUMMARY_REVIEW_REQUIRED,
            "omission_reason": "unsupported_content_type",
            "fallback_text": fallback,
            "original_char_count": 0,
            "summary_char_count": 0,
            "line_count": 0,
            "removed_duplicate_sentences": 0,
            "removed_boilerplate_sentences": 0,
            "truncated": False,
        }

    cleaned = clean_display_content(content)
    width_fn = measure or _estimated_width
    line_count = _wrapped_line_count(cleaned.text, spec=spec, measure=width_fn) if cleaned.text else 0
    ready = bool(cleaned.text) and line_count <= spec["max_lines"]
    if ready:
        omission_reason = ""
    elif cleaned.text:
        omission_reason = "complete_content_exceeds_budget"
    else:
        omission_reason = "no_informative_content_after_cleanup"
    return {
        "text": cleaned.text if ready else "",
        "source_text": content,
        "source_sha256": source_hash,
        "rule_version": RULE_VERSION,
        "display_spec": spec,
        "status": SUMMARY_READY if ready else SUMMARY_REVIEW_REQUIRED,
        "omission_reason": omission_reason,
        "fallback_text": fallback,
        "original_char_count": len(content),
        "summary_char_count": len(cleaned.text),
        "line_count": line_count,
        "removed_duplicate_sentences": cleaned.removed_duplicate_sentences,
        "removed_boilerplate_sentences": cleaned.removed_boilerplate_sentences,
        "truncated": False,
    }


def _cache_key(content: Any, spec: DisplaySummarySpec) -> tuple[str, str, str]:
    spec_key = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return RULE_VERSION, content_source_sha256(content), spec_key


class DisplaySummaryCache:
    """后台准备线程拥有的有限 LRU；不跨 generation 修改 immutable 数据。"""

    def __init__(self, *, max_entries: int = 256) -> None:
        self.max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[tuple[str, str, str], DisplaySummary] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._entries)

    def build(
        self,
        content: Any,
        *,
        display_spec: Mapping[str, Any],
        measure: MeasureText | None = None,
    ) -> DisplaySummary:
        spec = _normalized_spec(display_spec)
        key = _cache_key(content, spec)
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            return deepcopy(cached)
        summary = build_display_summary(content, display_spec=spec, measure=measure)
        self._entries[key] = deepcopy(summary)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return summary


def _summary_is_current(summary: Any, content: Any, spec: DisplaySummarySpec) -> bool:
    if not isinstance(summary, Mapping):
        return False
    return (
        summary.get("rule_version") == RULE_VERSION
        and summary.get("source_sha256") == content_source_sha256(content)
        and summary.get("display_spec") == spec
        and summary.get("status") in {SUMMARY_READY, SUMMARY_REVIEW_REQUIRED}
        and summary.get("truncated") is False
    )


def ensure_display_summary(
    item: Mapping[str, Any],
    *,
    display_spec: Mapping[str, Any],
    measure: MeasureText | None = None,
    cache: DisplaySummaryCache | None = None,
) -> dict[str, Any]:
    """返回带派生摘要的新条目，既不修改输入，也不替换原始 ``content``。"""

    prepared = deepcopy(dict(item))
    spec = _normalized_spec(display_spec)
    content = item.get("content")
    existing = item.get("display_summary")
    if isinstance(existing, Mapping) and _summary_is_current(existing, content, spec):
        prepared["display_summary"] = deepcopy(dict(existing))
        return prepared
    summary = (
        cache.build(content, display_spec=spec, measure=measure)
        if cache is not None
        else build_display_summary(content, display_spec=spec, measure=measure)
    )
    prepared["display_summary"] = summary
    return prepared


__all__ = [
    "CleanedDisplayContent",
    "DisplaySummary",
    "DisplaySummaryCache",
    "DisplaySummarySpec",
    "REVIEW_FALLBACK_TEXT",
    "RULE_VERSION",
    "SUMMARY_READY",
    "SUMMARY_REVIEW_REQUIRED",
    "build_display_summary",
    "clean_display_content",
    "content_source_sha256",
    "ensure_display_summary",
]
