#!/usr/bin/env bash
# deploy.sh — pull latest origin/main on morpheus.
#
# Usage:
#   ./scripts/deploy.sh              # uses defaults
#   PI_HOST=192.168.1.21 ./scripts/deploy.sh
#
# Requires passwordless SSH to $PI_HOST (should already be set up for dev work).
# Run this after `git push` to sync the Pi to the latest commit.

set -euo pipefail

PI_HOST="${PI_HOST:-morpheus}"
PI_REPO="${PI_REPO:-~/raspberry-ai}"

echo "[deploy] ${PI_HOST}:${PI_REPO} — pulling ..."
ssh "$PI_HOST" "git -C ${PI_REPO} pull --ff-only"
echo "[deploy] done"
