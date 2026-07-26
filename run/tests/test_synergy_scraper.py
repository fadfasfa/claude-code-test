"""Apex 与 Mayhem 新联动管线回归测试。"""

from __future__ import annotations

from hextech.contracts import FailureKind
from hextech.modules.acquisition.apex.parser import ApexPageState, classify_apex_page


def test_apex_empty_requires_identity_and_explicit_marker() -> None:
    invalid = classify_apex_page("<html></html>", expected_slug="vi", entry_count=0, status_code=200)
    assert invalid.failure_kind in {FailureKind.INVALID_PAYLOAD, FailureKind.SCHEMA_CHANGED}
    page = '<html><a href="/champions/vi">Vi</a><div>No synergies found</div></html>'
    confirmed = classify_apex_page(page, expected_slug="vi", entry_count=0, status_code=200)
    assert confirmed.state is ApexPageState.CONFIRMED_EMPTY


def test_apex_parser_exception_cannot_be_confirmed_empty() -> None:
    from hextech.infrastructure.sources.apex.common import ChampionInfo, FetchedResource
    from hextech.infrastructure.sources.apex.service import _extract_and_classify_champion

    class BrokenExtractor:
        def extract(self, _resources):
            raise ValueError("fixture schema changed")

    page = '<html><a href="/champions/vi">Vi</a><div>No synergies found</div></html>'
    champion = ChampionInfo(id="254", name="皮城执法官", title="", en_name="vi", slug="vi")
    resource = FetchedResource(url="https://apexlol.info/zh/champions/vi", text=page, source="fixture")

    entries, synergy_map, outcome = _extract_and_classify_champion(
        BrokenExtractor(),  # type: ignore[arg-type]
        champion,
        resource,
        expected_slug="vi",
    )

    assert entries == []
    assert synergy_map == {}
    assert outcome.state is ApexPageState.FAILED
    assert outcome.failure_kind is FailureKind.SCHEMA_CHANGED
    assert outcome.evidence == "parser_exception"


def test_apex_real_extractor_allows_verified_locke_empty_page() -> None:
    from hextech.infrastructure.sources.apex.common import ChampionInfo, FetchedResource
    from hextech.infrastructure.sources.apex.extractor import SynergyExtractor
    from hextech.infrastructure.sources.apex.service import _extract_and_classify_champion

    champion = ChampionInfo(id="805", name="灰烬驱魔人", title="洛克", en_name="Locke", slug="locke")
    page = (
        '<html><link href="/zh/champions/Locke"><h1>灰烬驱魔人</h1>'
        "<p>0 条联动</p><p>该英雄暂时还没有联动卡片，后续可以继续补录。</p></html>"
    )
    resource = FetchedResource(
        url="https://apexlol.info/zh/champions/Locke",
        text=page,
        source="fixture",
        status_code=200,
    )
    extractor = SynergyExtractor(champion_lookup={"locke": champion}, augment_name_map={})

    entries, synergy_map, outcome = _extract_and_classify_champion(
        extractor,
        champion,
        resource,
        expected_slug="Locke",
    )

    assert entries == []
    assert synergy_map == {}
    assert outcome.state is ApexPageState.CONFIRMED_EMPTY
    assert outcome.failure_kind is FailureKind.CONFIRMED_EMPTY
    assert outcome.evidence == "explicit_empty_state"


def test_apex_complete_gate_accepts_172_successes_and_verified_locke_empty() -> None:
    from hextech.contracts import ItemOutcome
    from hextech.modules.acquisition.apex.validation import validate_apex_run

    successful_ids = [str(index) for index in range(1, 173)]
    payload = {
        champion_id: {"synergy_items": [{"augment_names": [f"海克斯{champion_id}"]}]}
        for champion_id in successful_ids
    }
    payload["805"] = {"synergy_items": []}
    outcomes = [
        ItemOutcome(item_id=champion_id, state="success", stage="detail", record_count=1)
        for champion_id in successful_ids
    ]
    outcomes.append(
        ItemOutcome(
            item_id="805",
            state="confirmed_empty",
            stage="detail",
            failure_kind=FailureKind.CONFIRMED_EMPTY,
            details={"page_identity_verified": True, "evidence": "explicit_empty_state"},
        )
    )

    result = validate_apex_run(payload, outcomes, expected_champion_ids=[*successful_ids, "805"])

    assert result["expected_champions"] == 173
    assert result["successful_champions"] == 172
    assert result["confirmed_empty_champions"] == 1
    assert result["record_count"] == 172


def test_runtime_has_no_browser_cloaking_requirement() -> None:
    from hextech.modules.session.python_environment import REQUIRED_RUNTIME_PACKAGES

    assert "cloak" + "browser" not in REQUIRED_RUNTIME_PACKAGES


def _noise_extractor():
    from hextech.infrastructure.sources.apex.common import ChampionInfo
    from hextech.infrastructure.sources.apex.extractor import SynergyExtractor

    champion = ChampionInfo(id="254", name="皮城执法官", title="", en_name="Vi", slug="vi")
    return SynergyExtractor(
        champion_lookup={"vi": champion},
        augment_name_map={"回归基本功": "回归基本功"},
    )


def test_visible_extraction_filters_ui_noise_and_unanchored_groups() -> None:
    """回归：真机 unresolved 样本里的"筛选/排序"UI 词与装备名污染必须被过滤。

    四组卡片分别验证：词表可解析组正常产出且噪声词不入名；无词表命中且无
    tier 佐证的组整体丢弃；句子形状的行不入名；tier 结构确认的新海克斯保留。
    """

    extractor = _noise_extractor()
    # 每组结构对齐真实页面：名称/tier → 评分行 → 作者行 → 内容 → "评论"停止行。
    lines = [
        "筛选",
        "排序",
        "按热度",
        "默认排序",
        "回归基本功",
        "黄金",
        "S 强力联动",
        "作者：小明",
        "这套联动非常强，优先拿。",
        "评论",
        "瑞莱的冰晶节杖",
        "A 强力联动",
        "作者：小红",
        "一段无关说明。",
        "评论",
        "任务：艾卡西亚的陷落",
        "B 强力联动",
        "作者：小刚",
        "另一段说明。",
        "评论",
        "新海克斯强化名",
        "棱彩",
        "B 娱乐",
        "作者：小李",
        "这是新强化的说明。",
    ]
    html = "<html><body>" + "".join(f"<div>{line}</div>" for line in lines) + "</body></html>"

    entries = extractor._extract_from_visible_html_text(html, "https://apexlol.info/zh/champions/vi")

    names = {tuple(entry.augment_names) for entry in entries}
    assert ("回归基本功",) in names
    assert ("新海克斯强化名",) in names
    flattened = [name for entry in entries for name in entry.augment_names]
    assert "瑞莱的冰晶节杖" not in flattened
    assert "任务：艾卡西亚的陷落" not in flattened
    assert all(name not in {"筛选", "排序", "按热度", "默认排序"} for name in flattened)
    assert extractor.noise_filtered_count >= 1
    assert any("瑞莱的冰晶节杖" in sample["sample"] for sample in extractor.noise_filter_samples)


def test_json_augment_names_reject_sentences_and_ui_words() -> None:
    """回归：JSON 路径的兜底名同样要过滤句子与 UI 控件词。"""

    extractor = _noise_extractor()

    names = extractor._resolve_augment_names(
        {
            "augmentNames": [
                "回归基本功",
                "这是一整段用户评论，说了很多话，明显超过海克斯名的长度。",
                "筛选",
            ]
        }
    )

    assert names == ["回归基本功"]
    assert extractor.noise_filtered_count == 2
