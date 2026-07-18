"""DataService 发布和消费者读取的版本化数据快照。

发布器先在独立 generation 目录写入并校验全部文件，最后才原子切换 current
指针。客户端始终整代读取；当前代损坏时只允许整体回退上一代，禁止混用文件。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from hextech.contracts import (
    DataContractError,
    DataSnapshotManifestV2,
    SnapshotFileDescriptor,
    SourceProvenance,
)
from hextech.modules.data.catalog.runtime_store import get_runtime_root_dir
from hextech.modules.data.ports.atomic import atomic_write_json
from .validation import (
    SNAPSHOT_ROLES,
    SnapshotValidationError,
    content_fingerprint,
    count_records,
    safe_generation_file,
    sha256_file,
    validate_generation_directory,
)


SNAPSHOT_SCHEMA_VERSION = 2
_GENERATION_ID_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{10}$")
logger = logging.getLogger(__name__)


SnapshotFile = SnapshotFileDescriptor
DataSnapshotManifest = DataSnapshotManifestV2


def default_snapshot_root() -> Path:
    return get_runtime_root_dir() / "snapshots"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(f"快照 JSON 无法读取：{path.name}: {exc}") from exc


_count_records = count_records


class DataSnapshotPublisher:
    """DataService 专用的 generation 发布器。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_snapshot_root()
        self.generations_dir = self.root / "generations"
        self.staging_dir = self.root / "staging"
        self.current_path = self.root / "current.v2.json"
        self.previous_path = self.root / "previous.v2.json"

    def publish(
        self,
        payloads: Mapping[str, Any],
        *,
        source_files: Sequence[SourceProvenance | Mapping[str, Any]] = (),
        require_complete_provenance: bool = False,
    ) -> DataSnapshotManifest:
        normalized = {role: payloads.get(role) for role in SNAPSHOT_ROLES}
        champion_count, augment_count, stat_record_count = _count_records(normalized)
        provenance = tuple(
            item if isinstance(item, SourceProvenance) else SourceProvenance.from_mapping(item)
            for item in source_files
        )
        fingerprint = content_fingerprint(provenance)
        try:
            current = DataSnapshotClient(self.root).open_view()
            if current.manifest.content_fingerprint == fingerprint:
                return current.manifest
        except SnapshotValidationError:
            pass
        generation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{uuid.uuid4().hex[:10]}"
        staging = self.staging_dir / generation_id
        final = self.generations_dir / generation_id
        staging.mkdir(parents=True, exist_ok=False)
        promoted = False
        pointer_committed = False
        try:
            files: list[SnapshotFileDescriptor] = []
            for role in SNAPSHOT_ROLES:
                filename = f"{role}.json"
                path = staging / filename
                atomic_write_json(path, normalized[role], ensure_ascii=False, separators=(",", ":"))
                files.append(
                    SnapshotFileDescriptor(
                        role=role,
                        relative_path=filename,
                        size=path.stat().st_size,
                        sha256=sha256_file(path),
                    )
                )
            manifest = DataSnapshotManifest(
                schema_version=SNAPSHOT_SCHEMA_VERSION,
                generation_id=generation_id,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                content_fingerprint=fingerprint,
                source_files=provenance,
                champion_count=champion_count,
                augment_count=augment_count,
                stat_record_count=stat_record_count,
                files=tuple(files),
            )
            atomic_write_json(staging / "manifest.json", manifest.to_dict(), ensure_ascii=False, indent=2)
            validate_generation_directory(
                staging,
                manifest,
                payloads=normalized,
                allow_staging=True,
                require_complete_provenance=require_complete_provenance,
            )
            self.generations_dir.mkdir(parents=True, exist_ok=True)
            staging.replace(final)
            promoted = True
            previous_id = self._current_generation_id()
            if previous_id:
                atomic_write_json(
                    self.previous_path,
                    {"schema_version": SNAPSHOT_SCHEMA_VERSION, "generation_id": previous_id},
                    ensure_ascii=False,
                    indent=2,
                )
            atomic_write_json(
                self.current_path,
                {
                    "schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "current_generation_id": generation_id,
                },
                ensure_ascii=False,
                indent=2,
            )
            pointer_committed = True
            # generation 可能仍被 promotion journal 或历史 provenance 引用。保留策略
            # 由 DataService 在 journal committed 之后统一执行，publisher 不抢先删除。
            return manifest
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if promoted and not pointer_committed:
                shutil.rmtree(final, ignore_errors=True)
            raise

    def current_generation_id(self) -> str:
        """返回已发布 current generation；不存在时返回空字符串。"""

        return self._current_generation_id()

    def _current_generation_id(self) -> str:
        if not self.current_path.exists():
            return ""
        payload = _read_json(self.current_path)
        return str(payload.get("current_generation_id") or "") if isinstance(payload, Mapping) else ""

    def _remove_unreferenced_generations(self, keep: set[str]) -> None:
        if not self.generations_dir.is_dir():
            return
        for path in self.generations_dir.iterdir():
            if path.is_dir() and path.name not in keep:
                shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _validate_generation(
        directory: Path,
        manifest: DataSnapshotManifest,
        *,
        allow_staging: bool = False,
    ) -> None:
        validate_generation_directory(directory, manifest, allow_staging=allow_staging)


