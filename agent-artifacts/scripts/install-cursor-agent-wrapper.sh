#!/usr/bin/env bash
# Install repo wrapper to /home/agent/artifacts/ (voice → agent deposit). No arguments.
# See profiling-pi/cursor-agent-wrapper-provisioning.md
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC="$REPO_ROOT/agent-artifacts/cursor-agent-wrapper"
DST_DIR=/home/agent/artifacts
DST="$DST_DIR/cursor-agent-wrapper"

if [[ ! -f "$SRC" ]]; then
  echo "install-cursor-agent-wrapper: missing source: $SRC" >&2
  exit 1
fi

mkdir -p "$DST_DIR"
cp -f "$SRC" "$DST"
chown agent:agent "$DST_DIR" "$DST"
chmod 755 "$DST_DIR" "$DST"
