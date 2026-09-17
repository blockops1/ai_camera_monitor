#!/usr/bin/env python3
"""
verify_branch_merged.py — Audit all kanban tasks and report any whose branch
heads are not ancestors of main.

Usage:
    python3 scripts/verify_branch_merged.py

Output format:
    UNMERGED: <task_id>  branch=<branch>  head=<sha>  missing_from_main=True
    OK: <task_id>  branch=<branch>  head=<sha>  merged_to_main=True
"""

import os
import subprocess
import sys


def run(cmd, **kw):
    """Run a shell command, return (exit_code, stdout_str)."""
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, **kw
    )
    return result.returncode, result.stdout.strip()


def get_kanban_db():
    """Find the kanban DB. Check board-scoped DB first, then global."""
    kanban_db = os.environ.get("HERMES_KANBAN_DB")
    if kanban_db and os.path.isfile(kanban_db):
        return kanban_db
    board = os.environ.get("HERMES_KANBAN_BOARD", "default")
    board_db = os.path.expanduser(f"~/.hermes/kanban/boards/{board}/kanban.db")
    if os.path.isfile(board_db):
        return board_db
    global_db = os.path.expanduser("~/.hermes/kanban.db")
    if os.path.isfile(global_db):
        return global_db
    link = os.path.expanduser("~/.hermes/kanban.db/current")
    if os.path.islink(link):
        target = os.path.realpath(link)
        if os.path.isfile(target):
            return target
    print("ERROR: cannot find kanban.db", file=sys.stderr)
    sys.exit(1)


def load_tasks(db_path):
    """Load all tasks from the kanban DB as a dict of task_id -> task dict."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    tasks = {}
    for row in rows:
        tasks[row["id"]] = dict(row)
    conn.close()
    return tasks


def get_repo_path(task):
    """Extract the repo path from a task's body."""
    body = task.get("body", "")
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    for line in body.split("\n"):
        if "farm-surveillance-v2" in line:
            parts = line.split()
            for p in parts:
                if p.startswith("/Users/"):
                    if "farm-surveillance-v2" in p:
                        return os.path.dirname(p)
    return os.path.expanduser("~/farm-surveillance-v2")


def check_merged(repo, sha):
    """Check if <sha> is an ancestor of main in the given repo."""
    rc, out = run(
        f"cd '{repo}' && git merge-base --is-ancestor '{sha}' main 2>&1"
    )
    return rc == 0


def get_all_worktree_branches(repo):
    """Get all wt/* branches in the repo with their HEAD SHA."""
    rc, out = run(f"cd '{repo}' && git branch --list 'wt/*'")
    if rc != 0 or not out:
        return {}
    branches = {}
    for line in out.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Strip leading * (current branch marker) and + (stale marker)
        branch = line.lstrip("*+ ").strip()
        if not branch:
            continue
        rc2, sha = run(f"cd '{repo}' && git rev-parse '{branch}'")
        if rc2 == 0:
            branches[branch] = sha
    return branches


def match_task_to_branch(branch_name, tasks):
    """Try to find which task this branch belongs to by checking titles."""
    branch_prefix = branch_name.split("/")[-1].lower()
    for tid, task in tasks.items():
        title = (task.get("title") or "").lower()
        body = task.get("body") or ""
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        body_lower = body.lower()

        if branch_prefix in title:
            return task
        if branch_name.lower() in body_lower:
            return task
        if f"wt/{branch_prefix}" in body_lower:
            return task
    return None


def main():
    db_path = get_kanban_db()
    tasks = load_tasks(db_path)
    repo = os.path.expanduser("~/farm-surveillance-v2")

    branches = get_all_worktree_branches(repo)
    if not branches:
        print("No worktree branches found in the repository.")
        return 0

    unmerged = []
    ok_list = []

    for branch, sha in sorted(branches.items()):
        task = match_task_to_branch(branch, tasks)
        if task:
            task_id = task["id"]
            task_title = task.get("title", "unknown")[:80]
        else:
            task_id = "unknown"
            task_title = "no matching task"

        merged = check_merged(repo, sha)

        if merged:
            ok_list.append((task_id, branch, sha, task_title))
        else:
            unmerged.append((task_id, branch, sha, task_title))

    # Print results
    for tid, branch, sha, title in unmerged:
        print(f"UNMERGED: {tid}  branch={branch}  head={sha}  missing_from_main=True")

    for tid, branch, sha, title in ok_list:
        print(f"OK: {tid}  branch={branch}  head={sha}  merged_to_main=True")

    if unmerged:
        print(
            f"\nSummary: {len(unmerged)} unmerged task(s), "
            f"{len(ok_list)} merged task(s)"
        )
    else:
        print(
            f"\nSummary: All {len(ok_list)} worktree branches are merged to main."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
