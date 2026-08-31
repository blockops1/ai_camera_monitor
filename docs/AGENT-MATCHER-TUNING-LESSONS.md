# Agent Matcher Tuning — Lessons from a Wrong-Name Flip

A short field guide for AI agents tuning identity matchers (person, vehicle,
face, license plate, etc.) without shipping a wrong-name failure mode.

## Why this doc exists

The author's matcher regressed from "no match" to "wrong match" after a
threshold change was deployed with synthetic intuition instead of evidence.
This is the post-mortem + the three lessons extracted from that incident.

The lessons generalize to any matcher that emits a named identity (not a
score, not a label) — person matchers, vehicle matchers, face recognizers,
license-plate readers. Anywhere "lowering the threshold matches more" is
a plausible-but-wrong intuition.

---

## Lesson 1: Score against real captured events before proposing threshold changes

**Trigger.** The user reported "nothing gets matched." The obvious fix felt
like lowering the match threshold from 0.65 → 0.45 (synthetic intuition:
"lower threshold = more matches").

**What almost went wrong.** Without checking what the *real* scores looked
like, the change would have flipped the failure mode from "Unknown person
(correct)" to a named match with conf 0.533 — picking the *other* person
in the household instead of the actual one detected.

|                    | Person A (real) | Person B (other) |
|--------------------|-----------------|------------------|
| Stable-attribute score | 0.383       | 0.533            |

Both failed 0.65. Lowering the threshold to 0.45 would have matched Person B
(conf 0.533) instead of leaving the result as "Unknown." The detected person
was Person A — the named match would have been wrong.

**Pattern.** For matcher tuning, **run the actual scoring against captured
events before proposing threshold changes.** The synthetic "lower threshold
matches more" intuition hides the failure-mode flip from no-match to
wrong-match.

**Cost to implement.** ~10 lines of Python in a `.venv/bin/python -c` block.
Run as soon as the matcher hypothesis is on the table, before writing any
production code. Holds whether the suggestion is to change threshold, add a
path, change weights, or adjust the gap check.

---

## Lesson 2: Escalate a wrong-name failure mode yourself, not after the user asks

**Trigger.** The agent proposed a "safe" fix and the user said "yes, ship
it." The agent had a tidy plan: change threshold + change gap check + add
tests, in one commit.

**What almost happened.** The matcher tests would still pass — synthetic
test data doesn't have the right-failure-mode-shift pattern. The wrong-name
ship would have been silent.

**What saved it.** Re-running the scoring with the new threshold value
*before* writing the change surfaced the same Person B conf 0.533 against a
real Person A. The agent held the change and escalated to the operator with
the evidence.

**Pattern.** When the proposed fix has a "flip the failure mode" risk,
**run the fix against real data and check the output before committing.**
The cost is one Python script; the cost of not doing it is shipping a wrong
identity to Telegram.

**Heuristic.** If a matcher change could turn "Unknown" into a *named*
person, score it against at least one captured event with the actual
detected-person attributes before writing code. If the named-match picks
the wrong identity with confidence greater than the right identity, hold
the change.

---

## Lesson 3: Operator satisfaction is a stop signal, not an invitation to keep fixing

**Trigger.** The agent had identified 4 root causes (data missing, recognizer
returning 0 matches, threshold too high, picker chooses closest not
face-visible). The agent had a tidy fix plan.

**What the user said.** *"It is working now, we can tune later."*

**What the agent did correctly.** Shipped only the test cleanup, logged the
threshold change as rejected (with reasoning), deferred the rest, added a
top-level "Person-matching tuning (deferred)" section to the active-tasks
file. Stopped.

**Pattern.** When the user signals satisfaction with current behavior,
**stop proposing fixes.** Three identified bugs is not an action queue — it's
a parking lot. The user is the only one who decides when to revisit.

**Anti-pattern avoided.** "While we're here, we should also…" after the
user has stopped pushing. If the matcher returns "Unknown person (reason:
no match)" correctly, that IS the answer — not a bug.

---

## One-line summary

> **For matchers, the failure mode "Unknown" is correct behavior. Flipping it
> to a named match without evidence is the risk. Always score against real
> captured events before changing matcher thresholds.**

---

## Source

This document is a sanitized extraction of an internal session-corrections
note from a private farm-surveillance-refactor. Operator names, event
UUIDs, and identifying details were replaced with placeholders. The
underlying lessons are operational and generalize beyond the original
project.