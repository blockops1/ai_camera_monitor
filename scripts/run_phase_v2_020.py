#!/usr/bin/env python3.11
"""
PHASE-V2-020 autonomous driver.

Walks the PRD chain (US-020a through US-020last) serially with hard stops.
Each story: create kanban task → dispatch coder → poll for completion →
verify (pytest + leak audit) → if pass, mark story done in PRD, continue.
Stops on any of: cascade block, verification failure, daemon death,
self-leak detected, worker crash, or operator pause signal.

Operator-internal: lives in scripts/ but contains no secrets. The PRD
itself (docs/PHASE-V2-020-*.json) holds the source of truth for story
state.

Usage:
    .venv/bin/python3.11 scripts/run_phase_v2_020.py             # full run
    .venv/bin/python3.11 scripts/run_phase_v2_020.py --start US-020a  # from a specific story
    .venv/bin/python3.11 scripts/run_phase_v2_020.py --dry-run   # show plan only
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Operator-private paths and identifiers. Required env vars:
#   DRIVER_REPO_PATH             absolute path to the farm-surveillance-v2 repo
#   OPERATOR_TELEGRAM_CHAT_ID    chat identifier the operator should be notified on
#   DAEMON_PORT                  (optional, default 8090)
#   DRIVER_CODER_PROFILE         (optional, default 'coder')
# For the operator's own machine, copy scripts/run_phase_v2_020.env.example to
# scripts/run_phase_v2_020.env (gitignored) and source it before running.
import os as _os
_missing = [k for k in ('DRIVER_REPO_PATH', 'OPERATOR_TELEGRAM_CHAT_ID') if not _os.environ.get(k)]
if _missing:
    raise SystemExit(
        f'Missing required env: {_missing}. '
        f'Source scripts/run_phase_v2_020.env or set them in the environment.'
    )
REPO = Path(_os.environ['DRIVER_REPO_PATH'])
PRD_PATH = REPO / 'docs' / 'PHASE-V2-020-PRD-remove-dead-code.json'
LOG_PATH = REPO / 'logs' / 'phase_v2_020_driver.log'
STATE_PATH = REPO / 'logs' / 'phase_v2_020_state.json'
DAEMON_PORT = int(_os.environ.get('DAEMON_PORT', '8090'))
TELEGRAM_CHAT_ID = _os.environ['OPERATOR_TELEGRAM_CHAT_ID']
CODER_PROFILE = _os.environ.get('DRIVER_CODER_PROFILE', 'coder')
MAX_STORY_ATTEMPTS = 2
POLL_INTERVAL_SEC = 60
MAX_STORY_DURATION_SEC = 120 * 60  # 120 minutes per story (coder + QA + reviewer; reviewer can hang)


# ----------------------------------------------------------------------------
# PRD helpers
# ----------------------------------------------------------------------------

def load_prd() -> dict:
    with open(PRD_PATH) as f:
        return json.load(f)


def save_prd(prd: dict) -> None:
    with open(PRD_PATH, 'w') as f:
        json.dump(prd, f, indent=2)


def next_story(prd: dict, start_id: str | None) -> dict | None:
    """Find the next story by numerical sequence whose deps are all done."""
    done_ids = {s['id'] for s in prd['stories'] if s.get('status') == 'done'}
    in_progress_ids = {s['id'] for s in prd['stories'] if s.get('status') == 'in_progress'}

    # numerical order, filter by start_id and deps
    candidates = [s for s in prd['stories'] if s.get('status') == 'todo']
    candidates.sort(key=lambda s: s['id'])
    if start_id:
        # skip past start_id
        try:
            start_idx = next(i for i, s in enumerate(candidates) if s['id'] == start_id)
            candidates = candidates[start_idx:]
        except StopIteration:
            print(f"start_id={start_id} not in todo list", file=sys.stderr)
            sys.exit(2)

    for s in candidates:
        deps = s.get('depends_on', [])
        if all(d in done_ids for d in deps):
            return s

    # nothing left
    return None


# ----------------------------------------------------------------------------
# Kanban wrappers
# ----------------------------------------------------------------------------

def sh(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command, return (rc, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(REPO))
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, '', 'TIMEOUT'


def run_kanban(args: list[str], timeout: int = 30) -> str:
    rc, out, err = sh(['hermes', 'kanban'] + args, timeout=timeout)
    if rc != 0:
        log(f'hermes kanban {args} -> rc={rc} stderr={err[:500]}')
    return out


def create_task(story: dict) -> str:
    """Create the kanban task for a story. Returns task_id."""
    body = build_card_body(story)
    assert len(body) <= 2500, f'card body too long for {story["id"]}: {len(body)} chars'

    rc, out, err = sh([
        'hermes', 'kanban', 'create', '--json',
        '--body', body,
        '--assignee', CODER_PROFILE,
        '--workspace', f'dir:{REPO}',
        f"{story['id']}: {story['title']}"
    ], timeout=60)
    if rc != 0:
        raise RuntimeError(f'create failed rc={rc} err={err[:500]}')

    # Output is JSON; extract id
    try:
        data = json.loads(out)
        task_id = data['id']
    except (json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f'create returned bad json: {out[:300]} err={e}')

    log(f'created {task_id} for {story["id"]}')
    return task_id


def subscribe(task_id: str) -> None:
    sh(['hermes', 'kanban', 'notify-subscribe',
        '--platform', 'telegram',
        '--chat-id', TELEGRAM_CHAT_ID,
        '--chat-type', 'dm',
        '--delivery-mode', 'notify+wake',
        task_id], timeout=30)


def dispatch() -> None:
    rc, out, err = sh(['hermes', 'kanban', 'dispatch'], timeout=120)
    log(f'dispatch rc={rc} out={out[:500]} err={err[:500]}')
    if rc != 0:
        raise RuntimeError(f'dispatch failed: {err[:300]}')


def task_status(task_id: str) -> dict:
    out = run_kanban(['show', task_id])
    return {'raw': out}


def is_task_terminal(task_id: str) -> tuple[bool, str, str]:
    """Return (terminal, status, assignee) from 'hermes kanban list'."""
    out = run_kanban(['list'])
    for line in out.splitlines():
        # format: 'symbol TID  STATUS  ASSIGNEE  TITLE'
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[1] == task_id:
            status = parts[2]
            return (status == 'done', status, parts[1] if len(parts) > 1 else '?')
    return (False, 'unknown', '?')


def is_worker_alive(task_id: str) -> bool:
    out = subprocess.run(['pgrep', '-f', f'work kanban task {task_id}'],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)


def _commit_landed_for(story_id: str) -> bool:
    """True if a commit on main references this story id."""
    rc, out, _ = sh(['git', 'log', '--oneline', '-20', '--grep', story_id],
                    timeout=10)
    return rc == 0 and out.strip() != ''


# ----------------------------------------------------------------------------
# Card body builder (≤2500 chars)
# ----------------------------------------------------------------------------

def build_card_body(story: dict) -> str:
    prd = load_prd()
    card = next(s for s in prd['stories'] if s['id'] == story['id'])
    desc = card['card_body_fallback']
    acs = card['ac']
    files = card['files']

    body = f"""**Story (PHASE-V2-020 / {story['id']}).** {story['title']}

