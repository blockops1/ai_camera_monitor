"""Unit tests for identify_from_crops.

Uses monkeypatch to stub call_vision so we can simulate any vision
result without touching the network or disk.

Phase.100: identify_from_crops now sends ALL crops to Qwen in a
single multi-image API call. The per-crop loop is gone. The
`_informative_score` and `pick_best_signature` selection logic is
gone (Qwen consolidates). Tests pin behavior:

  - empty crops → no_motion fallback, vision not called
  - N crops → ONE call_vision with image_paths=list(all N)
  - N crops > TOP_N_CROPS → capped at TOP_N_CROPS
  - call_vision returns VisionError → vision_failed
  - call_vision returns VisionResult with empty signature →
    all_empty_signatures
  - call_vision returns VisionResult with non-empty signature →
    success (crops_used=1, signature extracted, no per-crop picking)
  - api_url and timeout_seconds forwarded to call_vision
  - output_dir persistence: success → raw_vision_multi.json,
    failure → success=False in same file
  - best-effort: bad output_dir logs warning, doesn't raise

Removed tests (Phase.100, design changed):
  - test_informative_score_* (5): _informative_score deprecated
  - test_pick_best_signature_* (3): pick_best_signature deprecated
  - test_partial_vision_failures_keeps_working_crops:
    no per-crop calls means no partial failures
  - test_successful_identification_picks_best_crop:
    renamed to test_sends_all_crops_in_one_call (picks the
    multi-crop contract, not the per-crop-pick contract)
  - test_all_vision_calls_fail_returns_vision_failed:
    renamed to test_vision_call_fails_returns_vision_failed
    (single failure mode now)
  - test_all_signatures_empty_returns_empty_signatures_fallback:
    renamed to test_empty_signature_returns_empty_signatures_fallback
    (single call, signature may be empty)
  - test_raw_vision_persists_success_response +
    test_raw_vision_persists_failure_response: replaced by single
    test_raw_vision_persists_multi_response that covers both paths
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

from vehicle_identifier.identifier import (
    IdentifierResult,
    identify_from_crops,
)
from vehicle_identifier.vision_client import VisionError, VisionResult


def _img(tmp_path: Path, name: str) -> str:
    """Helper: write a tiny JPEG stub to tmp_path and return its str path.

    Returns str (not Path) so list comprehensions produce list[str],
    which satisfies the invariant list[str | Path] parameter type on
    identify_from_crops. Pyright reports an error for list[Path] because
    Path is not a supertype of str in invariant generics.
    """
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    return str(p)


def _good_vision_result(color="white", make="GMC", model="Sierra 1500",
                        confidence=0.85, type_="pickup"):
    """Build a VisionResult mimicking what Qwen returns for a real crop."""
    return VisionResult(
        content={
            "color": color,
            "body_style_hint": type_,
            "make": make,
            "model": model,
            "vehicle_features": {
                "wheel_style": "alloy",
                "wheel_arch": "exposed",
                "wheel_color": "silver",
                "roofline_style": "standard",
                "front_grille_style": "chrome_bars",
                "headlight_signature": "rectangular",
                "rear_lights_signature": "standard",
                "tailgate_type": "standard",
                "badge_text_readable": make if make else None,
                "window_tint": "light",
                "cab_marker_lights": False,
                "bed_cover": "none",
            },
            "description": f"A {color} {make} {model}.",
            "confidence": confidence,
        },
        elapsed_ms=1234.0,
        raw_text="{}",
    )


def _error_vision_result(kind="timeout", message="simulated"):
    return VisionError(kind, message, elapsed_ms=500.0)


# --- Behavior tests (Phase.100) -------------------------------------------


def test_empty_crops_returns_no_motion_fallback(tmp_path, monkeypatch):
    """No crops → no_motion fallback, vision must not be called."""
    called = []
    def fake_call(**kwargs):
        called.append(kwargs)
        return _good_vision_result()

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    result = identify_from_crops(
        crop_paths=[],
        camera_name="Cam",
        captured_at="now",
    )
    assert isinstance(result, IdentifierResult)
    assert result.fallback_used == "no_motion"
    assert result.crops_used == 0
    assert result.signature == {}
    assert result.best_crop_path is None
    assert called == [], "vision must not be called when no crops"


def test_sends_all_crops_in_one_call(tmp_path, monkeypatch):
    """All crops go in a single call_vision(image_paths=[...all...]).

    Pin: the multi-image contract. If this regresses back to a per-crop
    loop (3 sequential calls), this test fails.
    """
    imgs = [_img(tmp_path, f"crop{i}.jpg") for i in range(3)]

    captured = []

    def fake_call(**kwargs):
        captured.append(kwargs)
        # Single response from Qwen: it consolidated.
        return _good_vision_result()

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    result = identify_from_crops(
        crop_paths=imgs,
        camera_name="Cam",
        captured_at="now",
    )
    assert result.fallback_used is None
    assert result.crops_used == 1, "one multi-crop call succeeded"
    assert result.signature["make"] == "GMC"
    assert result.signature["model"] == "Sierra 1500"
    assert result.best_crop_path is None, (
        "multi-crop has no single best crop — Qwen consolidated"
    )
    assert len(captured) == 1, (
        f"must call vision ONCE with all crops, got {len(captured)} calls"
    )
    # All crops passed in the single image_paths list.
    sent = captured[0]["image_paths"]
    assert sent == list(imgs), (
        "all crops must be in image_paths of the single call"
    )


def test_caps_at_top_n_crops(tmp_path, monkeypatch):
    """If caller passes more than TOP_N_CROPS, cap the list at 3.

    Pin: this protects against accidental N=10 vision calls. The cap
    used to be enforced by the loop body; now it's enforced by the
    image_paths slice.
    """
    imgs = [_img(tmp_path, f"crop{i}.jpg") for i in range(5)]
    captured = []

    def fake_call(**kwargs):
        captured.append(kwargs["image_paths"])
        return _good_vision_result()

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    identify_from_crops(
        crop_paths=imgs,
        camera_name="Cam",
        captured_at="now",
    )
    assert len(captured) == 1
    assert len(captured[0]) == 3, (
        f"expected TOP_N_CROPS=3 crops in image_paths, got {len(captured[0])}"
    )


def test_sends_crops_in_caller_order(tmp_path, monkeypatch):
    """Image order in image_paths reflects the caller's order.

    Pin: motion_detector passes crops in area-descending order, so
    crop_0 (largest) is first. We don't sort — we trust the caller.
    If a future caller passes unsorted crops, they get unsorted.
    """
    imgs = [_img(tmp_path, f"crop{i}.jpg") for i in range(3)]
    captured = []

    def fake_call(**kwargs):
        captured.append(kwargs["image_paths"])
        return _good_vision_result()

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    # Caller passes reverse order: crop2, crop1, crop0
    identify_from_crops(
        crop_paths=[imgs[2], imgs[1], imgs[0]],
        camera_name="Cam",
        captured_at="now",
    )
    assert captured[0] == [imgs[2], imgs[1], imgs[0]]


def test_vision_call_fails_returns_vision_failed(tmp_path, monkeypatch):
    """Single call_vision returns VisionError → vision_failed fallback."""
    imgs = [_img(tmp_path, f"crop{i}.jpg") for i in range(3)]

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision",
        lambda **kwargs: _error_vision_result("network", "down"),
    )

    result = identify_from_crops(
        crop_paths=imgs,
        camera_name="Cam",
        captured_at="now",
    )
    assert result.fallback_used == "vision_failed"
    assert result.crops_used == 0
    assert result.signature == {}
    assert isinstance(result.vision_result, VisionError)
    assert result.best_crop_path is None


def test_empty_signature_returns_empty_signatures_fallback(
    tmp_path, monkeypatch,
):
    """Vision returns OK but the signature is empty (no vehicle).

    This is the OFS-foliage case: Qwen saw trees, not a vehicle.
    Production alert body should suppress.
    """
    imgs = [_img(tmp_path, f"crop{i}.jpg") for i in range(3)]

    def fake_call(**kwargs):
        # All identification fields null — Qwen saw no vehicle.
        # Mirrors the real OFS-foliage response (raw_vision_crop_*.json).
        return VisionResult(
            content={"confidence": 0.0,
                     "vehicle_features": {},
                     "description": "The image shows dense foliage with "
                                    "no vehicle visible.",
                     "color": None,
                     "body_style_hint": "null",  # Qwen emits string "null"
                     "make": None,
                     "model": None},
            elapsed_ms=8000.0,
            raw_text="{}",
        )

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    result = identify_from_crops(
        crop_paths=imgs,
        camera_name="Cam",
        captured_at="now",
    )
    assert result.fallback_used == "all_empty_signatures"
    assert result.crops_used == 0
    assert result.signature == {}
    # The empty VisionResult is still carried so the alert pipeline can
    # log "vision saw X but produced no signature".
    assert isinstance(result.vision_result, VisionResult)


def test_result_carries_vision_result_for_telegram_rendering(
    tmp_path, monkeypatch,
):
    """The single VisionResult is the result.vision_result (no picking)."""
    img = _img(tmp_path, "crop1.jpg")
    vr = _good_vision_result()

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision",
        lambda **kwargs: vr,
    )

    result = identify_from_crops(
        crop_paths=[img],
        camera_name="Cam",
        captured_at="now",
    )
    assert result.vision_result is vr


def test_passes_api_url_through(tmp_path, monkeypatch):
    """api_url and timeout_seconds are forwarded to call_vision."""
    img = _img(tmp_path, "crop1.jpg")
    captured = []

    def fake_call(**kwargs):
        captured.append(kwargs)
        return _good_vision_result()

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    identify_from_crops(
        crop_paths=[img],
        camera_name="Cam",
        captured_at="now",
        api_url="http://custom:9999/v1/chat/completions",
        timeout_seconds=60.0,
    )
    assert captured[0]["api_url"] == "http://custom:9999/v1/chat/completions"
    assert captured[0]["timeout_seconds"] == 60.0


def test_does_not_pass_api_url_when_none(tmp_path, monkeypatch):
    """api_url=None means use the client default — don't pass the kwarg."""
    img = _img(tmp_path, "crop1.jpg")
    captured = []

    def fake_call(**kwargs):
        captured.append(kwargs)
        return _good_vision_result()

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    identify_from_crops(
        crop_paths=[img],
        camera_name="Cam",
        captured_at="now",
    )
    assert "api_url" not in captured[0]


