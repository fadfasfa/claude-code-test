from __future__ import annotations

import json
from pathlib import Path

import pytest

from hextech.infrastructure.persistence.cohort import CohortPromotionStore
from hextech.modules.data.ports.atomic import atomic_write_json


DEPENDENCIES = ("catalog", "hextech", "apex", "mayhem")


def _pointer(label: str) -> dict[str, str]:
    return {"label": label}


def _write_initial_pointers(root: Path) -> CohortPromotionStore:
    store = CohortPromotionStore(root)
    for role in DEPENDENCIES:
        atomic_write_json(store.pointer_path(role), _pointer(f"old-{role}"))
    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "old-current"})
    atomic_write_json(root / "snapshots" / "previous.v2.json", {"generation_id": "old-previous"})
    return store


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("promote_dependencies", (False, True))
def test_promotion_recovery_rolls_back_before_generation_switch(
    tmp_path: Path,
    promote_dependencies: bool,
) -> None:
    store = _write_initial_pointers(tmp_path)
    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    if promote_dependencies:
        store.promote_dependencies()

    recovered = CohortPromotionStore(tmp_path).recover()

    assert recovered == "rolled_back"
    for role in DEPENDENCIES:
        assert _read(store.pointer_path(role)) == _pointer(f"old-{role}")
    assert _read(store.pointer_path("generation"))["current_generation_id"] == "old-current"
    assert _read(tmp_path / "snapshots" / "previous.v2.json")["generation_id"] == "old-previous"
    assert not store.journal_path.exists()


def test_promotion_recovery_rolls_forward_after_generation_switch(tmp_path: Path) -> None:
    store = _write_initial_pointers(tmp_path)
    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    store.promote_dependencies()
    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "new-current"})
    atomic_write_json(tmp_path / "snapshots" / "previous.v2.json", {"generation_id": "old-current"})
    store.record_generation_promoted()

    # 模拟 generation pointer 已切换后进程异常导致依赖 pointer 被外部旧值覆盖。
    atomic_write_json(store.pointer_path("catalog"), _pointer("damaged"))
    recovered = CohortPromotionStore(tmp_path).recover()

    assert recovered == "rolled_forward"
    for role in DEPENDENCIES:
        assert _read(store.pointer_path(role)) == _pointer(f"new-{role}")
    assert _read(store.pointer_path("generation"))["current_generation_id"] == "new-current"
    assert _read(tmp_path / "snapshots" / "previous.v2.json")["generation_id"] == "old-current"
    assert not store.journal_path.exists()


def test_consistent_reader_hides_uncommitted_dependency_pointers(tmp_path: Path) -> None:
    store = _write_initial_pointers(tmp_path)
    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    store.promote_dependencies()

    during_dependencies = store.consistent_pointers()
    assert all(during_dependencies[role] == _pointer(f"old-{role}") for role in DEPENDENCIES)

    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "new-current"})
    atomic_write_json(tmp_path / "snapshots" / "previous.v2.json", {"generation_id": "old-current"})
    store.record_generation_promoted()
    during_generation = store.consistent_pointers()
    assert during_generation["generation"]["current"]["current_generation_id"] == "old-current"

    store.commit()
    committed = store.consistent_pointers()
    assert all(committed[role] == _pointer(f"new-{role}") for role in DEPENDENCIES)
    assert committed["generation"]["current"]["current_generation_id"] == "new-current"
