"""
test_status_endpoint_no_prompt_mode_6B102.py — Phase.102 / PLAN §11.30.

Phase.78 (2026-08-14) deleted VEHICLE_COMBINED_PROMPT_TEMPLATE (and
VEHICLE_MOTION_PROMPT_TEMPLATE) from infra/prompt_templates.py. Phase.50's
`prompt_mode` block in listener/listener.py lazy-imported those names and
silently caught the resulting ImportError, so /status had been returning
`prompt_mode={"error": "cannot import name 'VEHICLE_COMBINED_PROMPT_TEMPLATE'..."}`
for ~5 days.

Phase.102 deletes that block entirely. These tests pin:

  1. /status no longer carries a `prompt_mode` key
  2. /status does not crash (would have crashed with ImportError if the
     lazy import path was still wired up)
  3. Other /status fields operators depend on are still present
     (matcher_telemetry, motion_cooldown, matcher_failures, webhook_executor)

Test inventory (4 cases):
  1. test_status_200_no_prompt_mode_field
  2. test_status_contains_other_expected_fields
  3. test_status_no_import_error_in_any_value
  4. test_prompt_module_no_longer_exports_combined_template_constant
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


from listener.listener import create_app

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _make_cameras():
    """Minimal cameras dict for create_app() test_config."""
    return {
        "CAM5": {"ip": "192.168.1.103"},
        "CAM3": {"ip": "192.168.1.73"},
        "CAM4": {"ip": "192.168.1.20"},
    }


def _make_client():
    app = create_app(test_config={"cameras": _make_cameras()})
    return app.test_client()


def _get_status_json(client) -> dict[str, Any]:
    r = client.get("/status")
    assert r.status_code == 200, f"/status returned {r.status_code}, expected 200"
    return cast(dict[str, Any], json.loads(r.data))


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_status_200_no_prompt_mode_field():
    """Phase.102: /status must not include a prompt_mode key.

    Pre-6B.102: /status returned prompt_mode={"error": "...ImportError..."}
    Post-6B.102: prompt_mode key is absent from the response.
    """
    client = _make_client()
    body = _get_status_json(client)

    assert "prompt_mode" not in body, (
        f"FAIL: /status still carries prompt_mode field. "
        f"value={body.get('prompt_mode')!r}. "
        f"Phase.102 deleted this block; if you're seeing this, "
        f"the deletion was reverted or merged incorrectly."
    )


def test_status_contains_other_expected_fields():
    """Sanity: the /status fields operators depend on are still present."""
    client = _make_client()
    body = _get_status_json(client)

    expected = {
        "status",
        "cameras_loaded",
        "uptime_seconds",
        "matcher_telemetry",
        "motion_cooldown",
        "matcher_failures",
        "webhook_executor",
    }
    missing = expected - set(body.keys())
    assert not missing, f"/status missing expected fields: {sorted(missing)}"


def test_status_no_import_error_in_any_value():
    """Phase.78's silent ImportError shouldn't be hiding anywhere in /status.

    Defense-in-depth: even though prompt_mode was the only place the lazy
    import lived, an unrelated refactor could resurrect the same pattern.
    Scan every value for ImportError / ModuleNotFoundError text.
    """
    client = _make_client()
    body = _get_status_json(client)

    def _walk(o, path="root"):
        if isinstance(o, dict):
            for k, v in o.items():
                yield from _walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                yield from _walk(v, f"{path}[{i}]")
        elif isinstance(o, str):
            yield path, o

    leaks = []
    for path, val in _walk(body):
        if "ImportError" in val or "ModuleNotFoundError" in val:
            leaks.append((path, val[:120]))

    assert not leaks, (
        f"/status response contains an ImportError leak: {leaks!r}. "
        f"Phase.78's silent-import-failure pattern should not return."
    )


def test_prompt_module_no_longer_exports_combined_template_constant():
    """Defensive: infra.prompt_templates must not re-export the deleted name.

    If someone re-adds VEHICLE_COMBINED_PROMPT_TEMPLATE to prompt_templates.py
    thinking it's still consumed, this test will fail before any listener
    code is reached.
    """
    from infra import prompt_templates as pt

    assert not hasattr(pt, "VEHICLE_COMBINED_PROMPT_TEMPLATE"), (
        "VEHICLE_COMBINED_PROMPT_TEMPLATE was re-added to infra.prompt_templates. "
        "Phase.78 deliberately deleted it; re-adding resurrects the "
        "silent-ImportError pattern Phase.102 cleaned up."
    )
    assert not hasattr(pt, "VEHICLE_MOTION_PROMPT_TEMPLATE"), (
        "VEHICLE_MOTION_PROMPT_TEMPLATE was re-added to infra.prompt_templates. "
        "Phase.78 deleted both motion-judging prompts together."
    )
