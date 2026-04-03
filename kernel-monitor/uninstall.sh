#!/usr/bin/env bash
# kernel-monitor uninstall script
# Stops and removes the systemd service.
# Logs in /var/log/kernel-monitor/ are preserved — remove manually if not needed.
# Must be run as root: sudo bash kernel-monitor/uninstall.sh

set -euo pipefail

SERVICE_NAME="kernel-monitor"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root: sudo bash $0"
    exit 1
fi

echo "[uninstall] stopping ${SERVICE_NAME}"
systemctl stop "${SERVICE_NAME}" || true

echo "[uninstall] disabling ${SERVICE_NAME}"
systemctl disable "${SERVICE_NAME}" || true

if [[ -f "$UNIT_FILE" ]]; then
    echo "[uninstall] removing ${UNIT_FILE}"
    rm -f "$UNIT_FILE"
fi

echo "[uninstall] daemon-reload"
systemctl daemon-reload

echo ""
echo "[uninstall] done — logs preserved at /var/log/kernel-monitor/"
echo "            remove with: sudo rm -rf /var/log/kernel-monitor/"