@dataclass(frozen=True)
class DataSnapshotView:
    """固定在一次完整 generation 上的只读查询视图。"""

    manifest: DataSnapshotManifest
    _payloads: Mapping[str, Any]
    degraded: bool = False
    failed_generation_id: str = ""

    def status(self) -> dict[str, Any]:
        return {
            "state": "degraded" if self.degraded else "ready",
            "generation_id": self.manifest.generation_id,
            "failed_generation_id": self.failed_generation_id,
            "content_fingerprint": self.manifest.content_fingerprint,
            "created_at": self.manifest.created_at,
        }

    def get_champions(self) -> list[dict[str, Any]]:
        champions = self._payloads["champions"]
        return deepcopy([dict(item) for item in champions if isinstance(item, Mapping)])

    def get_champion(self, champion_id_or_name: object) -> dict[str, Any] | None:
        needle = str(champion_id_or_name).strip().casefold()
        for champion in self.get_champions():
            if needle in {str(champion.get("id", "")).casefold(), str(champion.get("name", "")).casefold()}:
                return champion
        return None

    def _champion_detail(self, champion_id_or_name: object) -> Mapping[str, Any] | None:
        details = self._payloads["champion_hextech"]
        needle = str(champion_id_or_name).strip().casefold()
        for name, detail in details.items():
            if not isinstance(detail, Mapping):
                continue
            if needle in {str(name).casefold(), str(detail.get("hero_id", "")).casefold()}:
                return detail
        return None

    def get_champion_detail(self, champion_id_or_name: object) -> dict[str, Any] | None:
        detail = self._champion_detail(champion_id_or_name)
        return deepcopy(dict(detail)) if detail is not None else None

    def get_champion_augments(self, champion_id_or_name: object) -> list[dict[str, Any]]:
        detail = self._champion_detail(champion_id_or_name)
        augments = detail.get("augments", []) if detail else []
        return deepcopy([dict(item) for item in augments if isinstance(item, Mapping)])

    def get_synergy_data(self) -> dict[str, Any]:
        details = self._payloads["champion_hextech"]
        result: dict[str, Any] = {}
        for hero_name, detail in details.items():
            if not isinstance(detail, Mapping):
                continue
            synergy = detail.get("synergy")
            if not isinstance(synergy, Mapping):
                continue
            hero_id = str(detail.get("hero_id") or "").strip()
            result[hero_id or str(hero_name)] = deepcopy(dict(synergy))
        return result

    def get_combo_stats(self, champion_id_or_name: object, augment_id: object) -> dict[str, Any] | None:
        identity = self.resolve_augment(augment_id)
        needle = str(identity.get("canonical_id") or "") if identity else ""
        if not needle:
            return None
        return next(
            (item for item in self.get_champion_augments(champion_id_or_name) if str(item.get("id")) == needle),
            None,
        )

    def resolve_augment(self, value: object) -> dict[str, Any] | None:
        """解析数字统计 ID、中文名或 Vision stable ID。"""

        from hextech.modules.recommendation.hints import normalize_augment_id, normalize_augment_name

        identities = self._payloads.get("identities", {})
        if not isinstance(identities, Mapping):
            return None
        raw = str(value or "").strip()
        aliases = identities.get("augment_aliases", {})
        canonical_id = ""
        if isinstance(aliases, Mapping):
            for candidate in (raw, normalize_augment_id(raw), normalize_augment_name(raw)):
                resolved = str(aliases.get(candidate) or "").strip()
                if resolved:
                    canonical_id = resolved
                    break
        catalog = identities.get("catalog_augments", {})
        catalog_item = None
        if isinstance(catalog, Mapping):
            for candidate in (raw, normalize_augment_id(raw), normalize_augment_name(raw)):
                item = catalog.get(candidate)
                if isinstance(item, Mapping):
                    catalog_item = dict(item)
                    break
            if catalog_item is None:
                catalog_item = next(
                    (
                        dict(item)
                        for item in catalog.values()
                        if isinstance(item, Mapping)
                        and normalize_augment_name(item.get("name")) == normalize_augment_name(raw)
                    ),
                    None,
                )
        augments = identities.get("augments", {})
        if not canonical_id and raw.isdecimal():
            canonical_id = raw
        if catalog_item is None and not canonical_id:
            return None
        result = catalog_item or {}
        result["canonical_id"] = canonical_id or str(result.get("canonical_id") or "")
        result["stats_available"] = bool(result["canonical_id"])
        if not result.get("name") and isinstance(augments, Mapping):
            result["name"] = str(augments.get(result["canonical_id"]) or "")
        return deepcopy(result)

    def get_item(self, item_id: object) -> dict[str, Any] | None:
        """装备域预留查询；当前 generation 尚未发布装备角色。"""

        return None

    def get_item_recommendations(self, champion_id: object) -> list[dict[str, Any]]:
        """装备推荐预留查询，未发布时返回稳定空集合。"""

        return []

    def get_overlay_hints(self) -> dict[str, Any]:
        return deepcopy(dict(self._payloads["overlay_hints"]))

    def get_identity_indexes(self) -> dict[str, Any]:
        return deepcopy(dict(self._payloads["identities"]))

