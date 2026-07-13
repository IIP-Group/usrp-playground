#!/bin/bash
# Start the USRP daemons: one combined daemon per USRP in the hardware
# inventory (see usrp_testbed_library/usrp_daemon.py), plus the inventory
# helper. Idempotent: already-running daemons are skipped.
#
#   ./start-daemons.sh            start everything
#   ./start-daemons.sh <usrp-id>  start only that USRP's daemon
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
DAEMON_DIR="${SCRIPT_DIR}/usrp_testbed_library"
PID_DIR="${SCRIPT_DIR}/.daemon_pids"
LOG_DIR="${SCRIPT_DIR}/logs"
ONLY_USRP="$1"

if [ ! -d "$VENV_DIR" ]; then
    echo "Error: .venv not found. Run ./setup-daemons.sh first."
    exit 1
fi

if [ -f "${SCRIPT_DIR}/.env" ]; then
    set -a
    source "${SCRIPT_DIR}/.env"
    set +a
fi

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
    UHD_SITE=$(python3 -c 'import uhd, os; print(os.path.dirname(os.path.dirname(uhd.__file__)))' 2>/dev/null || true)
else
    DAEMON_PYTHONPATH=$(sudo -u "$REPO_OWNER" "$PYTHON" -c "import sys; print(':'.join(p for p in sys.path if p))" 2>/dev/null || true)
    UHD_SITE=$(sudo -u "$REPO_OWNER" python3 -c 'import uhd, os; print(os.path.dirname(os.path.dirname(uhd.__file__)))' 2>/dev/null || true)
fi
# Also make sure the repo owner's ~/.local/bin (uhd_find_devices et al.) is
# reachable for the helper when running as root.
OWNER_HOME=$(eval echo "~$REPO_OWNER")
export PATH="$OWNER_HOME/.local/bin:$PATH"
if [ -n "$UHD_SITE" ]; then
    DAEMON_PYTHONPATH="${UHD_SITE}:${DAEMON_PYTHONPATH}"
fi

# ---- Preflight: refuse to spawn daemons that would crash-loop -------------
# Verifies the exact interpreter + PYTHONPATH the daemons will get. If this
# fails we abort with an actionable message instead of writing endless
# `ModuleNotFoundError` tracebacks into the logs.
if ! PYTHONPATH="$DAEMON_PYTHONPATH" "$PYTHON" -c "import uhd, zmq, numpy, h5py" 2>/tmp/usrp_preflight_err; then
    echo ""
    echo "ERROR: daemon environment is broken - refusing to start."
    echo "-------------------------------------------------------"
    cat /tmp/usrp_preflight_err
    echo "-------------------------------------------------------"
    echo "Fix:   ./setup-daemons.sh     (repairs the venv + UHD link)"
    echo "Test:  ${PYTHON} -c 'import uhd'"
    exit 1
fi

INVENTORY_DIR="${SIGNAL_DIR_HOST%/signals}/inventory"
INVENTORY_FILE="${INVENTORY_DIR}/inventory.json"

# USRP ids from the inventory (one daemon per USRP).
USRP_IDS=""
if [ -f "$INVENTORY_FILE" ]; then
    USRP_IDS=$(PYTHONPATH="$DAEMON_PYTHONPATH" "$PYTHON" -c "
import json, sys
inv = json.load(open(sys.argv[1]))
for u in inv.get('usrps', []):
    uid = str(u.get('id', '')).strip()
    if uid:
        print(uid)
" "$INVENTORY_FILE")
fi

echo "=========================================="
echo "  Starting USRP Daemons"
echo "=========================================="
echo "  Inventory: ${INVENTORY_FILE}"
echo "  USRPs:     $(echo $USRP_IDS | tr '\n' ' ')"
[ -n "$UHD_SITE" ] && echo "  UHD:       ${UHD_SITE}"
echo "  Signals:   ${SIGNAL_DIR_HOST}"
echo "=========================================="

# ---- Inventory helper (BEFORE daemons so its initial scan sees all USRPs) -
# Watches the shared /data/inventory volume for discovery triggers from the
# entrypoint container and responds with uhd_find_devices output. We boot
# this first so the very first scan happens before any daemon claims a
# USRP - otherwise claimed devices stay invisible.
sudo mkdir -p "$INVENTORY_DIR"
sudo chown "$(id -u):$(id -g)" "$INVENTORY_DIR" 2>/dev/null || true
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

if [ -z "$USRP_IDS" ]; then
    echo ""
    echo "No USRPs in the inventory yet - only the inventory helper is running."
    echo "Add USRPs + channels on the Hardware page, then run this again"
    echo "(or press Start there)."
    exit 0
fi

# ---- One daemon per USRP ---------------------------------------------------
for UID_ in $USRP_IDS; do
    if [ -n "$ONLY_USRP" ] && [ "$UID_" != "$ONLY_USRP" ]; then
        continue
    fi
    PID_FILE="${PID_DIR}/usrp_${UID_}.pid"
    if [ -f "$PID_FILE" ] && sudo kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Daemon for USRP '$UID_' already running (PID $(cat "$PID_FILE"))"
        continue
    fi
    echo "Starting daemon for USRP '$UID_' ..."
    "${SCRIPT_DIR}/run-daemon.sh" "$UID_" \
        >> "${LOG_DIR}/daemon_${UID_}.log" 2>&1 &
    D_PID=$!
    echo "$D_PID" > "$PID_FILE"
    echo "Daemon for '$UID_' started (PID ${D_PID}), log: ${LOG_DIR}/daemon_${UID_}.log"
done

echo ""
echo "Stop with: ./stop-daemons.sh [usrp-id]"
