"""冻结包只依赖 bundle profile 选择运行态；所有测试仅使用临时目录。"""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest


RUN_ROOT = Path(__file__).resolve().parents[1]
PATHS_FILE = RUN_ROOT / "src/hextech/modules/data/ports/paths.py"
LOGGING_FILE = RUN_ROOT / "src/hextech/infrastructure/observability/logging.py"
SINGLE_INSTANCE_FILE = RUN_ROOT / "src/hextech/interfaces/desktop/single_instance.py"
PATHS_MODULE = "hextech.modules.data.ports.paths"


def _load_paths(monkeypatch, tmp_path: Path, *, profile="isolated_candidate", raw_manifest=None):
    bundle = tmp_path / "candidate" / "_internal"
    bundle.mkdir(parents=True, exist_ok=True)
    manifest = bundle / "bundle_manifest.json"
    if raw_manifest is not None:
        manifest.write_text(raw_manifest, encoding="utf-8")
    elif profile is not None:
        manifest.write_text(json.dumps({"runtime_profile": profile}), encoding="utf-8")
    executable = bundle.parent / "HextechNexus.exe"
    executable.touch()
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "installed-local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "installed-roaming"))
    monkeypatch.setenv("HEXTECH_VAR_DIR", str(tmp_path / "conflicting-env"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    return runpy.run_path(str(PATHS_FILE))


def _bind_paths(monkeypatch, namespace):
    module = ModuleType(PATHS_MODULE)
    module.__dict__.update(namespace)
    monkeypatch.setitem(sys.modules, PATHS_MODULE, module)
    return module


def _junction_or_symlink(target: Path, link: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_candidate_ignores_environment_and_cwd(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path)
    root = tmp_path / "candidate" / "var"
    assert paths["RUNTIME_PROFILE"] == "isolated_candidate"
    assert paths["get_var_dir"]() == root
    assert paths["var_path"]("locks", "desktop_ui.lock") == root / "locks/desktop_ui.lock"
    assert paths["ensure_var_layout"]() == root
    assert {child.name for child in root.iterdir()} == set(paths["VAR_LAYOUT_DIRS"])
    for dirname in ("installed-local", "installed-roaming", "conflicting-env"):
        assert not (tmp_path / dirname).exists()


def test_installed_keeps_appdata_root(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path, profile="installed")
    assert paths["get_var_dir"]() == tmp_path / "installed-local/HextechNexus/var"
    assert not (tmp_path / "candidate/var").exists()


def test_installed_preserves_roaming_and_home_fallbacks(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path, profile="installed")
    monkeypatch.delenv("LOCALAPPDATA")
    assert paths["_var_root"]() == tmp_path / "installed-roaming/HextechNexus/var"
    monkeypatch.delenv("APPDATA")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert paths["_var_root"]() == tmp_path / "home/.hextech_nexus/var"


def test_reimported_workers_keep_candidate_root(monkeypatch, tmp_path: Path):
    first = _load_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("HEXTECH_VAR_DIR", str(tmp_path / "another-runtime"))
    monkeypatch.chdir(tmp_path)
    second = runpy.run_path(str(PATHS_FILE))
    assert first["get_var_dir"]() == second["get_var_dir"]() == tmp_path / "candidate/var"


@pytest.mark.parametrize("raw", ["{}", "[]", "null", "{", '{"runtime_profile": "other"}',
                                 '{"runtime_profile": ["installed"]}', '{"runtime_profile": false}'])
def test_invalid_manifest_fails_closed(monkeypatch, tmp_path: Path, raw):
    with pytest.raises(RuntimeError, match="runtime_profile"):
        _load_paths(monkeypatch, tmp_path, raw_manifest=raw)
    assert not (tmp_path / "installed-local").exists()


def test_missing_manifest_fails_closed(monkeypatch, tmp_path: Path):
    with pytest.raises(RuntimeError, match="runtime_profile"):
        _load_paths(monkeypatch, tmp_path, profile=None)
    assert not (tmp_path / "installed-local").exists()


def test_missing_bundle_root_fails_closed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "installed-local"))
    with pytest.raises(RuntimeError, match="bundle root"):
        runpy.run_path(str(PATHS_FILE))
    assert not (tmp_path / "installed-local").exists()


def test_unreadable_manifest_fails_closed(monkeypatch, tmp_path: Path):
    original_read = Path.read_text

    def blocked_read(path, *args, **kwargs):
        if path.name == "bundle_manifest.json":
            raise PermissionError("manifest access denied")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked_read)
    with pytest.raises(RuntimeError, match="runtime_profile"):
        _load_paths(monkeypatch, tmp_path)
    assert not (tmp_path / "installed-local").exists()


