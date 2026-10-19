"""
night_corroborate.py — Night-time vehicle corroboration gate (US-114a).

STATUS: new (US-114a, 2026-10-19)
THREAD SAFETY: single-threaded, no shared mutable state
IMPORT ORDER: stdlib -> third-party -> local (infra.)

INPUTS:
    - env vars: FARMSURV_NIGHT_CORROBORATE (off|night_vehicle|all)
    - vm1_result: dict with keys class, confidence (string enum)
    - vm2_result: dict with keys vehicle_features (dict), color (str|null)
    - timestamp: tz-aware UTC datetime

OUTPUTS:
    - corroborate_enabled() -> str: current mode (off|night_vehicle|all)
    - night_corroborate(vm1_result, vm2_result, timestamp, alert_id, camera_id)
      -> bool: True to deliver, False to suppress (reason=flare_suspect)

DOES NOT DO:
    - Night window computation (delegates to time_of_day.is_night_at_edt)
    - Gate classification (caller decides)
    - VM1/VM2 inference

CALLS INTO:
    - infra.time_of_day.is_night_at_edt

CALLER:
    - listener/daemon.py (vm1_override_gate path + night_rescue path)
"""
from __future__ import annotations

import logging
import os
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

from infra.time_of_day import is_night_at_edt

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env helpers (read at CALL time, not import time — pattern: night_rescue ~54)
# ---------------------------------------------------------------------------

_VALID_MODES = frozenset({"off", "night_vehicle", "all"})
_DEFAULT_MODE = "off"


def corroborate_enabled() -> str:
    """Read FARMSURV_NIGHT_CORROBORATE at call time.

    off      = OFF (default, byte-identical pre-change behavior).
    night_vehicle = ENABLED for vm1_class=vehicle only.
    all      = ENABLED for all classes (reserved for future).

    Unknown values → off + warning.

    Returns:
        Mode string.
    """
    raw = os.environ.get("FARMSURV_NIGHT_CORROBORATE", _DEFAULT_MODE)
    mode = raw.strip().lower()
    if mode not in _VALID_MODES:
        warnings.warn(
            f"night_corroborate: unknown mode {raw!r}, defaulting to {_DEFAULT_MODE}",
            stacklevel=2,
        )
        return _DEFAULT_MODE
    return mode


def _count_vm2_features(vm2_result: dict) -> int:
    """Count non-null values in vm2 vehicle_features + color.

    Args:
        vm2_result: VM2 detail dict.

    Returns:
        Number of non-null feature values.
    """
    count = 0
    vf = vm2_result.get("vehicle_features")
    if isinstance(vf, dict):
        for value in vf.values():
            if value is not None and str(value).strip().lower() != "null":
                count += 1
    color = vm2_result.get("color")
    if color is not None and str(color).strip().lower() != "null":
        count += 1
    return count


def night_corroborate(
    vm1_result: dict,
    vm2_result: dict | None,
    timestamp: datetime,
    alert_id: str,
    camera_id: str,
) -> bool:
    """Decide whether to deliver or suppress a night vehicle alert.

    Rule (vm1_class=vehicle, night only):
      Deliver only if AT LEAST ONE:
        (a) vm1 confidence == "definite", OR
        (b) vm2 vehicle_features has >= 2 non-null values.
      Else suppress as flare_suspect (drop path, keep event record).
      Person/animal classes pass through unchanged.

    Args:
        vm1_result: VM1 classification dict (class, confidence).
        vm2_result: VM2 detail dict, or None (night_rescue path has no vm2).
        timestamp: UTC datetime from the webhook.
        alert_id: Alert identifier.
        camera_id: Camera identifier.

    Returns:
        True to deliver, False to suppress.
    """
    mode = corroborate_enabled()
    if mode == "off":
        return True

    vm1_class = vm1_result.get("class", "")

    # Mode "night_vehicle" — only apply to vehicle class.
    if mode == "night_vehicle" and vm1_class != "vehicle":
        return True

    # Mode "all" — apply to everything (person/animal included, reserved).
    # Person/animal are exempt in practice by design (see problem statement).

    # Daytime check — rule does NOT apply during day.
    if not is_night_at_edt(timestamp):
        return True

    # vm1_class=vehicle path: apply corroboration.
    if vm1_class != "vehicle":
        return True

    # Condition (a): definite confidence → deliver.
    conf = vm1_result.get("confidence", "")
    if isinstance(conf, str) and conf.strip().lower() == "definite":
        return True

    # Condition (b): vm2 features >= 2 → deliver.
    if vm2_result is not None:
        feats = _count_vm2_features(vm2_result)
        if feats >= 2:
            return True

    # Suppressed: not definite AND < 2 features.
    log.info(
        f"night_corroborate: flare_suspect alert_id={alert_id} "
        f"camera={camera_id} vm1_class={vm1_class} "
        f"vm1_conf={conf} feats={_count_vm2_features(vm2_result) if vm2_result else 0}"
    )
    return False
