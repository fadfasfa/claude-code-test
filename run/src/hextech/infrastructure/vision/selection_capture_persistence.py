"""选择区域缓存的严格所有者检查、配额轮转与原子持久化。"""
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .diagnostic_capture_session import _reject_reparse_path


CACHE_OWNER = "overlay-selection-cache-v2"
CACHE_GROUP_LIMIT = 200
CACHE_BYTE_LIMIT = 256 * 1024 * 1024

_LEGACY_MANIFEST_FIELDS = frozenset({
    "schema_version", "owner", "diagnostic_id", "manual", "labels",
    "automatic_exemplar_eligible", "requires_manual_truth", "updated_at", "terminal",
    "first_frame", "clearest_frame", "last_frame", "frames",
})
_MANIFEST_FIELDS = _LEGACY_MANIFEST_FIELDS | {
    "retention_class", "evidence_completeness", "temporal_acceptance", "qualified",
}


def _retention_priority(retention_class: str) -> int:
    return {"manual": 400, "anomaly": 300, "success": 200, "weak": 100}[retention_class]


def _manifest_retention_priority(manifest: Mapping[str, Any]) -> int:
    # Exact legacy automatic groups predate the classification; treat them as normal successes.
    # Sparse final frames cannot disprove an earlier anomaly floor. Never
    # downgrade persisted severity merely because later frames became READY.
    return {"weak": 100, "success": 200, "anomaly": 300, "manual": 400}.get(
        str(manifest.get("retention_class") or "success"), 300,
    )


def _inspect_group(path: Path):
    if not path.is_dir() or len(path.name) != 32 or any(c not in "0123456789abcdef" for c in path.name):
        return None
    try:
        _reject_reparse_path(path)
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        names = {"manifest.json", *_manifest_assets(manifest)}
        children = list(path.iterdir())
        for child in children:
            _reject_reparse_path(child)
        exact_fields = frozenset(manifest)
        current_shape = (exact_fields == _MANIFEST_FIELDS
                         and manifest.get("retention_class") in {"weak", "success", "anomaly"}
                         and manifest.get("evidence_completeness") == "sparse"
                         and manifest.get("temporal_acceptance") is False
                         and manifest.get("qualified") is False)
        owned = ((exact_fields == _LEGACY_MANIFEST_FIELDS or current_shape)
                 and manifest.get("schema_version") == 2
                 and manifest.get("owner") == CACHE_OWNER and manifest.get("diagnostic_id") == path.name
                 and not manifest.get("manual") and not manifest.get("labels")
                 and not manifest.get("labelled") and not manifest.get("protected")
                 and {p.name for p in children} == names
                 and all(p.is_file() and (p.name == "manifest.json" or
                     (len(p.stem) == 64 and all(c in "0123456789abcdef" for c in p.stem)
                      and p.suffix == ".png")) for p in children))
        complete = names <= {p.name for p in children if p.is_file()}
        return path, manifest, sum(p.stat().st_size for p in children if p.is_file()), owned, complete
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _groups(root: Path):
    """Only enumerate this feature's exact, self-contained manifests; unknown/labelled content is protected."""
    if not root.exists():
        return []
    _reject_reparse_path(root)
    return [group for path in root.iterdir() if (group := _inspect_group(path)) is not None]


def _manifest_assets(manifest: Mapping) -> set[str]:
    names = set()
    for frame in manifest["frames"]:
        names.add(frame["file"])
        for slot in frame.get("slot_rois", []):
            for kind in ("name", "icon"):
                if slot[kind].get("file"):
                    names.add(slot[kind]["file"])
    return names


def _inventory(root: Path) -> tuple[int, int]:
    """All bytes under the new root count, including protected/manual/partial/unknown assets."""
    total = 0
    groups = 0
    pending = [root]
    while pending:
        parent = pending.pop()
        _reject_reparse_path(parent)
        for path in parent.iterdir():
            _reject_reparse_path(path)
            if path.is_dir():
                if parent == root:
                    groups += 1
                pending.append(path)
            elif path.is_file():
                total += path.stat().st_size
            else:
                raise ValueError("selection_cache_unknown_asset")
    return total, groups


def selection_cache_status(root: Path) -> dict:
    _reject_reparse_path(root)
    retained = _groups(root)
    total, count = _inventory(root) if root.exists() else (0, 0)
    return {"groups": sum(g[4] for g in retained), "automatic_groups": sum(g[3] for g in retained),
            "bytes": total, "quota_groups": count, "root": str(root),
            "byte_limit": CACHE_BYTE_LIMIT, "group_limit": CACHE_GROUP_LIMIT,
            "protected_bytes": total - sum(g[2] for g in retained if g[3]),
            "available_bytes": max(0, CACHE_BYTE_LIMIT - total)}


