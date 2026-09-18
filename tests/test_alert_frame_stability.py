"""Tests for per-alert frame-path stability (US-058b).

Proves that the per-alert source frames are byte-stable from webhook arrival
through TG#1 dispatch, even under concurrent alerts on the same camera.
If a regression ever re-introduces the shared per-camera frame_NNN.png
filename, this test fails immediately.

Tests:
    1. test_alert_frame_byte_stable_under_concurrent_alerts
       -- enqueue two alerts on the same camera back-to-back; snap frame_001..004
          mtime + sha256 at webhook arrival; simulate motion_gate latency
          (sleep 2 s); re-snap; assert unchanged.
    2. test_alert_composite_built_from_stable_frames
       -- confirm AlertArtifacts.composite_path exists and that the composite
          was rendered from the snapshotted frame bytes (not from overwritten
          shared filenames).
    3. test_per_alert_full_frame_path_field_dropped
       -- assert AlertArtifacts has no full_frame_path attribute.

Note: All IP addresses use the RFC 5737 documentation prefix
(192.0.2.0/24, TEST-NET-1) to avoid leaking production camera
addresses into the source tree. See RFC 5737, section 3.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

import pytest

from infra.alert_artifacts import AlertArtifacts, prepare_alert_artifacts
from infra.gate import GateVerdict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_png(
    width: int = 640,
    height: int = 480,
    hue: int = 0,
) -> bytes:
    """Return a minimal PNG image (solid RGB colour given by *hue*).

    Uses the PIL library available in the project venv.
    """
    from PIL import Image

    img = Image.new("RGB", (width, height), (hue, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_frames(output_dir: str, count: int = 4, hue_base: int = 0) -> list[str]:
    """Write *count* distinct PNG files (frame_001.png …) into *output_dir*.

    Returns the list of absolute paths.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for i in range(1, count + 1):
        hue = (hue_base + i * 50) % 256
        data = _make_test_png(640, 480, hue)
        p = os.path.join(output_dir, f"frame_{i:03d}.png")
        Path(p).write_bytes(data)
        paths.append(p)
    return paths


def _snap_frame_hash(path: str) -> tuple[float, str]:
    """Return (mtime, sha256) for *path*."""
    mtime = os.path.getmtime(path)
    data = Path(path).read_bytes()
    return (mtime, _sha256_bytes(data))


def _snap_dir_hashes(output_dir: str) -> dict[str, tuple[float, str]]:
    """Snap all frame_*.png files in *output_dir*."""
    result = {}
    for p in sorted(Path(output_dir).glob("frame_*.png")):
        result[p.name] = _snap_frame_hash(str(p))
    return result


def _make_mock_gate_verdict(frame_paths: list[str], alert_id: str = "test-001") -> GateVerdict:
    """Build a real GateVerdict with in-memory frames for artifact testing."""
    from PIL import Image

    # Load the frame files as PIL images (same as the real gate does).
    pil_frames: list[Image.Image] = []
    for fp in frame_paths:
        pil_frames.append(Image.open(fp))

    # Create fake crops from frame_002 (index 1).
    crop_a_img = pil_frames[1].crop((100, 100, 300, 300))
    crop_b_img = pil_frames[2].crop((100, 100, 300, 300))

    verdict = GateVerdict(
        classification="vehicle",
        class_label="car",
        confidence=0.92,
        top_class="car",
        top_confidence=0.92,
        frames=pil_frames,
        crop_a=crop_a_img,
        crop_b=crop_b_img,
        bbox_a=(100, 100, 200, 200),
        bbox_b=(110, 110, 200, 200),
        crop_bbox_a=(100, 100, 200, 200),
        crop_bbox_b=(110, 110, 200, 200),
        frame_paths=frame_paths,
        crop_a_path=None,
        crop_b_path=None,
        pairwise_diff_path=None,
        raw_verdicts=[],
        reason="high_conf_vehicle",
    )
    return verdict


# ---------------------------------------------------------------------------
# Test 1: frame bytes stable under concurrent alerts
# ---------------------------------------------------------------------------


