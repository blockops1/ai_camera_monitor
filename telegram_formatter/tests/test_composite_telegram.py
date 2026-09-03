"""Unit tests for telegram_formatter/composite_telegram.py.

Phase.115 (2026-08-25): the composite sender now takes
  - bbox_a, bbox_b  (the gate's diff bboxes, native resolution)
  - trajectory      (the gate-built 4-cell trajectory list)
  - frame_paths     (4 native-res frames from the gate, not 6)
instead of `primary_moving_object` + `frame_paths=6`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

from telegram_formatter.composite_telegram import (
    CompositeTelegramInput,
    _format_trajectory,
    build_composite_telegram_body,
    send_composite_alert,
)

try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None  # type: ignore[assignment]


def _stub_pil_frames(n=4):
    """Return N solid-gray PIL frames (640x480) for tests that don't actually render."""
    if _PILImage is None:
        import pytest
        pytest.skip("PIL not available")
    return [_PILImage.new("RGB", (640, 480), color=(128, 128, 128)) for _ in range(n)]


# --- _format_trajectory ----------------------------------------------------


def test_format_trajectory_joins_with_arrow():
    assert _format_trajectory(["a", "b", "c"]) == "a → b → c"


def test_format_trajectory_handles_single():
    assert _format_trajectory(["only"]) == "only"


def test_format_trajectory_handles_empty():
    assert _format_trajectory([]) == ""


# --- send_composite_alert (failure paths, no real Telegram HTTP) ------------


def test_send_skips_when_no_bbox():
    """No bbox_a and no bbox_b → return False without sending."""
    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=None,  # skip-tests don't reach render; omit output_dir
        bbox_a=None,
        bbox_b=None,
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="TOKEN",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is False


def test_send_skips_when_only_bbox_a_is_none():
    """bbox_a is None but bbox_b is set — caller requires at least bbox_a."""
    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=None,
        bbox_a=None,
        bbox_b=(100, 100, 50, 50),
        trajectory=["absent", "absent", "absent", "UM1"],
        bot_token="TOKEN",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is False


def test_send_skips_when_no_bot_token():
    """Empty bot_token → return False (no creds)."""
    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=None,
        bbox_a=(100, 100, 50, 50),
        bbox_b=(110, 100, 50, 50),
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is False


def test_send_skips_when_no_chat_id():
    """Empty chat_id → return False (no creds)."""
    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=None,
        bbox_a=(100, 100, 50, 50),
        bbox_b=(110, 100, 50, 50),
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="TOKEN",
        chat_id="",
        captured_at="2026-08-21",
    )
    assert sent is False


def test_send_swallows_render_exception(monkeypatch, tmp_path):
    """render_motion_composite raising an exception → return False, no crash."""

    def _explode(*a, **kw):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(
        "infra.motion_visualization.render_motion_composite", _explode,
    )

    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=str(tmp_path),
        bbox_a=(100, 100, 50, 50),
        bbox_b=(110, 100, 50, 50),
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="TOKEN",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is False


def test_send_swallows_send_exception(monkeypatch, tmp_path):
    """Telegram HTTP raising an exception → return False, no crash."""
    from PIL import Image

    # Create a real JPEG that render_motion_composite will accept.
    fake_jpeg = tmp_path / "frame.jpg"
    Image.new("RGB", (640, 480), color=(128, 128, 128)).save(fake_jpeg, "JPEG")

    # Mock render_motion_composite to return a real path (so render guard passes).
    monkeypatch.setattr(
        "infra.motion_visualization.render_motion_composite",
        lambda **kw: str(fake_jpeg),
    )

    # Mock send_photo_with_caption to raise.
    def _explode(*a, **kw):
        raise RuntimeError("simulated telegram failure")

    monkeypatch.setattr(
        "infra.send_telegram.send_photo_with_caption", _explode,
    )

    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=str(tmp_path),
        bbox_a=(100, 100, 50, 50),
        bbox_b=(110, 100, 50, 50),
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="TOKEN",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is False


def test_send_skips_when_render_returns_empty(monkeypatch, tmp_path):
    """render_motion_composite returning '' → return False."""
    monkeypatch.setattr(
        "infra.motion_visualization.render_motion_composite",
        lambda **kw: "",
    )

    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=str(tmp_path),
        bbox_a=(100, 100, 50, 50),
        bbox_b=(110, 100, 50, 50),
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="TOKEN",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is False


def test_send_skips_when_render_returns_nonexistent_path(monkeypatch, tmp_path):
    """render_motion_composite returning a path that doesn't exist → False."""
    monkeypatch.setattr(
        "infra.motion_visualization.render_motion_composite",
        lambda **kw: "/tmp/does-not-exist-composite-12345.jpg",
    )

    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=str(tmp_path),
        bbox_a=(100, 100, 50, 50),
        bbox_b=(110, 100, 50, 50),
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="TOKEN",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is False


