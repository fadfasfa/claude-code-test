"""真实失败 ROI 回归；同一 epoch 的两图不得冒充独立留出集。"""
import hashlib
import json
from pathlib import Path

from PIL import Image

from hextech.infrastructure.vision.ocr_shadow import (
    OnnxTextRecognizer, OcrVocabularyEntry, match_ocr_text, normalize_ocr_text, prepare_ocr_crop,
)


def test_frozen_failed_name_reads_exact_but_clipped_name_is_not_repaired_by_guessing():
    run = Path(__file__).resolve().parents[1]
    root = run / "tests/fixtures/diagnostics/hextech_20260907_names"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["split"] == "regression" and not manifest["independent_holdout_available"]
    images = []
    for item in manifest["samples"]:
        path = root / item["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        with Image.open(path) as image:
            images.append(prepare_ocr_crop(image.convert("RGB")))
    model = OnnxTextRecognizer(run / "resources/ocr/ch_PP-OCRv4_rec_infer.onnx")
    right, left = model.recognize(images)
    assert right[0] == manifest["samples"][0]["visible_text"]
    assert right[1] >= .95
    # 名称 ROI 缺字：高置信度本身不能授权完整名称，也不补猜缺失像素。
    name = "沃格勒特的巫师帽"
    vocabulary = (OcrVocabularyEntry("fixture", name, normalize_ocr_text(name)),)
    assert match_ocr_text(left[0], vocabulary)["match_rule"] != "exact"
