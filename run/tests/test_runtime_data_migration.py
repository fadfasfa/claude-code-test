from __future__ import annotations

import shutil
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
