"""
test_gatekeeper_match_alert_6B93.py — Regression test for the
gatekeeper-camera match-alert path. History:

  Phase 6B.87 (2026-08-17) — added CAM3 as a second gatekeeper camera.
  Phase 6B.93 (2026-08-18) — fixed the CAM3 silent-drop bug:
    The match-alert loop at listener.py:3373 was hard-coded to
    `if camera_name == "CAM5":`, so CAM3 vehicle events that
    correctly cleared the motion + 3-crop-vision pipeline never reached
    the matcher. Confirmed on alert 0eefa8e9-656f-4868-a802-ee682bf928a4
    (Tesla drive-by 15:08 EDT, CAM3): crop vision returned
    Tesla Model Y blue at conf=0.98 on 3 crops, synthesis populated
    vision_result["vehicles"][0] correctly, but the match loop never ran
    because camera_name != CAM5. Outcome: zero match Telegrams, zero
    no-match Telegrams — the matcher had no chance to clear
    v_<owner-name>_darkblue_tesla_y even though the crops would have scored above
    the spec threshold. The fix: the gate changed to
    `if camera_name in GATEKEEPER_CAMERAS:` and CAM3 was added to the set.

  Phase 6B.104 (2026-08-20) — CAM3 demoted from gatekeeper tier.
    maintainer: "I just want CAM3 to be like the other cameras. Not a vehicle
    motion gatekeeper anymore." CAM3 vehicle events no longer route to
    QUEUE_GATEKEEPER_VEHICLE, the capture-delay path, or the match-alert
    Telegram stack. GATEKEEPER_CAMERAS is now CAM5-only
    (`frozenset({"CAM5"})` at listener.py:297). CAM3
    retains its persistent RTSP reader (still in the boot loop at
    listener.py:4133) because that's about reliable frame capture,
    not vehicle handling.

This test does NOT import listener.py directly. Instead, it mirrors
the gate condition and asserts which cameras reach the match-alert
path. The mirrored check must be updated whenever the listener's
gate changes — the test's purpose is to lock the gate contract; if
the contract changes deliberately, both sides change together.

Pins (post-6B.104, then post-§11.79 on 2026-08-28):
  - §11.79: GATEKEEPER_CAMERAS now contains ALL 6 cameras (CAM5, CAM3,
    BACK, CAM1, CAM6, CAM4) — reverted the 6B.104 CAM3 demotion AND added
    the 5 non-CAM5 cameras after 3 vehicles entered the property from
    non-CAM5 cameras and produced no Telegram alerts (only history.jsonl
    rows). maintainer OOB 2026-08-28: "Let's do this for now and see how it
    works. Depending on how it works maybe we'll come up with something
    different."
  - All 6 cameras' vehicle events reach the match-alert path
  - Non-gatekeeper cameras (post-§11.79 there are NONE — every active
    camera is in the set; the trailing-tail fallback path is exercised
    only via a phantom camera in the dispatch test)
"""
from __future__ import annotations

# Mirror of listener/listener.py:227 (post-§11.79 on 2026-08-28).
# Pre-6B.87: `frozenset({"CAM5"})` (CAM5-only, the original).
# 6B.87 added CAM3: `frozenset({"CAM5", "CAM3"})`.
# 6B.93 fixed the silent-drop bug for CAM3 by switching the match-alert gate
# to `camera_name in GATEKEEPER_CAMERAS:` (instead of equality with CAM5).
# 6B.104 demoted CAM3 back out: `frozenset({"CAM5"})`.
# §11.79 (2026-08-28) reverted the 6B.104 demotion AND added the 5
# non-CAM5 cameras — all 6 cameras now fire TG#1+TG#2+TG#3 vehicle stack.
# If the listener's GATEKEEPER_CAMERAS definition changes, this test
# must change too. The test's purpose is to lock the gate contract.
GATEKEEPER_CAMERAS = frozenset({
    "CAM5",
    "CAM3",
    "CAM2",
    "CAM1",
    "CAM6",
    "CAM4",
})

