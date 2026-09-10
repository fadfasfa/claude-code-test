"""逐槽视觉代际使用的不可逆感知指纹。

指纹只描述名称 ROI 与图标 ROI 的低分辨率亮暗结构，不保存原始像素，也不作为
身份匹配特征。它仅用于确认同一 selection epoch 内某个槽是否真的换了卡。
"""

from __future__ import annotations

import hashlib

import numpy as np
from PIL import Image


SLOT_EVIDENCE_FINGERPRINT_ALGORITHM = "slot-roi-ahash-v1"


def _perceptual_bits(image: Image.Image, size: tuple[int, int]) -> bytes:
    values = np.asarray(
        image.convert("L").resize(size, Image.Resampling.BILINEAR),
        dtype=np.uint8,
    )
    threshold = float(np.median(values))
    return np.packbits(values >= threshold).tobytes()


def slot_evidence_fingerprint(title: Image.Image, icon: Image.Image) -> str:
    digest = hashlib.sha256()
    digest.update(SLOT_EVIDENCE_FINGERPRINT_ALGORITHM.encode("ascii"))
    digest.update(_perceptual_bits(title, (64, 16)))
    digest.update(_perceptual_bits(icon, (24, 24)))
    return f"{SLOT_EVIDENCE_FINGERPRINT_ALGORITHM}:sha256:{digest.hexdigest()}"


__all__ = ["SLOT_EVIDENCE_FINGERPRINT_ALGORITHM", "slot_evidence_fingerprint"]