def test_never_raises_on_any_failure(tmp_path, monkeypatch):
    """Even if call_vision itself raises, identify_from_crops must
    return an IdentifierResult, not propagate the exception.

    (Currently this WILL raise — documented behavior. If we want
    bulletproof, wrap in try/except. Same as the previous test.)
    """
    img = _img(tmp_path, "crop1.jpg")

    def fake_call(**kwargs):
        raise RuntimeError("catastrophic failure")

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    with pytest.raises(RuntimeError):
        identify_from_crops(
            crop_paths=[img],
            camera_name="Cam",
            captured_at="now",
        )


# --- IdentifierResult dataclass --------------------------------------------


def test_identifier_result_to_dict():
    r = IdentifierResult(
        vision_result=VisionResult({"color": "white"}, 100.0, "{}"),
        signature={"color": "white"},
        best_crop_path=None,
        crops_used=1,
        fallback_used=None,
        elapsed_ms=1234.0,
    )
    d = r.to_dict()
    assert d["signature"] == {"color": "white"}
    assert d["best_crop_path"] is None
    assert d["crops_used"] == 1
    assert d["fallback_used"] is None
    assert d["elapsed_ms"] == 1234.0
    assert d["vision_result"]["color"] == "white"


def test_identifier_result_to_dict_handles_error():
    r = IdentifierResult(
        vision_result=VisionError("timeout", "slow"),
        signature={},
        best_crop_path=None,
        crops_used=0,
        fallback_used="vision_failed",
        elapsed_ms=100.0,
    )
    d = r.to_dict()
    assert d["vision_result"]["error"]["kind"] == "timeout"
    assert d["fallback_used"] == "vision_failed"