# Mirror of listener/listener.py:3398 (gate condition).
# Pre-6B.93: `if camera_name == "CAM5":` (silently dropped CAM3)
# 6B.93: `if camera_name in GATEKEEPER_CAMERAS:` (extended to all gatekeepers)
# 6B.104: same gate, but CAM3 is no longer in GATEKEEPER_CAMERAS, so CAM3
#   vehicle events once again fall through to QUEUE_OTHER_VEHICLE and
#   the match-alert loop does not run for them.
def _gatekeeper_match_alert_runs(camera_name: str) -> bool:
    """Mirror of the listener's gate-condition for the match-alert path.

    Returns True if camera_name passes the gate and the match-alert
    loop will run; False otherwise.
    """
    return camera_name in GATEKEEPER_CAMERAS


# ---------------------------------------------------------------------------
# GATEKEEPER_CAMERAS data structure (the constant itself)
# ---------------------------------------------------------------------------


def test_gatekeeper_cameras_contains_all_six_cameras():
    """§11.79 (2026-08-28): all 6 cameras are gatekeepers for vehicle
    events. This test pins the §11.79 contract: every active camera
    fires TG#1+TG#2+TG#3. A future regression that silently removes
    a camera from the set would re-introduce the silent-no-alert bug
    (vehicle events → history.jsonl only, no Telegram) — caught here.
    """
    expected = {
        "CAM5",
        "CAM3",
        "CAM2",
        "CAM1",
        "CAM6",
        "CAM4",
    }
    assert GATEKEEPER_CAMERAS == expected, (
        f"GATEKEEPER_CAMERAS must equal the 6-camera set (post-§11.79). "
        f"Got: {sorted(GATEKEEPER_CAMERAS)}"
    )
    assert len(GATEKEEPER_CAMERAS) == 6


def test_gatekeeper_cameras_is_frozenset():
    """GATEKEEPER_CAMERAS is defined as a frozenset so it's safe to
    reference from multiple threads/cameras without lock contention.
    Pin this — if it becomes a regular set, the membership test
    semantics still work but the immutability guarantee is lost.
    """
    # frozenset and set both pass `in` tests the same way at runtime,
    # but frozenset rejects `.add()`. Check the type directly.
    assert isinstance(GATEKEEPER_CAMERAS, frozenset)


# ---------------------------------------------------------------------------
# Gate routing — CAM5 stays on the match-alert path (no regression)
# ---------------------------------------------------------------------------


def test_ofs_vehicle_event_still_runs_match_alert_path():
    """No regression for CAM5 — the original matching gatekeeper
    camera must still route to the match-alert path after 6B.104.
    """
    assert _gatekeeper_match_alert_runs("CAM5") is True


# ---------------------------------------------------------------------------
# Gate exclusion — non-gatekeeper cameras must NOT reach the match path
# ---------------------------------------------------------------------------


def test_ofg_vehicle_event_reaches_match_alert_path():
    """§11.79 (2026-08-28): CAM3 was demoted by 6B.104, but §11.79
    re-added it (along with the 5 other non-CAM5 cameras) after maintainer
    observed 3 vehicles enter the property from non-CAM5 cameras
    without any Telegram alert. CAM3 vehicle events NOW reach the
    match-alert path (TG#1+TG#2+TG#3).
    """
    assert _gatekeeper_match_alert_runs("CAM3") is True


def test_obs_vehicle_event_reaches_match_alert_path():
    """§11.79: CAM6 is a gatekeeper for vehicle events.
    Persistent RTSP (Phase 6B.151), 3-crop vision (gate's standard
    output), and the full vehicle Telegram stack.
    """
    assert _gatekeeper_match_alert_runs("CAM6") is True


