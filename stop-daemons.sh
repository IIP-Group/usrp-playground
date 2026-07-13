#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="${SCRIPT_DIR}/.daemon_pids"

echo "Stopping USRP daemons ..."

for daemon in tx rx inventory; do
    PID_FILE="${PID_DIR}/${daemon}.pid"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            sudo kill "$PID" 2>/dev/null
            echo "${daemon^^} stopped (PID ${PID})"
        else
            echo "${daemon^^} not running (stale PID ${PID})"
        fi
        rm -f "$PID_FILE"
    fi
done

# Belt and braces: also kill ORPHANED daemon processes whose PID files were
# lost or overwritten (e.g. after mixed manual/systemd starts). An orphan
# keeps the USRP claimed ("Device busy") and makes every restart die
# silently right after "starting".
for pattern in tx_daemon.py rx_daemon.py inventory_helper.py; do
    ORPHANS=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [ -n "$ORPHANS" ]; then
        echo "Killing orphaned ${pattern}: ${ORPHANS}"
        sudo pkill -f "$pattern" 2>/dev/null || true
    fi
done

echo "Done"
