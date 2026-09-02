"""The vehicle identifier.

Runs the vision model on a list of crop images and returns an
IdentifierResult.

NO matching. NO known_vehicles. The matcher is a separate concern
(see refactor/vehicle_matcher). The identifier's job ends at:
"here is what Qwen saw, here's the signature that captures it."

The result is a structured dict. Pure orchestration of the vision
client + signature extractor.

Phase 6B.100: sends ALL crops to Qwen in a single multi-image API
call (was: one call per crop + pick_best signature selection).
The previous per-crop loop produced wrong-vehicle matches when
3 separate calls tied at the same score and tie-break favored
the widest bbox. See docstring of `identify_from_crops` for the
two live-burn cases this fixed (CAM1 foliage, CAM2 Silverado).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .signature import extract_signature, is_empty_signature
from .vision_client import (
    VisionError,
    VisionResult,
    call_vision,
    is_vision_error,
)

log = logging.getLogger(__name__)


# Cap on how many crops to send through vision in one alert.
# Sending more is wasteful; sending fewer is risky (one bad crop
# shouldn't sink the alert).
TOP_N_CROPS = 3


class IdentifierResult:
    """The output of identify_from_crops.

    Attributes:
        vision_result: The raw Qwen response from the best crop.
            Could be a VisionResult or VisionError — caller checks
            is_vision_error() before consuming.
        signature: The extracted signature (flat dict). Empty dict
            if vision failed or signature was empty.
        best_crop_path: Absolute path to the crop that produced
            vision_result. None if no crops succeeded.
        crops_used: How many crops produced a valid signature
            (0..len(crop_paths), capped at TOP_N_CROPS).
        fallback_used: One of None, "no_motion", "vision_failed",
            "all_empty_signatures".
        elapsed_ms: Total wall-clock time spent in identify_from_crops.
    """

    def __init__(
        self,
        vision_result: VisionResult | VisionError | dict[str, Any] | None,
        signature: dict[str, Any],
        best_crop_path: str | None,
        crops_used: int,
        fallback_used: str | None,
        elapsed_ms: float,
    ) -> None:
        self.vision_result = vision_result
        self.signature = signature
        self.best_crop_path = best_crop_path
        self.crops_used = crops_used
        self.fallback_used = fallback_used
        self.elapsed_ms = elapsed_ms

    def to_dict(self) -> dict[str, Any]:
        """Plain dict form for callers that want dict semantics."""
        out: dict[str, Any] = {
            "crops_used": self.crops_used,
            "fallback_used": self.fallback_used,
            "elapsed_ms": self.elapsed_ms,
            "signature": self.signature,
            "best_crop_path": self.best_crop_path,
            "vision_result": self._vision_result_as_dict(),
        }
        return out

    def _vision_result_as_dict(self) -> dict[str, Any] | None:
        vr: Any = self.vision_result
        if vr is None:
            return None
        if isinstance(vr, VisionResult):
            return vr.to_dict()
        if isinstance(vr, VisionError):
            return vr.to_dict()
        if isinstance(vr, dict):
            return vr
        return None


# Phase 6B.100: per-crop selection logic (`_informative_score`,
# `pick_best_signature`) removed. Identifies behavior change:
# One multi-crop vision call replaces 3 sequential calls + picking.
# See `identify_from_crops` docstring for the rationale + two live
# alerts that motivated the change (CAM1 foliage, CAM2 Silverado).

def _persist_raw_vision(
    output_dir: str,
    alert_id: str,
    crops_sent: list[str],
    vr: VisionResult | VisionError,
) -> None:
    """
    Phase 6B.85/6B.100: persist the raw Qwen response to
    ``<output_dir>/raw_vision_multi.json``. Phase 6B.100 sends all crops
    in one API call, so there's a single response to log.

    Writes both the raw text the model returned (raw_text) and the
    parsed content dict so we can distinguish "model returned empty
    struct" from "model returned valid struct that the listener
    reformatted to nulls."

    Best-effort: logged on failure, never raises. Persistence is a
    forensic aid — a failed write must never abort the alert path.
    """
    raw_path = os.path.join(output_dir, "raw_vision_multi.json")
    elapsed = getattr(vr, "elapsed_ms", 0.0)
    try:
        os.makedirs(output_dir, exist_ok=True)
        if is_vision_error(vr):
            payload: dict[str, Any] = {
                "alert_id": alert_id,
                "crops_sent": crops_sent,
                "success": False,
                "error_kind": getattr(vr, "kind", "unknown"),
                "error_message": getattr(vr, "message", ""),
                "elapsed_ms": round(elapsed, 1),
                "raw_text": None,
                "content": None,
            }
        else:
            payload = {
                "alert_id": alert_id,
                "crops_sent": crops_sent,
                "success": True,
                "error_kind": None,
                "error_message": None,
                "elapsed_ms": round(elapsed, 1),
                "raw_text": getattr(vr, "raw_text", None),
                "content": getattr(vr, "content", None),
            }
        tmp = raw_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, raw_path)
    except OSError as e:
        log.warning(
            f"[{alert_id}] identify_from_crops: raw_vision_multi.json "
            f"persist failed: {e}"
        )


from collections.abc import Sequence


def identify_from_crops(
    crop_paths: Sequence[str | Path],
    camera_name: str,
    captured_at: str,
    api_url: str | None = None,
    timeout_seconds: float = 180.0,
    output_dir: str | None = None,
    alert_id: str = "",
    pairwise_diff_path: str | Path | None = None,
) -> IdentifierResult:
    """Send ALL crops to vision in a single API call (Phase 6B.100).

    Args:
        crop_paths: List of crop image paths (any order; usually
            sorted by bbox area descending). Sent as one multi-image
            payload to the vision model.
        camera_name: Human-readable camera label for the prompt.
        captured_at: ISO-8601 timestamp of the alert event.
        api_url: Vision API URL override. None = use the client's
            default.
        timeout_seconds: Per-call HTTP timeout.
        output_dir: If provided, persist the single multi-crop vision
            response to ``<output_dir>/raw_vision_multi.json``.
            Phase 6B.85/6B.100: enables forensic debugging when Qwen
            returns unexpected output. Best-effort write (logged on
            failure, never raises).
        alert_id: Used in the persisted file's log lines; pass
            the alert UUID for traceability. Defaults to "" if
            `output_dir` is also None (no-op).
        pairwise_diff_path: Phase 6B.144 (§11.66). Path to the
            pairwise differential image (abs(frame_3 − frame_4)) that
            Qwen uses as the disambiguating signal — "the moving
            subject is whatever lights up the diff." When provided, it
            is appended to the image list sent to vision (after the
            streak crops). When None or file missing, the call is
            unchanged (back-compat with old callers / tests).

    Returns:
        IdentifierResult. Always returns one. Never raises.

        On success: vision_result is a VisionResult, signature is
            non-empty, crops_used == 1 (one consolidated multi-crop
            call), fallback_used is None.

        On failure: vision_result is a VisionError, signature is
            empty, crops_used is 0, fallback_used is set.

    Behavior change (6B.100):
        Previously this looped through crops one-by-one (3 API calls),
        picked the highest `_informative_score` signature, and used
        the chosen crop's vision output. Live testing showed:
        - On CAM1 foliage events, the third crop's vision call hallucinated
          "green suv" while the first two correctly said "no vehicle",
          and the picker promoted the hallucination.
        - On CAM2 Silverado+Tesla, three valid signatures tied at score 16
          and the tie-break picked the wide crop (Tesla) over the tight
          crop (Silverado).

        Multi-crop: Qwen sees all crops simultaneously and consolidates.
        Verified on those two alerts:
        - CAM1 foliage → "no vehicle visible" (correct suppression)
        - CAM2 Silverado+Tesla → "Silverado 1500 Z71" (correct moving
          vehicle)

    Phase 6B.144 (§11.66):
        Vision payload is now [streak_A, streak_B, pairwise_diff] (when
        the diff path is provided). The prompt describes the 3-image
        payload and tells Qwen to identify the MOVING subject. See
        prompt_template.py for the full text.
    """
    t0 = time.perf_counter()

    # --- Guard: no crops → no_motion path ---
    if not crop_paths:
        log.info("identify_from_crops: no crops, fallback=no_motion")
        return IdentifierResult(
            vision_result=None,
            signature={},
            best_crop_path=None,
            crops_used=0,
            fallback_used="no_motion",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    # --- Run vision ONCE on all crops (Phase 6B.100) ---
    crops_to_send = [str(p) for p in crop_paths[:TOP_N_CROPS]]
    log.info(
        f"identify_from_crops: running vision on {len(crops_to_send)} crops "
        f"in a single call for camera={camera_name!r}"
    )

    kwargs: dict[str, Any] = {
        "image_paths": crops_to_send,
        "camera_name": camera_name,
        "captured_at": captured_at,
        "timeout_seconds": timeout_seconds,
    }
    if api_url:
        kwargs["api_url"] = api_url

    # Phase 6B.144 (§11.66): append pairwise diff image so Qwen sees
    # the moving-subject signal. Append AFTER the streak crops so the
    # prompt's "1, 2, 3" numbering matches the request order.
    diff_path_str = str(pairwise_diff_path) if pairwise_diff_path else None
    if diff_path_str and Path(diff_path_str).is_file():
        kwargs["image_paths"] = [*crops_to_send, diff_path_str]
        log.info(
            f"identify_from_crops: appended pairwise diff "
            f"({diff_path_str}) — Qwen payload = "
            f"{len(kwargs['image_paths'])} images"
        )
    elif diff_path_str:
        log.warning(
            f"identify_from_crops: pairwise_diff_path {diff_path_str} "
            f"not found on disk — sending streak crops only"
        )

    vr = call_vision(**kwargs)

    # Phase 6B.85/6B.100: persist raw vision response for forensic debugging.
    if output_dir:
        _persist_raw_vision(
            output_dir=output_dir,
            alert_id=alert_id,
            crops_sent=crops_to_send,
            vr=vr,
        )

    # --- Vision error → fallback_used="vision_failed" ---
    if is_vision_error(vr):
        err_kind = (
            getattr(vr, "kind", "unknown")
            if isinstance(vr, VisionError) else "dict"
        )
        log.warning(
            f"identify_from_crops: vision call failed: kind={err_kind}"
        )
        return IdentifierResult(
            vision_result=vr,
            signature={},
            best_crop_path=None,
            crops_used=0,
            fallback_used="vision_failed",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    # --- Vision OK → extract signature ---
    assert isinstance(vr, VisionResult)
    sig = extract_signature(vr.content)

    # Empty signature → fallback_used="all_empty_signatures"
    if is_empty_signature(sig):
        log.warning(
            "identify_from_crops: vision returned empty signature "
            f"(description={vr.content.get('description', '')[:120]!r})"
        )
        return IdentifierResult(
            vision_result=vr,
            signature={},
            best_crop_path=None,
            crops_used=0,
            fallback_used="all_empty_signatures",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    # --- Success ---
    log.info(
        f"identify_from_crops: 1/{len(crops_to_send)} multi-crop call "
        f"produced signature "
        f"(make={sig.get('make')!r}, model={sig.get('model')!r}, "
        f"confidence={sig.get('confidence')})"
    )
    return IdentifierResult(
        vision_result=vr,
        signature=sig,
        best_crop_path=None,  # 6B.100: multi-crop — no single "best crop"
        crops_used=1,         # one consolidated call succeeded
        fallback_used=None,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
    )
