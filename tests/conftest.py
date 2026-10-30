"""
conftest.py — Shared fixtures for farm-surveillance-v2 tests.

STATUS: stable
THREAD SAFETY: single-threaded (pytest fixtures are single-threaded)

INPUTS:
    - None (pytest discovers fixtures automatically)

OUTPUTS:
    - pytest fixtures: sample_alert, sample_frames, mock_llama_server

DOES NOT DO:
    - Make live network calls — all HTTP is mocked via httpx

CALLED BY:
    - tests/test_alert_route.py
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Load .env so all tests have access to required env vars (MOTION_GATE_*,
# TELEGRAM_*, FARMSURV_*, etc.).  Without this, infra imports crash at
# module level on _require_env calls.
# ---------------------------------------------------------------------------
from dotenv import load_dotenv

load_dotenv()

# Operator .env must NOT decide whether heavyweight embedder paths run in
# tests (PERSON_EMBEDDER_LIVE=1 on the operator box leaked into the suite
# and broke embedder_off assertions, 2026-10-30). Default OFF for tests;
# tests that need the live embedder set the flag explicitly and restore.
for _k in ("PERSON_EMBEDDER_LIVE", "ANIMAL_EMBEDDER_LIVE"):
    os.environ[_k] = "0"

# ---------------------------------------------------------------------------
# PyAV stub — the project venv may lack av; tests mock av.open() anyway.
# Must run at import time (before test collection) so infra.frame_capture
# can `import av` successfully.
# ---------------------------------------------------------------------------
import sys as _sys
from unittest.mock import MagicMock, patch

import pytest

_av_stub = MagicMock()
_av_stub.__version__ = "15.1.0"
_av_stub.container = MagicMock()
_av_stub.container.InputContainer = MagicMock
_sys.modules.setdefault("av", _av_stub)

# Patch datetime.UTC for Python < 3.11 compatibility.
# infra/api_server.py uses datetime.UTC at module level which crashes on 3.9.
# We add UTC to the datetime module namespace so `datetime.UTC` resolves.
import datetime
import sys

_dt_mod = sys.modules["datetime"]
if not hasattr(_dt_mod, "UTC"):
    _dt_mod.UTC = datetime.UTC  # type: ignore[attr-defined]

# Stub suntime — time_of_day.py imports it but it's not in the test venv.
# conftest.py runs before test collection so this patches before infra/time_of_day imports.
import types as _types

_suntime = _types.ModuleType("suntime")
_suntime.Sun = MagicMock()
sys.modules["suntime"] = _suntime

# Directory for test data assets.
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture(autouse=True)
def _restore_infra_modules():
    """Session-wide pollution guard: infra.* restored after every test.

    Several test files delete sys.modules['infra*'] to force fresh imports
    (DATA_DIR redirects, env-var reloads). Deleting without restoring leaks
    into later test files: importlib.reload() then fails ("not in
    sys.modules") and @patch("infra.reolink.X") re-imports a SECOND module
    copy, so the patch lands on a module nobody uses and production code
    calls the real reolink-cli. This guard snapshots infra.* before each
    test and puts the exact original module objects back afterwards, making
    module-namespace isolation automatic regardless of what a test does.
    """
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "infra" or name.startswith("infra.")
    }
    # Heal collection-time deletions: if a sibling test file deleted infra.*
    # during collection, repopulate the canonical modules so module-level
    # references and importlib.reload() keep working in this test.
    if "infra" not in sys.modules or "infra.paths" not in sys.modules:
        import importlib as _importlib
        import infra  # noqa: F401
        import infra.paths  # noqa: F401
        _ = _importlib
    yield
    for name in list(sys.modules.keys()):
        if name == "infra" or name.startswith("infra."):
            if saved.get(name) is not sys.modules.get(name):
                del sys.modules[name]
    sys.modules.update(saved)


@pytest.fixture()
def sample_alert():
    """Return a minimal sample alert dict as received from a camera."""
    return {
        "id": "evt-test-001",
        "camera_id": "CAM1",
        "camera_label": "Front Gate",
        "timestamp": "2026-09-07T10:00:00Z",
        "frames": [
            os.path.join(_DATA_DIR, "frame1.jpg"),
            os.path.join(_DATA_DIR, "frame2.jpg"),
        ],
    }


@pytest.fixture()
def sample_frames():
    """Return a list of two dummy JPEG paths pointing into tests/data/."""
    return [
        os.path.join(_DATA_DIR, "frame1.jpg"),
        os.path.join(_DATA_DIR, "frame2.jpg"),
    ]


# restore_environment: session-scoped env-snapshot fixture (see also mock_llama_server for per-test isolation)
@pytest.fixture(autouse=True, scope="session")
def restore_environment():
    """Snapshot os.environ at session start; restore verbatim at teardown.

    Session-scoped so we snapshot once and restore once — avoids per-test
    overhead and works where monkeypatch (function-scoped) cannot.  This
    neutralizes ALL bare ``os.environ[...]`` mutations across the suite so
    tests remain order-independent without per-call monkeypatch wiring.

    Tear-down strategy: clear then update (not update-then-clear) so any
    env vars added during the session are removed and all original vars
    are present after teardown.
    """
    original = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(original)


@pytest.fixture(autouse=True)
def mock_llama_server(monkeypatch):
    """Mock httpx.post to prevent any live HTTP call to the llama-server.

    Returns the mock so tests can inspect call_args.
    """
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"class": "vehicle", "subclass": "sedan", "confidence": "likely", "count": 1, "face_visible": false, "notes": null}'
            }
        }]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("httpx.Client", return_value=mock_client):
        yield mock_client


# ---------------------------------------------------------------------------
# Fixtures for synthetic E2E test (US-013b)
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_gate_verdict():
    """Build a mock GateVerdict with classification='vehicle'."""
    from unittest.mock import MagicMock

    from infra.gate import GateVerdict

    v = MagicMock(spec=GateVerdict)
    v.classification = "vehicle"
    v.class_label = "car"
    v.confidence = 0.95
    v.reason = "high_conf_vehicle"
    v.crop_a = None
    v.crop_b = None
    v.pairwise_diff_path = None
    return v


@pytest.fixture()
def mock_cooldown():
    """Return a PipelineCooldown mock that never suppresses."""
    from unittest.mock import MagicMock

    mc = MagicMock()
    mc.should_suppress.return_value = False
    return mc
