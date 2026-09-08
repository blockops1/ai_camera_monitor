# scripts/run_phase_v2_018.py

Operator-internal autonomous driver for PHASE-V2-018.

## What it does

Walks the PRD chain (US-018b through US-018j) **one story at a time**, with
hard stops on any anomaly:

1. **Pick next story** whose dependencies are all `status=done` (numerical order).
2. **Create kanban task** with a body built from the PRD card_body + ACs.
3. **Subscribe the operator** to per-story Telegram notifications.
4. **Dispatch** the task to the `coder` profile.
5. **Poll** for completion (`done` status from `hermes kanban list`), max 25 min.
6. **Verify:**
   - Full pytest suite (`124/124` or higher, green).
   - Daemon still serving on the configured port.
7. **Mark story done** in the PRD JSON and continue.
8. **Hard stop** on any of:
   - Worker process died before completion.
   - Story timed out.
   - Pytest failed after worker said it landed.
   - Daemon died after a story.
   - Story exhausted its attempt budget.

## Setup

```bash
# One-time
cp scripts/run_phase_v2_018.env.example scripts/run_phase_v2_018.env
$EDITOR scripts/run_phase_v2_018.env   # fill DRIVER_REPO_PATH and OPERATOR_TELEGRAM_CHAT_ID

# Every time
set -a; source scripts/run_phase_v2_018.env; set +a
```

## Usage

```bash
# See the current plan (what would run, in what order)
scripts/run_phase_v2_018.py --dry-run

# Run from where the PRD currently sits (resumes from first todo story)
scripts/run_phase_v2_018.py

# Run from a specific story (use only if you've manually reset earlier stories)
scripts/run_phase_v2_018.py --start US-018b
```

## Operator override signals

The driver checks the kanban board; if you (the operator) want to pause the
run, simply `hermes kanban pause <task_id>` on the active story — the
driver will see the worker not finish and trigger its hard stop on the next
poll cycle.

## Notes

- **Operator-side commits only.** The driver does NOT call `git push`. After
  the driver finishes, review the commit log, run your leak audit one more
  time, and push when you're satisfied.
- **No parallel dispatch.** One story at a time, by design.
- **Tree must be clean** between stories — if the operator committed
  something by hand, the driver will still proceed but a `git status` is
  recommended.
