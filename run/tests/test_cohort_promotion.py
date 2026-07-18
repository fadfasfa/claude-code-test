from __future__ import annotations

import json
from pathlib import Path

import pytest

from hextech.infrastructure.persistence.cohort import CohortPromotionError, CohortPromotionStore
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
    store.close()

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
    store.record_generation_promoted("new-current")

    # 模拟 generation pointer 已切换后进程异常导致依赖 pointer 被外部旧值覆盖。
    atomic_write_json(store.pointer_path("catalog"), _pointer("damaged"))
    store.close()
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
    store.record_generation_promoted("new-current")
    during_generation = store.consistent_pointers()
    assert during_generation["generation"]["current"]["current_generation_id"] == "old-current"

    store.commit()
    committed = store.consistent_pointers()
    assert all(committed[role] == _pointer(f"new-{role}") for role in DEPENDENCIES)
    assert committed["generation"]["current"]["current_generation_id"] == "new-current"


def test_second_promoter_cannot_begin_while_transaction_is_active(tmp_path: Path) -> None:
    first = _write_initial_pointers(tmp_path)
    second = CohortPromotionStore(tmp_path)
    first.begin()
    try:
        with pytest.raises(CohortPromotionError, match="另一个进程"):
            second.begin()
    finally:
        first.rollback()


def test_generation_id_mismatch_keeps_journal_recoverable(tmp_path: Path) -> None:
    store = _write_initial_pointers(tmp_path)
    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    store.promote_dependencies()
    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "unexpected"})
    atomic_write_json(tmp_path / "snapshots" / "previous.v2.json", {"generation_id": "old-current"})

    with pytest.raises(CohortPromotionError, match="expected=new-current actual=unexpected"):
        store.record_generation_promoted("new-current")

    assert store.journal_path.is_file()
    assert store.load().phase.value == "dependencies_promoted"
    store.rollback()
    assert _read(store.pointer_path("generation"))["current_generation_id"] == "old-current"
