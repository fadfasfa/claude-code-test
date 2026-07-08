"""测试 CDragon augment 同步工具的文件写入边界。

调用方: pytest; 关键依赖: tools.sync_cdragon_augments。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image


def _png_bytes() -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_download_one_uses_unique_same_directory_temp_file(tmp_path):
    from tools import sync_cdragon_augments
    from hextech.support.image_validation import is_valid_png_bytes

    class FakeResponse:
        content = _png_bytes()

        def raise_for_status(self):
            return None

    seen_tmp_paths: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen_tmp_paths.append(Path(name))
        return fd, name

    with (
        patch.object(sync_cdragon_augments.requests, "get", return_value=FakeResponse()),
        patch.object(sync_cdragon_augments.tempfile, "mkstemp", side_effect=tracking_mkstemp),
    ):
        result = sync_cdragon_augments._download_one(
            {"name": "测试海克斯", "filename": "augment.png", "source_icon_url": "https://example.test/augment.png"},
            force=True,
            timeout=1,
            asset_dir=tmp_path,
        )

    target = tmp_path / "augment.png"
    assert result["status"] == "downloaded"
    assert target.exists()
    assert is_valid_png_bytes(target.read_bytes())
    assert len(seen_tmp_paths) == 1
    assert seen_tmp_paths[0].parent == tmp_path
    assert seen_tmp_paths[0].name.startswith(".augment.png-")
    assert seen_tmp_paths[0].suffix == ".tmp"
    assert not seen_tmp_paths[0].exists()
    assert not (tmp_path / ".augment.png.tmp").exists()
