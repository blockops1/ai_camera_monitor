#!/bin/bash
# install-hooks.sh — Copy tracked hook sources into .git/hooks/.
#
# Usage:
#   bash scripts/hooks/install-hooks.sh
#
# This is a one-time install; hooks installed to .git/hooks/ are not tracked.
# To update, run this script again after editing the sources in scripts/hooks/.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_SRC_DIR="$REPO_ROOT/scripts/hooks"
HOOK_DST_DIR="$REPO_ROOT/.git/hooks"

mkdir -p "$HOOK_DST_DIR"

for hook in "$HOOK_SRC_DIR"/*; do
    name="$(basename "$hook")"
    cp -f "$hook" "$HOOK_DST_DIR/$name"
    chmod +x "$HOOK_DST_DIR/$name"
    echo "[install-hooks] installed $name"
done

echo "[install-hooks] done."