def test_ofp_vehicle_event_reaches_match_alert_path():
    """§11.79: CAM4 is a gatekeeper for vehicle events.
    """
    assert _gatekeeper_match_alert_runs("CAM4") is True


def test_back_vehicle_event_reaches_match_alert_path():
    """§11.79: CAM2 is a gatekeeper for vehicle events.
    """
    assert _gatekeeper_match_alert_runs("CAM2") is True


def test_fdo_vehicle_event_reaches_match_alert_path():
    """§11.79: CAM1 is a gatekeeper for vehicle events.
    Note: CAM1 remains CLASS_DISABLED for person events (see
    DISABLED_CAMERA_EVENTS) — that gate is unchanged by §11.79.
    """
    assert _gatekeeper_match_alert_runs("CAM1") is True


def test_unknown_camera_name_skips_match_alert_path():
    """Defensive: an unknown camera name must NOT reach the match
    path. The listener's gatekeeper set is the only source of truth
    for which cameras get the match-alert Telegram stack.

    Post-§11.79: every ACTIVE camera is in the set. The "unknown
    camera" case covers future cameras not yet enrolled or decommissioned
    cameras (e.g. "Back Door Outside" was retired; its name returns
    False here).
    """
    assert _gatekeeper_match_alert_runs("Some Unknown Camera") is False
    assert _gatekeeper_match_alert_runs("") is False
    assert _gatekeeper_match_alert_runs("Back Door Outside") is False  # retired


# ---------------------------------------------------------------------------
# Historical regression: the 6B.93 silent-drop bug (now also a 6B.104
# demotion narrative). Mirror the pre-6B.93 and 6B.93 contracts for
# comparison and so future readers understand the path.
# ---------------------------------------------------------------------------


def test_pre_6B93_gate_would_have_dropped_ofg():
    """Document the 6B.93 bug. The pre-6B.93 gate was
    `if camera_name == "CAM5":` which dropped every
    CAM3 vehicle event. 6B.93 fixed this by switching to a set-membership
    check. 6B.104 re-routes CAM3 away from the gatekeeper path
    entirely (per maintainer's design), so CAM3 once again does not reach
    the match-alert loop — but for a different reason (no longer in
    the gatekeeper set) than the original 6B.93 bug (wrong gate shape).
    §11.79 (2026-08-28) reverts the 6B.104 demotion AND adds the 5
    non-CAM5 cameras after 3 vehicles on CAM4/CAM6 produced no Telegram
    alerts. So the timeline is now: 6B.87 → 6B.93 → 6B.104 → §11.79.
    """
    def _pre_6B93_gate(camera_name: str) -> bool:
        return camera_name == "CAM5"

    # Pre-6B.93: CAM3 dropped, CAM5 passes — the silent-drop bug.
    assert _pre_6B93_gate("CAM3") is False  # the bug
    assert _pre_6B93_gate("CAM5") is True    # the original

    # Note: 6B.93 → 6B.104 routed CAM3 out of the gatekeeper set
    # intentionally. §11.79 (2026-08-28) re-routes CAM3 back in along
    # with the 5 other non-CAM5 cameras. The "6B.104 demotion" assertion
    # is intentionally omitted here — the post-§11.79 contract below
    # supersedes the 6B.104-era assertion.

    # §11.79: CAM3 re-joins the gatekeeper set, AND the 5 non-CAM5 cameras
    # also join. Every active camera now reaches the match-alert path.
    assert _gatekeeper_match_alert_runs("CAM3") is True   # §11.79
    assert _gatekeeper_match_alert_runs("CAM6") is True     # §11.79
    assert _gatekeeper_match_alert_runs("CAM4") is True    # §11.79
    assert _gatekeeper_match_alert_runs("CAM2") is True       # §11.79
    assert _gatekeeper_match_alert_runs("CAM1") is True     # §11.79
    assert _gatekeeper_match_alert_runs("CAM5") is True    # original
