"""
state.py — listener-wide singleton STATE dict.

Holds live counters and last-event metadata shared between listener.py
(composition root) and vehicle_event_pipeline.py (pipeline driver).

Lives in its own module (extracted from listener.py on 2026-08-21, Phase
6B.105c) so both modules can import it via the bare-name pattern that
works when listener.py runs as __main__ (sys.path[0] = listener/, where
'listener' as a package name is shadowed by listener.py).

STATUS: stable
THREAD SAFETY: thread-safe for read; mutations are not atomic across
multiple keys, but the caller (the pipeline + the listener) mutates
different keys so no contention in practice.

INPUTS:
    - (no inputs — pure singleton)

OUTPUTS:
    - module-level STATE dict with keys:
        total_alerts (int)
        by_threat_level (dict[int, int]) — -1 = error
        last_alert (dict | None) — last result, used by /status
        last_webhook_at (str | None) — ISO timestamp of last accepted POST
        start_time (str) — ISO timestamp at module load

PUBLIC API:
    STATE: dict  # the singleton, mutated in place

DOES NOT DO:
    - Hold any I/O state (frame paths, alert payloads) — those live in the
      pipeline's AlertContext
    - Persist across restarts — re-init'd from defaults at every boot
    - Decide which alerts to send — owned by infra.notifier.notify()

WHY HERE:
    Extracted from listener.py on 2026-08-21 (Phase 6B.105c). The pipeline
    module (vehicle_event_pipeline.py) needs to bump STATE counters, and
    both modules need to read STATE. Keeping it in listener.py created a
    cross-listener import that failed at runtime because when listener.py
    runs as __main__, sys.path[0] is listener/ and the name 'listener'
    shadows the listener/ package (see pipeline dual-context pattern for
    the full discussion). Moving STATE to its own sibling module under
    listener/ resolves both the import problem and the architectural smell
    of pipeline reaching into listener internals.

RELATED:
    - listener.vehicle_event_pipeline: mutates STATE in emit_result_stage
    - infra.timezone.EDT: source for the start_time timestamp
    - data/state/cooldown_map.json: persistent cooldown state (different
      concern; STATE itself is in-process only)

CALLED BY:
    - listener.listener: bumps total_alerts, by_threat_level, last_webhook_at
    - listener.vehicle_event_pipeline: bumps by_threat_level[-1] (errors),
      total_alerts, by_threat_level, last_alert (success path)
"""

import datetime

# Fixed EDT (UTC-4) per maintainer 2026-08-19 ("US Congress has decided that we're
# not going to move to eastern standard time this winter"). Use
# infra.timezone.EDT — NOT ZoneInfo("America/New_York") which auto-switches
# to EST in November and would re-introduce the UTC-leak bug Phase 6B.99
# just fixed. This deliberately bypasses infra.paths.LOCAL_TZ, which still
# uses ZoneInfo — that's a pre-existing latent bug in infra.paths, not in
# scope for 6B.105c.
from infra.timezone import EDT as _LOCAL_TZ

STATE = {
    "total_alerts": 0,
    "by_threat_level": {0: 0, 1: 0, 2: 0, -1: 0},  # -1 = error
    "last_alert": None,
    "last_webhook_at": None,
    "start_time": datetime.datetime.now(_LOCAL_TZ).isoformat(),
}
