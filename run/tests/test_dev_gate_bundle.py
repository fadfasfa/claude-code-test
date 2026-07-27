"""bundle 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    Path,
    RUN_DIR,
    TemporaryDirectory,
    get_runtime_log_paths,
    install_runtime_logging,
    install_summary_logging,
    io,
    json,
    logging,
    mkstemp,
    os,
    patch,
)

pytestmark = pytest.mark.dev_gate

def test_logging_contract() -> None:
    fd, tmp_name = mkstemp(prefix="hextech-dev-", suffix=".log")
    os.close(fd)
    file_handler = None
    try:
        file_handler = logging.FileHandler(tmp_name, encoding="utf-8")
        stream_buffer = io.StringIO()
        stream_handler = logging.StreamHandler(stream_buffer)

        install_summary_logging(handlers=[file_handler, stream_handler])

        assert file_handler.level == logging.ERROR
        assert stream_handler.level == logging.WARNING
    finally:
        if file_handler is not None:
            file_handler.close()
        try:
            os.remove(tmp_name)
        except OSError:
            pass

    with TemporaryDirectory() as tmp_dir:
        import hextech.infrastructure.observability.logging as log_utils

        runtime_root = Path(tmp_dir) / "runtime"
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_profile = getattr(root_logger, "_hextech_runtime_logging_profile", None)
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        if hasattr(root_logger, "_hextech_runtime_logging_profile"):
            delattr(root_logger, "_hextech_runtime_logging_profile")
        try:
            with patch.object(log_utils, "_runtime_root_dir", return_value=runtime_root):
                install_runtime_logging(profile="dev")
                paths = get_runtime_log_paths()
                logging.getLogger("hextech.dev_checks").info(
                    "logging contract token=secret",
                    extra={"component": "dev_checks", "event": "logging.contract"},
                )
                for handler in root_logger.handlers:
                    handler.flush()
                assert paths["full"].is_file()
                assert any(getattr(handler, "_hextech_handler_name", "") == "dev_full_jsonl" for handler in root_logger.handlers)

                install_runtime_logging(profile="dev")
                dev_full_handlers = [
                    handler
                    for handler in root_logger.handlers
                    if getattr(handler, "_hextech_handler_name", "") == "dev_full_jsonl"
                ]
                assert len(dev_full_handlers) == 1

            packaged_runtime_root = Path(tmp_dir) / "packaged-runtime"
            with patch.object(log_utils, "_runtime_root_dir", return_value=packaged_runtime_root):
                install_runtime_logging(profile="packaged")
                packaged_paths = get_runtime_log_paths()
                assert not any(
                    getattr(handler, "_hextech_handler_name", "") == "dev_full_jsonl"
                    for handler in root_logger.handlers
                )
                assert not packaged_paths["full"].exists()
        finally:
            for handler in list(root_logger.handlers):
                if getattr(handler, "_hextech_runtime_logging", False):
                    root_logger.removeHandler(handler)
                    handler.close()
            for handler in original_handlers:
                if handler not in root_logger.handlers:
                    root_logger.addHandler(handler)
            if original_profile is not None:
                root_logger._hextech_runtime_logging_profile = original_profile  # type: ignore[attr-defined]

    requirements = (RUN_DIR / "tooling" / "requirements" / "runtime.txt").read_text(encoding="utf-8")
    pyproject = (RUN_DIR / "pyproject.toml").read_text(encoding="utf-8")
    for dependency in (
        "requests>=2.32.3,<3",
        "scrapling[fetchers]>=0.4.8,<0.5",
        "urllib3>=2.2,<3",
        "charset-normalizer>=3.3,<4",
        "chardet>=5.2,<6",
        "pystray>=0.19.5,<0.20",
    ):
        assert dependency in pyproject
        assert dependency not in requirements
    assert "-e ." in requirements
    vision_text = (RUN_DIR / "src" / "hextech" / "infrastructure" / "vision" / "sidecar.py").read_text(encoding="utf-8")
    assert 'mode="L"' not in vision_text

def test_packaging_config() -> None:
    build_script = (RUN_DIR / "tooling" / "build" / "package.py").read_text(encoding="utf-8")
    rules_text = (RUN_DIR / "tooling" / "build" / "rules.py").read_text(encoding="utf-8")
    build_entry_text = (RUN_DIR / "src" / "hextech" / "bootstrap" / "desktop.py").read_text(encoding="utf-8")

    assert "PYINSTALLER_HIDDEN_IMPORTS = [" in build_script
    assert "PYINSTALLER_COLLECT_SUBMODULES = [" in build_script
    assert 'cmd.extend(["--hidden-import", module_name])' in build_script
    assert 'cmd.extend(["--collect-submodules", module_name])' in build_script
    assert "--workpath" in build_script
    assert "--distpath" in build_script
    assert "--specpath" in build_script
    assert "TemporaryDirectory" in build_script
    assert ".artifacts" in build_script
    for dependency in (
        "filelock",
        "tkinter",
        "_tkinter",
        "win32gui",
        "win32con",
        "scrapling.fetchers",
        "hextech.interfaces.overlay.lifecycle",
    ):
        assert f'"{dependency}"' in build_script
    for dependency in ("pystray", "pystray._win32"):
        assert f'"{dependency}"' in build_script
    # hidden-import 声明只保证"写了"；构建前必须强校验环境可解析，
    # 否则 PyInstaller 只警告不报错，产物静默缺模块（pystray 真机翻车回归）。
    assert "def verify_hidden_imports_importable" in build_script
    assert "verify_hidden_imports_importable()" in build_script
    assert "find_spec" in build_script
    for dependency in ("scrapling",):
        assert f'"{dependency}"' in build_script
    assert "resolve_tcl_runtime_dirs" in build_script
    assert "resolve_tkinter_package_dir" in build_script
    assert '"_tcl_data"' in build_script
    assert '"_tk_data"' in build_script
    assert '"tkinter"' in build_script
    assert "def main()" in build_entry_text
    assert '"--windowed"' in build_script
    assert '"--console"' not in build_script
    assert not (RUN_DIR / "tooling" / "build" / "build_bundle.py").exists()
    assert not (RUN_DIR / "Hextech伴生终端.spec").exists()
    manifest_script_text = (RUN_DIR / "tooling" / "build" / "manifest.py").read_text(encoding="utf-8")
    assert "hextech" in manifest_script_text
    assert "prepare_bundle_runtime" not in manifest_script_text
    assert "shutil.copy" not in manifest_script_text
    assert "PackageData" in rules_text
    assert "iter_package_data_entries" in rules_text
    assert '"display/' not in manifest_script_text
    assert '"processing/' not in manifest_script_text
    assert '"crawler/' not in manifest_script_text
    assert '"scraping/' not in manifest_script_text
    assert '"game_overlay/' not in manifest_script_text
    for module_name in ("hextech",):
        assert f'"{module_name}"' in build_script
    for legacy_module in ("display", "processing", "scraping", "crawler", "game_overlay"):
        assert f'"{legacy_module}"' not in build_script

def test_bundle_manifest() -> None:
    from tooling.checks import manual as dev_check_manual

    manifest = dev_check_manual.validate_bundle_manifest_contract()
    assert manifest["seed_files"]
    assert manifest["seed_health"]["valid"] is True

def test_packaged_smoke_uses_explicit_feature_flags() -> None:
    """验证空仓烟测不依赖桌面 UI 默认开关状态。"""

    smoke_text = (RUN_DIR / "tooling" / "acceptance" / "smoke_packaged_startup.py").read_text(encoding="utf-8")
    assert '"web_frontend_enabled": True' in smoke_text
    assert '"game_overlay_enabled": False' in smoke_text
    assert '"auto_open_browser": False' in smoke_text
    assert "_write_smoke_feature_flags(runtime_root)" in smoke_text
    assert "OVERLAY_ANCHOR_CALIBRATION_FILENAME" in smoke_text
    assert "package:resources/seeds/current.v2.json" in smoke_text
    assert "FORBIDDEN_PACKAGE_PATHS" in smoke_text
    assert 'child_env["LOCALAPPDATA"]' in smoke_text
    assert "runtime:data absent" in smoke_text
    for forbidden_rel in (
        "var",
        "data",
        "tests",
        "tooling",
        "__pycache__",
        ".pyc",
        ".pyo",
    ):
        assert forbidden_rel in smoke_text
    assert "_internal" in smoke_text
    assert "overlay_anchor_calibration.v1.json" in smoke_text

def test_packaged_smoke_extracts_representative_champion_id_variants() -> None:
    """验证打包烟测代表英雄提取兼容 Web API 的真实字段名。"""

    import tooling.acceptance.smoke_packaged_startup as smoke_packaged_startup

    champion_name, champion_id = smoke_packaged_startup._extract_representative_champion(
        [{"英雄名称": "德玛西亚之力", "英雄 ID": "86", "英文名": "Garen"}]
    )

    assert champion_name == "德玛西亚之力"
    assert champion_id == "86"

    legacy_name, legacy_id = smoke_packaged_startup._extract_representative_champion(
        [{"hero_name": "Garen", "hero_id": "86"}]
    )
    assert legacy_name == "Garen"
    assert legacy_id == "86"

def test_atomic_json_write_retries_transient_replace_conflict() -> None:
    """验证 Windows 下瞬时 replace 冲突不会让 startup_status 类 JSON 写入失败。"""

    import hextech.modules.data.ports.atomic as atomic_io

    with TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "startup_status.json"
        calls = {"replace": 0}
        original_replace = atomic_io.os.replace

        def flaky_replace(src: str, dst: str) -> None:
            calls["replace"] += 1
            if calls["replace"] == 1:
                raise PermissionError("simulated Windows replace race")
            original_replace(src, dst)

        with patch.object(atomic_io.os, "replace", side_effect=flaky_replace):
            atomic_io.atomic_write_json(target, {"ok": True}, ensure_ascii=False, indent=2)

        assert calls["replace"] == 2
        assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
        assert not list(target.parent.glob(f".{target.name}-*.tmp"))
