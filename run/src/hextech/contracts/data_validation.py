"""数据契约共用的纯字段验证；不依赖具体 DTO 或运行态。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DataContractError(ValueError):
    """版本化 DTO 结构或字段违反稳定契约。"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_sha256(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise DataContractError(f"{field_name} 必须是 64 位 SHA-256")
    return normalized


def require_identifier(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _ID_RE.fullmatch(normalized):
        raise DataContractError(f"{field_name} 格式无效：{value}")
    return normalized


def require_relative_path(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise DataContractError(f"{field_name} 必须是受控相对路径：{value}")
    return normalized


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataContractError(f"{field_name} 必须是非负整数")
    return value
