"""§11.115.14 live-observation smoke script (filter module only).

Smoke check for `listener/pipeline_filters.py`:
- Constants are sensible
- is_vehicle_allowed works on real camera codes
- Cooldown state transitions are correct (property-wide, 15-min)
- All drop paths return the documented reason strings

Does NOT exercise the full pipeline (that requires real Qwen).
Run with: .venv/bin/python scripts/smoke_filters_111514.py
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from listener.pipeline_filters import (
    ANIMAL_COOLDOWN_SECONDS,
    COOLDOWN_CLASSES,
    PERSON_COOLDOWN_SECONDS,
    PipelineCooldown,
    VEHICLE_CAMERAS_ALLOWLIST,
    VEHICLE_CAMERAS_ALLOWLIST_FRIENDLY,
    is_vehicle_allowed,
)


def banner(msg: str) -> None:
    print()
    print("=" * 72)
    print(msg)
    print("=" * 72)


banner("Step 1: constants")
print(f"PERSON_COOLDOWN_SECONDS = {PERSON_COOLDOWN_SECONDS} ({PERSON_COOLDOWN_SECONDS // 60} min)")
print(f"ANIMAL_COOLDOWN_SECONDS = {ANIMAL_COOLDOWN_SECONDS} ({ANIMAL_COOLDOWN_SECONDS // 60} min)")
print(f"COOLDOWN_CLASSES = {sorted(COOLDOWN_CLASSES)}")
assert PERSON_COOLDOWN_SECONDS == 15 * 60, "person cooldown must be 15 min"
assert ANIMAL_COOLDOWN_SECONDS == 15 * 60, "animal cooldown must be 15 min"
assert COOLDOWN_CLASSES == {"person", "animal"}, f"cooldown classes wrong: {COOLDOWN_CLASSES}"

banner("Step 2: VEHICLE_CAMERAS_ALLOWLIST")
print(f"codes:    {sorted(VEHICLE_CAMERAS_ALLOWLIST)}")
print(f"friendly: {sorted(VEHICLE_CAMERAS_ALLOWLIST_FRIENDLY)}")
assert "CAM5" in VEHICLE_CAMERAS_ALLOWLIST, "CAM5 (Outside Front Solar) must be in allowlist"
assert "CAM6" in VEHICLE_CAMERAS_ALLOWLIST, "CAM6 (Outside Back Solar) must be in allowlist"
assert len(VEHICLE_CAMERAS_ALLOWLIST) == 2, f"exactly 2 cameras allowed, got {len(VEHICLE_CAMERAS_ALLOWLIST)}"

banner("Step 3: is_vehicle_allowed for real cameras")
real_cameras = [
    ("CAM1", "Front Door Outside"),
    ("CAM2", "Back Door Inside"),
    ("CAM3", "Outside Front Garage"),
    ("CAM4", "Outside Front Power"),
    ("CAM5", "Outside Front Solar"),
    ("CAM6", "Outside Back Solar"),
]
for code, name in real_cameras:
    by_code = is_vehicle_allowed(code)
    by_name = is_vehicle_allowed(name)
    expected = code in {"CAM5", "CAM6"}
    ok = "OK" if by_code == expected and by_name == expected else "FAIL"
    print(f"  [{ok}] {code:5s} ({name:25s}) -> code={by_code} name={by_name} expected={expected}")
    assert by_code == expected, f"{code} allowlist check failed"
    assert by_name == expected, f"{name} allowlist check failed"

banner("Step 4: cooldown state transitions")
cd = PipelineCooldown()

# First event for person should pass
sup, reason = cd.should_suppress("person")
assert sup is False, f"first person event should not suppress, got {sup}"
print(f"  first person event -> (suppress={sup}, reason={reason!r})")
assert "no_prior" in reason or "first" in reason.lower() or reason == "no_cooldown_active", \
    f"expected first-event reason, got {reason!r}"

# Record a face match
cd.record_hit("person", matcher_hit=True)
print(f"  after record_hit(person, matcher_hit=True)")
print(f"    _last_hit = {cd._last_hit}")

# Now another person event within 15 min should suppress
sup, reason = cd.should_suppress("person")
assert sup is True, f"person within 15 min of face match should suppress, got {sup}"
print(f"  second person event -> (suppress={sup}, reason={reason!r})")
assert "cooldown_active" in reason or "remaining" in reason, \
    f"expected cooldown_active reason, got {reason!r}"

# Animal is independent
sup, reason = cd.should_suppress("animal")
assert sup is False, f"animal has independent state from person, got {sup}"
print(f"  animal (independent state) -> (suppress={sup}, reason={reason!r})")

# Vehicle never cooldowns
sup, reason = cd.should_suppress("vehicle")
assert sup is False, f"vehicle is never cooldowned, got {sup}"
print(f"  vehicle (no cooldown) -> (suppress={sup}, reason={reason!r})")

# Other never cooldowns
sup, reason = cd.should_suppress("other")
assert sup is False, f"other is never cooldowned, got {sup}"
print(f"  other (no cooldown) -> (suppress={sup}, reason={reason!r})")

# Person record_hit without face match should NOT suppress
cd2 = PipelineCooldown()
cd2.record_hit("person", matcher_hit=False)
sup, reason = cd2.should_suppress("person")
assert sup is False, f"person without face match should not suppress, got {sup}"
print(f"  person no-face-match record_hit -> next event (suppress={sup}, reason={reason!r})")

# Animal record_hit on ANY detection (matched or not)
cd3 = PipelineCooldown()
cd3.record_hit("animal", matcher_hit=False)
sup, reason = cd3.should_suppress("animal")
assert sup is True, f"animal record_hit (any) should suppress next, got {sup}"
print(f"  animal any-detection record_hit -> next event (suppress={sup}, reason={reason!r})")

# Backdated hit expires
cd4 = PipelineCooldown()
cd4._last_hit["person"] = time.time() - (PERSON_COOLDOWN_SECONDS + 60)
sup, reason = cd4.should_suppress("person")
assert sup is False, f"hit older than window should not suppress, got {sup}"
print(f"  backdated hit (>15 min old) -> (suppress={sup}, reason={reason!r})")

banner("Step 5: shared cooldown state across cameras (property-wide)")
cd_shared = PipelineCooldown()
cd_shared.record_hit("person", matcher_hit=True)
print("Recorded person hit on CAM1 (simulated)")
print("Now check suppression on CAM5:")
sup, reason = cd_shared.should_suppress("person")
assert sup is True, f"property-wide should suppress across cameras, got {sup}"
print(f"  CAM5 person event -> (suppress={sup}, reason={reason!r})")

print()
print("=" * 72)
print("All filter checks passed.")
print("=" * 72)