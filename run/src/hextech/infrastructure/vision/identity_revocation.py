"""Bounded lexical contradiction policy; enabled only by explicit offline replay.

Revoking an old identity never admits a new identity. Production confirmation
continues to use its existing exact confidence and independent frame gates.
"""
from __future__ import annotations

from dataclasses import replace
import math
from typing import Any, Mapping

from .state_models import CandidateEvidence, SlotTrack


def bind_lexical_evidence(observation: CandidateEvidence, evidence: Mapping[str, Any]) -> CandidateEvidence:
    """Caller validates session/epoch/slot/generation; bind to original pixels."""
    try:
        valid = (
            int(evidence.get("captured_frame_id") or 0) == observation.frame_id > 0
            and str(evidence.get("perceptual_fingerprint") or "") == observation.fingerprint
            and str(evidence.get("rgb_sha256") or "") == observation.rgb_sha256
            and len(observation.rgb_sha256) == 64
            and float(evidence.get("captured_at") or 0) == observation.captured_at > 0
            and math.isfinite(observation.captured_at)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid or not observation.revocation_eligible:
        return observation
    values = evidence.get("exact_candidate_ids")
    ids = tuple(sorted({str(value) for value in values if str(value)})) if isinstance(values, (list, tuple)) else ()
    timing = evidence.get("inference_timing") if isinstance(evidence.get("inference_timing"), Mapping) else {}
    inference_key = str((timing.get("inference_started_at"), timing.get("inference_completed_at"))) if timing.get("inference_started_at") else ""
    return replace(observation, inference_key=inference_key, lexical_name_key=str(evidence.get("exact_name_key") or ""),
                   lexical_candidate_ids=ids, inference_reused=bool(evidence.get("inference_reused")))


def contradiction_ready(track: SlotTrack, old_id: str) -> bool:
    """Require three consecutive independent captures with identical full name/hash."""
    window = track.observations[-3:]
    complete = bool(len(window) == 3 and track.baseline_fingerprints and track.baseline_input_hashes)
    if complete:
        first = window[0]
        complete = all(
            item.revocation_eligible and item.lexical_name_key and item.lexical_candidate_ids
            and old_id not in item.lexical_candidate_ids
            and item.lexical_name_key == first.lexical_name_key
            and item.lexical_candidate_ids == first.lexical_candidate_ids
            and item.rgb_sha256 == first.rgb_sha256
            and item.rgb_sha256 not in track.baseline_input_hashes
            and item.fingerprint and item.fingerprint not in track.baseline_fingerprints
            for item in window
        ) and all(a.frame_id < b.frame_id and a.captured_at < b.captured_at for a, b in zip(window, window[1:]))
    # Counts name their denominator; cached OCR is not independent inference.
    track.identity_diagnostics = {
        "policy": "lexical_revocation_offline", "eligible": False,
        "metric_scope": "last_three_capture_window",
        "observation_count": len(window),
        "total_observation_count": track.raw_observation_count,
        "independent_capture_frames": len({item.frame_id for item in window if item.frame_id > 0}),
        "actual_inference_count": len({item.inference_key for item in window if item.lexical_name_key and item.inference_key}),
        "contradiction_frames": sum(bool(item.lexical_candidate_ids and old_id not in item.lexical_candidate_ids)
                                    for item in window),
        "ready_contradiction": bool(complete),
    }
    return bool(complete)
