#!/bin/bash
# Stop USRP daemons.
#
#   ./stop-daemons.sh            stop everything (all daemons + helper)
#   ./stop-daemons.sh <usrp-id>  stop only that USRP's daemon

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="${SCRIPT_DIR}/.daemon_pids"
ONLY_USRP="$1"

stop_pidfile() {
    local name="$1" pid_file="$2"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            sudo kill "$pid" 2>/dev/null
            echo "${name} stopped (PID ${pid})"
        else
            echo "${name} not running (stale PID ${pid})"
        fi
        rm -f "$pid_file"
    fi
}

if [ -n "$ONLY_USRP" ]; then
    echo "Stopping daemon for USRP '$ONLY_USRP' ..."
    stop_pidfile "USRP $ONLY_USRP" "${PID_DIR}/usrp_${ONLY_USRP}.pid"
    # Orphan sweep for exactly this daemon (PID file may have been lost).
    sudo pkill -f "usrp_daemon.py --usrp-id ${ONLY_USRP} " 2>/dev/null || true
    echo "Done"
    exit 0
fi

echo "Stopping USRP daemons ..."

# Per-USRP daemons (dynamic PID files) + inventory helper + legacy tx/rx.
for pid_file in "${PID_DIR}"/usrp_*.pid; do
    [ -e "$pid_file" ] || continue
    name=$(basename "$pid_file" .pid)
    stop_pidfile "$name" "$pid_file"
done
stop_pidfile "INVENTORY" "${PID_DIR}/inventory.pid"
stop_pidfile "TX (legacy)" "${PID_DIR}/tx.pid"
stop_pidfile "RX (legacy)" "${PID_DIR}/rx.pid"

# Belt and braces: also kill ORPHANED daemon processes whose PID files were
# lost or overwritten (e.g. after mixed manual/systemd starts). An orphan
# keeps the USRP claimed ("Device busy") and makes every restart die
# silently right after "starting".
for pattern in usrp_daemon.py tx_daemon.py rx_daemon.py inventory_helper.py; do
    ORPHANS=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [ -n "$ORPHANS" ]; then
        echo "Killing orphaned ${pattern}: ${ORPHANS}"
        sudo pkill -f "$pattern" 2>/dev/null || true
    fi
done

echo "Done"
