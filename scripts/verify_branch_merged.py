#!/usr/bin/env python3
"""Verify that all 'done' kanban tasks have their branch heads merged into main.

A task is NOT truly done until its work is reachable from main. This script
audits the kanban DB and reports any tasks whose branch heads are not
ancestors of main AND whose content does not appear in main via a different
commit (e.g. cherry-pick with same patch-id).

Usage:
    python3 scripts/verify_branch_merged.py [--repo PATH] [--kanban-db PATH]

Exit codes:
    0 — no unmerged tasks
    1 — unmerged tasks found (printed to stdout)
    2 — script error
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def find_head_sha(repo: Path, task_id: str, branch: str | None) -> tuple[str | None, str | None]:
    """Find the head SHA for a done task. Search: worktree, branch ref, archive tag."""
    # 1. Worktree
    wt = repo / ".worktrees" / task_id
    if (wt / ".git").exists():
        r = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip(), f"worktree {wt}"
    # 2. Branch ref
    if branch:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{branch}"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip(), f"branch ref {branch}"
        # 3. Archive tag
        archive_tag = f"archive/{branch.replace('wt/', 'wt-')}"
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/tags/{archive_tag}"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip(), f"archive tag {archive_tag}"
    return None, None


def patch_id(repo: Path, sha: str) -> str | None:
    """Compute the patch-id for a commit (stable representation of its diff)."""
    r1 = subprocess.run(
        ["git", "-C", str(repo), "log", "--pretty=format:", "-p", sha, "-1"],
        capture_output=True, text=True,
    )
    r2 = subprocess.run(
        ["git", "-C", str(repo), "patch-id", "--stable"],
        input=r1.stdout, capture_output=True, text=True,
    )
    out = r2.stdout.strip()
    return out.split()[0] if out else None


def find_content_equivalent(repo: Path, head_sha: str) -> str | None:
    """Return a main SHA whose patch-id matches head_sha's patch-id, or None."""
    head_pid = patch_id(repo, head_sha)
    if not head_pid:
        return None
    r = subprocess.run(
        ["git", "-C", str(repo), "log", "--pretty=format:COMMIT:%H%n", "-p", "main"],
        capture_output=True, text=True,
    )
    chunks = r.stdout.split("COMMIT:")
    for chunk in chunks[1:]:
        sha_line, _, body = chunk.partition("\n")
        sha = sha_line.strip()
        pid = patch_id(repo, sha) if sha else None
        if pid == head_pid:
            return sha
    return None


def find_in_reflog(repo: Path, branch: str) -> str | None:
    """Check if a fast-forward merge of this branch appears in main's reflog.

    Returns the main SHA where the merge happened, or None.
    """
    r = subprocess.run(
        ["git", "-C", str(repo), "reflog", "--no-decorate", "main"],
        capture_output=True, text=True,
    )
    for line in r.stdout.split("\n"):
        if "Fast-forward" in line and f"merge {branch}" in line:
            sha = line.split()[0]
            return sha
    return None


def is_ancestor(repo: Path, sha: str, ref: str = "main") -> bool:
    r = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, ref],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="/Users/jill/farm-surveillance-v2")
    parser.add_argument("--kanban-db", default="/Users/jill/.hermes/kanban/boards/coder-farm-surveillance/kanban.db")
    args = parser.parse_args()

    repo = Path(args.repo)
    db = Path(args.kanban_db)
    if not db.exists():
        print(f"ERROR: kanban DB not found at {db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tasks = conn.execute(
        """
        SELECT id, title, branch_name
        FROM tasks
        WHERE status = 'done' AND workspace_kind = 'worktree'
        ORDER BY completed_at DESC
        """
    ).fetchall()

    on_main = []
    unmerged = []
    unknown = []

    for t in tasks:
        head, source = find_head_sha(repo, t["id"], t["branch_name"])
        if head and is_ancestor(repo, head, "main"):
            on_main.append((t["id"], t["title"][:60], head[:7], "ancestor", source))
            continue
        if head:
            equiv = find_content_equivalent(repo, head)
            if equiv:
                on_main.append((t["id"], t["title"][:60], head[:7], f"content-match {equiv[:7]}", source))
                continue
        # Check reflog for fast-forward merge (branch may have been cleaned after merge)
        if t["branch_name"]:
            reflog_sha = find_in_reflog(repo, t["branch_name"])
            if reflog_sha:
                on_main.append((t["id"], t["title"][:60], "n/a", f"reflog-merge {reflog_sha[:7]}", "main reflog"))
                continue
        if not head:
            unknown.append((t["id"], t["title"][:60], "no SHA recoverable (branch + worktree deleted)"))
        else:
            unmerged.append((t["id"], t["title"][:60], head[:7], t["branch_name"], source))

    print(f"Audit of {len(tasks)} done tasks:")
    print(f"  ON MAIN (ancestor or content-equivalent): {len(on_main)}")
    print(f"  UNMERGED (work NOT in main):              {len(unmerged)}")
    print(f"  UNKNOWN (no SHA recoverable):            {len(unknown)}")
    print()
    if unmerged:
        print("UNMERGED done-tasks (these need fast-forward merge or rollback):")
        for u in unmerged:
            print(f"  {u[0]}  head={u[2]}  branch={u[3]}")
            print(f"    title: {u[1]}")
            print(f"    source: {u[4]}")
        print()
    if unknown:
        print("UNKNOWN (cannot verify — original branch/worktree gone):")
        for u in unknown:
            print(f"  {u[0]}: {u[1]} ({u[2]})")
        print()

    conn.close()
    return 0 if not unmerged else 1


if __name__ == "__main__":
    sys.exit(main())
