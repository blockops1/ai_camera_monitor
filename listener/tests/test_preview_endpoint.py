"""
test_preview_endpoint.py — Tests for the Phase 6B.124 /preview endpoint.

The /preview route locates Synology preview thumbnails by camera +
timestamp. Unlike /snapshot (which uses live RTSP frames), /preview
reads pre-existing files from the SMB-mounted Synology share. We
monkeypatch `infra.synology_preview.SYNOLOGY_ROOT` to a temporary
directory so tests don't touch the real NAS.

Test inventory (8 cases):
  1. test_preview_400_when_camera_missing
  2. test_preview_400_when_ts_missing
  3. test_preview_400_when_ts_unparseable
  4. test_preview_404_when_no_preview_found
  5. test_preview_200_serves_jpeg_with_correct_headers
  6. test_preview_detail_json_returns_metadata
  7. test_preview_accepts_iso8601_timestamps
  8. test_preview_returns_404_for_unknown_camera
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


import pytest

# infra.synology_preview is operator-infrastructure-specific (Synology NAS
# SMB mount); not shipped in the public repo. Skip these tests on public.
pytest.importorskip("infra.synology_preview", reason="requires operator NAS module")

from infra import synology_preview as sp
from listener.listener import create_app

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _make_cameras() -> dict:
    return {
        "north_yard_cam": {"ip": "198.51.100.103"},
        "south_yard_cam": {"ip": "198.51.100.73"},
    }


def _make_app():
    return create_app(test_config={"cameras": _make_cameras()})


@pytest.fixture(autouse=True)
def fake_synology(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a fake Synology tree with one camera, one session, a few files.

    Applied automatically to every test in this module via monkeypatch.setattr,
    so individual tests don't need to take it as a parameter.
    """
    cam_dir = tmp_path / "north_yard_cam" / "@SSRECMETA" / "Preview"
    session_dir = cam_dir / "20260822PM" / "1787418219"
    session_dir.mkdir(parents=True)
    # 13:03:39 to 13:33:20 EDT, files every 20 seconds
    for offset in range(0, 1800, 20):
        ts = 1787418219 + offset
        (session_dir / str(ts)).write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    monkeypatch.setattr(sp, "SYNOLOGY_ROOT", str(tmp_path))
    return tmp_path


# -----------------------------------------------------------------------------
# Route-level tests
# -----------------------------------------------------------------------------


class TestPreviewEndpoint:
    def test_preview_400_when_camera_missing(self):
        app = _make_app()
        resp = app.test_client().get("/preview?ts=2026-08-22 13:10:00 EDT")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "missing_query_param"
        assert "camera" in body["detail"]

    def test_preview_400_when_ts_missing(self):
        app = _make_app()
        resp = app.test_client().get("/preview?camera=north_yard_cam")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "missing_query_param"
        assert "ts" in body["detail"]

    def test_preview_400_when_ts_unparseable(self):
        app = _make_app()
        resp = app.test_client().get(
            "/preview?camera=north_yard_cam&ts=garbage"
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "invalid_ts"
        assert "garbage" in body["detail"]

    def test_preview_404_when_no_preview_found(
        self, monkeypatch
    ):
        """A timestamp outside the fake tree's session window → 404."""
        # Monkeypatch SYNOLOGY_ROOT to a non-existent path
        monkeypatch.setattr(sp, "SYNOLOGY_ROOT", "/nonexistent/path")
        app = _make_app()
        resp = app.test_client().get(
            "/preview?camera=north_yard_cam&ts=2026-08-22 13:10:00 EDT"
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error"] == "no_preview_found"

    def test_preview_200_serves_jpeg_with_correct_headers(self):
        app = _make_app()
        resp = app.test_client().get(
            "/preview?camera=north_yard_cam&ts=2026-08-22 13:10:00 EDT"
        )
        assert resp.status_code == 200
        assert resp.mimetype == "image/jpeg"
        # Cache-Control no-store mirrors /snapshot behavior
        assert resp.headers.get("Cache-Control") == "no-store"
        # Filename in Content-Disposition uses the arg as-given
        # (spaces replaced with underscores).
        cd = resp.headers.get("Content-Disposition", "")
        assert cd.startswith("inline; filename=")
        assert ".jpg" in cd

    def test_preview_detail_json_returns_metadata(self):
        app = _make_app()
        resp = app.test_client().get(
            "/preview?camera=north_yard_cam&ts=2026-08-22 13:10:00 EDT&detail=json"
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["camera"] == "north_yard_cam"
        assert body["ts_requested"] == "2026-08-22 13:10:00 EDT"
        assert body["preview_path"].endswith(str(1787418599)) or \
               body["preview_path"].endswith(str(1787418619))
        assert body["size_bytes"] > 0

    def test_preview_accepts_iso8601_timestamps(self):
        app = _make_app()
        # ISO 8601 with timezone offset
        resp = app.test_client().get(
            "/preview?camera=north_yard_cam&ts=2026-08-22T13:10:00-04:00"
        )
        assert resp.status_code == 200

    def test_preview_returns_404_for_unknown_camera(self):
        """A camera not in the fake tree → 404 (no preview)."""
        app = _make_app()
        resp = app.test_client().get(
            "/preview?camera=zzz_unknown&ts=2026-08-22 13:10:00 EDT"
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error"] == "no_preview_found"
