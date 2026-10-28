from __future__ import annotations

import inspect
import io
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hextech.infrastructure.sources import version_sync
from hextech.interfaces.desktop import app_bootstrap
from hextech.interfaces.desktop import runtime_window
from hextech.modules.data.ports.paths import resource_path, var_path
from hextech.modules.data.catalog.versioned import CatalogValidationError


def test_version_sync_uses_runtime_asset_cache_and_never_refreshes_catalog_directly(tmp_path: Path) -> None:
    catalog = SimpleNamespace(root=tmp_path / "catalog")
    with (
        patch.object(version_sync, "load_active_catalog", return_value=catalog),
        patch.object(version_sync, "load_projected_champion_core_data", return_value={"1": {"name": "英雄"}}),
        patch.object(version_sync, "load_augment_tier_map", return_value={"强化": "黄金"}),
        patch.object(version_sync, "get_advanced_session", side_effect=AssertionError("不得直接联网刷新 Catalog")),
        patch.object(version_sync.os, "replace", side_effect=AssertionError("不得写入 Catalog 或 seed")),
    ):
        version_sync._last_sync_time = 0
        assert version_sync.sync_hero_data(allow_remote_check=True) is True

    assert Path(version_sync.ASSET_DIR) == var_path("cache", "assets")
    assert resource_path().resolve() not in Path(version_sync.ASSET_DIR).resolve().parents


def test_cleanup_missing_assets_uses_latest_when_catalog_is_invalid() -> None:
    observed_versions: list[str] = []
    session = SimpleNamespace(headers={})
    core_data = {"1": {"name": "英雄", "en_name": "Hero"}}
    with (
        patch.object(version_sync, "load_active_catalog", side_effect=CatalogValidationError("invalid")),
        patch.object(version_sync, "get_advanced_session", return_value=session),
        patch.object(version_sync, "_collect_missing_assets", return_value=[("1", "英雄", "Hero")]),
        patch.object(
            version_sync,
            "_download_champion_image",
            side_effect=lambda _session, version, _name, _path: observed_versions.append(version) or True,
        ),
    ):
        remaining = version_sync.cleanup_missing_assets(core_data=core_data)

    assert remaining == []
    assert observed_versions == ["latest"]






def test_desktop_bootstrap_does_not_create_resource_asset_directory() -> None:
    source = inspect.getsource(app_bootstrap.DesktopBootstrapMixin._post_visible_bootstrap)
    assert "ASSET_DIR" not in source
    assert "makedirs" not in source


def test_desktop_missing_seed_icon_downloads_only_to_runtime_cache(monkeypatch, tmp_path: Path) -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(buffer, format="PNG")
    response = SimpleNamespace(status_code=200, content=buffer.getvalue())
    seed_root = tmp_path / "readonly-resources" / "assets" / "champions"
    cache_root = tmp_path / "var"
    monkeypatch.setattr(runtime_window, "CHAMPION_ASSET_DIR", os.fspath(seed_root))
    monkeypatch.setattr(runtime_window, "var_path", lambda *parts: cache_root.joinpath(*parts))
    ui = SimpleNamespace(
        image_cache={},
        downloading_imgs=set(),
        img_write_lock=threading.Lock(),
        session=SimpleNamespace(get=lambda *_args, **_kwargs: response),
        _run_on_ui_thread=lambda _func: None,
    )
    label = SimpleNamespace(winfo_exists=lambda: True)

    runtime_window.load_and_set_img(ui, "266", label)

    assert (cache_root / "cache" / "assets" / "champions" / "266.png").is_file()
    assert not seed_root.exists()


def test_importing_version_sync_does_not_create_legacy_runtime_directories(tmp_path: Path) -> None:
    runtime_root = tmp_path / "var"
    env = dict(os.environ)
    env["HEXTECH_VAR_DIR"] = str(runtime_root)
    completed = subprocess.run(
        [sys.executable, "-c", "import hextech.infrastructure.sources.version_sync"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for retired in ("profile", "persisted", "ipc", "sources/synergy"):
        assert not (runtime_root / retired).exists(), retired
