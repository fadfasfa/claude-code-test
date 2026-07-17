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


def test_runtime_has_no_browser_cloaking_requirement() -> None:
    from hextech.modules.session.python_environment import REQUIRED_RUNTIME_PACKAGES

    assert "cloak" + "browser" not in REQUIRED_RUNTIME_PACKAGES
