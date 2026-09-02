"""
test_snapshot_endpoint.py — Tests for the Phase 6B.88 / PLAN §11.19
on-demand /snapshot endpoint.

maintainer asks Jill in chat for "an OFS snapshot" (or OFG). Jill curls
GET /snapshot?camera=<shorthand|canonical>, gets a JPEG, sends via
Telegram. This test file covers the listener HTTP route without
requiring a live RTSP stream — both get_reader() and the reader's
get_recent_frames() are stubbed.

Test inventory (10 cases):
  1. test_resolve_snapshot_camera_maps_OFS_to_canonical
  2. test_resolve_snapshot_camera_maps_OFG_to_canonical
  3. test_resolve_snapshot_camera_passes_through_canonical
  4. test_resolve_snapshot_camera_returns_None_for_unknown
  5. test_snapshot_400_when_camera_missing
  6. test_snapshot_400_when_camera_unknown
  7. test_snapshot_503_when_reader_not_registered
  8. test_snapshot_503_when_reader_has_no_frames
  9. test_snapshot_400_when_max_size_malformed
 10. test_snapshot_200_serves_jpeg_with_correct_headers_and_max_size

The tests below target the helper `_resolve_snapshot_camera` directly
for the pure-function cases (1-4) and use Flask's test_client for
the route-level cases (5-10).
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


from listener.listener import _resolve_snapshot_camera, create_app

# -----------------------------------------------------------------------------
# Pure-function tests: _resolve_snapshot_camera
# -----------------------------------------------------------------------------


def _make_cameras():
    """Minimal cameras dict for testing.

    Phase 6B.167 §13.4 Commit 17 (T3 C17): cameras dict is keyed by
    CAM{N} codes (per infra.cameras._LEGACY_PREFIX_TO_CODE), not
    operator-flavored friendly names. The alias resolver looks up the
    shorthand → CAM{N} mapping in _SNAPSHOT_CAMERA_ALIASES, and a bare
    CAM{N} arg passes through.
    """
    return {
        "CAM3": {"ip": "10.0.0.3"},   # → OUTSIDE_FRONT_GARAGE (synthetic)
        "CAM4": {"ip": "10.0.0.4"},   # → OUTSIDE_FRONT_POWER  (synthetic)
        "CAM5": {"ip": "10.0.0.5"},   # → OUTSIDE_FRONT_SOLAR  (synthetic)
    }


def _make_app_with_cameras():
    return create_app(test_config={"cameras": _make_cameras()})


def test_resolve_snapshot_camera_maps_OFS_to_canonical():
    """Phase 6B.167 §13.4 Commit 17: _SNAPSHOT_CAMERA_ALIASES now maps
    operator shorthand to CAM{N} codes, not operator-flavored names."""
    assert _resolve_snapshot_camera("OFS", _make_cameras()) == "CAM5"


def test_resolve_snapshot_camera_maps_OFG_to_canonical():
    """Phase 6B.167 §13.4 Commit 17: OFG → CAM3 (OUTSIDE_FRONT_GARAGE)."""
    assert _resolve_snapshot_camera("OFG", _make_cameras()) == "CAM3"


def test_resolve_snapshot_camera_passes_through_canonical():
    """Phase 6B.167 §13.4 Commit 17: cameras dict is CAM{N}-keyed —
    a bare CAM{N} arg passes through unchanged."""
    assert (
        _resolve_snapshot_camera("CAM4", _make_cameras())
        == "CAM4"
    )


def test_resolve_snapshot_camera_returns_None_for_unknown():
    cams = _make_cameras()
    assert _resolve_snapshot_camera("XYZ", cams) is None  # unknown alias — §11.80 added OFP/BACK/FDO/OBS to the alias map
    assert _resolve_snapshot_camera("", cams) is None
    assert _resolve_snapshot_camera("outside front solar", cams) is None  # case-sensitive
    assert _resolve_snapshot_camera("Inside Garage", cams) is None


# -----------------------------------------------------------------------------
# Route-level tests using Flask's test_client (stub get_reader + reader)
# -----------------------------------------------------------------------------


def test_snapshot_400_when_camera_missing():
    """No ?camera= -> 400 with known_aliases listed for the operator."""
    app = _make_app_with_cameras()
    client = app.test_client()
    resp = client.get("/snapshot")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "missing_query_param"
    assert "OFS" in body["known_aliases"]
    assert "OFG" in body["known_aliases"]


def test_snapshot_400_when_camera_unknown():
    """Unknown shorthand/canonical -> 400 with both known lists."""
    app = _make_app_with_cameras()
    client = app.test_client()
    resp = client.get("/snapshot?camera=NOPE")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "unknown_camera"
    assert "NOPE" in body["detail"]
    assert "OFS" in body["known_aliases"]
    assert "CAM5" in body["known_canonical"]


def test_snapshot_503_when_reader_not_registered():
    """get_reader(canonical) returns None -> 503 reader_not_booted."""
    app = _make_app_with_cameras()
    import listener.listener as listener_mod

    orig = listener_mod.get_reader
    listener_mod.get_reader = lambda name: None  # type: ignore[assignment]
    try:
        client = app.test_client()
        resp = client.get("/snapshot?camera=OFS")
        assert resp.status_code == 503
        body = resp.get_json()
        assert body["error"] == "reader_not_booted"
        assert "CAM5" in body["detail"]
    finally:
        listener_mod.get_reader = orig


def test_snapshot_503_when_reader_has_no_frames():
    """get_recent_frames returns [] -> 503 no_frames_in_ring_buffer."""
    app = _make_app_with_cameras()
    import listener.listener as listener_mod
    from infra.persistent_rtsp import PersistentRTSPReader

    class _StubReader:
        def get_recent_frames(self, n, output_dir, max_size=None):
            return []  # empty ring buffer

    orig = listener_mod.get_reader
    def _fake_get_reader(name: str) -> PersistentRTSPReader | None:
        return _StubReader()  # type: ignore[return-value]
    listener_mod.get_reader = _fake_get_reader  # type: ignore[assignment]
    try:
        client = app.test_client()
        resp = client.get("/snapshot?camera=OFS")
        assert resp.status_code == 503
        body = resp.get_json()
        assert body["error"] == "no_frames_in_ring_buffer"
        assert "CAM5" in body["detail"]
    finally:
        listener_mod.get_reader = orig


def test_snapshot_400_when_max_size_malformed():
    """max_size=WxH must parse. Anything else -> 400 bad_max_size."""
    app = _make_app_with_cameras()
    client = app.test_client()
    for bad in ("notasize", "100", "100x", "x100", "-10x10", "abcxdef"):
        resp = client.get(f"/snapshot?camera=OFS&max_size={bad}")
        assert resp.status_code == 400, f"max_size={bad!r} should be 400"
        body = resp.get_json()
        assert body["error"] == "bad_max_size"


def test_snapshot_200_serves_jpeg_with_correct_headers_and_max_size():
    """Happy path: stub reader writes a tiny JPEG, route serves it
    with image/jpeg content-type, correct filename, and a
    Cache-Control: no-store header. max_size is passed through to
    the reader's get_recent_frames call."""
    import os as _os

    app = _make_app_with_cameras()
    import listener.listener as listener_mod
    from infra.persistent_rtsp import PersistentRTSPReader

    # Minimal valid JPEG (smallest possible, ~125 bytes). The route
    # doesn't validate the JPEG bytes — it just serves whatever the
    # reader wrote. So a hand-crafted 1x1 black-pixel JPEG works.
    TINY_JPEG = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000"
        "ffdb004300030202020202030202020303030304060404040404"
        "0806060505070907080a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a"
        "ffc00011080001000103012200021101031101"
        "ffc4001f000001050101010101010000000000000000010203040506070809"
        "0a0b"
        "ffc4001f100001050101010101010000000000000000010203040506070809"
        "0a0b"
        "ffda000c03010002110311003f00fbd0"
        "ffd9"
    )

    captured_kwargs = {}

    class _StubReader:
        def get_recent_frames(self, n, output_dir, max_size=None):
            captured_kwargs["n"] = n
            captured_kwargs["output_dir"] = output_dir
            captured_kwargs["max_size"] = max_size
            out_path = _os.path.join(output_dir, "frame_001.jpg")
            with open(out_path, "wb") as f:
                f.write(TINY_JPEG)
            return [out_path]

    orig = listener_mod.get_reader
    def _fake_get_reader2(name: str) -> PersistentRTSPReader | None:
        return _StubReader()  # type: ignore[return-value]
    listener_mod.get_reader = _fake_get_reader2  # type: ignore[assignment]
    try:
        client = app.test_client()

        # 1. No max_size
        captured_kwargs.clear()
        resp = client.get("/snapshot?camera=OFS")
        assert resp.status_code == 200
        assert resp.mimetype == "image/jpeg"
        assert resp.headers.get("Cache-Control") == "no-store"
        cd = resp.headers.get("Content-Disposition", "")
        assert "CAM5.jpg" in cd
        assert resp.data == TINY_JPEG
        assert captured_kwargs["n"] == 1
        assert captured_kwargs["max_size"] is None

        # 2. With max_size — must pass through to the reader
        captured_kwargs.clear()
        resp = client.get("/snapshot?camera=OFG&max_size=1280x720")
        assert resp.status_code == 200
        assert resp.mimetype == "image/jpeg"
        assert captured_kwargs["max_size"] == (1280, 720)
        assert "CAM3.jpg" in resp.headers.get(
            "Content-Disposition", ""
        )
    finally:
        listener_mod.get_reader = orig


