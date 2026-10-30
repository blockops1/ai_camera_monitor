"""farm_api.vision — Vision analyzer status resource.

STATUS: alpha -- V2-068b8
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - infra/vision_analyzer.py module-level constants (_VISION_URL, _VISION_MODEL)

OUTPUTS:
    - VisionStatus dataclass
    - get_vision_status() -> VisionStatus

DOES NOT DO:
    - Query the actual llama-server for live health.
    - Read runtime counters (queue depth, latency, call counts).

RUNTIME GAP (documented 2026-09-22):
    vision_analyzer.py exposes no runtime state — no queue, no heartbeat
    file, no call counters. Fields that require runtime data (queue_depth,
    last_call_latency_ms, calls_last_24h) return None. If the analyzer
    is extended to expose these (e.g. a stats dict or a heartbeat JSON),
    get_vision_status() can be refactored to read them without changing
    the VisionStatus schema.

CALLERS:
    - infra.mcp_server (MCP tool adapter: get_vision_status)
    - Any code that needs a read-only snapshot of vision subsystem state.
"""

import logging
import re
from dataclasses import dataclass

from infra import vision_analyzer

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_LAN_IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
REDACTED = "[REDACTED-IP]"


def _redact_ip(value: str) -> str:
    """Replace all IPv4 addresses with [REDACTED-IP]."""
    return _LAN_IP_RE.sub(REDACTED, value)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisionStatus:
    """Read-only snapshot of the vision analyzer subsystem.

    Attributes:
        model_name: The model name the analyzer is configured to use,
            or None if not available.
        base_url_redacted: The base URL with IPs redacted, or None.
        queue_depth: Number of items in the analyzer's internal queue.
            Currently None — vision_analyzer.py has no exposed queue.
        last_call_latency_ms: Milliseconds since the last classify call.
            Currently None — no latency tracking.
        calls_last_24h: Number of vision calls in the last 24 hours.
            Currently None — no call counter.
    """

    model_name: str | None
    base_url_redacted: str | None
    queue_depth: int | None
    last_call_latency_ms: float | None
    calls_last_24h: int | None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_vision_status() -> VisionStatus:
    """Return a VisionStatus snapshot of the vision analyzer.

    Reads env-level config from infra.vision_analyzer module-level
    constants. Runtime fields (queue_depth, latency, call count) are
    None because the analyzer does not expose that state today.

    Returns:
        A VisionStatus dataclass with all fields populated (runtime
        fields will be None until the analyzer exposes counters).
    """
    model_name = getattr(vision_analyzer, "_VISION_MODEL", None)

    base_url = getattr(vision_analyzer, "_VISION_URL", None)
    base_url_redacted = _redact_ip(base_url) if base_url else None

    return VisionStatus(
        model_name=model_name,
        base_url_redacted=base_url_redacted,
        queue_depth=None,
        last_call_latency_ms=None,
        calls_last_24h=None,
    )
