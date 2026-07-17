"""Windows 开发入口必须只包装新 CLI，并暴露真实运行态路径。"""

from __future__ import annotations

from pathlib import Path


RUN_DIR = Path(__file__).resolve().parents[1]
DEV_DIR = RUN_DIR / "tooling" / "dev"


def test_dev_entrypoints_are_discoverable_without_restoring_root_scripts() -> None:
    expected = {
        "start_desktop.ps1",
        "start_web.ps1",
        "start_overlay_probe.ps1",
        "verify_machine.ps1",
    }
    assert expected <= {path.name for path in DEV_DIR.glob("*.ps1")}
    assert not list(RUN_DIR.glob("*.ps1"))


def test_dev_entrypoints_only_use_new_cli_and_runtime_layout() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in DEV_DIR.glob("*.ps1"))
    for command in (
        "hextech-desktop",
        "hextech-web",
        "hextech-overlay",
        "hextech-data-service",
        "hextech-supervisor",
    ):
        assert command in combined
    assert "state\\web_server_port.txt" in combined
    assert "state\\desktop" not in combined
    assert "auth_token.txt" not in combined
    for old_root in ("run\\scripts", "run\\data", "run\\hextech", "run\\frontend", "run\\tools"):
        assert old_root not in combined


def test_build_catalog_whitelist_excludes_dynamic_synergy() -> None:
    from tooling.build.rules import CATALOG_FILES

    assert "Champion_Synergy_Cleaned.json" not in CATALOG_FILES
