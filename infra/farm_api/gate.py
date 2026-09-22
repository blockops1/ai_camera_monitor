"""farm_api.gate — Motion-gate configuration and suppression rules.

STATUS: alpha - V2-068b3
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - os.environ (MOTION_GATE_NIGHT_SUPPRESS_ENABLED, FARMSURV_GLOBAL_CHANGE_MAX_PCT,
      MOTION_GATE_V2, motion_threshold)
    - data/gate_suppression/ (filesystem, initially empty)

OUTPUTS:
    - GateConfig dataclass
    - SuppressionRule dataclass
    - get_gate_config() -> GateConfig
    - list_suppression_rules() -> list[SuppressionRule]

DOES NOT DO:
    - Modify gate configuration (operator config via env vars only)
    - Enforce suppression rules (caller decides)
    - Connect to external services (pure env + filesystem reads)

CALLERS:
    - infra.mcp_server (MCP tool adapters: gate_config, suppression_rules)
    - Any code that needs a read-only view of gate configuration
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from infra.paths import DATA_DIR

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    """Read-only snapshot of the motion gate's configuration knobs.

    Attributes:
        motion_threshold: Per-class confidence threshold (float).
            Default 0.50; overridden per-camera in
            config/motion_gate_thresholds.json.
        night_suppress_enabled: Whether night-time suppression is active.
            Env var MOTION_GATE_NIGHT_SUPPRESS_ENABLED (0|1, default 0).
        global_change_max_pct: Diff-bbox area threshold for global-change
            suppression. Env var FARMSURV_GLOBAL_CHANGE_MAX_PCT (default 60).
        v2_enabled: Whether V2 fallback mode is active.
            Env var MOTION_GATE_V2 (1|0, default 0).
    """
    motion_threshold: float
    night_suppress_enabled: bool
    global_change_max_pct: float
    v2_enabled: bool


@dataclass(frozen=True)
class SuppressionRule:
    """A single suppression rule for the motion gate.

    Attributes:
        camera_id: Camera prefix, e.g. "FRONT", "BACK".
        class_name: COCO class to suppress, e.g. "dog", "cat".
        reason: Human-readable explanation for the suppression.
        expires_at: ISO-8601 timestamp after which this rule is inactive.
            None means the rule does not expire.
    """
    camera_id: str
    class_name: str
    reason: str
    expires_at: str | None


# ---------------------------------------------------------------------------
# Suppression rules storage
# ---------------------------------------------------------------------------

GATE_SUPPRESSION_DIR = Path(DATA_DIR) / "gate_suppression"


def _read_suppression_rules_file() -> list[dict]:
    """Read the suppression rules JSONL file.

    File path: data/gate_suppression/rules.jsonl
    Format: one JSON object per line, with keys:
        camera_id, class_name, reason, expires_at (optional ISO-8601 string)

    Returns:
        List of raw dicts. Empty list if file does not exist.
    """
    rules_path = GATE_SUPPRESSION_DIR / "rules.jsonl"
    if not rules_path.is_file():
        return []
    try:
        rules: list[dict] = []
        for line in rules_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rules.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("gate: malformed suppression rule line: %s", line)
        return rules
    except OSError as exc:
        log.warning("gate: failed to read suppression rules: %s", exc)
        return []


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_gate_config() -> GateConfig:
    """Return the current motion gate configuration.

    Reads environment variables to populate the config:
      - MOTION_GATE_NIGHT_SUPPRESS_ENABLED (0|1, default 0)
      - FARMSURV_GLOBAL_CHANGE_MAX_PCT (float, default 60)
      - MOTION_GATE_V2 (1|0, default 0)
      - MOTION_GATE_CONFIDENCE_THRESHOLD (float, default 0.50)

    Returns:
        GateConfig with the current env var values.
    """
    # Night suppress: 0|1 string -> bool
    night_suppress = os.environ.get(
        "MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0"
    ).strip() == "1"

    # Global change max pct: float, default 60
    try:
        global_change_max_pct = float(
            os.environ.get("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "60").strip()
        )
    except ValueError:
        global_change_max_pct = 60.0

    # V2 mode: 0|1 string -> bool
    v2_enabled = os.environ.get("MOTION_GATE_V2", "0").strip() == "1"

    # Motion threshold: float, default 0.50
    try:
        motion_threshold = float(
            os.environ.get("MOTION_GATE_CONFIDENCE_THRESHOLD", "0.50").strip()
        )
    except ValueError:
        motion_threshold = 0.50

    return GateConfig(
        motion_threshold=motion_threshold,
        night_suppress_enabled=night_suppress,
        global_change_max_pct=global_change_max_pct,
        v2_enabled=v2_enabled,
    )


def list_suppression_rules() -> list[SuppressionRule]:
    """Return the list of active suppression rules.

    Reads from data/gate_suppression/rules.jsonl. Rules with an
    expires_at timestamp in the past are silently excluded.

    Returns:
        A list of SuppressionRule objects. Empty list if no file or
        no active rules.
    """
    raw_rules = _read_suppression_rules_file()
    now_str = _now_iso()
    result: list[SuppressionRule] = []

    for raw in raw_rules:
        camera_id = raw.get("camera_id", "")
        class_name = raw.get("class_name", "")
        reason = raw.get("reason", "")
        expires_at = raw.get("expires_at")

        # Skip expired rules
        if expires_at is not None:
            try:
                if expires_at < now_str:
                    continue
            except TypeError:
                # Non-string expires_at — skip
                continue

        result.append(SuppressionRule(
            camera_id=camera_id,
            class_name=class_name,
            reason=reason,
            expires_at=expires_at,
        ))

    return result