def test_source_honors_environment_without_manifest(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("HEXTECH_VAR_DIR", str(tmp_path / "source-var"))
    paths = runpy.run_path(str(PATHS_FILE))
    assert paths["RUNTIME_PROFILE"] == "source"
    assert paths["get_var_dir"]() == tmp_path / "source-var"


def test_source_default_stays_project_var(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("HEXTECH_VAR_DIR", raising=False)
    paths = runpy.run_path(str(PATHS_FILE))
    assert paths["get_var_dir"]() == RUN_ROOT / "var"


@pytest.mark.parametrize("relative", ["var", "var/logs", "var/state", "var/locks"])
def test_candidate_rejects_reparse_escape(monkeypatch, tmp_path: Path, relative):
    target = tmp_path / "installed-local/HextechNexus/var"
    target.mkdir(parents=True)
    link = tmp_path / "candidate" / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    _junction_or_symlink(target, link)
    with pytest.raises(RuntimeError, match="重解析逃逸"):
        _load_paths(monkeypatch, tmp_path)
    assert list(target.iterdir()) == []


def test_candidate_revalidates_root_after_import(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path)
    target = tmp_path / "external"
    target.mkdir()
    _junction_or_symlink(target, tmp_path / "candidate/var")
    with pytest.raises(RuntimeError, match="重解析逃逸"):
        paths["get_var_dir"]()


def test_candidate_var_path_rejects_late_reparse_escape(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path)
    target = tmp_path / "external"
    target.mkdir()
    link = tmp_path / "candidate/var/state"
    link.parent.mkdir()
    _junction_or_symlink(target, link)
    with pytest.raises(ValueError, match="运行态路径越界"):
        paths["var_path"]("state", "worker.json")
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("profile", ["isolated_candidate", "installed"])
def test_log_and_single_instance_paths_share_root(monkeypatch, tmp_path: Path, profile):
    paths = _load_paths(monkeypatch, tmp_path, profile=profile)
    _bind_paths(monkeypatch, paths)
    logs = runpy.run_path(str(LOGGING_FILE))["get_runtime_log_paths"]()
    instance = runpy.run_path(str(SINGLE_INSTANCE_FILE))
    root = paths["get_var_dir"]()
    assert logs["root"] == root
    for key in ("summary", "error", "full", "events", "supervisor_events"):
        assert root in logs[key].parents
    for key in ("DEFAULT_LOCK_FILE", "DEFAULT_OWNER_FILE", "DEFAULT_ACTIVATION_FILE", "DEFAULT_CONFLICT_FILE"):
        assert root in instance[key].parents


def test_logging_propagates_runtime_resolution_failure(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path)
    module = _bind_paths(monkeypatch, paths)

    def fail():
        raise RuntimeError("runtime profile failure")

    monkeypatch.setattr(module, "get_var_dir", fail)
    log_utils = runpy.run_path(str(LOGGING_FILE))
    with pytest.raises(RuntimeError, match="runtime profile failure"):
        log_utils["get_runtime_log_paths"]()
    assert not (tmp_path / "installed-local").exists()


def test_logging_propagates_path_import_failure(monkeypatch, tmp_path: Path):
    _load_paths(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, PATHS_MODULE, None)
    log_utils = runpy.run_path(str(LOGGING_FILE))
    with pytest.raises(ModuleNotFoundError):
        log_utils["get_runtime_log_paths"]()
    assert not (tmp_path / "installed-local").exists()


def test_candidate_log_permission_error_does_not_fallback(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path)
    _bind_paths(monkeypatch, paths)
    blocked = paths["get_var_dir"]() / "logs"
    original_mkdir = Path.mkdir

    def blocked_mkdir(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("candidate logs access denied")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", blocked_mkdir)
    log_utils = runpy.run_path(str(LOGGING_FILE))
    with pytest.raises(PermissionError, match="candidate logs access denied"):
        log_utils["get_runtime_log_paths"]()
    assert not (tmp_path / "installed-local").exists()


def test_candidate_unwritable_logs_do_not_fallback(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path)
    _bind_paths(monkeypatch, paths)
    blocked = paths["get_var_dir"]() / "logs"
    blocked.parent.mkdir()
    blocked.write_text("not a directory", encoding="utf-8")
    log_utils = runpy.run_path(str(LOGGING_FILE))
    with pytest.raises(OSError):
        log_utils["get_runtime_log_paths"]()
    assert not (tmp_path / "installed-local").exists()


def test_candidate_rejects_escaped_log_file(monkeypatch, tmp_path: Path):
    paths = _load_paths(monkeypatch, tmp_path)
    _bind_paths(monkeypatch, paths)
    logs = paths["get_var_dir"]() / "logs"
    logs.mkdir(parents=True)
    target = tmp_path / "external"
    target.mkdir()
    # A junction works for nested log directories without requiring Windows symlink privileges.
    _junction_or_symlink(target, logs / "dev")
    log_utils = runpy.run_path(str(LOGGING_FILE))
    with pytest.raises(ValueError, match="路径越界"):
        log_utils["get_runtime_log_paths"]()
    assert list(target.iterdir()) == []
