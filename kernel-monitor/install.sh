#!/usr/bin/env bash
# kernel-monitor install script
# Installs the systemd service, resolving ExecStart to this script's location.
# Must be run as root: sudo bash kernel-monitor/install.sh

set -euo pipefail

SERVICE_NAME="kernel-monitor"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}.service"

# Resolve the directory containing this script (the kernel-monitor/ folder).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_PY="${SCRIPT_DIR}/monitor.py"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root: sudo bash $0"
    exit 1
fi

if [[ ! -f "$MONITOR_PY" ]]; then
    echo "ERROR: monitor.py not found at $MONITOR_PY"
    exit 1
fi

echo "[install] writing ${UNIT_DEST}"
cat > "$UNIT_DEST" <<EOF
[Unit]
Description=Kernel dmesg monitor for I2S/audio driver alarms
After=local-fs.target systemd-journald.service
DefaultDependencies=no

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${MONITOR_PY}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
User=root

[Install]
WantedBy=multi-user.target
EOF

echo "[install] daemon-reload"
systemctl daemon-reload

echo "[install] enabling and starting ${SERVICE_NAME}"
systemctl enable "${SERVICE_NAME}"
systemctl start "${SERVICE_NAME}"

echo ""
systemctl status "${SERVICE_NAME}" --no-pager
