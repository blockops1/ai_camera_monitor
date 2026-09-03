"""Pipeline cooldown + vehicle camera allowlist (§11.115.13).

Two filters that run between Qwen call 1 (shared classify) and Qwen
call 2 (cascade) in `single_pipeline.run`:

  1. PipelineCooldown — property-wide, per-class, time-window throttle
     for person + animal. Fires on Qwen call 1's class label, not on
     the cascade's response. Person: 15-minute cooldown starts when
     the matcher face-matches. Animal: 15-minute cooldown starts on
     any detection. Vehicle: no cooldown (motion-suppression is
     gate's job). Other: no cooldown (no Telegram anyway).

  2. VEHICLE_CAMERAS_ALLOWLIST — only the two outside-solar cameras
     get vehicle matching. Vehicle-class events on the other 4 cameras
     are dropped after Qwen call 1, no cascade, no matcher, no
     Telegram.

Both filters are log-and-return: every suppressed event writes a
log line with the reason so "why no alert?" questions can be answered
by reading the listener log.

STATUS: stable.
THREAD SAFETY: single-threaded. The PipelineCooldown state is a plain
              dict mutated by the listener's webhook-handling thread;
              external callers must serialize access. The listener
              processes webhooks sequentially so this is fine; if the
              webhook model changes to threaded/async, this becomes a
              threading.Lock wrapper.
INPUTS: (camera_code, class_label, now_epoch) for both filters.
OUTPUTS: should_suppress returns tuple[bool, str]; record_hit returns
         bool (True if hit was recorded). No file writes, no network
         calls. Module-level logging at INFO via `log = logging...`.
PUBLIC API: PipelineCooldown.should_suppress, .record_hit;
            VEHICLE_CAMERAS_ALLOWLIST frozenset,
            is_vehicle_allowed(camera_code).
DOES NOT DO: Suppress person on face-match (handled by matcher's
             matched field). Persist cooldown state across restarts.
             Throttle vehicle events (gate's `is_suppress` does).
             Cross-process coordination — single-listener only.
CALLED BY: listener.single_pipeline.run (Stage 4.5 between cascade
           calls; Stage 8 post-matcher).
CALLS INTO: time.time() (stdlib only); infra.cameras.by_code() at
            module import for allowlist resolution.
RELATED: infra.classify_schema.ClassLabel for class_label values.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vehicle camera allowlist (§11.115.13)
# ---------------------------------------------------------------------------
#
# Vehicle matching only runs on these two cameras. Vehicle-class events
# from the other four cameras are dropped after Qwen call 1 (no cascade,
# no matcher, no Telegram). The friendly names below are resolved to
# CAM{N} codes via infra.cameras.by_code() before lookup.
#
# Outside Front Solar (OFS / CAM5) — driveway-facing, vehicles enter here.
# Outside Back Solar (OBS / CAM6) — driveway-facing, vehicles exit here.
# Other four cameras (front door, back door, garage, front power) are
# pedestrian / non-driveway surfaces — vehicle alerts on them are noise.
VEHICLE_CAMERAS_ALLOWLIST_FRIENDLY: frozenset[str] = frozenset(
    {"Outside Front Solar", "Outside Back Solar"}
)

# Resolved CAM{N} codes. Populated at module import from
# infra.cameras.by_code(). Falls back to the friendly set if the
# cameras module is not importable (test environments, single-context
# runs) — see _resolve_allowlist_codes().
VEHICLE_CAMERAS_ALLOWLIST: frozenset[str] = frozenset()


def _resolve_allowlist_codes() -> frozenset[str]:
    """Resolve friendly allowlist names to CAM{N} codes.

    Falls back to the friendly names if infra.cameras cannot resolve
    them (test fixtures, missing camera-creds.env, etc.). The fallback
    is intentional: callers that use friendly names in fixtures still
    match.
    """
    try:
        from infra.cameras import code_for
    except Exception:  # pragma: no cover — defensive
        return VEHICLE_CAMERAS_ALLOWLIST_FRIENDLY
    resolved: set[str] = set()
    for friendly in VEHICLE_CAMERAS_ALLOWLIST_FRIENDLY:
        try:
            resolved.add(code_for(friendly))
        except Exception:  # pragma: no cover — defensive
            resolved.add(friendly)  # fall back to friendly name
    return frozenset(resolved)


VEHICLE_CAMERAS_ALLOWLIST = _resolve_allowlist_codes()


def is_vehicle_allowed(camera_code: str) -> bool:
    """True if vehicle matching is permitted for this camera_code.

    `camera_code` is the CAM{N} code resolved via infra.cameras.by_code()
    or `camera_code_lookup`. Falls back to friendly-name comparison
    if camera_code is not in the resolved set (handles test fixtures
    that pass friendly names directly).
    """
    if camera_code in VEHICLE_CAMERAS_ALLOWLIST:
        return True
    return camera_code in VEHICLE_CAMERAS_ALLOWLIST_FRIENDLY


# ---------------------------------------------------------------------------
# Pipeline cooldown (§11.115.13)
# ---------------------------------------------------------------------------
#
# Property-wide cooldown for person + animal classes. Window is 15
# minutes per class. The cooldown fires on Qwen call 1's class label
# (post-classify, pre-cascade), so a suppressed event burns ZERO Qwen
# call 2 cost.
#
# Person: record_hit only when matcher returned a successful face match
# (matched=True). Until a face is matched, person events flow through
# normally — this is intentional. The user wants the FIRST face match
# to be loud, then suppress for 15 min.
#
# Animal: record_hit on any detection (matched or not). The user said
# "once I see an animal, I don't need another alert about an animal
# for 15 minutes" — AnimalNoMatch still means "I saw an animal."
#
# Vehicle: never touches cooldown state (motion-suppression is the
# gate's job, not this module's).
#
# Other: never touches cooldown state (no Telegram anyway).
PERSON_COOLDOWN_SECONDS: int = 15 * 60
ANIMAL_COOLDOWN_SECONDS: int = 15 * 60
COOLDOWN_CLASSES: frozenset[str] = frozenset({"person", "animal"})


class PipelineCooldown:
    """Property-wide, per-class, time-window throttle.

    State is in-memory. Listener restart wipes the cooldown (intentional —
    after a restart we want fresh detections, not stale suppression).
    """

    def __init__(self) -> None:
        # {class_label: last_hit_epoch_seconds}
        self._last_hit: dict[str, float] = {}

    def should_suppress(
        self,
        class_label: str,
        now_epoch: float | None = None,
    ) -> tuple[bool, str]:
        """Pre-cascade check: should this class event be suppressed?

        Args:
            class_label: ClassLabel.value string ("person", "animal",
                "vehicle", "other"). Vehicle + other pass through
                unconditionally.
            now_epoch: Override for tests. Defaults to time.time().

        Returns:
            (should_suppress, reason). reason is "no_cooldown_for_class"
            for vehicle/other, "no_prior_hit" if no hit on record yet,
            "cooldown_active_{N}s_remaining" if within window,
            "cooldown_expired" if past window.
        """
        if class_label not in COOLDOWN_CLASSES:
            return False, "no_cooldown_for_class"

        last = self._last_hit.get(class_label)
        if last is None:
            return False, "no_prior_hit"

        window = (
            PERSON_COOLDOWN_SECONDS
            if class_label == "person"
            else ANIMAL_COOLDOWN_SECONDS
        )
        now = now_epoch if now_epoch is not None else time.time()
        elapsed = now - last
        if elapsed < window:
            remaining = int(window - elapsed)
            return True, f"cooldown_active_{remaining}s_remaining"
        return False, "cooldown_expired"

    def record_hit(
        self,
        class_label: str,
        *,
        matcher_hit: bool,
        now_epoch: float | None = None,
    ) -> bool:
        """Post-matcher update: record a hit if this class warrants one.

        Args:
            class_label: "person" / "animal" / "vehicle" / "other".
            matcher_hit: True if the matcher returned a successful
                identification (face match for person, any detection
                for animal). False = no-op for person; records for
                animal regardless.
            now_epoch: Override for tests.

        Returns:
            True if the hit was recorded, False if it was a no-op
            (wrong class, or person without face match).
        """
        if class_label not in COOLDOWN_CLASSES:
            return False
        # Person: only record on face match.
        # Animal: record on any detection (matcher_hit is advisory —
        # AnimalNoMatch still means we saw an animal, so we record).
        if class_label == "person" and not matcher_hit:
            return False
        now = now_epoch if now_epoch is not None else time.time()
        self._last_hit[class_label] = now
        return True
