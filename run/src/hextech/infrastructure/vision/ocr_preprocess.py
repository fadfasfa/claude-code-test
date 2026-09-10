"""OCR 名称图的确定性亮字裁剪。"""

from __future__ import annotations

import numpy as np
from PIL import Image


OCR_BRIGHT_TEXT_THRESHOLD = 130
OCR_BRIGHT_TEXT_PADDING_PX = 5


def prepare_ocr_crop(
    image: Image.Image,
    *,
    threshold: int = OCR_BRIGHT_TEXT_THRESHOLD,
    padding: int = OCR_BRIGHT_TEXT_PADDING_PX,
) -> Image.Image:
    """裁掉卡名 ROI 四周装饰，只保留亮色文字及有限上下文。"""

    rgb = image.convert("RGB")
    pixels = np.asarray(rgb, dtype=np.uint8)
    if pixels.size == 0:
        return rgb
    mask = pixels.max(axis=2) >= max(0, min(255, int(threshold)))
    ys, xs = np.nonzero(mask)
    if xs.size == 0 or ys.size == 0:
        return rgb
    safe_padding = max(0, int(padding))
    left = max(0, int(xs.min()) - safe_padding)
    top = max(0, int(ys.min()) - safe_padding)
    right = min(rgb.width, int(xs.max()) + safe_padding + 1)
    bottom = min(rgb.height, int(ys.max()) + safe_padding + 1)
    if right <= left or bottom <= top:
        return rgb
    return rgb.crop((left, top, right, bottom))


__all__ = [
    "OCR_BRIGHT_TEXT_PADDING_PX",
    "OCR_BRIGHT_TEXT_THRESHOLD",
    "prepare_ocr_crop",
]
