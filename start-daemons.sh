#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
DAEMON_DIR="${SCRIPT_DIR}/usrp_testbed_library"
PID_DIR="${SCRIPT_DIR}/.daemon_pids"
LOG_DIR="${SCRIPT_DIR}/logs"

if [ ! -d "$VENV_DIR" ]; then
    echo "Error: .venv not found. Run ./setup-daemons.sh first."
    exit 1
fi

if [ -f "${SCRIPT_DIR}/.env" ]; then
    set -a
    source "${SCRIPT_DIR}/.env"
    set +a
fi

DEVICE_TYPE="${USRP_DEVICE_TYPE:-x4xx}"

# Per-device-type address and MCR overrides.
# If USRP_TX_ADDR_B200 / USRP_TX_ADDR_X4XX etc. are set in .env,
# use those; otherwise fall back to the generic USRP_TX_ADDR.
DTYPE_KEY=$(echo "$DEVICE_TYPE" | tr '[:lower:]' '[:upper:]' | tr '-' '_')

_tx_specific="USRP_TX_ADDR_${DTYPE_KEY}"
_rx_specific="USRP_RX_ADDR_${DTYPE_KEY}"
_mcr_specific="MASTER_CLOCK_RATE_${DTYPE_KEY}"

USRP_TX_ADDR="${!_tx_specific:-${USRP_TX_ADDR:-192.168.10.2}}"
USRP_RX_ADDR="${!_rx_specific:-${USRP_RX_ADDR:-192.168.20.2}}"
MCR="${!_mcr_specific:-${MASTER_CLOCK_RATE:-0}}"
BUFFER_SCALE="${DAEMON_BUFFER_SCALE:-1.0}"

# Resolve SIGNAL_DIR_HOST to absolute path
if [ -n "$SIGNAL_DIR_HOST" ]; then
    mkdir -p "$SIGNAL_DIR_HOST"
    SIGNAL_DIR_HOST="$(cd "$SIGNAL_DIR_HOST" && pwd)"
else
    SIGNAL_DIR_HOST="${SCRIPT_DIR}/data/signals"
fi

mkdir -p "$PID_DIR" "$LOG_DIR" "$SIGNAL_DIR_HOST"

PYTHON="${VENV_DIR}/bin/python"

# Resolve full Python path so UHD (possibly in user site-packages) is found
# under sudo. IMPORTANT: resolve as the REPO OWNER, not the current user -
# under systemd this script runs as root, and root's user-site does not
# contain UHD, which would crash the daemons at `import uhd`.
REPO_OWNER=$(stat -c '%U' "$SCRIPT_DIR" 2>/dev/null || stat -f '%Su' "$SCRIPT_DIR")
if [ "$(id -un)" = "$REPO_OWNER" ]; then
    DAEMON_PYTHONPATH=$("$PYTHON" -c "import sys; print(':'.join(p for p in sys.path if p))" 2>/dev/null || true)
else
    DAEMON_PYTHONPATH=$(sudo -u "$REPO_OWNER" "$PYTHON" -c "import sys; print(':'.join(p for p in sys.path if p))" 2>/dev/null || true)
fi
# Also make sure the repo owner's ~/.local/bin (uhd_find_devices et al.) is
# reachable for the helper when running as root.
OWNER_HOME=$(eval echo "~$REPO_OWNER")
export PATH="$OWNER_HOME/.local/bin:$PATH"

# UHD's Python bindings can live in a system location the venv does NOT
# include even with --system-site-packages - e.g. a from-source install
# under /usr/local/lib/pythonX.Y/site-packages. Ask the system python3
# (which finds uhd) where it lives and prepend that dir, so `import uhd`
# works inside the venv. Portable: no hard-coded path, works on any machine
# where uhd is importable. venv and system python3 share the same minor
# version (the venv is built with `python3 -m venv`), so the ABI matches.
_uhd_probe='import uhd, os; print(os.path.dirname(os.path.dirname(uhd.__file__)))'
if [ "$(id -un)" = "$REPO_OWNER" ]; then
    UHD_SITE=$(python3 -c "$_uhd_probe" 2>/dev/null || true)
else
    UHD_SITE=$(sudo -u "$REPO_OWNER" python3 -c "$_uhd_probe" 2>/dev/null || true)
