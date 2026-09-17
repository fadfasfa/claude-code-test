"""Task-local, immutable HTTP response bodies; callers admit only successful responses.

A revision manifest is the commit marker. Interrupted writes are never adopted
or overwritten, but unrelated committed entries remain reusable. No eviction.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .file_lock import InterProcessFileLock


class RawResponseIntegrityError(RuntimeError):
    """Existing cache data is damaged, incomplete, or unsafe."""


class RawResponseConflict(RuntimeError):
    """An immutable URL already has different content."""


class RawResponseBudgetExceeded(RuntimeError):
    """The revision/source body budget or revision inventory limit is exhausted."""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_BODY_NAME = re.compile(r"[0-9a-f]{64}(?:-[0-9a-f]{64}(?:-[0-9a-f]{32})?)?\.body\Z")
MAX_SOURCE_REVISIONS = 4096


def _body_name(key: str, entry: dict) -> str:
    """Manifest paths are derived identities, never arbitrary relative paths."""
    filename = entry.get("filename")
    attempt = entry.get("attempt")
    if filename is None and attempt is None:
        return f"{key}.body"  # Existing v1 entries remain readable.
    if attempt is not None and (not isinstance(attempt, str) or re.fullmatch(r"[0-9a-f]{32}", attempt) is None):
        raise RawResponseIntegrityError("Invalid body attempt identity")
    expected = f"{key}-{entry['sha256']}" + (f"-{attempt}" if attempt is not None else "") + ".body"
    if filename != expected:
        raise RawResponseIntegrityError("Invalid derived body filename")
    return expected


def _safe(path: Path, *, parents: bool = True) -> None:
    # lstat every ancestor: resolve() alone would hide links and junctions.
    for candidate in reversed((path, *path.parents) if parents else (path,)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RawResponseIntegrityError(f"Reparse/link path refused: {candidate}")
        if candidate != path and not stat.S_ISDIR(info.st_mode):
            raise RawResponseIntegrityError(f"Non-directory ancestor: {candidate}")


class RawResponseCache:
    """Serialize readers/writers with an OS lock; metadata follows durable bodies.

    The caller owns root exclusively. Path checks reject existing reparse points;
    like the existing file-lock primitive this is not a hostile-user sandbox.
    max_bytes covers raw bodies and orphan/pending bytes within this revision,
    excluding the committed manifest and lock metadata. max_source_bytes applies
    the same accounting across this source's hashed revision directories. Source
    scans occur only on new puts/status, never gets; capacity stops, never evicts.
    """

    def __init__(self, root: Path, *, source: str, revision: str,
                 max_bytes: int = 2 * 1024**3,
                 max_source_bytes: int = 6 * 1024**3) -> None:
        if not source or not revision:
            raise ValueError("source and revision must be nonempty")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError("max_bytes must be a nonnegative integer")
        if isinstance(max_source_bytes, bool) or not isinstance(max_source_bytes, int) or max_source_bytes < 0:
            raise ValueError("max_source_bytes must be a nonnegative integer")
        self.root = Path(os.path.abspath(root))
        self.source_directory = self.root / _digest(source.encode())
        self.directory = self.source_directory / _digest(revision.encode())
        self.max_bytes = max_bytes
        self.max_source_bytes = max_source_bytes
        self.source = source
        self.revision = revision

    @contextmanager
    def _locked(self, path: Path | None = None):
        path = path if path is not None else self.directory / ".lock"
        _safe(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        _safe(path.parent)
        _safe(path)
        lock = InterProcessFileLock(path)
        deadline = time.monotonic() + 30
        while True:
            try:
                acquired = lock.acquire()
            except PermissionError:
                # Windows may deny the primitive's initial byte flush while
                # another process owns that byte, including its close flush.
                acquired = False
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Raw response cache lock timed out")
            time.sleep(0.01)
            _safe(path)
        try:
            _safe(path.parent)
            yield
        finally:
            lock.release()

    def _source_usage(self) -> int:
        """Stat only the task-owned source namespace while its budget lock is held."""
        total = 0
        revisions = []
        for revision in self.source_directory.iterdir():
            _safe(revision, parents=False)
            if revision.name == ".budget.lock" and revision.is_file():
                continue
            if re.fullmatch(r"[0-9a-f]{64}", revision.name) is None or not revision.is_dir():
                raise RawResponseIntegrityError("Unknown source cache entry")
            revisions.append(revision)
            if len(revisions) > MAX_SOURCE_REVISIONS:
                raise RawResponseBudgetExceeded("Raw response source revision inventory limit exceeded")
        for revision in revisions:
            for child in revision.iterdir():
                _safe(child, parents=False)
                info = child.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise RawResponseIntegrityError("Non-file source cache entry")
                if child.name in {".lock", "manifest.json"}:
                    continue
                if (_BODY_NAME.fullmatch(child.name) is None
                        and not (child.name.startswith(".") and child.name.endswith(".pending"))):
                    raise RawResponseIntegrityError("Unknown source cache body")
                total += info.st_size
        return total

    def _manifest(self, *, validate_files: bool = True) -> tuple[dict, int, int]:
        path = self.directory / "manifest.json"
        # _locked has checked the complete parent chain. Check each child itself
        # without repeating that chain for every entry in a revision inventory.
        _safe(path, parents=False)
        try:
            data = json.loads(path.read_bytes())
        except FileNotFoundError:
            data = {"version": 1, "entries": {}}
        except (ValueError, OSError) as exc:
            raise RawResponseIntegrityError("Unreadable manifest") from exc
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1 or not isinstance(data.get("entries"), dict)):
            raise RawResponseIntegrityError("Invalid manifest")
        expected = {".lock", "manifest.json"}
        for key, entry in data["entries"].items():
            if (len(key) != 64 or any(c not in "0123456789abcdef" for c in key)
                    or not isinstance(entry, dict)
                    or type(entry.get("size")) is not int or entry["size"] < 0
                    or not isinstance(entry.get("sha256"), str)
                    or len(entry["sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in entry["sha256"])):
                raise RawResponseIntegrityError("Invalid entry metadata")
            body_path = self.directory / _body_name(key, entry)
            if not validate_files:
                continue
            _safe(body_path, parents=False)
            try:
                info = body_path.stat()
            except OSError as exc:
                raise RawResponseIntegrityError("Missing body") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
                raise RawResponseIntegrityError("Body size mismatch")
            expected.add(body_path.name)
        if not validate_files:
            # get() never inspects unrelated bodies or orphan inventory. A miss
            # is not adoption; put/status retain the full integrity/budget gate.
            return data, 0, 0
        orphan_bytes = 0
        orphan_count = 0
        for child in self.directory.iterdir():
            _safe(child, parents=False)
            if not child.is_file():
                raise RawResponseIntegrityError(f"Non-file cache entry: {child.name}")
            if child.name not in expected:
                name = child.name
                is_body = _BODY_NAME.fullmatch(name) is not None
                is_pending = name.startswith(".") and name.endswith(".pending")
                if not (is_body or is_pending):
                    raise RawResponseIntegrityError(f"Unknown cache entry: {name}")
                orphan_bytes += child.stat().st_size
                orphan_count += 1
        return data, orphan_bytes, orphan_count

    def _read(self, key: str, entry: dict) -> bytes:
        path = self.directory / _body_name(key, entry)
        _safe(path, parents=False)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
                raise RawResponseIntegrityError("Body size/type mismatch")
            body = path.read_bytes()
        except OSError as exc:
            raise RawResponseIntegrityError("Unreadable body") from exc
        if len(body) != entry["size"] or _digest(body) != entry["sha256"]:
            raise RawResponseIntegrityError("Body hash/size mismatch")
        return body

    def _atomic_write(self, path: Path, body: bytes) -> None:
        _safe(path)
        temporary = self.directory / f".{uuid.uuid4().hex}.pending"
        _safe(temporary)
        with temporary.open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _safe(path)
        if path.name.endswith(".body") and path.exists():
            raise RawResponseIntegrityError("Refusing to overwrite an existing body")
        os.replace(temporary, path)

    def get(self, url: str) -> bytes | None:
        key = _digest(url.encode())
        with self._locked():
            manifest, _, _ = self._manifest(validate_files=False)
            entry = manifest["entries"].get(key)
            return None if entry is None else self._read(key, entry)

    def put(self, url: str, body: bytes) -> None:
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        key = _digest(url.encode())
        # Uniform source -> revision ordering serializes reservations across
        # revisions, including other processes. Readers take only revision lock.
        with self._locked(self.source_directory / ".budget.lock"), self._locked():
            manifest, orphan_bytes, _ = self._manifest()
            entries = manifest["entries"]
            if key in entries:
                if self._read(key, entries[key]) != body:
                    raise RawResponseConflict("URL already has different immutable content")
                return
            if sum(entry["size"] for entry in entries.values()) + orphan_bytes + len(body) > self.max_bytes:
                raise RawResponseBudgetExceeded("Raw response revision budget exceeded")
            if self._source_usage() + len(body) > self.max_source_bytes:
                raise RawResponseBudgetExceeded("Raw response source budget exceeded")
            entry = {"size": len(body), "sha256": _digest(body),
                     "stored_at": datetime.now(timezone.utc).isoformat()}
            body_path = self.directory / f"{key}.body"
            if body_path.exists():
                # An orphan is evidence, not a cache hit. Publish a fresh copy
                # only after a new successful response has reached this method.
                entry["filename"] = f"{key}-{entry['sha256']}.body"
                body_path = self.directory / entry["filename"]
                while body_path.exists():
                    entry["attempt"] = uuid.uuid4().hex
                    entry["filename"] = f"{key}-{entry['sha256']}-{entry['attempt']}.body"
                    body_path = self.directory / entry["filename"]
            self._atomic_write(body_path, body)
            entries[key] = entry
            self._atomic_write(self.directory / "manifest.json",
                               json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())

    def status(self) -> dict:
        with self._locked(self.source_directory / ".budget.lock"), self._locked():
            manifest, orphan_bytes, orphan_count = self._manifest()
            entries = manifest["entries"]
            committed_bytes = sum(e["size"] for e in entries.values())
            return {"source": self.source, "revision": self.revision,
                    "count": len(entries), "bytes": committed_bytes + orphan_bytes,
                    "committed_bytes": committed_bytes, "orphan_bytes": orphan_bytes,
                    "orphan_count": orphan_count,
                    "max_bytes": self.max_bytes, "source_bytes": self._source_usage(),
                    "max_source_bytes": self.max_source_bytes}

    def data_at(self) -> str | None:
        """Earliest committed acquisition time, computed once when publishing.

        Legacy/invalid metadata uses the preserved body mtime, never the current
        reprocessing clock. Impossible/future fallback times fail closed.
        """
        with self._locked():
            manifest, _, _ = self._manifest()
            timestamps = []
            now = datetime.now(timezone.utc).timestamp()
            for key, entry in manifest["entries"].items():
                stamp = None
                raw = entry.get("stored_at")
                if isinstance(raw, str):
                    try:
                        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        candidate = parsed.timestamp() if parsed.tzinfo is not None else float("nan")
                        if math.isfinite(candidate) and candidate <= now:
                            stamp = candidate
                    except (ValueError, OverflowError, OSError):
                        pass
                if stamp is None:
                    path = self.directory / _body_name(key, entry)
                    _safe(path, parents=False)
                    stamp = path.stat().st_mtime
                    if not math.isfinite(stamp) or stamp > now:
                        raise RawResponseIntegrityError("Invalid raw response acquisition time")
                timestamps.append(stamp)
            if not timestamps:
                return None
            try:
                return datetime.fromtimestamp(min(timestamps), timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError) as exc:
                raise RawResponseIntegrityError("Invalid raw response acquisition time") from exc
