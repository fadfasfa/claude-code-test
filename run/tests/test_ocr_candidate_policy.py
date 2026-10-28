"""未获验收的严格标点策略只可做离线比较。"""

from types import SimpleNamespace

import pytest

from tooling.diagnostics.ocr_candidate_policy import (
    build_strict_ocr_vocabulary,
    evaluate_collision_aware_candidate,
    evaluate_strict_ocr_candidate,
    normalize_strict_ocr_text,
)


def _vocabulary(*names):
    return build_strict_ocr_vocabulary([
        SimpleNamespace(augment_id=identity, name=name) for identity, name in names
    ])


def test_strict_candidate_preserves_punctuation_and_never_becomes_eligible():
    assert normalize_strict_ocr_text(" 罪恶 快感！\n") == "罪恶快感!"
    assert normalize_strict_ocr_text("ＡＢＣ： １２３？") == "abc:123?"
    vocabulary = _vocabulary(("1322", "罪恶快感"), ("3004", "罪恶快感！"))
    complete = evaluate_strict_ocr_candidate("罪恶快感!", 0.99, vocabulary)
    assert complete["matched_id"] == "3004"
    assert complete["would_admit"] is True
    assert complete["eligible"] is False
    assert complete["state"] == "offline_candidate"
    bare = evaluate_strict_ocr_candidate("罪恶快感", 0.994981, vocabulary)
    assert bare["reason"] == "punctuation_ambiguous"
    assert bare["candidate_ids"] == ["1322", "3004"]
    assert bare["would_admit"] is False
    assert bare["eligible"] is False


def test_strict_candidate_does_not_repair_nonambiguous_missing_punctuation_or_wrong_character():
    vocabulary = _vocabulary(("3004", "罪恶快感！"), ("1401", "生机迸发"))
    missing = evaluate_strict_ocr_candidate("罪恶快感", 0.99, vocabulary)
    assert missing["reason"] == "punctuation_missing"
    wrong = evaluate_strict_ocr_candidate("生机进发", 0.999474, vocabulary)
    assert wrong["reason"] == "exact_name_missing"
    assert missing["eligible"] is wrong["eligible"] is False


def test_strict_candidate_aliases_merge_by_id_and_no_first_id_wins():
    aliases = _vocabulary(("7", "ＡＢＣ！"), ("7", "abc!"), ("7", "abc"))
    assert len(aliases) == 2
    for name in ("abc", "abc!"):
        assert evaluate_strict_ocr_candidate(name, 0.99, aliases)["matched_id"] == "7"
    collision = _vocabulary(("7", "abc!"), ("9", "abc!"))
    for entries in (collision, tuple(reversed(collision))):
        result = evaluate_strict_ocr_candidate("abc!", 0.99, entries)
        assert result["reason"] == "ambiguous_identity"
        assert result["would_admit"] is result["eligible"] is False


@pytest.mark.parametrize("confidence", [0.9499999, float("nan"), float("inf")])
def test_strict_candidate_confidence_floor_and_ineligibility(confidence):
    result = evaluate_strict_ocr_candidate("双刀流", confidence, _vocabulary(("1225", "双刀流")))
    assert result["reason"] == "low_confidence"
    assert result["would_admit"] is result["eligible"] is False


def test_candidate_rebuilds_keys_from_names_instead_of_production_normalized_keys():
    vocabulary = [SimpleNamespace(canonical_id="3004", name="罪恶快感！", normalized_name="罪恶快感")]
    result = evaluate_strict_ocr_candidate("罪恶快感", 0.99, vocabulary)
    assert result["reason"] == "punctuation_missing"
    assert result["eligible"] is False


def test_collision_candidate_preserves_compatible_noncollision_and_requires_complete_punctuation():
    vocabulary = _vocabulary(("1322", "罪恶快感"), ("3004", "罪恶快感！"), ("coin", "哎哟，我的硬币！"))
    full = evaluate_collision_aware_candidate(" 罪恶 快感！", .99, vocabulary)
    bare = evaluate_collision_aware_candidate("罪恶快感", .99, vocabulary)
    coin = evaluate_collision_aware_candidate("哎哟，我的硬币", .99, vocabulary)
    assert full["matched_id"] == "3004" and full["would_admit"]
    assert not bare["would_admit"] and bare["candidate_ids"] == ["1322", "3004"]
    assert coin["matched_id"] == "coin" and coin["would_admit"]
    assert all(item["eligible"] is False for item in (full, bare, coin))


@pytest.mark.parametrize("text", ["罪恶快感?", "罪恶快感!!", "生机进发", "前生机迸发"])
def test_collision_candidate_does_not_guess_punctuation_or_substitute_characters(text):
    vocabulary = _vocabulary(("1322", "罪恶快感"), ("3004", "罪恶快感！"), ("1401", "生机迸发"))
    assert not evaluate_collision_aware_candidate(text, .9999, vocabulary)["would_admit"]
