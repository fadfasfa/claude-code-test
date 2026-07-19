"""Windows 开发入口必须只包装新 CLI，并暴露真实运行态路径。"""

from __future__ import annotations

from pathlib import Path


RUN_DIR = Path(__file__).resolve().parents[1]
DEV_DIR = RUN_DIR / "tooling" / "dev"


def test_frequent_entrypoints_are_exposed_at_run_root() -> None:
    assert {path.name for path in RUN_DIR.glob("*.ps1")} == {
        "启动Hextech.ps1",
        "打包并部署Hextech.ps1",
        "调试Web前端.ps1",
    }
    expected_tooling = {
        "start_overlay_probe.ps1",
        "verify_machine.ps1",
    }
    assert expected_tooling <= {path.name for path in DEV_DIR.glob("*.ps1")}


def test_dev_entrypoints_only_use_new_cli_and_runtime_layout() -> None:
    scripts = [*RUN_DIR.glob("*.ps1"), *DEV_DIR.glob("*.ps1")]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in scripts)
    for command in (
        "hextech-desktop",
        "hextech-web",
        "hextech-overlay",
        "hextech-data-service",
        "hextech-supervisor",
    ):
        assert command in combined
    assert "state\\web_server_port.txt" in combined
    assert "tooling.dev.web_stack" in combined
    assert "WithOverlay" in combined
    assert "state\\desktop" not in combined
    assert "auth_token.txt" not in combined
    for old_root in ("run\\scripts", "run\\data", "run\\hextech", "run\\frontend", "run\\tools"):
        assert old_root not in combined


def test_build_catalog_whitelist_excludes_dynamic_synergy() -> None:
    from tooling.build.rules import CATALOG_FILES

    assert "Champion_Synergy_Cleaned.json" not in CATALOG_FILES


def test_champion_asset_path_is_classified() -> None:
    from hextech.modules.data.ports.paths import ASSET_DIR, CHAMPION_ASSET_DIR

    assert Path(CHAMPION_ASSET_DIR) == Path(ASSET_DIR) / "champions"
