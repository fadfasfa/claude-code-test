import json

import pytest

import hextech.infrastructure.persistence.cohort_validation_receipt as receipts
from hextech.infrastructure.persistence.cohort_recovery import validate_generation_cohort
from hextech.modules.data.ports.atomic import atomic_write_json
from test_cohort_v3 import _v3_runtime


@pytest.fixture
def certified(tmp_path):
    runtime, generation_id, child = _v3_runtime(tmp_path, details=True)
    candidate = validate_generation_cohort(runtime, generation_id)
    assert receipts.write_validation_receipt(runtime, candidate)
    return runtime, candidate, child


def test_v3_receipt_fast_load_without_full_validation(certified, monkeypatch):
    runtime, candidate, _ = certified
    monkeypatch.setattr(receipts, "validate_generation_cohort", lambda *args: pytest.fail("fast load did full validation"))
    assert receipts.load_valid_validation_receipt(runtime) == candidate


def test_v3_receipt_missing_optional_is_valid(tmp_path):
    runtime, generation_id, _ = _v3_runtime(tmp_path, details=False)
    candidate = validate_generation_cohort(runtime, generation_id)
    assert set(candidate.pointers) == {"catalog", "aramkit"}
    assert receipts.write_validation_receipt(runtime, candidate)
    assert receipts.load_valid_validation_receipt(runtime) == candidate


def test_v3_receipt_child_drift_invalidates_and_cannot_be_recertified(certified):
    runtime, candidate, child = certified
    assert child is not None
    child.write_text("corrupted", encoding="utf-8")
    assert receipts.load_valid_validation_receipt(runtime) is None
    assert not receipts.write_validation_receipt(runtime, candidate)


@pytest.mark.parametrize("relative", ["sources/aramkit/current.v2.json", "snapshots/previous.v2.json",
                                     "state/data-service/refresh_schedule.v1.json",
                                     "state/data-service/cohort_recovery_point.v1.json"])
def test_v3_receipt_header_drift_or_appearance_invalidates(certified, relative):
    runtime, _, _ = certified
    atomic_write_json(runtime / relative, {"changed": True})
    assert receipts.load_valid_validation_receipt(runtime) is None


def test_v3_receipt_build_drift_invalidates(certified, monkeypatch):
    runtime, _, _ = certified
    monkeypatch.setattr(receipts, "get_build_identity", lambda: {"build_id": "other", "source_fingerprint": "changed"})
    assert receipts.load_valid_validation_receipt(runtime) is None


def test_v3_receipt_new_generation_inventory_invalidates(certified):
    runtime, _, _ = certified
    atomic_write_json(runtime / "snapshots/generations/new-untracked/manifest.json", {"created_at": "later"})
    assert receipts.load_valid_validation_receipt(runtime) is None


def test_v3_receipt_cannot_omit_unit_from_certificate(certified):
    runtime, _, _ = certified
    path = receipts.receipt_path(runtime)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["units"].pop("aramkit/hero-v3-a")
    atomic_write_json(path, payload)
    assert receipts.load_valid_validation_receipt(runtime) is None
