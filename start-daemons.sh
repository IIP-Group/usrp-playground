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

USRP_TX_ADDR="${USRP_TX_ADDR:-192.168.10.2}"
USRP_RX_ADDR="${USRP_RX_ADDR:-192.168.10.2}"
DEVICE_TYPE="${USRP_DEVICE_TYPE:-x4xx}"
MCR="${MASTER_CLOCK_RATE:-0}"
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

# Resolve full Python path so UHD (possibly in user site-packages) is found under sudo
DAEMON_PYTHONPATH=$("$PYTHON" -c "import sys; print(':'.join(p for p in sys.path if p))" 2>/dev/null || true)

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