def test_identifier_result_to_dict_handles_none():
    r = IdentifierResult(
        vision_result=None,
        signature={},
        best_crop_path=None,
        crops_used=0,
        fallback_used="no_motion",
        elapsed_ms=1.0,
    )
    d = r.to_dict()
    assert d["vision_result"] is None


# --- Phase.85/6B.100: raw_vision_multi.json persistence -----------------
#
# identify_from_crops() persists the single multi-crop Qwen response to
# ``<output_dir>/raw_vision_multi.json`` when output_dir is passed.
# Writes successes (raw_text + content + elapsed_ms) and failures
# (error_kind + error_message). Best-effort: never raises.
# --------------------------------------------------------------------------


def test_raw_vision_persists_multi_response(tmp_path, monkeypatch):
    """On a successful VisionResult, raw_vision_multi.json contains
    crops_sent (list of paths), success=True, raw_text, content,
    elapsed_ms."""
    img1 = _img(tmp_path, "crop1.jpg")
    img2 = _img(tmp_path, "crop2.jpg")

    expected_raw = '{"color":"white","make":"GMC","model":"Sierra"}'

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision",
        lambda **kwargs: VisionResult(
            content={"color": "white", "make": "GMC",
                     "model": "Sierra", "vehicle_features": {},
                     "confidence": 0.8},
            elapsed_ms=2500.0,
            raw_text=expected_raw,
        ),
    )

    output_dir = str(tmp_path / "alert-out")
    result = identify_from_crops(
        crop_paths=[str(img1), str(img2)],
        camera_name="Cam",
        captured_at="now",
        output_dir=output_dir,
        alert_id="unit-test-1",
    )
    assert result.fallback_used is None

    # One file, not three.
    raw = tmp_path / "alert-out" / "raw_vision_multi.json"
    assert raw.exists(), "must persist to raw_vision_multi.json"

    with open(raw) as f:
        payload = json.loads(f.read())

    assert payload["alert_id"] == "unit-test-1"
    assert payload["success"] is True
    assert payload["error_kind"] is None
    assert payload["error_message"] is None
    assert payload["elapsed_ms"] == 2500.0
    assert payload["raw_text"] == expected_raw
    assert payload["content"]["color"] == "white"
    assert payload["content"]["make"] == "GMC"
    # crops_sent is the list of paths that went in the single call.
    assert payload["crops_sent"] == [str(img1), str(img2)]