def test_send_happy_path(monkeypatch, tmp_path):
    """Successful render + successful send → return True."""
    from PIL import Image

    fake_jpeg = tmp_path / "frame.jpg"
    Image.new("RGB", (640, 480), color=(128, 128, 128)).save(fake_jpeg, "JPEG")

    monkeypatch.setattr(
        "infra.motion_visualization.render_motion_composite",
        lambda **kw: str(fake_jpeg),
    )

    sent_marker = []

    def _fake_send(bot_token, chat_id, path, body, **kwargs):
        sent_marker.append((bot_token, chat_id, path, body, kwargs))
        return True

    monkeypatch.setattr(
        "infra.send_telegram.send_photo_with_caption", _fake_send,
    )

    sent = send_composite_alert(
        alert_id="abc-123",
        camera_name="Outside Front Solar",
        frames=_stub_pil_frames(),
        output_dir=str(tmp_path),
        bbox_a=(100, 100, 50, 50),
        bbox_b=(110, 100, 50, 50),
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="TOKEN",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is True
    assert len(sent_marker) == 1
    assert sent_marker[0][0] == "TOKEN"
    assert sent_marker[0][1] == "CHAT"
    assert "Outside Front Solar" in sent_marker[0][3]


def test_send_falsy_return_value_treated_as_failure(monkeypatch, tmp_path):
    """send_photo_with_caption returning None → False."""
    from PIL import Image

    fake_jpeg = tmp_path / "frame.jpg"
    Image.new("RGB", (640, 480), color=(128, 128, 128)).save(fake_jpeg, "JPEG")

    monkeypatch.setattr(
        "infra.motion_visualization.render_motion_composite",
        lambda **kw: str(fake_jpeg),
    )
    monkeypatch.setattr(
        "infra.send_telegram.send_photo_with_caption",
        lambda *a, **kw: None,
    )

    sent = send_composite_alert(
        alert_id="abc",
        camera_name="Cam",
        frames=_stub_pil_frames(),
        output_dir=str(tmp_path),
        bbox_a=(100, 100, 50, 50),
        bbox_b=(110, 100, 50, 50),
        trajectory=["absent", "absent", "UM1", "UM1"],
        bot_token="TOKEN",
        chat_id="CHAT",
        captured_at="2026-08-21",
    )
    assert sent is False


# --- build_composite_telegram_body (pure function) -------------------------


def test_body_contains_camera_name_and_trajectory():
    """The body includes the camera name and the trajectory arrow."""

    body = build_composite_telegram_body(CompositeTelegramInput(
        camera_name="OFS",
        captured_at_iso="2026-08-25 10:17:23 EDT",
        trajectory=["absent", "absent", "UM1", "UM1"],
    ))
    assert "OFS" in body
    assert "absent → absent → UM1 → UM1" in body
    # captured_at appears as a footer.
    assert "2026-08-25" in body


def test_body_no_motion():
    """No motion → body still includes the camera header + footer."""

    body = build_composite_telegram_body(CompositeTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        trajectory=[],
    ))
    # Empty trajectory → "no motion" path is taken in some impls.
    # At minimum: camera header is shown + footer captured_at.
    assert "OFS" in body
    assert body.endswith("t")


# ---------------------------------------------------------------------------
# Phase.167 §13.5 Commit 13: display-name lookup contract.
# ---------------------------------------------------------------------------


def test_body_header_uses_display_name_for_lookup(monkeypatch):
    """Phase.167 §13.5 (Commit 13): the camera_name in the body's
    header is the result of display_name_for(input.camera_name), NOT the
    raw caller-supplied string. Mock the helper to verify the body
    reaches it.
    """
    from telegram_formatter import composite_telegram as ct

    monkeypatch.setattr(
        ct, "display_name_for",
        lambda identifier, env_path=None: "Fake Porch" if identifier == "CAM1" else identifier,
    )

    body = build_composite_telegram_body(CompositeTelegramInput(
        camera_name="CAM1",
        captured_at_iso="t",
        trajectory=["B1", "B2", "B3"],
    ))
    assert "Vehicle in motion at Fake Porch" in body
    # The CAM1 code should NOT appear in the rendered header line.
    header = body.split("\n", 1)[0]
    assert "CAM1" not in header


def test_body_header_uses_lookup_with_friendly_name(monkeypatch):
    """Caller passes a friendly name; lookup normalizes to canonical."""

    from telegram_formatter import composite_telegram as ct

    monkeypatch.setattr(
        ct, "display_name_for",
        lambda identifier, env_path=None: "Canonical Porch" if identifier == "Old Name" else identifier,
    )

    body = build_composite_telegram_body(CompositeTelegramInput(
        camera_name="Old Name",
        captured_at_iso="t",
        trajectory=["B1", "B2", "B3"],
    ))
    assert "Canonical Porch" in body
    assert "Old Name" not in body


def test_body_header_passes_through_when_not_in_registry(monkeypatch):
    """Unknown identifier → display_name_for returns input unchanged."""

    from telegram_formatter import composite_telegram as ct

    monkeypatch.setattr(ct, "display_name_for", lambda i, env_path=None: i)

    body = build_composite_telegram_body(CompositeTelegramInput(
        camera_name="Z9_UNKNOWN",
        captured_at_iso="t",
        trajectory=[],
    ))
    assert "Z9_UNKNOWN" in body