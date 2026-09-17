from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import pytest

from hextech.infrastructure.persistence.raw_responses import (
    RawResponseCache, RawResponseBudgetExceeded, RawResponseConflict,
    RawResponseIntegrityError,
)


def cache(tmp_path, **kwargs):
    return RawResponseCache(tmp_path / "raw", source="test", revision="../../escape", **kwargs)


def test_roundtrip_dedupe_conflict(tmp_path):
    store = cache(tmp_path)
    assert store.get("url") is None
    store.put("url", b"abc")
    before = {p.name: p.stat().st_mtime_ns for p in store.directory.iterdir() if p.name != ".lock"}
    store.put("url", b"abc")
    assert before == {p.name: p.stat().st_mtime_ns for p in store.directory.iterdir() if p.name != ".lock"}
    assert store.get("url") == b"abc"
    assert store.status()["count"] == 1
    assert store.status()["bytes"] == 3
    assert store.directory.is_relative_to(tmp_path / "raw")
    with pytest.raises(RawResponseConflict):
        store.put("url", b"different")


def test_budget_no_payload_and_revision_isolation(tmp_path):
    store = cache(tmp_path, max_bytes=3)
    with pytest.raises(RawResponseBudgetExceeded):
        store.put("large", b"abcd")
    assert not list(store.directory.glob("*.body"))
    store.put("one", b"abc")
    with pytest.raises(RawResponseBudgetExceeded):
        store.put("two", b"x")
    other = RawResponseCache(store.root, source="test", revision="different", max_bytes=3)
    other.put("one", b"xyz")
    assert other.get("one") == b"xyz"


def test_thread_concurrency(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: cache(tmp_path).put(str(i % 5), b"body"), range(40)))
    assert cache(tmp_path).status()["count"] == 5


def test_concurrent_budget_is_atomic(tmp_path):
    def attempt(i):
        try:
            cache(tmp_path, max_bytes=6).put(str(i), b"abc")
            return True
        except RawResponseBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(10))) == 2
    assert cache(tmp_path).status()["bytes"] == 6


def test_manifest_cannot_reference_outside(tmp_path):
    store = cache(tmp_path)
    store.get("url")
    (store.directory / "manifest.json").write_text(
        '{"version":1,"entries":{"../../outside":{"size":3,"sha256":"' + '0' * 64 + '"}}}', encoding="utf-8")
    with pytest.raises(RawResponseIntegrityError):
        store.put("url", b"abc")


def test_process_concurrency(tmp_path):
    code = "from pathlib import Path; from hextech.infrastructure.persistence.raw_responses import RawResponseCache; import sys; c=RawResponseCache(Path(sys.argv[1]),source='test',revision='../../escape'); [c.put(str(i),b'body') for i in range(5)]"
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "raw")]) for _ in range(4)]
    assert [p.wait(timeout=30) for p in processes] == [0] * 4
    assert cache(tmp_path).status()["count"] == 5


@pytest.mark.parametrize("damage", ["hash", "missing_body", "bad_manifest"])
def test_damage_fails_closed(tmp_path, damage):
    store = cache(tmp_path)
    store.put("url", b"abc")
    body = next(store.directory.glob("*.body"))
    if damage == "hash":
        body.write_bytes(b"xyz")
    elif damage == "missing_body":
        body.unlink()
    else:
        (store.directory / "manifest.json").write_bytes(b"{")
    with pytest.raises(RawResponseIntegrityError):
        store.get("url")
    with pytest.raises(RawResponseIntegrityError):
        store.put("url", b"abc")


def test_crash_before_manifest_is_not_a_hit(tmp_path, monkeypatch):
    store = cache(tmp_path)
    original = store._atomic_write
    def fail_manifest(path, body):
        if path.name == "manifest.json":
            raise OSError("simulated crash")
        original(path, body)
    monkeypatch.setattr(store, "_atomic_write", fail_manifest)
    with pytest.raises(OSError):
        store.put("url", b"complete body")
    assert cache(tmp_path).get("url") is None
    orphan = next(store.directory.glob("*.body"))
    before = orphan.read_bytes()
    cache(tmp_path).put("url", b"complete body")
    assert cache(tmp_path).get("url") == b"complete body"
    assert orphan.read_bytes() == before
    assert cache(tmp_path).status()["bytes"] == 2 * len(before)


@pytest.mark.parametrize("orphan_kind", ["body", "pending"])
def test_orphans_preserve_committed_reads_and_count_toward_budget(tmp_path, orphan_kind):
    store = cache(tmp_path, max_bytes=9)
    store.put("A", b"abc")
    orphan_name = (hashlib.sha256(b"B").hexdigest() + ".body"
                   if orphan_kind == "body" else ".interrupted.pending")
    orphan = store.directory / orphan_name
    orphan.write_bytes(b"12345")
    assert store.get("A") == b"abc"
    assert store.get("B") is None
    store.put("A", b"abc")
    assert store.status()["orphan_count"] == 1
    assert store.status()["orphan_bytes"] == 5
    assert store.status()["bytes"] == 8
    with pytest.raises(RawResponseBudgetExceeded):
        store.put("C", b"xx")
    store.put("C", b"x")
    assert store.get("A") == b"abc"
    assert store.get("C") == b"x"
    if orphan_kind == "body":
        with pytest.raises(RawResponseBudgetExceeded):
            store.put("B", b"12345")
    assert orphan.read_bytes() == b"12345"


