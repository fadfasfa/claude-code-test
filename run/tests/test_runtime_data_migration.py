"""测试 运行态数据迁移。

调用方: pytest; 关键依赖: tools.migrate_runtime_data。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from unittest import mock
import json
import os
from pathlib import Path


class RuntimeDataMigrationTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        run_dir = root / "run"
        raw_hextech = run_dir / "data" / "raw" / "hextech"
        raw_synergy = run_dir / "data" / "raw" / "synergy"
        processed = run_dir / "data" / "processed"
        state = run_dir / "data" / "runtime" / "state"
        for path in (raw_hextech, raw_synergy, processed, state, run_dir / "data" / "seed" / "startup"):
            path.mkdir(parents=True)
        (raw_hextech / "Hextech_Data_2099-01-01.csv").write_text("英雄ID,英雄名称\n1,A\n", encoding="utf-8")
        (raw_hextech / "backups").mkdir()
        (raw_hextech / "backups" / "Hextech_Data_2099-01-01.backup-20990102-010101.csv").write_text(
            "英雄ID,英雄名称\n1,A\n",
            encoding="utf-8",
        )
        (raw_synergy / "Champion_Synergy_20990101_010101.json").write_text("{}", encoding="utf-8")
        (raw_synergy / "Champion_Synergy_latest.v1.json").write_text(
            '{"version":1,"filename":"Champion_Synergy_20990101_010101.json"}',
            encoding="utf-8",
        )
        (processed / "cache.json").write_text('{"ok":true}', encoding="utf-8")
        (state / "auth_token.txt").write_text("do-not-read", encoding="utf-8")
        return run_dir

    def test_dry_run_writes_manifest_without_moving_files(self):
        from tools.migrate_runtime_data import execute_migration

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = self._fixture(root)
            artifact_root = root / ".artifacts"

            manifest = execute_migration(base_dir=run_dir, artifact_root=artifact_root, apply=False)

            self.assertEqual(manifest["mode"], "dry-run")
            self.assertTrue((run_dir / "data" / "raw" / "hextech" / "Hextech_Data_2099-01-01.csv").exists())
            self.assertTrue((Path(manifest["output_dir"]) / "migration_manifest.v1.json").exists())
            self.assertTrue(any(entry["category"] == "startup_snapshot/hextech" for entry in manifest["entries"]))
            self.assertTrue(any(item["path"].endswith("auth_token.txt") for item in manifest["sensitive_excluded"]))
            self.assertFalse(any("sha256" in item for item in manifest["sensitive_excluded"]))

    def test_apply_backs_up_moves_and_supports_manifest_rollback(self):
        from tools.migrate_runtime_data import execute_migration

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = self._fixture(root)
            artifact_root = root / ".artifacts"

            manifest = execute_migration(base_dir=run_dir, artifact_root=artifact_root, apply=True)
            output_dir = Path(manifest["output_dir"])
            moved_entry = next(entry for entry in manifest["entries"] if entry["category"] == "raw/hextech")
            source = run_dir / moved_entry["source"]
            target = run_dir / moved_entry["target"]
            backup = output_dir / moved_entry["backup"]

            self.assertFalse(source.exists())
            self.assertTrue(target.exists())
            self.assertTrue(backup.exists())
            self.assertTrue((run_dir / "data" / "seed" / "startup" / "hextech" / "Hextech_Data_2099-01-01.csv").exists())
            self.assertFalse(
                (
                    run_dir
                    / "data"
                    / "seed"
                    / "startup"
                    / "hextech"
                    / "Hextech_Data_2099-01-01.backup-20990102-010101.csv"
                ).exists()
            )
            self.assertTrue(
                (
                    run_dir
                    / "data"
                    / "runtime"
                    / "raw"
                    / "hextech"
                    / "backups"
                    / "Hextech_Data_2099-01-01.backup-20990102-010101.csv"
                ).exists()
            )
            self.assertTrue((run_dir / "data" / "processed" / "MIGRATED_TO_RUNTIME.txt").exists())

            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))
            self.assertTrue(source.exists())
            self.assertFalse(target.exists())


class RuntimeBundleSeedTests(unittest.TestCase):
    def _seed(
        self,
        root: Path,
        *,
        manifest: dict,
        bundled_files: dict[str, str],
        runtime_files: dict[str, str],
    ) -> tuple[Path, Path]:
        from tools.runtime_bundle import seed_bundled_resources

        bundle_root = root / "bundle"
        runtime_root = root / "runtime"
        bundle_root.mkdir()
        (bundle_root / "bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        for relative_name, content in bundled_files.items():
            path = bundle_root / relative_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for relative_name, content in runtime_files.items():
            path = runtime_root / relative_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        seed_bundled_resources(
            bundle_root=bundle_root,
            runtime_static_dir=runtime_root / "static",
            runtime_index_dir=runtime_root / "indexes",
            runtime_asset_dir=runtime_root / "assets",
            runtime_hextech_dir=runtime_root / "hextech",
            runtime_synergy_dir=runtime_root / "synergy",
        )
        return bundle_root, runtime_root

    def test_timestamp_snapshot_is_only_copied_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative_name = "data/seed/startup/hextech/Hextech_Data_2026-07-05.csv"
            bundle_root = root / "bundle"
            runtime_target = root / "runtime" / "hextech" / "Hextech_Data_2026-07-05.csv"
            bundle_source = bundle_root / relative_name
            bundle_source.parent.mkdir(parents=True)
            bundle_source.write_text("bundled-old", encoding="utf-8")
            runtime_target.parent.mkdir(parents=True)
            runtime_target.write_text("runtime-new", encoding="utf-8")
            os.utime(bundle_source, (5000, 5000))
            os.utime(runtime_target, (1000, 1000))
            manifest = {
                "static_files": [],
                "index_files": [],
                "asset_files": [],
                "hextech_snapshot_files": [relative_name],
                "synergy_data_files": [],
                "synergy_data_file": "",
                "seed_metadata": {
                    relative_name: {"dataset_version": "2026-07-05", "sha256": "unused"}
                },
            }
            (bundle_root / "bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            from tools.runtime_bundle import seed_bundled_resources

            seed_bundled_resources(
                bundle_root=bundle_root,
                runtime_static_dir=root / "runtime" / "static",
                runtime_index_dir=root / "runtime" / "indexes",
                runtime_asset_dir=root / "runtime" / "assets",
                runtime_hextech_dir=runtime_target.parent,
            )

            self.assertEqual(runtime_target.read_text(encoding="utf-8"), "runtime-new")

    def test_verified_generation_is_seeded_before_current_pointer(self):
        import hashlib
        from hextech.data_snapshot import DataSnapshotClient, DataSnapshotPublisher
        from tools import runtime_bundle

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source-snapshots"
            published = DataSnapshotPublisher(source_root).publish(
                {
                    "champions": [{"id": "1", "name": "英雄一"}],
                    "champion_hextech": {"英雄一": {"hero_id": "1", "augments": [{"id": "a1"}]}},
                    "overlay_hints": {"augments": {"a1": {"name": "强化一"}}},
                    "identities": {"champions": {"1": "英雄一"}, "augments": {"a1": "强化一"}},
                },
                private_stats_enabled=True,
                source_files=(
                    {
                        "name": "Champion_Synergy_Cleaned.json",
                        "size": 2,
                        "sha256": "a" * 64,
                        "record_count": 1,
                    },
                ),
            )
            bundle_root = root / "bundle"
            snapshot_files: list[str] = []
            metadata: dict[str, dict[str, str]] = {}
            for source in source_root.rglob("*"):
                if not source.is_file():
                    continue
                relative = source.relative_to(source_root)
                bundled_name = (Path("data/seed/startup/snapshots") / relative).as_posix()
                target = bundle_root / bundled_name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                snapshot_files.append(bundled_name)
                metadata[bundled_name] = {"dataset_version": "legacy", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
            bundle_root.mkdir(exist_ok=True)
            (bundle_root / "bundle_manifest.json").write_text(
                json.dumps({"snapshot_seed_files": snapshot_files, "seed_metadata": metadata}),
                encoding="utf-8",
            )
            runtime_snapshot_root = root / "runtime" / "snapshots"

            runtime_state = root / "runtime" / "state"
            with mock.patch.object(
                runtime_bundle,
                "build_runtime_state_path",
                side_effect=lambda filename: str(runtime_state / filename),
            ):
                runtime_bundle.seed_bundled_resources(
                    bundle_root=bundle_root,
                    runtime_static_dir=root / "runtime" / "static",
                    runtime_index_dir=root / "runtime" / "indexes",
                    runtime_asset_dir=root / "runtime" / "assets",
                    runtime_snapshot_dir=runtime_snapshot_root,
                )

            status = DataSnapshotClient(runtime_snapshot_root).status()
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["generation_id"], published.generation_id)
            startup = json.loads((runtime_state / "startup_status.json").read_text(encoding="utf-8"))
            self.assertTrue(startup["hero_ready"])
            self.assertTrue(startup["hextech_ready"])
            self.assertTrue(startup["synergy_ready"])
            self.assertEqual(startup["data_snapshot"]["state"], "ready")
            self.assertEqual(startup["data_snapshot"]["generation_id"], published.generation_id)
            self.assertEqual(startup["data_snapshot"]["source"], "verified_bundle_seed")

    def test_stable_latest_promotes_newer_dataset_version_ignoring_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative_name = "data/seed/startup/synergy/Champion_Synergy_latest.v1.json"
            bundled = json.dumps(
                {"version": 1, "filename": "Champion_Synergy_20260710_010101.json"}
            )
            runtime = json.dumps(
                {"version": 1, "filename": "Champion_Synergy_20260709_230000.json"}
            )
            import hashlib

            manifest = {
                "static_files": [],
                "index_files": [],
                "asset_files": [],
                "hextech_snapshot_files": [],
                "synergy_data_files": [relative_name],
                "synergy_data_file": relative_name,
                "seed_metadata": {
                    relative_name: {
                        "dataset_version": "20260710_010101",
                        "sha256": hashlib.sha256(bundled.encode()).hexdigest(),
                    }
                },
            }
            _, runtime_root = self._seed(
                root,
                manifest=manifest,
                bundled_files={relative_name: bundled},
                runtime_files={"synergy/Champion_Synergy_latest.v1.json": runtime},
            )

            self.assertEqual(
                json.loads(
                    (runtime_root / "synergy" / "Champion_Synergy_latest.v1.json").read_text(encoding="utf-8")
                )["filename"],
                "Champion_Synergy_20260710_010101.json",
            )

    def test_stable_latest_never_replaces_newer_runtime_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative_name = "data/seed/startup/synergy/Champion_Synergy_latest.v1.json"
            bundled = json.dumps(
                {"version": 1, "filename": "Champion_Synergy_20260709_230000.json"}
            )
            runtime = json.dumps(
                {"version": 1, "filename": "Champion_Synergy_20260710_010101.json"}
            )
            import hashlib

            manifest = {
                "static_files": [],
                "index_files": [],
                "asset_files": [],
                "hextech_snapshot_files": [],
                "synergy_data_files": [relative_name],
                "synergy_data_file": relative_name,
                "seed_metadata": {
                    relative_name: {
                        "dataset_version": "20260709_230000",
                        "sha256": hashlib.sha256(bundled.encode()).hexdigest(),
                    }
                },
            }
            _, runtime_root = self._seed(
                root,
                manifest=manifest,
                bundled_files={relative_name: bundled},
                runtime_files={"synergy/Champion_Synergy_latest.v1.json": runtime},
            )

            self.assertEqual(
                json.loads(
                    (runtime_root / "synergy" / "Champion_Synergy_latest.v1.json").read_text(encoding="utf-8")
                )["filename"],
                "Champion_Synergy_20260710_010101.json",
            )

    def test_legacy_manifest_only_fills_missing_latest_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative_name = "data/seed/startup/synergy/Champion_Synergy_latest.v1.json"
            _, runtime_root = self._seed(
                root,
                manifest={
                    "static_files": [],
                    "index_files": [],
                    "asset_files": [],
                    "hextech_snapshot_files": [],
                    "synergy_data_files": [relative_name],
                    "synergy_data_file": relative_name,
                },
                bundled_files={relative_name: '{"filename":"bundled.json"}'},
                runtime_files={
                    "synergy/Champion_Synergy_latest.v1.json": '{"filename":"runtime.json"}'
                },
            )

            self.assertEqual(
                json.loads(
                    (runtime_root / "synergy" / "Champion_Synergy_latest.v1.json").read_text(encoding="utf-8")
                )["filename"],
                "runtime.json",
            )


if __name__ == "__main__":
    unittest.main()
