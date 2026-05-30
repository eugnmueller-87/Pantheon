#!/usr/bin/env bash
# Install Pantheon OS git hooks
# Usage: bash scripts/install-hooks.sh
# Run once after cloning. Hook runs automatically on every git commit.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_SRC="$REPO_ROOT/scripts/pre-commit"
HOOK_DST="$REPO_ROOT/.git/hooks/pre-commit"

if [ ! -f "$HOOK_SRC" ]; then
    echo "ERROR: $HOOK_SRC not found. Run from repo root." >&2
    exit 1
fi

cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"
echo "✅  pre-commit hook installed at $HOOK_DST"
echo "    It will block commits that contain hardcoded credentials."
