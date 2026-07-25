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
