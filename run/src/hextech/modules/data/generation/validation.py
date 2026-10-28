"""Small current-release validation primitives; v3 migration checks live offline."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Mapping

class SnapshotValidationError(ValueError):
    """generation 结构、文件、来源或哈希不满足完整代约束。"""

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def safe_generation_file(directory: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        raise SnapshotValidationError(f"generation 文件路径无效：{relative_path}")
    root = directory.resolve()
    candidate = (directory / relative_path).resolve()
    if root not in candidate.parents:
        raise SnapshotValidationError(f"generation 文件路径越界：{relative_path}")
    return candidate

def champion_detail_complete(detail: object) -> bool:
    """检查实际详情；声明 complete 不能把缺失统计变成完整英雄。"""
    if not isinstance(detail, Mapping) or detail.get("data_status") == "pending":
        return False
    augments = detail.get("augments")
    if not isinstance(augments, list) or not augments:
        return False
    if "stages" in detail:
        stages = detail["stages"]
        if not isinstance(stages, Mapping) or set(stages) != {"1", "2", "3", "4"}:
            return False
        if any(not isinstance(rows, list) or not rows for rows in stages.values()):
            return False
    return True
