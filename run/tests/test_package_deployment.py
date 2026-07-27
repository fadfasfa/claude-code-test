"""稳定安装目录部署测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _package(root: Path, marker: str = "new") -> Path:
    from tooling.build.manifest import RUNTIME_CONTRACT_VERSIONS

    internal = root / "_internal"
    internal.mkdir(parents=True)
    (root / "Hextech伴生终端.exe").write_text(marker, encoding="utf-8")
    (root / "启动 Hextech.bat").write_text("start", encoding="utf-8")
    (root / "README_首次使用.txt").write_text("guide", encoding="utf-8")
    (internal / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "build_id": "test-build",
                "runtime_contracts": RUNTIME_CONTRACT_VERSIONS,
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def _isolate_deployment_runtime(monkeypatch):
    """目录轮转测试不得启动或检查本机正在运行的正式客户端。"""

    from tooling.build import deploy

    real_start_install = deploy._start_install
    monkeypatch.setattr(deploy, "_start_install", lambda _executable: object())
    monkeypatch.setattr(
        deploy,
        "verify_deployment",
        lambda *_args, **_kwargs: {
            "desktop": 101,
            "data_service": 102,
            "supervisor": 103,
            "overlay_host": 104,
            "vision_sidecar": 105,
        },
    )
    return real_start_install


def test_deploy_release_promotes_verified_copy_and_keeps_one_previous(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    shortcut = tmp_path / "Hextech伴生终端.lnk"
    shortcut.write_text("shortcut", encoding="utf-8")
    shortcut_calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(deploy, "shutdown_existing_install", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(deploy, "converge_desktop_shortcuts", lambda *_args, **_kwargs: ())
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
    assert result.previous_dir is not None
    assert (result.previous_dir / "old.txt").read_text(encoding="utf-8") == "old"
    assert shortcut_calls == [(shortcut, target / "Hextech伴生终端.exe")]
    assert result.removed_shortcuts == ()
    assert result.restarted is False
    assert result.started is True
    assert result.verified is True
    assert dict(result.process_ids) == {
        "desktop": 101,
        "data_service": 102,
        "supervisor": 103,
        "overlay_host": 104,
        "vision_sidecar": 105,
    }
    assert result.build_id == "test-build"


def test_deploy_starts_stable_install_and_verifies_even_when_no_old_process_exists(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(deploy, "shutdown_existing_install", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        deploy,
        "_start_install",
        lambda executable: events.append(("start", executable)) or object(),
    )

    def verify(executable: Path, **kwargs):
        events.append(("verify", (executable, kwargs["expected_build_id"])))
        return {
            "desktop": 1,
            "data_service": 2,
            "supervisor": 3,
            "overlay_host": 4,
            "vision_sidecar": 5,
        }

    monkeypatch.setattr(deploy, "verify_deployment", verify)

    result = deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert events[0] == ("start", target / deploy.APP_EXE_NAME)
    assert events[1] == ("verify", (target / deploy.APP_EXE_NAME, "test-build"))
    assert result.started is True
    assert result.verified is True


def test_windows_start_install_delegates_to_user_explorer(tmp_path, monkeypatch, _isolate_deployment_runtime):
    from tooling.build import deploy

    executable = tmp_path / "HextechCompanion" / deploy.APP_EXE_NAME
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(deploy.os, "name", "nt")
    monkeypatch.setattr(
        deploy.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or object(),
    )

    _isolate_deployment_runtime(executable)

    assert calls == [
        (
            ["explorer.exe", str(executable)],
            {
                "cwd": str(executable.parent),
                "creationflags": int(deploy.subprocess.CREATE_NEW_PROCESS_GROUP),
            },
        )
    ]


@pytest.mark.parametrize(
    ("name", "executable", "command_line"),
    [
        ("Hextech伴生终端.exe", r"C:\\HextechCompanion.previous\\Hextech伴生终端.exe", ()),
        ("python.exe", r"C:\\Python311\\python.exe", ("python", "-m", "hextech.bootstrap.overlay")),
        (
            "python.exe",
            r"C:\\Python311\\python.exe",
            ("python", "-m", "hextech.infrastructure.vision.sidecar", "--loop"),
        ),
    ],
)
def test_deployment_process_matcher_includes_old_and_source_overlay_runtimes(name, executable, command_line):
    from tooling.build import deploy

    assert deploy._is_managed_runtime_process(
        name=name,
        executable=executable,
        command_line=command_line,
    )


def test_shutdown_existing_install_force_kills_every_matching_runtime(tmp_path, monkeypatch):
    from tooling.build import deploy

    identities = [
        deploy.ProcessIdentity(11, 1.0, executable=r"C:\\HextechCompanion.previous\\Hextech伴生终端.exe"),
        deploy.ProcessIdentity(
            12,
            2.0,
            executable=r"C:\\Python311\\python.exe",
            command_line=("python", "-m", "hextech.infrastructure.vision.sidecar"),
        ),
    ]
    scans = iter((identities, []))
    killed: list[int] = []
    monkeypatch.setattr(deploy, "_matching_deployment_processes", lambda: next(scans))
    monkeypatch.setattr(deploy, "_kill_process_tree", lambda identity: killed.append(identity.pid))
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)

    found = deploy.shutdown_existing_install(tmp_path / deploy.APP_EXE_NAME, timeout=1.0)

    assert found is True
    assert killed == [11, 12]


def test_deployment_process_validation_requires_one_stable_process_per_role(tmp_path, monkeypatch):
    from tooling.build import deploy

    executable = tmp_path / "HextechCompanion" / deploy.APP_EXE_NAME
    role_commands = {
        "desktop": (str(executable),),
        "data_service": (str(executable), "--data-service"),
        "supervisor": (str(executable), "--runtime-supervisor"),
        "overlay_host": (str(executable), "--game-overlay"),
        "vision_sidecar": (str(executable), "--overlay-sidecar"),
    }
    identities = [
        deploy.ProcessIdentity(
            pid=200 + index,
            create_time=float(index),
            executable=str(executable),
            command_line=command_line,
        )
        for index, command_line in enumerate(role_commands.values())
    ]
    monkeypatch.setattr(deploy, "_matching_deployment_processes", lambda: identities)

    errors, role_pids = deploy._deployment_process_errors(executable)

    assert errors == []
    assert set(role_pids) == set(role_commands)

    identities.append(
        deploy.ProcessIdentity(
            pid=999,
            create_time=999.0,
            executable=str(tmp_path / "HextechCompanion.previous" / deploy.APP_EXE_NAME),
            command_line=(deploy.APP_EXE_NAME, "--game-overlay"),
        )
    )
    errors, _role_pids = deploy._deployment_process_errors(executable)
    assert any("非稳定目录" in error for error in errors)


def test_runtime_build_validation_requires_fresh_matching_states_and_sidecar_pid(tmp_path, monkeypatch):
    from tooling.build import deploy

    monkeypatch.setattr(deploy, "_packaged_var_dir", lambda: tmp_path)
    for relative_path, schema_version in deploy.RUNTIME_BUILD_STATE_SPECS:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": schema_version, "build_id": "build-new"}
        if relative_path.name == "game_overlay_sidecar_status.json":
            payload.update({"status": "running", "pid": 505})
        path.write_text(json.dumps(payload), encoding="utf-8")

    errors = deploy._runtime_build_errors(
        expected_build_id="build-new",
        launch_started_at=0.0,
        sidecar_pid=505,
    )
    assert errors == []

    mismatch_path = tmp_path / "state" / "game_overlay_visibility.v1.json"
    os.utime(mismatch_path, (1.0, 1.0))
    errors = deploy._runtime_build_errors(
        expected_build_id="build-new",
        launch_started_at=100.0,
        sidecar_pid=505,
    )
    assert any("本次启动刷新" in error for error in errors)

    mismatch = json.loads(mismatch_path.read_text(encoding="utf-8"))
    mismatch["build_id"] = "build-old"
    mismatch_path.write_text(json.dumps(mismatch), encoding="utf-8")
    errors = deploy._runtime_build_errors(
        expected_build_id="build-new",
        launch_started_at=0.0,
        sidecar_pid=505,
    )
    assert any("Build ID 不一致" in error for error in errors)


def test_deploy_rolls_back_when_post_start_verification_fails(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    start_calls: list[Path] = []
    monkeypatch.setattr(deploy, "shutdown_existing_install", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(deploy, "_start_install", lambda executable: start_calls.append(executable) or object())
    monkeypatch.setattr(
        deploy,
        "verify_deployment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(deploy.DeploymentError("build id mismatch")),
    )

    with pytest.raises(deploy.DeploymentError, match="build id mismatch"):
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert start_calls == [target / deploy.APP_EXE_NAME, target / deploy.APP_EXE_NAME]


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


def test_previous_cleanup_failure_rolls_back_current_install(tmp_path, monkeypatch):
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
    monkeypatch.setattr(deploy, "shutdown_existing_install", lambda *_args, **_kwargs: False)

    with pytest.raises(OSError, match="previous is busy"):
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (previous / "older.txt").read_text(encoding="utf-8") == "older"


def test_partial_previous_cleanup_restores_verified_backup(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    previous = tmp_path / "HextechCompanion.previous"
    previous.mkdir()
    (previous / "older.txt").write_text("older", encoding="utf-8")
    real_remove_tree = deploy._remove_tree
    partial_failure = True

    def remove_previous_partially(path: Path) -> None:
        nonlocal partial_failure
        if path == previous and partial_failure:
            partial_failure = False
            (previous / "older.txt").unlink()
            raise OSError("previous cleanup partially failed")
        real_remove_tree(path)

    monkeypatch.setattr(deploy, "_remove_tree", remove_previous_partially)
    monkeypatch.setattr(deploy, "shutdown_existing_install", lambda *_args, **_kwargs: False)

    with pytest.raises(OSError, match="previous cleanup partially failed"):
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (previous / "older.txt").read_text(encoding="utf-8") == "older"
    assert not list(tmp_path.glob(".HextechCompanion.previous-backup-*"))


def test_previous_rotation_rename_failure_restores_install_and_previous(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    previous = tmp_path / "HextechCompanion.previous"
    previous.mkdir()
    (previous / "older.txt").write_text("older", encoding="utf-8")
    real_replace = deploy.os.replace

    def fail_previous_rotation(source_path: Path, target_path: Path) -> None:
        if source_path.name.startswith(".HextechCompanion.rollback-") and target_path == previous:
            raise OSError("previous rotation is busy")
        real_replace(source_path, target_path)

    monkeypatch.setattr(deploy, "shutdown_existing_install", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(deploy.os, "replace", fail_previous_rotation)

    with pytest.raises(OSError, match="previous rotation is busy"):
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (previous / "older.txt").read_text(encoding="utf-8") == "older"
    assert not list(tmp_path.glob(".HextechCompanion.previous-backup-*"))


def test_previous_backup_copy_failure_cleans_partial_backup(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    previous = tmp_path / "HextechCompanion.previous"
    previous.mkdir()
    (previous / "older.txt").write_text("older", encoding="utf-8")
    real_copytree = deploy.shutil.copytree

    def fail_after_partial_previous_copy(source_path: Path, target_path: Path, *args, **kwargs):
        copied = real_copytree(source_path, target_path, *args, **kwargs)
        if source_path == previous:
            raise OSError("previous backup copy failed")
        return copied

    monkeypatch.setattr(deploy, "shutdown_existing_install", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(deploy.shutil, "copytree", fail_after_partial_previous_copy)

    with pytest.raises(OSError, match="previous backup copy failed"):
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (previous / "older.txt").read_text(encoding="utf-8") == "older"
    assert not list(tmp_path.glob(".HextechCompanion.previous-backup-*"))


def test_shutdown_failure_keeps_existing_previous_directory(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "release")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    previous = tmp_path / "HextechCompanion.previous"
    previous.mkdir()
    (previous / "older.txt").write_text("older", encoding="utf-8")
    monkeypatch.setattr(
        deploy,
        "shutdown_existing_install",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(deploy.DeploymentError("old client is still running")),
    )

    with pytest.raises(deploy.DeploymentError, match="old client is still running"):
        deploy.deploy_release(source, target, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (previous / "older.txt").read_text(encoding="utf-8") == "older"


def test_update_shortcut_refuses_to_create_missing_file(tmp_path):
    from tooling.build import deploy

    missing = tmp_path / "missing.lnk"

    with pytest.raises(deploy.DeploymentError, match="拒绝创建"):
        deploy.update_shortcut(missing, tmp_path / "Hextech伴生终端.exe")

    assert not missing.exists()


def test_converge_desktop_shortcuts_removes_only_managed_targets(tmp_path, monkeypatch):
    from tooling.build import deploy

    one_drive = tmp_path / "OneDrive" / "Desktop"
    local_desktop = tmp_path / "User" / "Desktop"
    public_desktop = tmp_path / "Public" / "Desktop"
    for root in (one_drive, local_desktop, public_desktop):
        root.mkdir(parents=True)

    canonical = one_drive / deploy.APP_SHORTCUT_NAME
    stable_duplicate = local_desktop / "Hextech伴生终端.exe - 快捷方式.lnk"
    current_release_duplicate = public_desktop / "Hextech 当前版本.lnk"
    old_release_duplicate = local_desktop / "旧 Hextech.lnk"
    unrelated_same_name = public_desktop / "Hextech伴生终端 (测试环境).lnk"
    unrelated = public_desktop / "其他程序.lnk"
    nested_duplicate = one_drive / "folder" / "Hextech伴生终端.lnk"
    nested_duplicate.parent.mkdir()
    for path in (
        canonical,
        stable_duplicate,
        current_release_duplicate,
        old_release_duplicate,
        unrelated_same_name,
        unrelated,
        nested_duplicate,
    ):
        path.write_text("shortcut", encoding="utf-8")

    stable_exe = tmp_path / "HextechCompanion" / deploy.APP_EXE_NAME
    releases = tmp_path / ".artifacts" / "hextech" / "releases"
    current_release_exe = releases / "HextechCompanion-20260720" / deploy.APP_EXE_NAME
    old_release_exe = releases / "HextechCompanion-20260719" / deploy.APP_EXE_NAME
    targets = {
        stable_duplicate.resolve(): Path(str(stable_exe).upper()),
        current_release_duplicate.resolve(): current_release_exe,
        old_release_duplicate.resolve(): old_release_exe,
        unrelated_same_name.resolve(): tmp_path / "Other" / deploy.APP_EXE_NAME,
        unrelated.resolve(): tmp_path / "Other" / "Other.exe",
        nested_duplicate.resolve(): stable_exe,
    }
    monkeypatch.setattr(deploy, "_desktop_roots", lambda _canonical: (one_drive, local_desktop, public_desktop))
    monkeypatch.setattr(deploy, "_read_shortcut_target", lambda path: targets[path.resolve()])

    removed = deploy.converge_desktop_shortcuts(canonical, stable_exe, releases)

    assert set(removed) == {
        stable_duplicate.resolve(),
        current_release_duplicate.resolve(),
        old_release_duplicate.resolve(),
    }
    assert canonical.exists()
    assert unrelated_same_name.exists()
    assert unrelated.exists()
    assert nested_duplicate.exists()


def test_desktop_roots_include_known_user_and_public_desktops_once(tmp_path, monkeypatch):
    from tooling.build import deploy

    known_desktop = tmp_path / "OneDrive" / "Desktop"
    user_profile = tmp_path / "User"
    public_profile = tmp_path / "Public"
    for root in (known_desktop, user_profile / "Desktop", public_profile / "Desktop"):
        root.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    monkeypatch.setenv("PUBLIC", str(public_profile))

    roots = deploy._desktop_roots(known_desktop / deploy.APP_SHORTCUT_NAME)

    assert roots == (known_desktop.resolve(), (user_profile / "Desktop").resolve(), (public_profile / "Desktop").resolve())


def test_converge_desktop_shortcuts_rejects_unreadable_suspicious_link(tmp_path, monkeypatch):
    from tooling.build import deploy

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    canonical = desktop / deploy.APP_SHORTCUT_NAME
    suspicious = desktop / "Hextech伴生终端.exe - 快捷方式.lnk"
    canonical.write_text("canonical", encoding="utf-8")
    suspicious.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(deploy, "_desktop_roots", lambda _canonical: (desktop,))
    monkeypatch.setattr(
        deploy,
        "_read_shortcut_target",
        lambda _path: (_ for _ in ()).throw(deploy.DeploymentError("unreadable")),
    )

    with pytest.raises(deploy.DeploymentError, match="unreadable"):
        deploy.converge_desktop_shortcuts(
            canonical,
            tmp_path / "HextechCompanion" / deploy.APP_EXE_NAME,
            tmp_path / "releases",
        )

    assert canonical.exists()
    assert suspicious.exists()


def test_converge_desktop_shortcuts_rejects_reparse_point(tmp_path, monkeypatch):
    from tooling.build import deploy

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    canonical = desktop / deploy.APP_SHORTCUT_NAME
    duplicate = desktop / "Hextech伴生终端 (2).lnk"
    canonical.write_text("canonical", encoding="utf-8")
    duplicate.write_text("duplicate", encoding="utf-8")
    stable_exe = tmp_path / "HextechCompanion" / deploy.APP_EXE_NAME
    monkeypatch.setattr(deploy, "_desktop_roots", lambda _canonical: (desktop,))
    monkeypatch.setattr(deploy, "_is_reparse_point", lambda path: path == duplicate)

    with pytest.raises(deploy.DeploymentError, match="reparse point"):
        deploy.converge_desktop_shortcuts(canonical, stable_exe, tmp_path / "releases")

    assert duplicate.exists()


def test_shortcut_cleanup_failure_rolls_back_to_existing_install(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "HextechCompanion-20260720")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    shortcut = tmp_path / deploy.APP_SHORTCUT_NAME
    shortcut.write_text("shortcut", encoding="utf-8")
    monkeypatch.setattr(
        deploy,
        "converge_desktop_shortcuts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(deploy.DeploymentError("duplicate is busy")),
    )
    shutdown_calls: list[Path] = []
    monkeypatch.setattr(
        deploy,
        "shutdown_existing_install",
        lambda executable, **_kwargs: shutdown_calls.append(executable) or False,
    )

    with pytest.raises(deploy.DeploymentError, match="duplicate is busy"):
        deploy.deploy_release(
            source,
            target,
            shortcut_path=shortcut,
            lock_path=tmp_path / "deploy.lock",
        )

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (target / deploy.APP_EXE_NAME).exists()
    # 第一次停旧安装；快捷方式收敛失败后，回滚前还会确认候选安装未在运行。
    assert shutdown_calls == [target / deploy.APP_EXE_NAME, target / deploy.APP_EXE_NAME]


def test_shutdown_failure_does_not_remove_existing_duplicate_shortcuts(tmp_path, monkeypatch):
    from tooling.build import deploy

    source = _package(tmp_path / "HextechCompanion-20260720")
    target = tmp_path / "HextechCompanion"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    shortcut = tmp_path / deploy.APP_SHORTCUT_NAME
    shortcut.write_text("shortcut", encoding="utf-8")
    monkeypatch.setattr(
        deploy,
        "shutdown_existing_install",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(deploy.DeploymentError("old client is still running")),
    )
    monkeypatch.setattr(
        deploy,
        "converge_desktop_shortcuts",
        lambda *_args, **_kwargs: pytest.fail("shutdown failure removed desktop shortcuts"),
    )

    with pytest.raises(deploy.DeploymentError, match="old client is still running"):
        deploy.deploy_release(source, target, shortcut_path=shortcut, lock_path=tmp_path / "deploy.lock")

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"


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


def test_build_without_deploy_does_not_call_deployer(tmp_path, monkeypatch, capsys):
    from tooling.build import package

    monkeypatch.setattr(package, "cleanup", lambda: None)
    monkeypatch.setattr(package, "prepare_runtime_data_for_package", lambda **_kwargs: None)
    monkeypatch.setattr(package, "write_generated_manifest", lambda *_args, **_kwargs: tmp_path / "manifest.json")
    monkeypatch.setattr(package, "generate_version_info", lambda *_args, **_kwargs: tmp_path / "version.txt")
    monkeypatch.setattr(package, "build_exe", lambda *_args, **_kwargs: tmp_path / "exe")
    monkeypatch.setattr(
        package,
        "finalize_output",
        lambda _exe: (tmp_path / "HextechCompanion-20260720", tmp_path / "release.zip"),
    )
    monkeypatch.setattr(
        package,
        "deploy_release",
        lambda *_args, **_kwargs: pytest.fail("ordinary build called the deployer"),
    )

    package.main([])

    assert "候选未部署" in capsys.readouterr().out


def test_packaged_smoke_runs_overlay_self_check_before_desktop(tmp_path, monkeypatch):
    from tooling.acceptance import smoke_packaged_startup as smoke

    package_dir = _package(tmp_path / "HextechCompanion-20260720")
    events: list[str] = []

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(
        smoke,
        "_overlay_self_check",
        lambda *_args, **_kwargs: events.append("overlay") or {"ok": True},
    )
    monkeypatch.setattr(smoke, "_validate_windows_gui_subsystem", lambda _exe: None)
    monkeypatch.setattr(
        smoke.subprocess,
        "Popen",
        lambda *_args, **_kwargs: events.append("desktop") or FinishedProcess(),
    )
    monkeypatch.setattr(smoke, "_required_paths_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(smoke, "_read_port", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(smoke, "_terminate_process_tree", lambda _proc: None)

    result = smoke.run_smoke(package_dir, timeout_seconds=1)

    assert events == ["overlay", "desktop"]
    assert result["ok"] is False
    assert result["last_error"] == "进程提前退出：returncode=0"