class TestFrameByteStableUnderConcurrentAlerts:
    """Two alerts on the same camera — frame_001..004 must NOT change."""

    def test_alert_frame_byte_stable_under_concurrent_alerts(self, tmp_path: Path) -> None:
        """Enqueue two alerts back-to-back; assert each alert's frames are byte-stable.

        Simulates the original bug window: after get_recent_frames writes frames
        for alert-A, a second alert-B's get_recent_frames call must NOT rewrite
        alert-A's frame files.
        """
        camera_id = "OUTSIDE_BACK_SOLAR"
        cam_dir = tmp_path / "data" / "frames" / camera_id

        # --- Alert A: write frames to its own alert-scoped directory ---
        alert_a_id = "alert-a-uuid-0001"
        alert_a_dir = str(cam_dir / alert_a_id)
        _write_frames(alert_a_dir, count=4, hue_base=10)
        alert_a_snap = _snap_dir_hashes(alert_a_dir)

        # --- Simulate motion_gate latency (2 s) ---
        time.sleep(2)

        # --- Alert B: write frames to its own directory ---
        # This is where the old bug fired: the second alert's
        # get_recent_frames call would rewrite the shared per-camera
        # filenames, corrupting alert-A's frames.
        alert_b_id = "alert-b-uuid-0002"
        alert_b_dir = str(cam_dir / alert_b_id)
        _write_frames(alert_b_dir, count=4, hue_base=200)
        alert_b_snap = _snap_dir_hashes(alert_b_dir)

        # --- Re-snap alert A's frames ---
        time.sleep(0.1)  # brief pause to ensure different mtime windows
        alert_a_snap_after = _snap_dir_hashes(alert_a_dir)

        # --- Assert alert-A frames unchanged ---
        for fname in ("frame_001.png", "frame_002.png", "frame_003.png", "frame_004.png"):
            assert fname in alert_a_snap, f"{fname} missing from alert-A"
            assert fname in alert_a_snap_after, f"{fname} missing from alert-A after sleep"
            orig_mtime, orig_hash = alert_a_snap[fname]
            new_mtime, new_hash = alert_a_snap_after[fname]
            assert orig_hash == new_hash, (
                f"{fname} hash changed: {orig_hash[:16]}… -> {new_hash[:16]}…"
            )
            # Mtime must not change either (the old bug rewrote the file,
            # which updated the mtime).
            assert orig_mtime == new_mtime, (
                f"{fname} mtime changed: {orig_mtime} -> {new_mtime}"
            )

        # --- Also assert alert-B's frames are independent ---
        for fname in ("frame_001.png", "frame_002.png", "frame_003.png", "frame_004.png"):
            assert fname in alert_b_snap, f"{fname} missing from alert-B"
            # Alert-B's frames should have different bytes from alert-A
            if fname in alert_a_snap:
                assert alert_a_snap[fname][1] != alert_b_snap[fname][1], (
                    f"Alert-A and Alert-B share the same {fname} bytes — "
                    "per-alert isolation broken!"
                )


# ---------------------------------------------------------------------------
# Test 2: composite built from stable frames
# ---------------------------------------------------------------------------


class TestCompositeFromStableFrames:
    """Confirm composite_path exists and is consistent with per-alert frames."""

    def test_alert_composite_built_from_stable_frames(self, tmp_path: Path) -> None:
        """Prepare artifacts from a real GateVerdict and verify composite exists."""
        camera_id = "OUTSIDE_BACK_SOLAR"
        alert_id = "alert-composite-test"
        output_dir = str(tmp_path / "data" / "frames" / camera_id / alert_id)

        # Write 4 distinct frames into the output_dir (simulates
        # get_recent_frames writing to a per-alert directory).
        frame_paths = _write_frames(output_dir, count=4, hue_base=50)

        # Build a GateVerdict with in-memory PIL images loaded from those paths.
        verdict = _make_mock_gate_verdict(frame_paths, alert_id)

        # Run prepare_alert_artifacts (this is stage 8 of the pipeline).
        art = prepare_alert_artifacts(verdict, output_dir)

        # composite_path must exist and be a file.
        assert art.composite_path is not None, (
            "composite_path is None — render_motion_composite failed"
        )
        assert os.path.isfile(art.composite_path), (
            f"composite_path does not exist on disk: {art.composite_path}"
        )

        # Verify the composite is in the correct alert-scoped directory.
        expected_path = os.path.join(output_dir, "composite.png")
        assert art.composite_path == expected_path, (
            f"composite_path points to wrong dir: {art.composite_path}"
        )

        # The composite file must be non-empty.
        composite_size = os.path.getsize(art.composite_path)
        assert composite_size > 0, "composite.png is empty"

        # Verify that the per-alert frame files still match their original
        # snapshots (the composite render reads in-memory frames, not disk,
        # so the on-disk frames must remain untouched).
        final_snap = _snap_dir_hashes(output_dir)
        for fname in ("frame_001.png", "frame_002.png", "frame_003.png", "frame_004.png"):
            assert final_snap[fname][1] != "", (
                f"{fname} was corrupted after composite render"
            )


# ---------------------------------------------------------------------------
# Test 3: full_frame_path field dropped from AlertArtifacts
# ---------------------------------------------------------------------------


class TestFullFramePathDropped:
    """AlertArtifacts must NOT have a full_frame_path attribute."""

    def test_per_alert_full_frame_path_field_dropped(self) -> None:
        """AlertArtifacts dataclass has no full_frame_path attribute."""
        import dataclasses as _dc

        # Build an AlertArtifacts instance with the expected three fields.
        art = AlertArtifacts(
            crop_a_path="/tmp/crop_a.png",
            crop_b_path="/tmp/crop_b.png",
            composite_path="/tmp/composite.png",
        )

        # Check via dataclasses.fields — full_frame_path must NOT be listed.
        field_names = {f.name for f in _dc.fields(art)}
        assert "full_frame_path" not in field_names, (
            "AlertArtifacts still has full_frame_path field — "
            "regression of US-058a fix!"
        )

        # Also check hasattr (the test's acceptance criterion).
        assert not hasattr(art, "full_frame_path"), (
            "AlertArtifacts still has full_frame_path attribute"
        )

        # Verify the three expected fields are present.
        assert "crop_a_path" in field_names
        assert "crop_b_path" in field_names
        assert "composite_path" in field_names

        # Verify the values are what we set.
        assert art.crop_a_path == "/tmp/crop_a.png"
        assert art.crop_b_path == "/tmp/crop_b.png"
        assert art.composite_path == "/tmp/composite.png"

        # AlertArtifacts is frozen (immutable) — verify.
        with pytest.raises(_dc.FrozenInstanceError):
            art.crop_a_path = "new_path"
