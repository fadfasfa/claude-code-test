import json

import pytest

import hextech.infrastructure.persistence.cohort_validation_receipt as receipts
from hextech.infrastructure.persistence.cohort_recovery import validate_generation_cohort
from hextech.modules.data.ports.atomic import atomic_write_json
from test_cohort_v3 import _publish_independent_recognition_catalog, _v3_runtime


@pytest.fixture
def certified(tmp_path):
    runtime, generation_id, child = _v3_runtime(tmp_path, details=True)
    candidate = validate_generation_cohort(runtime, generation_id)
    assert receipts.write_validation_receipt(runtime, candidate)
    return runtime, candidate, child


def test_v3_receipt_fast_load_without_full_validation(certified, monkeypatch):
    runtime, candidate, _ = certified
    monkeypatch.setattr(receipts, "validate_generation_cohort", lambda *args: pytest.fail("fast load did full validation"))
    monkeypatch.setattr(
        receipts,
        "validated_catalog_manifest",
        lambda *args: pytest.fail("fast load rehashed recognition Catalog"),
    )
    assert receipts.load_valid_validation_receipt(runtime) == candidate


def test_v3_receipt_missing_optional_is_valid(tmp_path):
    runtime, generation_id, _ = _v3_runtime(tmp_path, details=False)
    candidate = validate_generation_cohort(runtime, generation_id)
    assert set(candidate.pointers) == {"catalog", "aramkit"}
    assert receipts.write_validation_receipt(runtime, candidate)
    assert receipts.load_valid_validation_receipt(runtime) == candidate


def test_v3_receipt_certifies_independent_live_recognition_catalog(tmp_path):
    runtime, generation_id, _ = _v3_runtime(tmp_path, details=True)
    candidate = validate_generation_cohort(runtime, generation_id)
    statistics_catalog_id = str(
        candidate.pointers["catalog"]["catalog_generation_id"]
    )
    recognition = _publish_independent_recognition_catalog(runtime)

    assert receipts.write_validation_receipt(runtime, candidate)
    assert receipts.load_valid_validation_receipt(runtime) == candidate

    payload = json.loads(receipts.receipt_path(runtime).read_text(encoding="utf-8"))
    assert payload["pointers"]["catalog"]["catalog_generation_id"] == statistics_catalog_id
    assert (
        payload["recognition_catalog_pointer"]["catalog_generation_id"]
        == recognition["catalog_generation_id"]
    )


def test_receipt_never_certifies_file_drift_after_final_comparison(tmp_path, monkeypatch):
    runtime, generation_id, child = _v3_runtime(tmp_path, details=True)
    candidate = validate_generation_cohort(runtime, generation_id)
    assert child is not None
    inventory = receipts._inventory
    calls = 0

    def drift_after_comparison(root):
        nonlocal calls
        result = inventory(root)
        calls += 1
        if calls == 2:
            child.write_bytes(child.read_bytes() + b"drift-after-validation")
        return result

    monkeypatch.setattr(receipts, "_inventory", drift_after_comparison)
    assert receipts.write_validation_receipt(runtime, candidate)
    # A receipt may be written, but it must contain the validated snapshot,
    # never freshly reread metadata that would certify the unvalidated drift.
    assert receipts.load_valid_validation_receipt(runtime) is None


@pytest.mark.parametrize("drift", ["pointer", "manifest", "file"])
def test_v3_receipt_invalidates_live_recognition_catalog_drift(tmp_path, drift):
    runtime, generation_id, _ = _v3_runtime(tmp_path, details=True)
    candidate = validate_generation_cohort(runtime, generation_id)
    recognition = _publish_independent_recognition_catalog(runtime)
    assert receipts.write_validation_receipt(runtime, candidate)
    recognition_root = (
        runtime
        / "catalog/generations"
        / str(recognition["catalog_generation_id"])
    )

    if drift == "pointer":
        pointer_path = runtime / "catalog/current.v2.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["content_sha256"] = "0" * 64
        atomic_write_json(pointer_path, pointer)
    elif drift == "manifest":
        manifest_path = recognition_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = "2026-09-16T00:00:01+00:00"
        atomic_write_json(manifest_path, manifest)
    else:
        data_file = next(
            path
            for path in recognition_root.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        )
        data_file.write_bytes(data_file.read_bytes() + b"drift")

    assert receipts.load_valid_validation_receipt(runtime) is None


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