class DataSnapshotClient:
    """桌面、Web 与 Overlay 共用的只读 generation 客户端。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_snapshot_root()
        self._loaded: tuple[str, DataSnapshotManifest, dict[str, Any], bool, str] | None = None

    def _pointer(self) -> Mapping[str, Any]:
        payload = _read_json(self.root / "current.v2.json")
        if not isinstance(payload, Mapping) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotValidationError("current.v2.json 指针无效")
        return payload

    def _previous_generation_id(self) -> str:
        path = self.root / "previous.v2.json"
        if not path.is_file():
            return ""
        payload = _read_json(path)
        if not isinstance(payload, Mapping) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotValidationError("previous.v2.json 指针无效")
        return str(payload.get("generation_id") or "")

    def _load_generation(self, generation_id: str) -> tuple[DataSnapshotManifest, dict[str, Any]]:
        if not _GENERATION_ID_RE.fullmatch(generation_id):
            raise SnapshotValidationError("generation_id 格式无效")
        directory = self.root / "generations" / generation_id
        try:
            manifest = DataSnapshotManifest.from_mapping(_read_json(directory / "manifest.json"))
        except DataContractError as exc:
            raise SnapshotValidationError(f"generation manifest 结构无效：{exc}") from exc
        payloads: dict[str, Any] = {}
        for item in manifest.files:
            payloads[item.role] = _read_json(safe_generation_file(directory, item.relative_path))
        validate_generation_directory(directory, manifest, payloads=payloads)
        return manifest, payloads

    def _load(self) -> tuple[DataSnapshotManifest, dict[str, Any], bool, str]:
        pointer = self._pointer()
        current_id = str(pointer.get("current_generation_id") or "")
        previous_id = self._previous_generation_id()
        if self._loaded is not None and self._loaded[0] == current_id:
            return self._loaded[1:]
        try:
            manifest, payloads = self._load_generation(current_id)
            result = (manifest, payloads, False, "")
        except SnapshotValidationError:
            if not previous_id:
                raise
            manifest, payloads = self._load_generation(previous_id)
            result = (manifest, payloads, True, current_id)
        self._loaded = (current_id, *result)
        return result

    def status(self) -> dict[str, Any]:
        try:
            return self.open_view().status()
        except SnapshotValidationError as exc:
            return {"state": "unavailable", "generation_id": "", "reason": str(exc)}

    def open_view(self) -> DataSnapshotView:
        """固定并返回一代数据，供单次响应内完成全部查询。"""

        manifest, payloads, degraded, failed_id = self._load()
        return DataSnapshotView(manifest, payloads, degraded, failed_id)

    def open_generation(self, generation_id: str) -> DataSnapshotView:
        """按 ID 固定打开 immutable generation；不存在或损坏时明确失败。"""

        manifest, payloads = self._load_generation(str(generation_id or ""))
        return DataSnapshotView(manifest, payloads)

    def load_manifest(self) -> DataSnapshotManifest:
        return self._load()[0]

    def get_champions(self) -> list[dict[str, Any]]:
        return self.open_view().get_champions()

    def get_champion(self, champion_id_or_name: object) -> dict[str, Any] | None:
        return self.open_view().get_champion(champion_id_or_name)

    def get_champion_detail(self, champion_id_or_name: object) -> dict[str, Any] | None:
        """返回当前 generation 中单英雄的完整 Web/Overlay 详情副本。"""

        return self.open_view().get_champion_detail(champion_id_or_name)

    def get_champion_augments(self, champion_id_or_name: object) -> list[dict[str, Any]]:
        return self.open_view().get_champion_augments(champion_id_or_name)

    def get_synergy_data(self) -> dict[str, Any]:
        """返回当前 generation 内随英雄详情一起发布的联动数据。"""

        return self.open_view().get_synergy_data()

    def get_combo_stats(self, champion_id_or_name: object, augment_id: object) -> dict[str, Any] | None:
        return self.open_view().get_combo_stats(champion_id_or_name, augment_id)

    def resolve_augment(self, value: object) -> dict[str, Any] | None:
        return self.open_view().resolve_augment(value)

    def get_overlay_hints(self) -> dict[str, Any]:
        return self.open_view().get_overlay_hints()

    def get_identity_indexes(self) -> dict[str, Any]:
        return self.open_view().get_identity_indexes()
