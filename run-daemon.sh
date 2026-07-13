#!/bin/bash
# Run ONE USRP daemon in the FOREGROUND for the given inventory USRP id.
# Used by start-daemons.sh (backgrounds it) and by the daemon agent for
# per-USRP starts. All environment resolution lives here so every start
# path behaves identically.
#
#   ./run-daemon.sh <usrp-id>
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
DAEMON_DIR="${SCRIPT_DIR}/usrp_testbed_library"

USRP_ID="$1"
if [ -z "$USRP_ID" ]; then
    echo "Usage: $0 <usrp-id>   (id from the hardware inventory)"
    exit 1
fi

if [ -f "${SCRIPT_DIR}/.env" ]; then
    set -a
    source "${SCRIPT_DIR}/.env"
    set +a
fi

if [ -n "$SIGNAL_DIR_HOST" ]; then
    SIGNAL_DIR_HOST="$(cd "$SIGNAL_DIR_HOST" 2>/dev/null && pwd || echo "$SIGNAL_DIR_HOST")"
else
    SIGNAL_DIR_HOST="${SCRIPT_DIR}/data/signals"
fi
INVENTORY_FILE="${SIGNAL_DIR_HOST%/signals}/inventory/inventory.json"

if [ ! -f "$INVENTORY_FILE" ]; then
    echo "No inventory at $INVENTORY_FILE - add USRPs on the Hardware page first."
    exit 1
fi

PYTHON="${VENV_DIR}/bin/python"

# Resolve PYTHONPATH as the repo owner (systemd runs this as root, whose
# user-site does not contain UHD) and add the system UHD location - see
# start-daemons.sh for the full rationale.
REPO_OWNER=$(stat -c '%U' "$SCRIPT_DIR" 2>/dev/null || stat -f '%Su' "$SCRIPT_DIR")
if [ "$(id -un)" = "$REPO_OWNER" ]; then
    DAEMON_PYTHONPATH=$("$PYTHON" -c "import sys; print(':'.join(p for p in sys.path if p))" 2>/dev/null || true)
    UHD_SITE=$(python3 -c 'import uhd, os; print(os.path.dirname(os.path.dirname(uhd.__file__)))' 2>/dev/null || true)
else
    DAEMON_PYTHONPATH=$(sudo -u "$REPO_OWNER" "$PYTHON" -c "import sys; print(':'.join(p for p in sys.path if p))" 2>/dev/null || true)
    UHD_SITE=$(sudo -u "$REPO_OWNER" python3 -c 'import uhd, os; print(os.path.dirname(os.path.dirname(uhd.__file__)))' 2>/dev/null || true)
fi
[ -n "$UHD_SITE" ] && DAEMON_PYTHONPATH="${UHD_SITE}:${DAEMON_PYTHONPATH}"

# Look up identifier, roles, type and ports from the inventory. One line,
# space-separated; '-' marks an empty type so word-splitting stays stable.
RESOLVED=$(PYTHONPATH="$DAEMON_PYTHONPATH" "$PYTHON" -c "
import json, sys
from usrp_testbed_library.endpoints import endpoints_from_inventory
inv = json.load(open(sys.argv[1]))
eps = endpoints_from_inventory(inv)
uid = sys.argv[2]
if uid not in eps:
    sys.exit('USRP id ' + repr(uid) + ' not found in inventory')
e = eps[uid]
roles = {'tx': 'tx', 'rx': 'rx'}.get(e['role'], 'tx,rx')
print(e['identifier'].replace(' ', ''), roles, e['type'] or '-',
      e['tx_rep'], e['tx_pub'], e['rx_rep'], e['rx_pub'], e['index'])
" "$INVENTORY_FILE" "$USRP_ID")
read -r IDENTIFIER ROLES DTYPE TX_REP TX_PUB RX_REP RX_PUB INDEX <<< "$RESOLVED"

if [ -z "$IDENTIFIER" ] || [ "$IDENTIFIER" = "-" ]; then
    echo "USRP '$USRP_ID' has no identifier in the inventory."
    exit 1
fi

# Device type: inventory > env > b200
[ "$DTYPE" = "-" ] && DTYPE="${USRP_DEVICE_TYPE:-b200}"
DTYPE_KEY=$(echo "$DTYPE" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
_mcr_specific="MASTER_CLOCK_RATE_${DTYPE_KEY}"
MCR="${!_mcr_specific:-${MASTER_CLOCK_RATE:-0}}"

# Pin each daemon to TWO dedicated cores with RT priority - the combined
# daemon runs a TX and an RX thread; a single core can starve the RX
# stream on multi-channel start (USB overflow / out-of-sequence errors).
NPROC=$(nproc 2>/dev/null || echo 4)
CPU_A=$(( (2 + 2 * INDEX) % NPROC ))
CPU_B=$(( (CPU_A + 1) % NPROC ))

echo "Starting daemon for USRP '$USRP_ID' ($IDENTIFIER, roles=$ROLES, type=$DTYPE, cpus=$CPU_A,$CPU_B)"
exec sudo PYTHONPATH="$DAEMON_PYTHONPATH" \
    taskset -c "$CPU_A,$CPU_B" chrt -f 80 \
    "$PYTHON" "${DAEMON_DIR}/usrp_daemon.py" \
    --usrp-id "$USRP_ID" \
    --usrp-addr "$IDENTIFIER" \
    --device-type "$DTYPE" \
    --mcr "$MCR" \
    --roles "$ROLES" \
    --tx-rep-port "$TX_REP" --tx-pub-port "$TX_PUB" \
    --rx-rep-port "$RX_REP" --rx-pub-port "$RX_PUB"
