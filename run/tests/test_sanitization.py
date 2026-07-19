"""跨进程诊断消息的敏感信息脱敏回归。"""

from __future__ import annotations

import pytest

from hextech.infrastructure.observability.sanitization import sanitize_event_message


@pytest.mark.parametrize(
    "raw, secrets",
    (
        ("GET http://127.0.0.1:1234/path?token=query-secret", ("query-secret",)),
        ("Authorization: Bearer bearer-secret", ("bearer-secret",)),
        ("Set-Cookie: sid=cookie-secret; HttpOnly", ("cookie-secret",)),
        (
            "cookie=one token=two nonce=three api_key=four authorization=five",
            ("one", "two", "three", "four", "five"),
        ),
    ),
)
def test_sanitize_event_message_removes_sensitive_material(raw: str, secrets: tuple[str, ...]) -> None:
    sanitized = sanitize_event_message(raw)

    assert all(secret not in sanitized for secret in secrets)
    assert "<redacted>" in sanitized
    assert sanitize_event_message(sanitized) == sanitized


def test_sanitize_event_message_preserves_non_sensitive_context() -> None:
    raw = "GET http://127.0.0.1:1234/path?token=secret failed at supervisor.start"

    sanitized = sanitize_event_message(raw)

    assert "http://127.0.0.1:1234/path" in sanitized
    assert "failed at supervisor.start" in sanitized
    assert "secret" not in sanitized
