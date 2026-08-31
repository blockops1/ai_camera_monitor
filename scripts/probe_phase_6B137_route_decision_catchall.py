"""Phase.137 probe - verify _route_decision catchall fix (§11.59).

Production bug (2026-08-23 to 2026-08-27): _route_decision catchall returned
decision="vehicle" regardless of whether a vehicle-class detection was
present. That crashed the vehicle pipeline on alerts where the actual
detection was person, train, bench, zebra, fire hydrant, etc.

Evidence: 225 occurrences of "decision=vehicle class={person|train|zebra|...}"
in logs/launchctl-stderr.log.

The fix splits rule 5 into:
  - vehicle is in the mix -> vehicle pipeline ("mixed_vehicle_wins")
  - no vehicle in the mix -> suppress with class-named reason

This probe pokes _route_decision directly across four representative
inputs (one per historical fire case), then greps the source for the
text that described the bug.

Usage: just run it.
    python scripts/probe_phase_6B137_route_decision_catchall.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.14" / "site-packages"))
sys.path.insert(0, str(ROOT))

from infra.quick_classifier import QuickVerdict
from listener.motion_gate_pipeline import (
    THRESHOLDS_BY_CLASS,
    _route_decision,
)


def _verdict(class_name, conf, decision="pass_with_hint"):
    return QuickVerdict(
        top_class=class_name,
        top_confidence=conf,
        decision=decision,
    )


def case(name, va, vb, expect_decision, expect_label, expect_reason_contains):
    """Drive _route_decision and check the contract."""
    decision, label, _conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    ok_decision = decision == expect_decision
    ok_label = label == expect_label
    ok_reason = expect_reason_contains in reason
    ok = ok_decision and ok_label and ok_reason
    print(
        f"  [{'PASS' if ok else 'FAIL'}] {name}: decision={decision} label={label} reason={reason}"
    )
    if not ok:
        if not ok_decision:
            print(f"          expected decision={expect_decision}")
        if not ok_label:
            print(f"          expected label={expect_label}")
        if not ok_reason:
            print(f"          expected reason to contain {expect_reason_contains!r}, got {reason!r}")
    return ok


def check_catchall_text():
    """Verify the source contains the bug-comments describing what was fixed.

    This makes the regression reviewable from source alone — no need to
    read the docs. If a future refactor erases the explanation, this
    static check at least flags the section so a reviewer can add their
    own note.
    """
    src = (ROOT / "listener" / "motion_gate_pipeline.py").read_text()
    required_phrases = [
        ("Phase.137 (§11.59)", "section anchor for the fix"),
        ("high_conf_<class>", "new reason template"),
        ("no vehicle anywhere in the mix", "clarification note (case insensitive)"),
        ("Vehicle in motion: <bench>", "historical symptom, named explicitly"),
        ("225 such occurrences", "evidence: log-analysis count"),
        ("LOCKED fix", "explicit lock marker"),
    ]
    failed = []
    for phrase, descr in required_phrases:
        if phrase.lower() in src.lower():
            print(f"  [PASS] {descr}: {phrase!r}")
        else:
            print(f"  [FAIL] {descr}: {phrase!r}")
            failed.append(phrase)
    return len(failed) == 0


def main() -> int:
    print("=" * 78)
    print("Probe: Phase.137 _route_decision catchall fix")
    print("=" * 78)

    print("\n§1. Four historical production-log cases:")
    results = []
    # 1. The exact alert that crashed: 9a78a254 -- person alone, conf 0.48.
    results.append(
        case(
            "alert 9a78a254 (person 0.48 + none)",
            _verdict("person", 0.48),
            _verdict("none", 0.0, "suppress"),
            expect_decision="suppress",
            expect_label="person",
            expect_reason_contains="person_not_vehicle_no_pipeline",
        )
    )
    # 2. zebra -- appears in the log at 6a517560 conf 0.57.
    results.append(
        case(
            "alert 6a517560 (zebra 0.57 + none)",
            _verdict("zebra", 0.57),
            _verdict("none", 0.0, "suppress"),
            expect_decision="suppress",
            expect_label="zebra",
            expect_reason_contains="zebra_not_vehicle_no_pipeline",
        )
    )
    # 3. train -- another sunrise false positive conf 0.56.
    results.append(
        case(
            "alert 74e03267 (train 0.56 + none)",
            _verdict("train", 0.56),
            _verdict("none", 0.0, "suppress"),
            expect_decision="suppress",
            expect_label="train",
            expect_reason_contains="train_not_vehicle_no_pipeline",
        )
    )
    # 4. fire hydrant -- PLAN §11.59 example: a091b49c conf 0.69.
    results.append(
        case(
            "alert a091b49c (fire hydrant 0.69 + none)",
            _verdict("fire hydrant", 0.69),
            _verdict("none", 0.0, "suppress"),
            expect_decision="suppress",
            expect_label="fire hydrant",
            expect_reason_contains="fire hydrant_not_vehicle_no_pipeline",
        )
    )

    print("\n§2. LEGITIMATE rule-5 (vehicle in the mix):")
    results.append(
        case(
            "person 0.75 + car 0.60 - rule 5 LEGITIMATE",
            _verdict("person", 0.75),
            _verdict("car", 0.60),
            expect_decision="vehicle",
            expect_label="car",
            expect_reason_contains="mixed_vehicle_wins",
        )
    )
    print("\n§3. Other-not-vehicle anywhere (vehicle IS in the mix, just lower):")
    results.append(
        case(
            "fire hydrant 0.69 top + car 0.55 (vehicle in mix)",
            _verdict("fire hydrant", 0.69),
            _verdict("car", 0.55),
            expect_decision="vehicle",
            expect_label="car",
            expect_reason_contains="mixed_vehicle_wins",
        )
    )

    print("\n§4. Static check: source-embedded regression notes")
    results.append(check_catchall_text())

    failures = sum(1 for r in results if not r)
    print(f"\n{'=' * 78}")
    if failures:
        print(f"FAIL — {failures} of {len(results)} checks failed.")
        return 1
    print(f"PASS — {len(results)} of {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())