fi
if [ -n "$UHD_SITE" ]; then
    DAEMON_PYTHONPATH="${UHD_SITE}:${DAEMON_PYTHONPATH}"
    echo "  UHD:      ${UHD_SITE}"
fi

echo "=========================================="
echo "  Starting USRP Daemons"
echo "=========================================="
echo "  TX USRP:  ${USRP_TX_ADDR}"
echo "  RX USRP:  ${USRP_RX_ADDR}"
echo "  Type:     ${DEVICE_TYPE}"
echo "  MCR:      ${MCR} Hz (0 = UHD auto)"
echo "  Buffer:   ${BUFFER_SCALE}x"
echo "  Signals:  ${SIGNAL_DIR_HOST}"
echo "=========================================="

# ---- Inventory helper (BEFORE daemons so its initial scan sees all USRPs) -
# Watches the shared /data/inventory volume for discovery triggers from the
# entrypoint container and responds with uhd_find_devices output. We boot
# this first so the very first scan happens before any TX/RX daemon claims
# a USRP - otherwise claimed devices stay invisible.
INVENTORY_DIR="${SIGNAL_DIR_HOST%/signals}/inventory"
sudo mkdir -p "$INVENTORY_DIR"
sudo chown "$(id -u):$(id -g)" "$INVENTORY_DIR"
sudo chmod 0775 "$INVENTORY_DIR"
if [ -f "${PID_DIR}/inventory.pid" ] && kill -0 "$(cat ${PID_DIR}/inventory.pid)" 2>/dev/null; then
    echo "Inventory helper already running (PID $(cat ${PID_DIR}/inventory.pid))"
else
    echo "Starting inventory helper ..."
    INVENTORY_WATCH_DIR="$INVENTORY_DIR" PYTHONPATH="$DAEMON_PYTHONPATH" \
        "$PYTHON" "${DAEMON_DIR}/inventory_helper.py" \
        >> "${LOG_DIR}/inventory.log" 2>&1 &
    INV_PID=$!
    echo "$INV_PID" > "${PID_DIR}/inventory.pid"
    echo "Inventory helper started (PID ${INV_PID}), log: ${LOG_DIR}/inventory.log"
    # Give the helper a moment to run its initial uhd_find_devices BEFORE the
    # daemons claim anything. 5 s is enough on USB; 0.5 s on Ethernet.
    sleep 5
fi

if [ -f "${PID_DIR}/tx.pid" ] && sudo kill -0 "$(cat ${PID_DIR}/tx.pid)" 2>/dev/null; then
    echo "TX daemon already running (PID $(cat ${PID_DIR}/tx.pid))"
else
    echo "Starting TX daemon ..."
    sudo PYTHONPATH="$DAEMON_PYTHONPATH" \
        taskset -c 2 chrt -f 80 \
        "$PYTHON" "${DAEMON_DIR}/tx_daemon.py" \
        --usrp-addr "$USRP_TX_ADDR" \
        --device-type "$DEVICE_TYPE" \
        --mcr "$MCR" \
        --buffer-scale "$BUFFER_SCALE" \
        >> "${LOG_DIR}/tx_daemon.log" 2>&1 &
    TX_PID=$!
    echo "$TX_PID" > "${PID_DIR}/tx.pid"
    echo "TX daemon started (PID ${TX_PID}), log: ${LOG_DIR}/tx_daemon.log"
fi

if [ -f "${PID_DIR}/rx.pid" ] && sudo kill -0 "$(cat ${PID_DIR}/rx.pid)" 2>/dev/null; then
    echo "RX daemon already running (PID $(cat ${PID_DIR}/rx.pid))"
else
    echo "Starting RX daemon ..."
    sudo PYTHONPATH="$DAEMON_PYTHONPATH" \
        taskset -c 3 chrt -f 80 \
        "$PYTHON" "${DAEMON_DIR}/rx_daemon.py" \
        --usrp-addr "$USRP_RX_ADDR" \
        --device-type "$DEVICE_TYPE" \
        --mcr "$MCR" \
        --buffer-scale "$BUFFER_SCALE" \
        >> "${LOG_DIR}/rx_daemon.log" 2>&1 &
    RX_PID=$!
    echo "$RX_PID" > "${PID_DIR}/rx.pid"
    echo "RX daemon started (PID ${RX_PID}), log: ${LOG_DIR}/rx_daemon.log"
fi

echo ""
echo "Stop with: ./stop-daemons.sh"
