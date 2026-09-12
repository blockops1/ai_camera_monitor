# V2-035: Reviewer re-reads original PRD (option C, AUTHORIZED 2026-09-12)

## Context

The v2 review pipeline (`coder → qa → reviewer → complete`) had a gap: the
reviewer read the kanban card body but never re-read the original PRD JSON
that the card body summarized. When a card body drifted from PRD intent (or
when a worker satisfied the card body but missed the original PRD's
shipping_definition), the reviewer would approve work that did not actually
accomplish what the PRD asked.

The operator's authorization (2026-09-12, verbatim): "OK back to the
reviewer question where you had suggested that it read the PRD and that you
make the changes to have that it do that. Yes I like your proposal on that
go ahead and make those changes."

## Patches applied

### 1. `~/.hermes/profiles/reviewer/SOUL.md`

Added a new section "Round 3+ lens: re-read the ORIGINAL PRD (operator
invariant, 2026-09-12)" between "What you read (in order)" and "What you
check". It tells the reviewer:

- The card body is a derived summary; the original PRD is the contract.
- Procedure on every review: `cat <prd_source_file>` first, re-state the
  PRD's `shipping_definition` + `operator_decisions_captured` in own
  words, walk PRD-level ACs against the diff, then walk card body ACs.
- If the two walks disagree, the PRD walk wins — FAIL with both cited.
- Reviewer is still read-only (`cat`, `git show`, `git diff`, `grep`,
  `jq`). Cannot run pytest, lint, or daemon. Mark executable checks as
  `EXECUTABLE_CHECK_NEEDED`.

### 2. `~/.hermes/skills/devops/sdlc-review/SKILL.md`

Two edits:

- Round 3+ Contract lens row updated: explicit reference to
  `prd_source_file:` field, audit against `stories[].acceptance[]` and
  `shipping_definition` from PRD JSON (not card body ACs alone), and
  "PRD walk wins when they disagree."
- Procedure §1 (Orient from durable task record) gains an explicit
  "If card body has `prd_source_file:` field, you MUST `cat` that file
  before walking ACs. ... If `prd_source_file` is absent, FAIL with
  'card body missing prd_source_file; cannot re-read original PRD.'"

### 3. Card body updates — DEFERRED (constraint discovered)

The operator's third step was "add `prd_source_file:` field to each V2-034
card body". This is BLOCKED by:

- The kanban CLI does not expose an `edit-body` subcommand.
- The orchestrator skill rule "Body immutable post-create — must be
  right first time" makes direct body mutation off-limits by convention.
- Direct SQLite UPDATE on the body column would bypass the convention
  but preserve all subscriptions, links, and history.

**Decision (operator, 2026-09-12, option C):** Do NOT mutate the existing
V2-034 card bodies. Instead, the reviewer derives `prd_source_file` from
the card title via glob.

**Convention (option C, locked in 2026-09-12):**

1. Every v2 PRD-shaped document lives at
   `docs/PHASE-V2-NNN-PRD-<slug>.{json,md}` where `NNN` is a 3-digit
   zero-padded PRD number and `<slug>` is a hyphenated short
   description. Most are `.json`; some (like V2-035) are `.md` when the
   "PRD" is a procedural record rather than a structured spec.
2. Every card title contains the substring `V2-NNN` where `NNN` matches
   the document's number.
3. The reviewer extracts `NNN` from the card title (regex
   `V2-(\d{3})`), then runs:
   ```
   ls docs/PHASE-V2-NNN-PRD-*.{json,md}   # confirm exactly one match
   cat docs/PHASE-V2-NNN-PRD-*.{json,md}  # the PRD ground truth
   ```
   In practice: `ls docs/PHASE-V2-NNN-PRD-*` (no extension filter)
   because the operator always names the slug uniquely.
4. If glob returns 0 matches → FAIL with "PRD not found for V2-NNN in
   docs/ — operator must file the PRD before dispatch".
5. If glob returns >1 matches → FAIL with "ambiguous PRD: <list>" — the
   title number should be unique per PRD.
6. If `prd_source_file:` IS present in the card body, prefer it over
   the glob (lets a card reference a non-v2 PRD or an archived PRD).

Going forward, every NEW card body (V2-035+) SHOULD still include
`prd_source_file:` for explicitness, but it is not required: the glob
fallback is sufficient.

The first V2-034 card to reach review will pass without mutation.
This satisfies the operator invariant without breaking the
"body immutable post-create" rule.

## Verification

- SOUL.md patch landed at line 79-103 (24 new lines). Verified by
  `git diff --no-index /dev/null ~/.hermes/profiles/reviewer/SOUL.md`.
- sdlc-review skill patch landed: row 67 (Round 3+ lens) and lines
  86-88 (Procedure §1 paragraph). Verified by `git diff --no-index
  /dev/null ~/.hermes/skills/devops/sdlc-review/SKILL.md`.
- The skill file is untracked in `~/.hermes` git. It's part of the
  Hermes skill catalog, not the v2 project. No commit needed in the
  v2 repo.

## Status

- [x] SOUL.md patched
- [x] sdlc-review SKILL.md patched (Round 3+ row + Procedure §1)
- [ ] V2-034 card body updates — DEFERRED, awaiting operator decision
- [ ] Demonstrate the new rule on a real review (recommend: dispatch
      V2-034 cards and let the first reviewer catch the missing field)
