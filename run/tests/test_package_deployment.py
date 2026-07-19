"""稳定安装目录部署测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _package(root: Path, marker: str = "new") -> Path:
    internal = root / "_internal"
    internal.mkdir(parents=True)
    (root / "Hextech伴生终端.exe").write_text(marker, encoding="utf-8")
    (root / "启动 Hextech.bat").write_text("start", encoding="utf-8")
    (root / "README_首次使用.txt").write_text("guide", encoding="utf-8")
    (internal / "bundle_manifest.json").write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    return root


def test_deploy_release_promotes_verified_copy_and_keeps_one_previous(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    shortcut = tmp_path / "client.lnk"
    shortcut.write_text("shortcut", encoding="utf-8")
    shortcut_calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(deploy, "shutdown_existing_install", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        deploy,
        "update_shortcut",
        lambda path, exe: shortcut_calls.append((path, exe)) or path,
    )

    result = deploy.deploy_release(
        source,
        target,
        shortcut_path=shortcut,
        lock_path=tmp_path / "deploy.lock",
    )

    assert (target / "Hextech伴生终端.exe").read_text(encoding="utf-8") == "new"
    assert result.previous_dir == tmp_path / "HextechCompanion.previous"
    assert (result.previous_dir / "old.txt").read_text(encoding="utf-8") == "old"
    assert shortcut_calls == [(shortcut, target / "Hextech伴生终端.exe")]
    assert result.restarted is False


def test_deploy_validates_candidate_before_stopping_existing_install(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    events: list[str] = []
    real_copytree = deploy.shutil.copytree

    def copytree(*args, **kwargs):
        events.append("copy")
        return real_copytree(*args, **kwargs)

    monkeypatch.setattr(deploy.shutil, "copytree", copytree)
    monkeypatch.setattr(
        deploy,
        "_tree_fingerprint",
        lambda _root: events.append("fingerprint") or {"payload": (1, "hash")},
    )
    monkeypatch.setattr(
        deploy,
        "shutdown_existing_install",
        lambda *_args, **_kwargs: events.append("shutdown") or False,
    )

    deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert events[-1] == "shutdown"
    assert events.count("fingerprint") == 2
    assert max(index for index, event in enumerate(events) if event in {"copy", "fingerprint"}) < events.index("shutdown")


def test_deploy_rejects_missing_shortcut_before_copy_or_shutdown(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        deploy,
        "shutdown_existing_install",
        lambda *_args, **_kwargs: pytest.fail("missing shortcut stopped the existing install"),
    )
    monkeypatch.setattr(
        deploy.shutil,
        "copytree",
        lambda *_args, **_kwargs: pytest.fail("missing shortcut copied a candidate"),
    )

    with pytest.raises(deploy.DeploymentError, match="拒绝创建"):
        deploy.deploy_release(
            source,
            target,
            shortcut_path=tmp_path / "missing.lnk",
            lock_path=tmp_path / "deploy.lock",
        )

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"


def test_previous_cleanup_failure_does_not_stop_or_replace_current_install(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    previous = tmp_path / "HextechCompanion.previous"
    previous.mkdir()
    (previous / "older.txt").write_text("older", encoding="utf-8")
    real_remove_tree = deploy._remove_tree

    def fail_previous_cleanup(path: Path) -> None:
        if path == previous:
            raise OSError("previous is busy")
        real_remove_tree(path)

    monkeypatch.setattr(deploy, "_remove_tree", fail_previous_cleanup)
    monkeypatch.setattr(
        deploy,
        "shutdown_existing_install",
        lambda *_args, **_kwargs: pytest.fail("previous cleanup failure stopped the current install"),
    )

    with pytest.raises(OSError, match="previous is busy"):
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (previous / "older.txt").read_text(encoding="utf-8") == "older"


def test_update_shortcut_refuses_to_create_missing_file(tmp_path):
    from tooling.build import deploy

    missing = tmp_path / "missing.lnk"

    with pytest.raises(deploy.DeploymentError, match="拒绝创建"):
        deploy.update_shortcut(missing, tmp_path / "Hextech伴生终端.exe")

    assert not missing.exists()


def test_deploy_script_updates_only_canonical_existing_shortcut():
    script = (Path(__file__).parents[1] / "打包并部署Hextech.ps1").read_text(encoding="utf-8")

    assert "'Hextech伴生终端.lnk'" in script
    assert "'Hextech伴生终端.exe - 快捷方式.lnk'" not in script
    assert "既有快捷方式不存在，拒绝创建重复入口" in script
    assert "[Environment]::GetFolderPath('Desktop')" in script
    assert "$env:OneDrive" not in script


def test_deploy_release_rolls_back_when_restart_fails(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    shutdown_calls = 0

    def shutdown(*_args, **_kwargs):
        nonlocal shutdown_calls
        shutdown_calls += 1
        return shutdown_calls == 1

    monkeypatch.setattr(deploy, "shutdown_existing_install", shutdown)
    monkeypatch.setattr(deploy, "_start_install", lambda _exe: (_ for _ in ()).throw(deploy.DeploymentError("start failed")))

    with pytest.raises(deploy.DeploymentError, match="start failed"):
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (target / "Hextech伴生终端.exe").exists()
    assert not (tmp_path / "HextechCompanion.previous").exists()


def test_rollback_continues_after_new_install_shutdown_failure(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    shutdown_calls = 0
    start_calls = 0

    def shutdown(*_args, **_kwargs):
        nonlocal shutdown_calls
        shutdown_calls += 1
        if shutdown_calls == 1:
            return True
        raise OSError("new install is busy")

    def start(executable: Path):
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            raise deploy.DeploymentError("new start failed")
        assert executable == target / "Hextech伴生终端.exe"
        return object()

    monkeypatch.setattr(deploy, "shutdown_existing_install", shutdown)
    monkeypatch.setattr(deploy, "_start_install", start)

    with pytest.raises(deploy.DeploymentError, match="关闭新版本失败") as raised:
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert isinstance(raised.value.__cause__, deploy.DeploymentError)
    assert "new start failed" in str(raised.value.__cause__)
    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (target / "Hextech伴生终端.exe").exists()
    assert start_calls == 2


def test_deploy_rejects_arbitrary_target_directory(tmp_path):
    from tooling.build.deploy import DeploymentError, validate_install_dir

    with pytest.raises(DeploymentError, match="HextechCompanion"):
        validate_install_dir(tmp_path / "unrelated")


def test_deploy_rejects_package_with_runtime_state(tmp_path):
    from tooling.build.deploy import DeploymentError, validate_package_dir

    source = _package(tmp_path / "release")
    (source / "var").mkdir()

    with pytest.raises(DeploymentError, match="var"):
        validate_package_dir(source)


def test_build_deploy_arguments_are_explicit(monkeypatch):
    from tooling.build import package

    monkeypatch.setattr(package.sys, "platform", "win32")
    default_args = package.parse_build_args([])
    deploy_args = package.parse_build_args(["--deploy"])

    assert default_args.deploy is False
    assert default_args.install_dir is None
    assert deploy_args.deploy is True
    assert deploy_args.install_dir.name == "HextechCompanion"

    with pytest.raises(SystemExit):
        package.parse_build_args(["--shortcut", "client.lnk"])