# -----------------------------------------------------------------------------
# T3 C19 (Phase 6B.167 §13.5 follow-on): listener must call
# init_reader_registry(load_cameras()) at boot so that
# get_reader(CAM{N}) succeeds when readers are registered under the
# canonical friendly names. Without init, _code_for_name returns None
# inside set_reader(), the code-keyed storage branch is dead code, and
# /snapshot?camera=OFS returns 503 even though _resolve_snapshot_camera
# correctly mapped OFS→CAM5.
# -----------------------------------------------------------------------------


def test_init_reader_registry_enables_code_keyed_lookup():
    """Pins the listener-bootstrap contract for /snapshot CAM{N} lookup.

    Simulates the boot sequence in main(): init_reader_registry(specs)
    → set_reader(spec.name, reader) → get_reader(spec.code) returns
    the same reader. Pre-init, get_reader(spec.code) returns None.
    """
    from infra.cameras import CameraSpec
    from infra.persistent_rtsp import (
        clear_reader_registry,
        get_reader,
        get_reader_by_code,
        init_reader_registry,
        set_reader,
    )

    clear_reader_registry()
    try:
        specs = [
            CameraSpec(
                code="CAM5",
                name="Test Front Solar",
                ip="10.0.0.5",
                zone="yard",
                http_user="u",
                http_pass="p",
            ),
        ]

        class _StubReader:
            def __init__(self, url):
                self._rtsp_url = url

        # Pre-init: register by friendly name → code-keyed lookup fails.
        r_pre = _StubReader("rtsp://u:p@10.0.0.5:554/stream")
        set_reader("Test Front Solar", r_pre)  # type: ignore[arg-type]
        assert get_reader("Test Front Solar") is r_pre
        assert get_reader_by_code("CAM5") is None  # ← the bug

        # Clear and re-run with init_reader_registry called first.
        clear_reader_registry()
        init_reader_registry(specs)
        r_post = _StubReader("rtsp://u:p@10.0.0.5:554/stream")
        set_reader("Test Front Solar", r_post)  # type: ignore[arg-type]

        # Now get_reader(CAM5) finds the reader (init_reader_registry
        # populated _name_to_code, so set_reader dual-wrote under both
        # keys).
        assert get_reader("Test Front Solar") is r_post
        assert get_reader_by_code("CAM5") is r_post
        # And the listener's snapshot lookup path uses get_reader()
        # which is code-aware via _code_for_name.
        assert get_reader("CAM5") is r_post
    finally:
        clear_reader_registry()