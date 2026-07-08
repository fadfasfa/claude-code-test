"""图片字节校验工具。

文件职责：
- 为下载到 `.png` 目标路径的资源提供统一内容校验
- 先检查 PNG magic bytes，再用 Pillow 解码校验，避免 HTML 错误页污染静态资源

调用方: scraping.icon_resolver、scraping.version_sync、display.web.runtime、tools.sync_cdragon_augments。
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def is_valid_png_bytes(data: bytes) -> bool:
    """确认字节内容是真 PNG；失败返回 False，不向调用方泄露 Pillow 细节。"""

    if not isinstance(data, bytes) or not data.startswith(PNG_MAGIC):
        return False
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
    except Exception:
        return False
    return True
