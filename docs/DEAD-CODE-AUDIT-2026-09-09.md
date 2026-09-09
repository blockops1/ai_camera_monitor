# Dead-Code Audit — 2026-09-09 (US-020d)

## Purpose

Post-cleanup orphan audit for farm-surveillance-v2. Confirms zero production
modules are orphaned after the deletion of 9 known-orphans from Phase V2-020.

## Methodology

`scripts/audit_orphans.py` scans every production `.py` file (infra/,
listener/, telegram_formatter/, vehicle_matcher/) and for each module counts:

1. **Symbol cross-references** — how many other production files import or
   reference each public function/class defined in the module.
2. **Module-level imports** — how many other files import the module itself
   (e.g. `from infra.paths import FRAMES_DIR`), so modules that export
   constants but not functions aren't falsely flagged.

A module is "orphaned" only if **both** counts are zero.

## Deleted Modules (from Phase V2-020)

| # | Module | Status | Notes |
|---|--------|--------|-------|
| 1 | `vehicle_position/` (package) | deleted | Multi-job violation, replaced by in-line gate logic |
| 2 | `vehicle_identifier/` (package) | deleted | Same — replaced by in-line quick_classifier |
| 3 | `vehicle_matcher/` (old) | deleted | Replaced by new `vehicle_matcher/match.py` |
| 4 | `infra/autonomous_dispatcher.py` | deleted | Out of scope for v2 |
| 5 | `infra/motion_detector.py` (v1) | deleted | Replaced by `infra/gate.py` |
| 6 | `infra/frame_diff.py` (v1) | deleted | Consolidated into gate.py |
| 7 | `infra/notifier.py` (old) | deleted | Replaced by dispatcher + alert/detail |
| 8 | `infra/vision_pool.py` (old) | deleted | Replaced by direct llama.cpp calls |
| 9 | `infra/camera_queue.py` (old) | deleted | Replaced by ring buffer in frame_capture.py |

## Current Production Module Inventory (19 files)

| Module | Symbols | Sym XRefs | Mod Imports | Status |
|--------|---------|-----------|-------------|--------|
| animal_prompt.py | 1 | 1 | 9 | linked |
| camera_creds.py | 5 | 4 | 9 | linked |
| frame_capture.py | 8 | 2 | 8 | linked |
| frame_diff.py | 8 | 4 | 9 | linked |
| gate.py | 15 | 7 | 9 | linked |
| paths.py | 3 | 0 | 13 | linked (constants-only) |
| person_prompt.py | 1 | 1 | 9 | linked |
| pipeline_cooldown.py | 1 | 1 | 9 | linked |
| quick_classifier.py | 9 | 2 | 9 | linked |
| vehicle_prompt.py | 1 | 1 | 9 | linked |
| vision_analyzer.py | 5 | 2 | 8 | linked |
| vm1_prompt.py | 1 | 1 | 9 | linked |
| daemon.py | 7 | 10 | 0 | linked |
| pipeline.py | 5 | 4 | 2 | linked |
| alert.py | 1 | 1 | 3 | linked |
| detail.py | 1 | 1 | 3 | linked |
| dispatcher.py | 6 | 10 | 4 | linked |
| match_alert.py | 1 | 1 | 3 | linked |
| match.py | 1 | 3 | 1 | linked |

**Result: 0 orphans. All 19 production modules are referenced by at least one
other module.**

## Test Suite

- 142 tests collected (132 baseline + 10 from recent additions)
- 141 pass, 1 pre-existing failure (unrelated to this audit)
- Pre-existing failure: `test_home_env.py::test_telegram_token_length_revealed_not_value`
  asserts token length 26; fixture token is 29 chars.

## Conclusion

Zero dead-code orphans remain. The production tree is clean.
