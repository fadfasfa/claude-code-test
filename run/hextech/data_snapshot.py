"""DataService 发布和消费者读取的版本化数据快照。

发布器先在独立 generation 目录写入并校验全部文件，最后才原子切换 current
指针。客户端始终整代读取；当前代损坏时只允许整体回退上一代，禁止混用文件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from hextech.catalog.runtime_store import get_runtime_root_dir
from hextech.support.atomic_io import atomic_write_json


SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_ROLES = ("champions", "champion_hextech", "overlay_hints", "identities")
_GENERATION_ID_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{10}$")
logger = logging.getLogger(__name__)


class SnapshotValidationError(RuntimeError):
    """快照结构、文件或哈希不满足完整代约束。"""


@dataclass(frozen=True)
class SnapshotFile:
    role: str
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class DataSnapshotManifest:
    schema_version: int
    generation_id: str
    created_at: str
    private_stats_enabled: bool
    source_files: tuple[dict[str, Any], ...]
    champion_count: int
    augment_count: int
    stat_record_count: int
    files: tuple[SnapshotFile, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DataSnapshotManifest":
        try:
            integer_fields = ("schema_version", "champion_count", "augment_count", "stat_record_count")
            if any(isinstance(payload[field], bool) or not isinstance(payload[field], int) for field in integer_fields):
                raise TypeError("schema_version 和计数字段必须是整数")
            if not isinstance(payload["private_stats_enabled"], bool):
                raise TypeError("private_stats_enabled 必须是 bool")
            files = tuple(SnapshotFile(**item) for item in payload["files"])
            return cls(
                schema_version=payload["schema_version"],
                generation_id=str(payload["generation_id"]),
                created_at=str(payload["created_at"]),
                private_stats_enabled=payload["private_stats_enabled"],
                source_files=tuple(dict(item) for item in payload.get("source_files", [])),
                champion_count=payload["champion_count"],
                augment_count=payload["augment_count"],
                stat_record_count=payload["stat_record_count"],
                files=files,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotValidationError(f"快照 manifest 结构无效：{exc}") from exc


def default_snapshot_root() -> Path:
    return get_runtime_root_dir() / "snapshots"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(f"快照 JSON 无法读取：{path.name}: {exc}") from exc


def _count_records(payloads: Mapping[str, Any]) -> tuple[int, int, int]:
    champions = payloads.get("champions")
    details = payloads.get("champion_hextech")
    hints = payloads.get("overlay_hints")
    identities = payloads.get("identities")
    if not isinstance(champions, list):
        raise SnapshotValidationError("champions 必须是列表")
    if not all(isinstance(value, Mapping) for value in (details, hints, identities)):
        raise SnapshotValidationError("champion_hextech、overlay_hints 和 identities 必须是对象")

    champion_ids: set[str] = set()
    for champion in champions:
        if not isinstance(champion, Mapping):
            raise SnapshotValidationError("champions 元素必须是对象")
        champion_id = str(champion.get("id", "")).strip()
        champion_name = str(champion.get("name", "")).strip()
        if not champion_id or not champion_name:
            raise SnapshotValidationError("champion 必须包含非空 id 和 name")
        if champion_id in champion_ids:
            raise SnapshotValidationError(f"champion id 重复：{champion_id}")
        champion_ids.add(champion_id)

    if not champion_ids:
        raise SnapshotValidationError("generation 至少需要一个英雄")

    augment_ids: set[str] = set()
    detail_champion_ids: set[str] = set()
    stat_records = 0
    for detail in details.values():
        if not isinstance(detail, Mapping):
            raise SnapshotValidationError("英雄详情必须是对象")
        detail_champion_id = str(detail.get("hero_id") or "").strip()
        if not detail_champion_id:
            raise SnapshotValidationError("英雄详情必须包含非空 hero_id")
        detail_champion_ids.add(detail_champion_id)
        augments = detail.get("augments", [])
        if not isinstance(augments, list):
            raise SnapshotValidationError("英雄 augments 必须是列表")
        for augment in augments:
            if not isinstance(augment, Mapping) or augment.get("id") is None:
                raise SnapshotValidationError("augment 统计必须是包含 id 的对象")
            augment_ids.add(str(augment["id"]))
            stat_records += 1
    missing_details = champion_ids - detail_champion_ids
    if missing_details:
        raise SnapshotValidationError(f"英雄详情覆盖不完整：{sorted(missing_details)}")
    if stat_records <= 0:
        raise SnapshotValidationError("generation 不得发布 0 条英雄海克斯统计")
    hint_augments = hints.get("augments", {})
    if isinstance(hint_augments, Mapping):
        augment_ids.update(str(key) for key in hint_augments)
    return len(champions), len(augment_ids), stat_records


class DataSnapshotPublisher:
    """DataService 专用的 generation 发布器。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_snapshot_root()
        self.generations_dir = self.root / "generations"
        self.current_path = self.root / "current.v1.json"

    def publish(
        self,
        payloads: Mapping[str, Any],
        *,
        private_stats_enabled: bool,
        source_files: Sequence[Mapping[str, Any]] = (),
    ) -> DataSnapshotManifest:
        normalized = {role: payloads.get(role) for role in SNAPSHOT_ROLES}
        champion_count, augment_count, stat_record_count = _count_records(normalized)
        generation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{uuid.uuid4().hex[:10]}"
        staging = self.generations_dir / f".staging-{generation_id}"
        final = self.generations_dir / generation_id
        staging.mkdir(parents=True, exist_ok=False)
        promoted = False
        pointer_committed = False
        try:
            files: list[SnapshotFile] = []
            for role in SNAPSHOT_ROLES:
                filename = f"{role}.json"
                path = staging / filename
                atomic_write_json(path, normalized[role], ensure_ascii=False, separators=(",", ":"))
                files.append(SnapshotFile(role=role, relative_path=filename, size=path.stat().st_size, sha256=_sha256(path)))
            manifest = DataSnapshotManifest(
                schema_version=SNAPSHOT_SCHEMA_VERSION,
                generation_id=generation_id,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                private_stats_enabled=bool(private_stats_enabled),
                source_files=tuple(dict(item) for item in source_files),
                champion_count=champion_count,
                augment_count=augment_count,
                stat_record_count=stat_record_count,
                files=tuple(files),
            )
            atomic_write_json(staging / "manifest.json", asdict(manifest), ensure_ascii=False, indent=2)
            self._validate_generation(staging, manifest, allow_staging=True)
            staging.replace(final)
            promoted = True
            previous_id = self._current_generation_id()
            atomic_write_json(
                self.current_path,
                {
                    "schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "current_generation_id": generation_id,
                    "previous_generation_id": previous_id,
                },
                ensure_ascii=False,
                indent=2,
            )
            pointer_committed = True
            try:
                self._remove_unreferenced_generations({generation_id, previous_id})
            except OSError:
                logger.warning("快照旧 generation 清理失败，将在后续发布重试", exc_info=True)
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
        for path in self.generations_dir.iterdir():
            if path.is_dir() and not path.name.startswith(".staging-") and path.name not in keep:
                shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _validate_generation(
        directory: Path,
        manifest: DataSnapshotManifest,
        *,
        allow_staging: bool = False,
    ) -> None:
        _validate_manifest(directory, manifest, allow_staging=allow_staging)