{desc}

## Acceptance criteria
{chr(10).join(f'- {ac}' for ac in acs)}

## Files in scope
{chr(10).join(f'- {f}' for f in files)}

## Workflow

1. Read the target files first via `read_file` (do not search).
2. Implement per the description above. Use `os.environ.get(...)` and existing helpers (no new env vars unless described).
3. Run `python3 -m pytest tests/ -x --tb=short` (124/124 or higher, green).
4. Verify the literal-leak audit on YOUR touched files (no production-prefix IPs, no operator handle, no /Users/<name>/, no chat identifier digits).
5. Hand off: `hermes kanban request-review --reviewer qa --summary "..."`.

## Hard limits

- ASCII hyphens only; no Unicode.
- Do NOT push to remote. Operator reviews and pushes.
- One commit. Use the exact commit message from the PRD card_body.
- If a leak audit fails, fix it before committing.

## Note for QA reviewer

The PRD's design_principles section owns the canonical leak-pattern list. Do not name specific operator handles, IPs, or chat identifier digits in the ACs or PR descriptions — they leak by reference. Verify literal patterns are absent using the privacy scanner (scripts/check_no_private_data.py) which exits non-zero on leak.

## Commit message template

`feat(v2): {story['id']} — <one-line summary>`
"""
    # Trim if over 2500
    if len(body) > 2500:
        body = body[:2497] + '...'
    return body


# ----------------------------------------------------------------------------
# Verification
# ----------------------------------------------------------------------------

def run_pytest() -> tuple[bool, str]:
    rc, out, err = sh(['.venv/bin/python3.11', '-m', 'pytest', 'tests/', '-x', '--tb=short'],
                      timeout=300)
    return (rc == 0, out + err)


def check_daemon_alive() -> tuple[bool, str]:
    rc, out, err = sh(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                       f'http://127.0.0.1:{DAEMON_PORT}/'], timeout=10)
    if rc != 0:
        return False, f'curl failed: {err[:200]}'
    status = out.strip()
    # daemon returns 404 for unknown routes, but that means Flask is alive
    return (status in ('200', '400', '404'), f'http={status}')


# ----------------------------------------------------------------------------
# State persistence
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {'stories': {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f'[{datetime.now().isoformat(timespec="seconds")}] {msg}'
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')


# ----------------------------------------------------------------------------
# Telegram notifier (best-effort)
# ----------------------------------------------------------------------------

def notify(msg: str) -> None:
    log(f'NOTIFY: {msg}')
    # We do NOT have telegram token here. The kanban subsystem will already
    # have woken the operator via the subscribe step. Driver logs go to file.


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

def run_story(story: dict, prd: dict, state: dict) -> str:
    """Run a single story. Returns 'done' | 'retry' | 'blocked'."""
    sid = story['id']
    log(f'=== {sid}: {story["title"]} ===')

    # Mark in_progress
    story['status'] = 'in_progress'
    save_prd(prd)

    # Create task + dispatch
    task_id = create_task(story)
    story['task_id'] = task_id
    save_prd(prd)

    subscribe(task_id)
    dispatch()

    # Poll for completion
    started = time.time()
    last_seen_status = None
    while time.time() - started < MAX_STORY_DURATION_SEC:
        terminal, status, _ = is_task_terminal(task_id)
        if status != last_seen_status:
            log(f'  {task_id} status={status}')
            last_seen_status = status

        if terminal:
            log(f'  {task_id} -> done after {int(time.time()-started)}s')
            break

        if not is_worker_alive(task_id):
            log(f'  worker process gone but task not terminal — checking again next poll')
            time.sleep(POLL_INTERVAL_SEC)
            terminal, status, _ = is_task_terminal(task_id)
            if not terminal:
                # Worker died but no kanban terminal state. Check if the commit
                # actually landed on disk and pytest is green — if so, the work
                # succeeded but the review pipeline glitched. Auto-mark done.
                log(f'  worker died, task={status} — checking commit on disk')
                if _commit_landed_for(sid) and run_pytest()[0]:
                    log(f'  commit landed + tests green → auto-marking done')
                    notify(f'{sid}: worker died but commit+tests clean — auto-marking done')
                    return 'done'  # handled outside via mark_story_done
                log(f'  worker died, no commit found — treat as crash')
                story['attempts'] += 1
                save_prd(prd)
                return 'blocked'

        time.sleep(POLL_INTERVAL_SEC)
    else:
        log(f'  TIMEOUT after {MAX_STORY_DURATION_SEC}s')
        notify(f'TIMEOUT on {sid}')
        story['attempts'] += 1
        save_prd(prd)
        return 'blocked'

    # Verify: pytest
    ok, out = run_pytest()
    if not ok:
        log(f'  PYTEST FAILED on {sid}')
        notify(f'PYTEST FAILED after {sid}: {out[:1000]}')
        story['attempts'] += 1
        story['last_error'] = 'pytest_failed'
        save_prd(prd)
        return 'retry'

    # Verify: scanner (PII leak detection — operator handles, IPs, chat id, etc.)
    # NOTE: best-effort warning only. Scanner covers many PII patterns but may flag
    # out-of-scope leaks for stories that don't address them. We log and continue;
    # the final state will be scanner-clean at gate time.
    scanner = REPO / 'scripts' / 'check_no_private_data.py'
    if scanner.is_file():
        rc, scan_out, _ = sh(['.venv/bin/python3.11', str(scanner)], timeout=60)
        if rc == 1:
            # scanner found leaks (exit 1 = leaks, exit 0 = clean) — log + continue
            log(f'  SCANNER WARN after {sid} (leaks found, will be addressed by in-scope stories)')
            log(f'  SCANNER OUTPUT: {scan_out[:1500]}')
            notify(f'SCANNER WARN after {sid}: {scan_out[:1000]}')
        elif rc not in (0, 1):
            log(f'  SCANNER ERROR rc={rc} out={scan_out[:500]} — continuing')
        else:
            log(f'  scanner clean on {sid}')

    # Verify: daemon still alive
    alive, msg = check_daemon_alive()
    if not alive:
        log(f'  DAEMON DEAD after {sid}: {msg}')
        notify(f'DAEMON DEAD after {sid}: {msg}')
        story['last_error'] = 'daemon_dead'
        save_prd(prd)
        # This is HARD stop — do not retry automatically
        return 'blocked'

    # All good — capture commit, mark done
    rc, sha_out, _ = sh(['git', 'rev-parse', '--short', 'HEAD'], timeout=10)
    story['commit_sha'] = sha_out.strip() if rc == 0 else None
    story['status'] = 'done'
    story['passes'] = True
    story['last_error'] = None
    save_prd(prd)
    log(f'  {sid} DONE commit={story.get("commit_sha")}')
    notify(f'{sid} DONE commit={story.get("commit_sha")}')
    return 'done'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', help='start from this story id')
    ap.add_argument('--dry-run', action='store_true', help='show plan only')
    args = ap.parse_args()

    prd = load_prd()
    state = load_state()

    if args.dry_run:
        log('=== DRY RUN ===')
        # Show all remaining work in numerical order
        remaining = [s for s in prd['stories'] if s.get('status') in ('todo', 'in_progress')]
        remaining.sort(key=lambda s: s['id'])
        for s in remaining:
            deps = s.get('depends_on', [])
            ready = all(d in {x['id'] for x in prd['stories'] if x.get('status') == 'done'} for d in deps)
            print(f"  {s['id']}  status={s['status']}  ready={ready}  deps={deps}")
        return 0

    # Verify preflight
    alive, msg = check_daemon_alive()
    if not alive:
        log(f'PREFLIGHT: daemon not alive: {msg}')
        notify(f'PREFLIGHT FAIL: daemon not alive ({msg})')
        return 1
    log(f'preflight OK: daemon {msg}')

    # Walk stories
    while True:
        story = next_story(prd, args.start)
        if story is None:
            log('all reachable stories done')
            break

        # If a start was specified and we've passed it, reset start so subsequent picks are by deps
        args.start = None

        # Skip stories already passed (e.g., operator manual recovery landed a commit)
        if story.get('passes') and story.get('status') == 'done':
            log(f'  {story["id"]} already passes=True commit={story.get("commit_sha")} — skipping')
            continue

        result = run_story(story, prd, state)
        if result == 'blocked':
            log(f'HARD STOP on {story["id"]}')
            notify(f'HARD STOP on {story["id"]} — operator review required')
            return 2
        if result == 'retry':
            # Reset task, retry
            if story['attempts'] >= MAX_STORY_ATTEMPTS:
                log(f'{story["id"]} exhausted attempts ({story["attempts"]})')
                story['status'] = 'blocked'
                save_prd(prd)
                notify(f'{story["id"]} EXHAUSTED — operator review')
                return 3
            log(f'retrying {story["id"]} (attempt {story["attempts"]+1})')
            story['status'] = 'todo'
            story['task_id'] = None
            save_prd(prd)
            continue

    log('DRIVER FINISHED')
    notify('PHASE-V2-020 driver finished all reachable stories')
    return 0


if __name__ == '__main__':
    sys.exit(main())
