#!/usr/bin/env bash
# Deploy cursor-agent-wrapper to /home/agent/artifacts/.
#
# Dual-use: install as a git hook AND run directly after local edits.
#   Hook:   ln -sf ../../agent-artifacts/scripts/post-merge-hook.sh .git/hooks/post-merge
#   Manual: agent-artifacts/scripts/post-merge-hook.sh
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="$REPO_ROOT/agent-artifacts/scripts/install-cursor-agent-wrapper.sh"

if [[ ! -x "$INSTALLER" ]]; then
  echo "post-merge-hook: installer not executable: $INSTALLER" >&2
  echo "  try: chmod +x $INSTALLER" >&2
  exit 1
fi

sudo -n "$INSTALLER"