def _safe_generation_file(directory: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        raise SnapshotValidationError(f"快照文件路径无效：{relative_path}")
    root = directory.resolve()
    candidate = (directory / relative_path).resolve()
    if root not in candidate.parents:
        raise SnapshotValidationError(f"快照文件路径越界：{relative_path}")
    return candidate


def _validate_manifest(
    directory: Path,
    manifest: DataSnapshotManifest,
    *,
    allow_staging: bool = False,
) -> None:
    if manifest.schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotValidationError(f"不支持的快照 schema：{manifest.schema_version}")
    expected_names = {manifest.generation_id}
    if allow_staging:
        expected_names.add(f".staging-{manifest.generation_id}")
    if directory.name not in expected_names:
        raise SnapshotValidationError("manifest generation_id 与目录不一致")
    if len(manifest.files) != len(SNAPSHOT_ROLES) or {item.role for item in manifest.files} != set(SNAPSHOT_ROLES):
        raise SnapshotValidationError("manifest 文件角色不完整")
    for item in manifest.files:
        if isinstance(item.size, bool) or not isinstance(item.size, int) or item.size < 0:
            raise SnapshotValidationError("manifest 文件大小必须是非负整数")
        if not isinstance(item.sha256, str) or len(item.sha256) != 64:
            raise SnapshotValidationError("manifest 文件 sha256 格式无效")
        path = _safe_generation_file(directory, item.relative_path)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise SnapshotValidationError(f"快照文件缺失：{item.relative_path}") from exc
        if size != item.size or _sha256(path) != item.sha256:
            raise SnapshotValidationError(f"快照文件校验失败：{item.relative_path}")
    relative_paths = [item.relative_path for item in manifest.files]
    if len(relative_paths) != len(set(relative_paths)):
        raise SnapshotValidationError("manifest 快照文件路径重复")
    for source in manifest.source_files:
        if not isinstance(source.get("name"), str) or not source.get("name"):
            raise SnapshotValidationError("source_files.name 必须是非空字符串")
        source_size = source.get("size")
        record_count = source.get("record_count")
        if (
            isinstance(source_size, bool)
            or not isinstance(source_size, int)
            or source_size < 0
            or isinstance(record_count, bool)
            or not isinstance(record_count, int)
            or record_count < 0
        ):
            raise SnapshotValidationError("source_files 计数不能为负数")
        sha256 = str(source.get("sha256", ""))
        if sha256 and (len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256.lower())):
            raise SnapshotValidationError("source_files.sha256 格式无效")


