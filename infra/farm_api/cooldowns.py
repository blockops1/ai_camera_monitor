"""farm_api.cooldowns — Cooldowns resource.

STATUS: alpha — V2-068b4
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - None (pipeline state not yet exposed; returns empty list)

OUTPUTS:
    - Cooldown dataclass
    - list_active_cooldowns() -> list[Cooldown]

DOES NOT DO:
    - Read from pipeline runtime state (not yet exposed; returns [])
    - Write cooldowns (write tools ship in V2-069)
    - Connect to external services (pure Python, no I/O)

CALLERS:
    - infra.mcp_server (MCP tool adapter: list_active_cooldowns)
    - Any code that needs a read-only view of active cooldowns

NOTE:
    Pipeline cooldown state lives in infra.pipeline_cooldown._state,
    which is guarded by a threading.Lock and not yet exposed via a
    public accessor. When that changes, this module should read from
    the exposed state instead of returning [].
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cooldown:
    """A single active cooldown entry.

    Attributes:
        camera_id: Camera prefix, e.g. "FRONT", "BACK".
        class_name: COCO class under cooldown, e.g. "car", "dog".
        expires_at: ISO-8601 timestamp after which this cooldown expires.
            Populated when pipeline state is exposed; None until then.
        set_by: Who set this cooldown. Currently "pipeline" (internal).
        reason: Human-readable explanation. Currently empty.
    """

    camera_id: str
    class_name: str
    expires_at: str | None
    set_by: str
    reason: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_active_cooldowns() -> list[Cooldown]:
    """Return a list of currently active cooldowns.

    Reads from pipeline runtime state if exposed, else returns an
    empty list and logs a TODO. Write tools ship in V2-069.

    Returns:
        A list of Cooldown objects. Currently always empty because
        pipeline state (infra.pipeline_cooldown._state) is not yet
        exposed via a public accessor.
    """
    log.info(
        "cooldowns: pipeline state not yet exposed — "
        "list_active_cooldowns returns empty list (TODO)"
    )
    return []