def persist_selection_capture(root: Path, draft: Any, *, group_limit=CACHE_GROUP_LIMIT,
                              byte_limit=CACHE_BYTE_LIMIT) -> dict:
    """Runs on the existing writer thread. Only new unlabelled auto groups may be rotated."""
    from hextech.infrastructure.persistence.file_lock import InterProcessFileLock
    _reject_reparse_path(root / ".writer.lock")
    lock = InterProcessFileLock(root / ".writer.lock")
    if not lock.acquire():
        raise ValueError("selection_cache_writer_busy")
    try:
        return _persist_locked(root, draft, group_limit=group_limit, byte_limit=byte_limit)
    finally:
        lock.release()


def _rgb_sha256(image: Any) -> str:
    digest = hashlib.sha256()
    digest.update(str(image.mode).encode("ascii", errors="strict"))
    digest.update(f"{image.size[0]}x{image.size[1]}".encode("ascii"))
    digest.update(image.pixels)
    return digest.hexdigest()


def _reusable_asset(path: Path, record: Mapping[str, Any]) -> bool:
    filename = record.get("file")
    digest = record.get("sha256")
    if not isinstance(filename, str) or filename != f"{digest}.png":
        return False
    target = path / filename
    try:
        _reject_reparse_path(target)
        return (target.is_file() and target.stat().st_size == record.get("byte_size")
                and hashlib.sha256(target.read_bytes()).hexdigest() == digest)
    except (OSError, ValueError, TypeError):
        return False