def test_orphan_retry_survives_repeated_crashes_without_overwrites(tmp_path, monkeypatch):
    store = cache(tmp_path, max_bytes=15)
    store.put("A", b"aaa")
    original = store._atomic_write
    def crash(path, body):
        if path.name == "manifest.json":
            raise OSError("crash before commit")
        original(path, body)
    monkeypatch.setattr(store, "_atomic_write", crash)
    for _ in range(2):
        with pytest.raises(OSError):
            store.put("B", b"bbb")
        assert store.get("B") is None
        assert store.get("A") == b"aaa"
    evidence = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.directory.glob("*.body")}
    monkeypatch.setattr(store, "_atomic_write", original)
    store.put("B", b"bbb")
    assert store.get("B") == b"bbb"
    assert store.get("A") == b"aaa"
    assert store.status()["orphan_bytes"] == 6
    assert store.status()["bytes"] == 12
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == before for path, before in evidence.items())
    store.put("B", b"bbb")
    assert store.status()["bytes"] == 12
    with pytest.raises(RawResponseConflict):
        store.put("B", b"different")
    with pytest.raises(RawResponseBudgetExceeded):
        store.put("C", b"four")


@pytest.mark.parametrize("filename", ["../escape.body", "0" * 64 + ".body", "unrelated.body"])
def test_manifest_filename_is_strictly_derived(tmp_path, filename):
    store = cache(tmp_path)
    store.put("A", b"aaa")
    manifest_path = store.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][hashlib.sha256(b"A").hexdigest()]["filename"] = filename
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RawResponseIntegrityError, match="derived body filename"):
        store.get("A")


def test_source_budget_covers_revisions_orphans_but_not_other_sources(tmp_path, monkeypatch):
    first = RawResponseCache(tmp_path, source="one", revision="r1", max_source_bytes=8)
    second = RawResponseCache(tmp_path, source="one", revision="r2", max_source_bytes=8)
    first.put("A", b"aaa")
    (first.directory / ".interrupted.pending").write_bytes(b"xx")
    orphan = first.directory / (hashlib.sha256(b"orphan").hexdigest() + ".body")
    orphan.write_bytes(b"x")
    second.put("B", b"bb")
    assert second.status()["source_bytes"] == 8
    assert second.status()["max_source_bytes"] == 8
    with pytest.raises(RawResponseBudgetExceeded, match="source budget"):
        second.put("C", b"c")
    assert second.get("C") is None
    assert orphan.read_bytes() == b"x"
    RawResponseCache(tmp_path, source="other", revision="r1", max_source_bytes=8).put("C", b"other")
    (tmp_path / "unrelated.txt").write_bytes(b"not counted")
    assert first.status()["source_bytes"] == 8
    monkeypatch.setattr(first, "_source_usage", lambda: pytest.fail("get scanned other revisions"))
    assert first.get("A") == b"aaa"


