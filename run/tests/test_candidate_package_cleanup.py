from __future__ import annotations

from pathlib import Path

import pytest


def test_candidate_cleanup_removes_only_binary_copies(tmp_path: Path) -> None:
    from tooling.diagnostics.cleanup import cleanup_candidate_packages

    release = tmp_path / ".artifacts/hx/releases/HextechCompanion-20260914-rf8"
    release.mkdir(parents=True)
    archive = release.with_suffix(".zip")
    archive.write_bytes(b"zip")
    smoke_package = tmp_path / ".artifacts/rf8-smoke/HextechCompanion-20260914-rf8-175820"
    smoke_package.mkdir(parents=True)
    report = tmp_path / ".artifacts/rf8-smoke/result.json"
    report.write_text("{}", encoding="utf-8")
    user_data = tmp_path / ".artifacts/rf4-user/Local"
    user_data.mkdir(parents=True)

    removed = cleanup_candidate_packages(base_dir=tmp_path)

    assert set(removed) == {release, archive, smoke_package}
    assert not release.exists()
    assert not archive.exists()
    assert not smoke_package.exists()
    assert report.is_file()
    assert user_data.is_dir()


def test_candidate_shortcut_cleanup_is_exact_and_dry_run(tmp_path: Path) -> None:
    from tooling.diagnostics.cleanup import cleanup_obsolete_candidate_shortcut

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    candidate = desktop / "Hextech重构候选.lnk"
    candidate.write_bytes(b"shortcut")

    assert cleanup_obsolete_candidate_shortcut(candidate, dry_run=True) is True
    assert candidate.is_file()
    assert cleanup_obsolete_candidate_shortcut(candidate) is True
    assert not candidate.exists()

    stable = desktop / "Hextech伴生终端.lnk"
    stable.write_bytes(b"stable")
    with pytest.raises(ValueError, match="非候选桌面快捷方式"):
        cleanup_obsolete_candidate_shortcut(stable)
    assert stable.is_file()
