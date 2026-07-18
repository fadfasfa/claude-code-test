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


def test_runtime_has_no_browser_cloaking_requirement() -> None:
    from hextech.modules.session.python_environment import REQUIRED_RUNTIME_PACKAGES

    assert "cloak" + "browser" not in REQUIRED_RUNTIME_PACKAGES
