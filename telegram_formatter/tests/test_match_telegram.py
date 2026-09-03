"""Unit tests for build_match_telegram_body."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


from telegram_formatter.match_telegram import (
    MatchTelegramInput,
    build_match_telegram_body,
)
from vehicle_matcher.matcher import MatchVerdict


def _carson_kv():
    return {
        "id": "v_carson_white",
        "label": "name two's white pickup",
        "owner": "name two",
        "color": "white",
        "type": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "vehicle_features": {
            "wheel_style": "black steel",
            "cab_marker_lights": False,
            "bed_cover": "none",
        },
    }


def _verdict(kv=None, score=5.5, gap=2.0,
             breakdowns=None, all_scores=None):
    if kv is None:
        kv = _carson_kv()
    if breakdowns is None:
        breakdowns = {
            "color": 1.0, "type": 1.0, "make": 1.0, "model": 1.0,
            "wheel_style": 1.0, "cab_marker_lights": 0.5, "bed_cover": 0.5,
        }
    if all_scores is None:
        all_scores = [("v_carson_white", score), ("v_jayco_camper", 1.0)]
    return MatchVerdict(
        known_vehicle=kv,
        score=score,
        gap=gap,
        breakdowns=breakdowns,
        rank=0,
        all_scores=all_scores,
    )


def test_includes_camera_name_and_timestamp():
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "OFS" in body
    assert "t" in body


def test_includes_edt_timestamp_string():
    """Phase.99 (PLAN.md §11.26): match Telegram body must carry the EDT
    string the listener converted at the webhook boundary. Reolink's raw
    UTC ISO format (`...+0000`) must NOT appear in the body.
    """
    edt = "2026-08-19 12:14:32 EDT"
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso=edt,
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert edt in body
    assert "+0000" not in body
    assert not body.split("\n", 1)[0].endswith("Z")


def test_includes_match_header():
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "✅ Match" in body


def test_includes_matched_label_only_not_id():
    """Phase.121: slim body shows just the label, not (id) + (label) duplicated.
    Pinned: Note 2026-08-22 "the old title, what it matched to".
    """
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    # Label must be there
    assert "name two's white pickup" in body
    # ID must NOT be in the body — slim version drops it
    assert "v_carson_white" not in body


def test_includes_confidence_with_gap():
    """Phase.121: 'Confidence:' replaces 'Score:'. Gap still shown in parens."""
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(score=5.5, gap=2.0),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Confidence: 5.50" in body
    assert "gap: 2.00" in body
    # Old "Score:" wording is gone
    assert "Score: 5.50" not in body


def test_drops_breakdown_dimensions():
    """Phase.121: per-dimension score breakdowns are gone.

    Pinned: the user said "less busy message" — breakdowns were the
    biggest contributor to noise. If anyone re-adds them, this catches it.
    """
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    # Breakdown keys must not appear
    for dim in ("color:", "type:", "make:", "model:", "wheel_style:", "bed_cover:"):
        assert dim not in body, f"breakdown leaked into slim body: {dim!r}"


def test_drops_thresholds_line():
    """Phase.121: thresholds line removed (was debug noise)."""
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Thresholds:" not in body
    assert "confidence≥" not in body
    assert "gap≥" not in body


def test_runner_ups_limited_to_two():
    """Phase.121: top-2 runner-ups (Note: "the two runner-up").

    all_scores has 4 entries (matched + 3 runner-ups); body must show
    only 2 runner-ups. If anyone re-adds the third, this catches it.
    """
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(all_scores=[
            ("v_carson_white", 5.5),
            ("v_jayco_camper", 1.0),
            ("v_brown_f150", 0.5),
            ("v_third_runner", 0.3),  # Should NOT appear
        ]),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Runner-ups:" in body
    assert "v_jayco_camper" in body
    assert "v_brown_f150" in body
    # Third runner-up is clipped at the top-2 limit
    assert "v_third_runner" not in body


def test_runner_ups_section_omitted_when_only_one_candidate():
    """Phase.121: no Runner-ups section when only the match exists."""
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(all_scores=[("v_carson_white", 5.5)]),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Runner-ups:" not in body


def test_includes_owner_when_present():
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Owner: name two" in body


def test_owner_omitted_when_not_present():
    kv = _carson_kv()
    del kv["owner"]
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(kv=kv),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Owner:" not in body


def test_includes_color_make_model_no_body():
    """Phase.121: slim body shows Color + Make/Model but drops Body type.

    "Body: pickup" was redundant with the label "name two's white pickup".
    """
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Color: white" in body
    assert "Make/Model: GMC Sierra 1500" in body
    # Body type line is gone (label carries it)
    assert "Body: pickup" not in body


def test_alert_id_removed_phase_6b114():
    """Phase.114: alert_id removed from header (diagnostic noise).

    Timestamp moves to footer for user-facing visibility.
    """
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
        alert_id="abc123",
    )
    body = build_match_telegram_body(inp)
    assert "[abc123]" not in body  # Phase.114
    assert body.endswith("t")  # footer is captured_at_iso


def test_does_not_echo_qwen_output():
    """Match Telegram is the second Telegram. The Motion Telegram
    already sent Qwen's full output. The Match Telegram should not
    duplicate it."""
    inp = MatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    # No Qwen-output-field names should appear.
    assert "description" not in body.lower()
    assert "vehicle_features" not in body


# ---------------------------------------------------------------------------
# Phase.167 §13.5 Commit 13: display-name lookup contract.
# ---------------------------------------------------------------------------


def test_header_uses_display_name_for_lookup(monkeypatch):
    """Phase.167 §13.5 (Commit 13): the header resolves the camera
    identifier via infra.cameras.display_name_for — i.e. it does NOT
    echo whatever string the caller passed in raw.

    Mock display_name_for to return a synthetic value. If the body
    still echoes the caller's raw string, this catches it.
    """
    from telegram_formatter import match_telegram as mt

    monkeypatch.setattr(
        mt, "display_name_for",
        lambda identifier, env_path=None: "Fake Porch" if identifier == "CAM1" else identifier,
    )

    inp = MatchTelegramInput(
        camera_name="CAM1",  # code
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "✅ Match — Fake Porch" in body
    assert "CAM1" not in body.split("\n", 1)[0]  # header should not echo the code


def test_header_uses_lookup_when_name_passed(monkeypatch):
    """Caller passes a friendly name; lookup normalizes it via the registry.

    This is what listeners do today (they carry ctx.camera_name from the
    webhook). The body still goes through display_name_for to apply the
    registry's canonical name.
    """
    from telegram_formatter import match_telegram as mt

    monkeypatch.setattr(
        mt, "display_name_for",
        lambda identifier, env_path=None: "Canonical Porch" if identifier == "Old Name" else identifier,
    )

    inp = MatchTelegramInput(
        camera_name="Old Name",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Canonical Porch" in body
    assert "Old Name" not in body


def test_header_passes_through_when_not_in_registry(monkeypatch):
    """Unknown identifier → display_name_for returns input unchanged.

    The header should show the original string. This is the fallback
    contract for legacy callers / stale fixtures.
    """
    from telegram_formatter import match_telegram as mt

    # Default display_name_for behavior (no match → return input).
    monkeypatch.setattr(mt, "display_name_for", lambda i, env_path=None: i)

    inp = MatchTelegramInput(
        camera_name="Z9_UNKNOWN",
        captured_at_iso="t",
        verdict=_verdict(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_match_telegram_body(inp)
    assert "Z9_UNKNOWN" in body
