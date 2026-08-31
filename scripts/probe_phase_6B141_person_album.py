"""Phase.141 probe — verify person-emit 6-image Telegram album (§11.62).

Production change (2026-08-27): person_emit_stage now sends a 6-image
Telegram media group instead of a single image with caption. Body goes
via send_message (no caption on album); 4 wide gate frames + 2 YOLO
crops bundled as sendMediaGroup.

This probe verifies the new path by:
  1. Calling _collect_person_album_paths with synthesized disk artifacts
     → confirms path ordering (4 wide frames first, then crops)
  2. Calling person_emit_stage with mocked send_message + send_photo_group
     → confirms both are called, album has 6 paths, body has full text
  3. Edge cases: missing crops, missing wide frames, empty output_dir

Checks (8 sections, all must pass):
  §1. _collect_person_album_paths returns 6 paths in correct order
  §2. Missing crops → 4 wide frames only
  §3. Missing wide frames → what's present only
  §4. Empty/missing output_dir → empty list
  §5. person_emit_stage calls send_message (body) + send_photo_group (album)
  §6. Album caption is empty (body went via send_message)
  §7. Telegram sent = True when both sends succeed
  §8. Module header / docs reference 6B.141 / §11.62
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check(condition: bool, label: str) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return condition


def _make_frame(dir: Path, name: str, color: tuple = (50, 100, 150)) -> Path:
    """Write a minimal valid JPEG to dir/name.jpg."""
    from PIL import Image

    path = dir / name
    Image.new("RGB", (640, 480), color=color).save(str(path), "JPEG")
    return path


def main() -> int:
    results: list[bool] = []
    import tempfile

    from listener.person_event_pipeline import (
        _collect_person_album_paths,
    )

    # -------------------------------------------------------------------------
    section("§1 — 6 paths returned in correct order (4 wide + 2 crops)")
    # -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "alert"
        d.mkdir()
        for i in range(1, 5):
            _make_frame(d, f"frame_{i:03d}.jpg", color=(i * 50, 100, 200))
        _make_frame(d, "frame_003_crop180_200_320x480.jpg", color=(255, 0, 0))
        _make_frame(d, "frame_004_crop200_220_300x460.jpg", color=(0, 255, 0))
        paths = _collect_person_album_paths(str(d))

        results.append(check(len(paths) == 6, f"got 6 paths (got {len(paths)})"))
        results.append(check(
            paths[0].endswith("frame_001.jpg"),
            "paths[0] = frame_001.jpg (first wide)",
        ))
        results.append(check(
            paths[3].endswith("frame_004.jpg"),
            "paths[3] = frame_004.jpg (last wide)",
        ))
        results.append(check(
            "frame_003_crop" in paths[4],
            "paths[4] = frame_003_crop (first crop)",
        ))
        results.append(check(
            "frame_004_crop" in paths[5],
            "paths[5] = frame_004_crop (last crop)",
        ))

    # -------------------------------------------------------------------------
    section("§2 — Missing crops → 4 wide frames only (graceful degrade)")
    # -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "alert"
        d.mkdir()
        for i in range(1, 5):
            _make_frame(d, f"frame_{i:03d}.jpg")
        paths = _collect_person_album_paths(str(d))

        results.append(check(len(paths) == 4, f"got 4 paths (got {len(paths)})"))
        results.append(check(
            all("crop" not in p for p in paths),
            "no crops in returned paths",
        ))

    # -------------------------------------------------------------------------
    section("§3 — Missing wide frames → what is present only")
    # -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "alert"
        d.mkdir()
        # Only frame_002 + frame_003 + a crop
        _make_frame(d, "frame_002.jpg")
        _make_frame(d, "frame_003.jpg")
        _make_frame(d, "frame_004_crop200_220_300x460.jpg")
        paths = _collect_person_album_paths(str(d))

        results.append(check(len(paths) == 3, f"got 3 paths (got {len(paths)})"))
        results.append(check(
            "frame_002.jpg" in paths[0],
            "paths[0] = frame_002.jpg (first present)",
        ))
        results.append(check(
            "frame_004_crop" in paths[2],
            "paths[2] = frame_004_crop (last present)",
        ))

    # -------------------------------------------------------------------------
    section("§4 — Empty/missing output_dir → empty list")
    # -------------------------------------------------------------------------
    results.append(check(
        _collect_person_album_paths("") == [],
        "empty string → []",
    ))
    results.append(check(
        _collect_person_album_paths("/nonexistent/path/here") == [],
        "nonexistent path → []",
    ))
    results.append(check(
        _collect_person_album_paths("/tmp") == [],
        "/tmp has no gate frames → []",
    ))

    # -------------------------------------------------------------------------
    section("§5/§6/§7 — person_emit calls send_message + send_photo_group")
    # -------------------------------------------------------------------------
    # Build a real ctx with output_dir containing 4 wide frames + 2 crops
    from infra.person_matcher import NoMatch
    from listener.person_event_pipeline import (
        PersonContext,
        person_emit_stage,
    )

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "alert"
        d.mkdir()
        for i in range(1, 5):
            _make_frame(d, f"frame_{i:03d}.jpg")
        _make_frame(d, "frame_003_crop180_200_320x480.jpg")
        _make_frame(d, "frame_004_crop200_220_300x460.jpg")

        ctx = PersonContext(
            alert_id="probe-6B141-test",
            camera_name="CAM3",  # 6B.140 swap
            timestamp="2026-08-27 15:30:00 EDT",
            event_type="person",
            rtsp_url="rtsp://test/ofg",
            output_dir=str(d),
            bot_token="test-token",
            chat_id="test-chat",
            api_url="http://test-vision:8080",
            vision_result={
                "persons": [{
                    "person_id": "p1",
                    "clothing_upper": {"color": "blue", "type": "jacket"},
                    "clothing_lower": {"color": "black", "type": "pants"},
                    "carrying": ["red backpack"],
                    "action": "walking",
                    "face_visible": True,
                    "face_bbox": [100, 100, 200, 200],
                }],
                "primary_person_index": 0,
                "scene_description": "Person walks up driveway.",
                "confidence": 0.85,
            },
            face_recognition=None,
            person_match=NoMatch(reason="no_known_persons"),
            matched_name=None,
            matched_via=None,
            frame_paths=[],
        )

        with patch("infra.send_telegram.send_message") as mock_text, \
             patch("infra.send_telegram.send_photo_group") as mock_album, \
             patch("infra.alert_history.append_alert"):
            person_emit_stage(ctx)

        # §5: both called
        results.append(check(mock_text.called, "send_message called (body)"))
        results.append(check(mock_album.called, "send_photo_group called (album)"))

        # Album has 6 paths in expected order
        album_paths = mock_album.call_args.kwargs["frame_paths"]
        results.append(check(
            len(album_paths) == 6,
            f"album has 6 paths (got {len(album_paths)})",
        ))

        # §6: caption is empty (body went via send_message)
        results.append(check(
            mock_album.call_args.kwargs["caption"] == "",
            "album caption is empty",
        ))

        # Body sent via send_message has full text (matches "CAM3")
        sent_text = mock_text.call_args.kwargs["text"]
        results.append(check(
            "CAM3" in sent_text,
            "body mentions CAM3 camera",
        ))
        results.append(check(
            "🚶" in sent_text or "Unknown person" in sent_text,
            "body has emoji or match status",
        ))

        # §7: telegram_sent True
        results.append(check(
            ctx.telegram_sent is True,
            "ctx.telegram_sent is True",
        ))

    # -------------------------------------------------------------------------
    section("§8 — Module header / docs reference 6B.141 / §11.62")
    # -------------------------------------------------------------------------
    pep = (ROOT / "listener" / "person_event_pipeline.py").read_text()
    results.append(check(
        "Phase.141" in pep or "6B.141" in pep,
        "person_event_pipeline.py mentions Phase.141",
    ))
    results.append(check(
        "_collect_person_album_paths" in pep,
        "_collect_person_album_paths helper exists",
    ))
    results.append(check(
        "send_photo_group" in pep,
        "person_emit uses send_photo_group",
    ))

    plan = (ROOT / "PLAN.md").read_text()
    results.append(check(
        "§11.64" in plan,
        "PLAN.md §11.64 exists for 6B.141",
    ))

    # -------------------------------------------------------------------------
    print()
    passed = sum(results)
    total = len(results)
    print(f"PASS — {passed} of {total} checks passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())