def _validate_manifest_counts(manifest: DataSnapshotManifest, payloads: Mapping[str, Any]) -> None:
    actual = _count_records(payloads)
    expected = (manifest.champion_count, manifest.augment_count, manifest.stat_record_count)
    if actual != expected:
        raise SnapshotValidationError(f"manifest 计数与快照内容不一致：expected={expected} actual={actual}")


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
            "private_stats_enabled": self.manifest.private_stats_enabled,
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

        from hextech.overlay.hints import normalize_augment_id, normalize_augment_name

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

    def payloads_for_policy_transition(self, *, private_stats_enabled: bool) -> dict[str, Any]:
        """供 DataService 切换隐私策略时重发固定代；消费者不得修改原视图。"""

        payloads = deepcopy(dict(self._payloads))
        if private_stats_enabled:
            return payloads
        hints = payloads.get("overlay_hints")
        if not isinstance(hints, dict):
            return payloads
        source = hints.get("source")
        if isinstance(source, dict):
            source["private_policy_stats_enabled"] = False
        private_fields = {
            "rank",
            "score",
            "winrate",
            "pickrate",
            "stats_by_champion_id",
            "stats_by_champion_name",
        }
        hint_map = hints.get("hints")
        if isinstance(hint_map, dict):
            for hint in hint_map.values():
                if isinstance(hint, dict):
                    for field in private_fields:
                        hint.pop(field, None)
        return payloads


class DataSnapshotClient:
    """桌面、Web 与 Overlay 共用的只读 generation 客户端。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_snapshot_root()
        self._loaded: tuple[str, DataSnapshotManifest, dict[str, Any], bool, str] | None = None

    def _pointer(self) -> Mapping[str, Any]:
        payload = _read_json(self.root / "current.v1.json")
        if not isinstance(payload, Mapping):
            raise SnapshotValidationError("current 指针不是对象")
        return payload

    def _load_generation(self, generation_id: str) -> tuple[DataSnapshotManifest, dict[str, Any]]:
        if not _GENERATION_ID_RE.fullmatch(generation_id):
            raise SnapshotValidationError("generation_id 格式无效")
        directory = self.root / "generations" / generation_id
        manifest = DataSnapshotManifest.from_mapping(_read_json(directory / "manifest.json"))
        _validate_manifest(directory, manifest)
        payloads: dict[str, Any] = {}
        for item in manifest.files:
            payloads[item.role] = _read_json(_safe_generation_file(directory, item.relative_path))
        _validate_manifest_counts(manifest, payloads)
        return manifest, payloads

    def _load(self) -> tuple[DataSnapshotManifest, dict[str, Any], bool, str]:
        pointer = self._pointer()
        current_id = str(pointer.get("current_generation_id") or "")
        previous_id = str(pointer.get("previous_generation_id") or "")
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
