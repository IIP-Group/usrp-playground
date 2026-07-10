#!/bin/bash
# Install the usrp-daemons systemd service so the TX/RX daemons start
# automatically on boot. Portable: run this once per machine, from any
# checkout location - the repo path is filled in automatically.
#
#   sudo ./deploy/install-daemons-service.sh
#
# Afterwards:
#   systemctl status usrp-daemons
#   sudo systemctl restart usrp-daemons
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo $0"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_SRC="${SCRIPT_DIR}/usrp-daemons.service"
UNIT_DST="/etc/systemd/system/usrp-daemons.service"

if [ ! -x "${REPO_DIR}/start-daemons.sh" ]; then
    echo "Error: ${REPO_DIR}/start-daemons.sh not found or not executable."
    exit 1
fi
if [ ! -d "${REPO_DIR}/.venv" ]; then
    echo "Warning: ${REPO_DIR}/.venv missing - run ./setup-daemons.sh first,"
    echo "otherwise the service will fail at boot."
fi

sed "s|__REPO_DIR__|${REPO_DIR}|g" "$UNIT_SRC" > "$UNIT_DST"

# Agent unit: bridges the admin web UI (Hardware page) to the host so
# daemons can be started/stopped from the browser.
AGENT_SRC="${SCRIPT_DIR}/usrp-daemon-agent.service"
AGENT_DST="/etc/systemd/system/usrp-daemon-agent.service"
sed "s|__REPO_DIR__|${REPO_DIR}|g" "$AGENT_SRC" > "$AGENT_DST"

systemctl daemon-reload
systemctl enable usrp-daemons usrp-daemon-agent
systemctl restart usrp-daemon-agent

echo "Installed and enabled: ${UNIT_DST} + ${AGENT_DST} (repo: ${REPO_DIR})"
echo "Agent is running. Start the daemons with:  sudo systemctl start usrp-daemons"
echo "(or from the admin web UI, Hardware page)"