def _existing_frames(path: Path, manifest: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(manifest, Mapping):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for record in manifest.get("frames", []):
        if not isinstance(record, Mapping):
            continue
        digest = record.get("rgb_sha256")
        if isinstance(digest, str) and len(digest) == 64 and _reusable_asset(path, record):
            result[digest] = record
    return result


def _matching_slot_asset(path: Path, previous: Mapping[str, Any] | None, *, slot_index: int,
                         kind: str, box: list[int]) -> Mapping[str, Any] | None:
    if not isinstance(previous, Mapping):
        return None
    slots = previous.get("slot_rois") if isinstance(previous.get("slot_rois"), list) else []
    candidate = next((slot.get(kind) for slot in slots if isinstance(slot, Mapping)
                      and slot.get("slot") == slot_index and isinstance(slot.get(kind), Mapping)), None)
    if (not isinstance(candidate, Mapping) or candidate.get("valid") is not True
            or candidate.get("box") != box or not _reusable_asset(path, candidate)):
        return None
    return candidate


def _persist_locked(root: Path, draft: Any, *, group_limit: int, byte_limit: int) -> dict:
    from .failure_evidence import _atomic_write_bytes, _png_bytes, RoiImage
    if len(draft.diagnostic_id) != 32 or any(c not in "0123456789abcdef" for c in draft.diagnostic_id):
        raise ValueError("selection_cache_invalid_diagnostic_id")
    _reject_reparse_path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / draft.diagnostic_id
    _reject_reparse_path(path)
    groups = _groups(root)
    existing = next((group for group in groups if group[0] == path), None)
    if path.exists() and (existing is None or not existing[3]):
        raise ValueError("selection_cache_existing_group_protected")
    existing_priority = _manifest_retention_priority(existing[1]) if existing is not None else 0
    existing_manifest = existing[1] if existing else None
    reusable_frames = _existing_frames(path, existing_manifest)
    payloads: dict[str, bytes] = {}
    observations = []
    for frame in draft.frames:
        rgb_sha256 = _rgb_sha256(frame.image)
        previous = reusable_frames.get(rgb_sha256)
        if previous is not None:
            filename = str(previous["file"])
            byte_size = int(previous["byte_size"])
        else:
            encoded = _png_bytes(frame.image)
            filename = hashlib.sha256(encoded).hexdigest()+".png"
            byte_size = len(encoded)
            payloads[filename] = encoded
        observation = {"file": filename, "sha256": filename[:-4], "byte_size": byte_size,
                       "rgb_sha256": rgb_sha256, "sampled_at": frame.sampled_at,
                       "clarity": frame.clarity, **deepcopy(frame.metadata)}
        image = frame.image.to_image()
        selection = observation["selection_box"]
        for slot in observation.get("slot_rois", []):
            for kind in ("name", "icon"):
                roi = slot[kind]
                if not roi["valid"]:
                    continue
                box = roi["box"]
                local = (box[0]-selection[0], box[1]-selection[1], box[2]-selection[0], box[3]-selection[1])
                if not (0 <= local[0] < local[2] <= image.width and 0 <= local[1] < local[3] <= image.height):
                    roi.update(valid=False, reason="slot_roi_outside_capture")
                    continue
                reusable = _matching_slot_asset(path, previous, slot_index=int(slot["slot"]),
                                                kind=kind, box=box)
                if reusable is not None:
                    roi.update(file=reusable["file"], sha256=reusable["sha256"],
                               byte_size=reusable["byte_size"])
                else:
                    content = _png_bytes(RoiImage.from_image(image.crop(local)))
                    digest = hashlib.sha256(content).hexdigest()
                    roi.update(file=digest+".png", sha256=digest, byte_size=len(content))
                    payloads[digest+".png"] = content
        observations.append(observation)
    retention_class = max(
        (draft.retention_class, str(existing_manifest.get("retention_class") or "success")
         if existing_manifest else "weak"),
        key=_retention_priority,
    )
    effective_priority = max(draft.priority, existing_priority)
    manifest = {"schema_version": 2, "owner": CACHE_OWNER, "diagnostic_id": draft.diagnostic_id,
                "manual": draft.manual, "labels": [], "automatic_exemplar_eligible": False,
                "requires_manual_truth": True, "updated_at": time.time(), "terminal": draft.terminal,
                "retention_class": retention_class, "evidence_completeness": "sparse",
                "temporal_acceptance": False, "qualified": False,
                "first_frame": draft.first_id, "clearest_frame": draft.clear_id, "last_frame": draft.last_id,
                "frames": observations}
    payloads["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    asset_sizes: dict[str, int] = {}
    for observation in observations:
        asset_sizes[str(observation["file"])] = int(observation["byte_size"])
        for slot in observation.get("slot_rois", []):
            for kind in ("name", "icon"):
                roi = slot[kind]
                if roi.get("file"):
                    asset_sizes[str(roi["file"])] = int(roi["byte_size"])
    needed = len(payloads["manifest.json"]) + sum(asset_sizes.values())
    if needed > byte_limit:
        raise ValueError("selection_cache_group_exceeds_byte_budget")
    previous_assets = _manifest_assets(existing[1]) if existing else set()
    remaining = [
        group for group in groups
        if group[0] != path and group[3]
        and _manifest_retention_priority(group[1]) <= effective_priority
    ]
    # Protected/partial bytes and the atomic-write temporary count at every instant. New PNGs
    # accumulate before the manifest is committed; the old manifest still exists while its
    # replacement temporary is written. Reused content-addressed PNGs add no bytes.
    total, count = _inventory(root)
    new_asset_bytes = sum(
        len(content) for name, content in payloads.items()
        if name != "manifest.json" and not (path / name).exists()
    )
    transient_peak = total + new_asset_bytes + len(payloads["manifest.json"])
    while count+int(not path.exists()) > group_limit or transient_peak > byte_limit:
        if not remaining:
            raise ValueError("selection_cache_budget_exhausted")
        victim = min(remaining, key=lambda g: (
            _manifest_retention_priority(g[1]), g[1].get("updated_at", 0), g[0].name,
        ))
        # Revalidate immediately before deletion; no recursive deletion or external references.
        checked = _inspect_group(victim[0])
        if checked is None or not checked[3]:
            raise ValueError("selection_cache_rotation_protected")
        for child in checked[0].iterdir():
            child.unlink()
        checked[0].rmdir()
        remaining.remove(victim)
        total -= checked[2]
        count -= 1
        transient_peak -= checked[2]
    path.mkdir(exist_ok=True)
    # Manifest is committed last; leftovers on failure are intentionally protected.
    for name, content in payloads.items():
        _reject_reparse_path(path / name)
        if name == "manifest.json" or not (path / name).exists():
            _atomic_write_bytes(path / name, content)
        elif (path / name).read_bytes() != content:
            raise ValueError("selection_cache_asset_hash_mismatch")
    for stale_name in previous_assets - _manifest_assets(manifest):
        stale = path / stale_name
        _reject_reparse_path(stale)
        stale.unlink(missing_ok=True)
    # An accepted task is not a successful save until every manifest asset really exists.
    committed = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if committed != manifest:
        raise ValueError("selection_cache_manifest_mismatch")
    for filename in _manifest_assets(manifest):
        if hashlib.sha256((path / filename).read_bytes()).hexdigest()+".png" != filename:
            raise ValueError("selection_cache_asset_hash_mismatch")
    return selection_cache_status(root)
