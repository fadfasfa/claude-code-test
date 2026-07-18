"""Catalog、来源 current 与 generation 的可恢复 promotion journal。

Windows 不提供跨多个 JSON 文件的原子替换，因此 DataService 在切换依赖前先保存
完整旧、新指针。generation 始终最后提交；异常退出后可据 journal 整体回滚或
向前完成，消费者不会自行拼接半套 current。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping

from hextech.contracts import PromotionJournalPhase, PromotionJournalV1, utc_now_iso
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir


COHORT_ROLES = ("catalog", "hextech", "apex", "mayhem", "generation")


class CohortPromotionError(RuntimeError):
    """promotion journal 或目标 pointer 无法安全恢复。"""


class CohortPromotionStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else get_var_dir()
        self.journal_path = self.root / "state" / "data-service" / "promotion_journal.v1.json"

    def pointer_path(self, role: str) -> Path:
        if role == "catalog":
            return self.root / "catalog" / "current.v2.json"
        if role in {"hextech", "apex", "mayhem"}:
            return self.root / "sources" / role / "current.v2.json"
        if role == "generation":
            return self.root / "snapshots" / "current.v2.json"
        raise CohortPromotionError(f"未知 cohort role：{role}")

    def _previous_generation_path(self) -> Path:
        return self.root / "snapshots" / "previous.v2.json"

    @staticmethod
    def _read_object(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CohortPromotionError(f"pointer 无法读取：{path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CohortPromotionError(f"pointer 必须是对象：{path}")
        return payload

    def _read_role(self, role: str) -> dict[str, Any]:
        if role != "generation":
            return self._read_object(self.pointer_path(role))
        return {
            "current": self._read_object(self.pointer_path(role)),
            "previous": self._read_object(self._previous_generation_path()),
        }

    @staticmethod
    def _restore_file(path: Path, payload: Mapping[str, Any]) -> None:
        if payload:
            atomic_write_json(path, dict(payload), ensure_ascii=False, indent=2)
        else:
            path.unlink(missing_ok=True)

    def _write_role(self, role: str, payload: Mapping[str, Any]) -> None:
        if role != "generation":
            self._restore_file(self.pointer_path(role), payload)
            return
        current = payload.get("current")
        previous = payload.get("previous")
        if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
            raise CohortPromotionError("generation journal 必须同时包含 current 和 previous")
        self._restore_file(self.pointer_path(role), current)
        self._restore_file(self._previous_generation_path(), previous)

    def _write_journal(self, journal: PromotionJournalV1) -> None:
        atomic_write_json(self.journal_path, journal.to_dict(), ensure_ascii=False, indent=2)

    def begin(self) -> PromotionJournalV1:
        if self.journal_path.exists():
            raise CohortPromotionError("存在未恢复的 promotion journal")
        old = {role: self._read_role(role) for role in COHORT_ROLES}
        journal = PromotionJournalV1(
            transaction_id=f"promotion-{uuid.uuid4().hex}",
            phase=PromotionJournalPhase.PREPARED,
            created_at=utc_now_iso(),
            old_pointers=old,
            target_pointers={role: dict(payload) for role, payload in old.items()},
        )
        self._write_journal(journal)
        return journal

    def load(self) -> PromotionJournalV1 | None:
        if not self.journal_path.is_file():
            return None
        payload = self._read_object(self.journal_path)
        try:
            phase = PromotionJournalPhase(str(payload["phase"]))
            return PromotionJournalV1(
                schema_version=payload["schema_version"],
                transaction_id=str(payload["transaction_id"]),
                phase=phase,
                created_at=str(payload["created_at"]),
                old_pointers=payload["old_pointers"],
                target_pointers=payload["target_pointers"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CohortPromotionError(f"promotion journal 无效：{exc}") from exc

    def record_target(self, role: str, pointer: Mapping[str, Any]) -> PromotionJournalV1:
        journal = self.load()
        if journal is None or role not in COHORT_ROLES:
            raise CohortPromotionError("没有可更新的 promotion journal")
        target = {key: dict(value) for key, value in journal.target_pointers.items()}
        target[role] = dict(pointer)
        updated = PromotionJournalV1(
            transaction_id=journal.transaction_id,
            phase=journal.phase,
            created_at=journal.created_at,
            old_pointers=journal.old_pointers,
            target_pointers=target,
        )
        self._write_journal(updated)
        return updated

    def capture_target(self, role: str) -> PromotionJournalV1:
        """把刷新器刚写出的真实 pointer 记录为本次 target。"""

        return self.record_target(role, self._read_role(role))

    def promote_dependencies(self) -> PromotionJournalV1:
        journal = self.load()
        if journal is None or journal.phase is not PromotionJournalPhase.PREPARED:
            raise CohortPromotionError("promotion 未处于 prepared")
        for role in ("catalog", "hextech", "apex", "mayhem"):
            self._write_role(role, journal.target_pointers[role])
        updated = PromotionJournalV1(
            transaction_id=journal.transaction_id,
            phase=PromotionJournalPhase.DEPENDENCIES_PROMOTED,
            created_at=journal.created_at,
            old_pointers=journal.old_pointers,
            target_pointers=journal.target_pointers,
        )
        self._write_journal(updated)
        return updated

    def record_generation_promoted(self) -> PromotionJournalV1:
        journal = self.load()
        if journal is None or journal.phase is not PromotionJournalPhase.DEPENDENCIES_PROMOTED:
            raise CohortPromotionError("dependencies 尚未 promotion")
        target = {key: dict(value) for key, value in journal.target_pointers.items()}
        target["generation"] = self._read_role("generation")
        updated = PromotionJournalV1(
            transaction_id=journal.transaction_id,
            phase=PromotionJournalPhase.GENERATION_PROMOTED,
            created_at=journal.created_at,
            old_pointers=journal.old_pointers,
            target_pointers=target,
        )
        self._write_journal(updated)
        return updated

    def commit(self) -> None:
        journal = self.load()
        if journal is None or journal.phase is not PromotionJournalPhase.GENERATION_PROMOTED:
            raise CohortPromotionError("generation 尚未 promotion")
        committed = PromotionJournalV1(
            transaction_id=journal.transaction_id,
            phase=PromotionJournalPhase.COMMITTED,
            created_at=journal.created_at,
            old_pointers=journal.old_pointers,
            target_pointers=journal.target_pointers,
        )
        self._write_journal(committed)
        self.journal_path.unlink(missing_ok=True)

    def rollback(self) -> None:
        journal = self.load()
        if journal is None:
            return
        for role in COHORT_ROLES:
            self._write_role(role, journal.old_pointers[role])
        self.journal_path.unlink(missing_ok=True)

    def recover(self) -> str:
        journal = self.load()
        if journal is None:
            return "clean"
        if journal.phase in {PromotionJournalPhase.GENERATION_PROMOTED, PromotionJournalPhase.COMMITTED}:
            for role in COHORT_ROLES:
                self._write_role(role, journal.target_pointers[role])
            result = "rolled_forward"
        else:
            for role in COHORT_ROLES:
                self._write_role(role, journal.old_pointers[role])
            result = "rolled_back"
        self.journal_path.unlink(missing_ok=True)
        return result

    def consistent_pointers(self) -> dict[str, dict[str, Any]]:
        journal = self.load()
        if journal is not None and journal.phase is not PromotionJournalPhase.COMMITTED:
            return {key: dict(value) for key, value in journal.old_pointers.items()}
        return {role: self._read_role(role) for role in COHORT_ROLES}


__all__ = ["COHORT_ROLES", "CohortPromotionError", "CohortPromotionStore"]
