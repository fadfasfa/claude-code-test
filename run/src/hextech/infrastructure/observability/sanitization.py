"""跨进程事件写入前的有限敏感信息脱敏。"""

from __future__ import annotations

import re


def sanitize_event_message(value: object) -> str:
    """剥离 URL query、凭据、Cookie、token 与 nonce。"""

    text = str(value or "")
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"Authorization:\s*Bearer\s+[^\s,;]+", "Authorization: <redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"Set-Cookie:\s*[^,\n;]+(?:;[^\n,]*)?", "Set-Cookie: <redacted>", text, flags=re.IGNORECASE)
    return re.sub(
        r"\b(cookie|token|nonce|api[_-]?key|authorization)=([^,\s;]+)",
        r"\1=<redacted>",
        text,
        flags=re.IGNORECASE,
    )


__all__ = ["sanitize_event_message"]
