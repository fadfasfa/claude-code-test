from __future__ import annotations

import random

import pytest

from hextech.contracts import FailureKind
from hextech.modules.acquisition.common.policy import HostCircuitBreaker, HostCircuitOpen, RetryPolicy
from hextech.modules.acquisition.apex.parser import ApexPageState, classify_apex_page
from hextech.infrastructure.sources.apex.source import ApexSlugMapError, build_champion_slug_map
from hextech.infrastructure.sources.hextech.source import ChampionCatalogMismatch, build_expected_champions


def test_retry_policy_uses_bounded_exponential_delay_with_seeded_jitter() -> None:
    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=3.0, jitter_ratio=0.1)
    rng = random.Random(7)
    delays = [policy.delay_seconds(attempt, rng=rng) for attempt in (1, 2, 3, 4)]
    assert 0.9 <= delays[0] <= 1.1
    assert 1.8 <= delays[1] <= 2.2
    assert all(2.7 <= value <= 3.3 for value in delays[2:])


def test_host_circuit_opens_only_for_403_and_429() -> None:
    now = [100.0]
    circuit = HostCircuitBreaker(cooldown_seconds=30, clock=lambda: now[0])
    url = "https://example.test/data"
    circuit.record(url, FailureKind.HTTP_403)
    with pytest.raises(HostCircuitOpen) as captured:
        circuit.check(url)
    assert captured.value.failure_kind is FailureKind.HTTP_403
    now[0] = 131.0
    circuit.check(url)


def test_apex_confirmed_empty_requires_page_identity_and_explicit_marker() -> None:
    valid = '<link href="/zh/champions/vi"><p>暂无联动</p>'
    outcome = classify_apex_page(valid, expected_slug="Vi", entry_count=0, status_code=200)
    assert outcome.state is ApexPageState.CONFIRMED_EMPTY

    unknown = classify_apex_page("<html></html>", expected_slug="Vi", entry_count=0, status_code=200)
    assert unknown.state is ApexPageState.FAILED
    assert unknown.failure_kind is FailureKind.SCHEMA_CHANGED


def test_apex_normal_page_may_reference_cloudflare_resources() -> None:
    page = (
        '<html><link href="/zh/champions/vi">'
        '<script src="https://static.cloudflareinsights.com/beacon.min.js"></script>'
        '<div data-synergy="1">valid content</div></html>'
    )
    outcome = classify_apex_page(page, expected_slug="Vi", entry_count=1, status_code=200)
    assert outcome.state is ApexPageState.HAS_SYNERGY


def test_apex_current_zero_count_copy_is_explicit_empty_evidence() -> None:
    page = '<html><link href="/zh/champions/locke"><p>0 条联动</p><p>该英雄暂时还没有联动卡片</p></html>'
    outcome = classify_apex_page(page, expected_slug="Locke", entry_count=0, status_code=200)
    assert outcome.state is ApexPageState.CONFIRMED_EMPTY


@pytest.mark.parametrize(
    "page",
    [
        '<html><script>window._cf_chl_opt={}</script></html>',
        '<html><title>Attention Required</title><p>Cloudflare</p></html>',
        '<html><title>Just a moment...</title></html>',
        '<html><body>Access denied</body></html>',
    ],
)
def test_apex_challenge_pages_remain_blocked(page: str) -> None:
    outcome = classify_apex_page(page, expected_slug="Vi", entry_count=1, status_code=200)
    assert outcome.state is ApexPageState.FAILED
    assert outcome.failure_kind is FailureKind.HTTP_403


def test_apex_slug_map_rejects_missing_or_duplicate_slug() -> None:
    with pytest.raises(ApexSlugMapError):
        build_champion_slug_map({"1": {"en_name": "Vi"}, "2": {"en_name": "vi"}})
    with pytest.raises(ApexSlugMapError):
        build_champion_slug_map({"1": {"name": "缺少英文 slug"}})


def test_hextech_expected_set_is_catalog_driven() -> None:
    catalog = {"1": {"name": "A"}, "2": {"name": "B"}}
    assert [item["championId"] for item in build_expected_champions(catalog, [{"championId": "2"}, {"championId": "1"}])] == ["1", "2"]
    with pytest.raises(ChampionCatalogMismatch):
        build_expected_champions(catalog, [{"championId": "1"}])