def test_source_budget_cross_process_race_across_two_revisions(tmp_path):
    code = """from pathlib import Path
import sys
from hextech.infrastructure.persistence.raw_responses import RawResponseCache, RawResponseBudgetExceeded
cache = RawResponseCache(Path(sys.argv[1]), source='race', revision=sys.argv[2], max_source_bytes=6)
print('ready', flush=True)
sys.stdin.readline()
try:
    cache.put(sys.argv[3], b'four')
except RawResponseBudgetExceeded:
    sys.exit(7)
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i % 2), str(i)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) for i in range(4)]
    for process in processes:
        assert process.stdout.readline().strip() == "ready"
    for process in processes:
        process.stdin.write("go\n")
        process.stdin.flush()
        process.stdin.close()
    assert sorted(p.wait(timeout=30) for p in processes) == [0, 7, 7, 7]
    assert RawResponseCache(tmp_path, source="race", revision="0", max_source_bytes=6).status()["source_bytes"] == 4


def test_revision_inventory_has_bounded_scan(tmp_path, monkeypatch):
    import hextech.infrastructure.persistence.raw_responses as module
    first = RawResponseCache(tmp_path, source="source", revision="1")
    first.put("A", b"a")
    second = RawResponseCache(tmp_path, source="source", revision="2")
    second.get("empty")
    monkeypatch.setattr(module, "MAX_SOURCE_REVISIONS", 1)
    with pytest.raises(RawResponseBudgetExceeded, match="revision inventory"):
        second.put("B", b"b")
    assert first.get("A") == b"a"


def test_other_revision_reparse_refused_before_new_body_write(tmp_path):
    store = RawResponseCache(tmp_path, source="source", revision="safe")
    store.put("A", b"a")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.source_directory / ("f" * 64)
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RawResponseIntegrityError, match="Reparse/link"):
        store.put("B", b"b")
    assert store.get("A") == b"a"
    assert store.get("B") is None
    assert list(outside.iterdir()) == []


def test_get_inventory_cost_200_entries(tmp_path, monkeypatch):
    store = cache(tmp_path)
    store.get("empty")
    entries = {}
    for index in range(200):
        key = hashlib.sha256(str(index).encode()).hexdigest()
        body = str(index).encode()
        (store.directory / f"{key}.body").write_bytes(body)
        entries[key] = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    (store.directory / "manifest.json").write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    counts = {"stat": 0, "lstat": 0, "iterdir": 0}
    for operation in counts:
        original = getattr(Path, operation)
        def traced(self, *args, _original=original, _operation=operation, **kwargs):
            counts[_operation] += 1
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(Path, operation, traced)
    start = time.perf_counter()
    for _ in range(10):
        assert store.get("0") == b"0"
    elapsed = time.perf_counter() - start
    print(f"raw-get-200 ten_hits seconds={elapsed:.6f} operations={counts}")
    assert counts["iterdir"] == 0
    assert counts["stat"] < 2000


def test_get_does_not_stat_unrelated_body_or_list_directory(tmp_path, monkeypatch):
    store = cache(tmp_path)
    store.put("A", b"aaa")
    store.put("B", b"bbb")
    unrelated = store.directory / (hashlib.sha256(b"B").hexdigest() + ".body")
    original = Path.stat
    def stat_requested_only(path, *args, **kwargs):
        assert path != unrelated, "get inspected unrelated body B"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat_requested_only)
    monkeypatch.setattr(Path, "iterdir", lambda path: pytest.fail("get scanned directory"))
    assert store.get("A") == b"aaa"
    assert store.get("missing") is None


def test_missing_other_body_does_not_block_scoped_get(tmp_path):
    store = cache(tmp_path)
    store.put("A", b"aaa")
    store.put("B", b"bbb")
    (store.directory / (hashlib.sha256(b"B").hexdigest() + ".body")).unlink()
    assert store.get("A") == b"aaa"
    with pytest.raises(RawResponseIntegrityError):
        store.get("B")
    with pytest.raises(RawResponseIntegrityError):
        store.status()
    with pytest.raises(RawResponseIntegrityError):
        store.put("C", b"ccc")


@pytest.mark.parametrize("payload", [[], {"version": True, "entries": {}},
                                    {"version": 1, "entries": []},
                                    {"version": 1, "entries": {"f" * 64: {"size": True, "sha256": "a" * 64}}}])
def test_fast_get_still_rejects_invalid_manifest_schema(tmp_path, payload):
    store = cache(tmp_path)
    store.get("empty")
    (store.directory / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RawResponseIntegrityError):
        store.get("missing")


def test_data_at_earliest_commit_is_not_rewritten_on_reuse(tmp_path, monkeypatch):
    import hextech.infrastructure.persistence.raw_responses as module
    class Clock(datetime):
        year_value = 2020
        @classmethod
        def now(cls, tz=None):
            return datetime(cls.year_value, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", Clock)
    store = cache(tmp_path)
    assert store.data_at() is None
    store.put("A", b"aaa")
    Clock.year_value = 2021
    store.put("B", b"bbb")
    Clock.year_value = 2022
    store.put("A", b"aaa")
    assert store.data_at() == "2020-01-01T00:00:00+00:00"


@pytest.mark.parametrize("stored_at", [None, "invalid", "2020-01-01T00:00:00", "2999-01-01T00:00:00Z", 123])
def test_data_at_legacy_or_bad_timestamp_uses_original_body_mtime(tmp_path, stored_at):
    store = cache(tmp_path)
    store.put("A", b"aaa")
    key = hashlib.sha256(b"A").hexdigest()
    manifest_path = store.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if stored_at is None:
        manifest["entries"][key].pop("stored_at")
    else:
        manifest["entries"][key]["stored_at"] = stored_at
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(store.directory / f"{key}.body", (timestamp, timestamp))
    assert store.get("A") == b"aaa"
    assert store.data_at() == "2020-01-01T00:00:00+00:00"


def test_bad_timestamp_and_future_mtime_fail_closed(tmp_path):
    store = cache(tmp_path)
    store.put("A", b"aaa")
    key = hashlib.sha256(b"A").hexdigest()
    manifest_path = store.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][key]["stored_at"] = "bad"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    timestamp = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(store.directory / f"{key}.body", (timestamp, timestamp))
    with pytest.raises(RawResponseIntegrityError, match="acquisition time"):
        store.data_at()


@pytest.mark.parametrize("placement", ["root", "child"])
def test_reparse_refused(tmp_path, placement):
    outside = tmp_path / "outside"
    outside.mkdir()
    store = cache(tmp_path)
    if placement == "root":
        link = store.root
    else:
        store.get("none")
        link = store.directory / "bad"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RawResponseIntegrityError):
        store.put("url", b"body")
    assert list(outside.iterdir()) == []