def test_raw_vision_persists_failure_response(tmp_path, monkeypatch):
    """On a VisionError (timeout/network/parse), the JSON has
    success=False and surfaces error_kind + error_message."""
    img1 = _img(tmp_path, "crop1.jpg")

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision",
        lambda **kwargs: VisionError("timeout", "vision took too long"),
    )

    output_dir = str(tmp_path / "alert-out")
    result = identify_from_crops(
        crop_paths=[str(img1)],
        camera_name="Cam",
        captured_at="now",
        output_dir=output_dir,
        alert_id="unit-test-fail",
    )
    assert result.fallback_used == "vision_failed"
    assert result.crops_used == 0

    raw = tmp_path / "alert-out" / "raw_vision_multi.json"
    assert raw.exists()
    with open(raw) as f:
        payload = json.loads(f.read())

    assert payload["success"] is False
    assert payload["error_kind"] == "timeout"
    assert payload["error_message"] == "vision took too long"
    assert payload["raw_text"] is None
    assert payload["content"] is None
    assert payload["alert_id"] == "unit-test-fail"
    # crops_sent is still recorded even on failure — forensic value.
    assert payload["crops_sent"] == [str(img1)]


def test_raw_vision_no_persist_when_output_dir_omitted(tmp_path, monkeypatch):
    """Default: backward-compatible — no file written when output_dir
    is None. Caller doesn't need to opt in."""
    img1 = _img(tmp_path, "crop1.jpg")

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision",
        lambda **kwargs: _good_vision_result(),
    )

    # No output_dir → no persistence at all.
    cwd_before = set(tmp_path.iterdir())
    identify_from_crops(
        crop_paths=[str(img1)],
        camera_name="Cam",
        captured_at="now",
    )
    cwd_after = set(tmp_path.iterdir())
    # crop1.jpg was already there before — assertion is no NEW files.
    new_files = cwd_after - cwd_before
    assert new_files == set(), f"unexpected new files: {new_files}"


def test_raw_vision_no_op_on_no_crops(tmp_path, monkeypatch):
    """When no crops are passed, no raw_vision JSON is written."""
    called = []
    def fake_call(**kwargs):
        called.append(kwargs)
        return _good_vision_result()

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    output_dir = str(tmp_path / "alert-empty")
    identify_from_crops(
        crop_paths=[],
        camera_name="Cam",
        captured_at="now",
        output_dir=output_dir,
        alert_id="empty-test",
    )
    assert called == []
    # output_dir should not even be created when there are no crops.
    assert not (tmp_path / "alert-empty").exists()


def test_raw_vision_persist_handles_bad_dir(tmp_path, monkeypatch):
    """Best-effort contract: a failed disk write logs a warning,
    doesn't raise. macOS path: /dev/null is a character device,
    mkdir /dev/null/x fails."""
    img1 = _img(tmp_path, "crop1.jpg")

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision",
        lambda **kwargs: _good_vision_result(),
    )

    # Should NOT raise even though /dev/null can't be made into a dir.
    bad_dir = "/dev/null/this/should/fail"
    result = identify_from_crops(
        crop_paths=[str(img1)],
        camera_name="Cam",
        captured_at="now",
        output_dir=bad_dir,
        alert_id="bad-dir-test",
    )
    # Pipeline still completes successfully — persistence is best-effort.
    assert isinstance(result, IdentifierResult)
    assert result.fallback_used is None
