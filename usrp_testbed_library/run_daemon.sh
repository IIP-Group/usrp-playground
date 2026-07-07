#!/bin/bash

# Script to run rx_daemon.py or tx_daemon.py with optional parameters
# Usage: ./run_daemon.sh [rx|tx] [OPTIONS]
# Options:
#   --buffer-scale <value>  Buffer scale value (default: 1.0)
#   --usrp-addr <address>   USRP address (default: 192.168.10.2)
#   --use-dpdk             Enable DPDK (requires --mgmt-addr)
#   --mgmt-addr <address>   Management address (required with --use-dpdk)
#   -h, --help             Show this help message

# Get the original user's home directory (before sudo)
if [ -n "$SUDO_USER" ]; then
    USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    USER_HOME="$HOME"
fi

# Default values
DAEMON_TYPE=""
BUFFER_SCALE="1.0"
USRP_ADDR="192.168.10.2"
USE_DPDK=false
MGMT_ADDR=""

# Function to display usage
show_usage() {
    echo "Usage: $0 [rx|tx] [OPTIONS]"
    echo ""
    echo "Daemon type (required):"
    echo "  rx                     Run rx_daemon.py"
    echo "  tx                     Run tx_daemon.py"
    echo ""
    echo "Options:"
    echo "  --buffer-scale <value>  Buffer scale value (default: 1.0)"
    echo "  --usrp-addr <address>   USRP address (default: 192.168.10.2)"
    echo "  --use-dpdk             Enable DPDK (requires --mgmt-addr)"
    echo "  --mgmt-addr <address>   Management address (required with --use-dpdk)"
    echo "  -h, --help             Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 rx                                    # Run RX daemon with defaults"
    echo "  $0 tx --buffer-scale 4.0                 # Run TX daemon with custom buffer"
    echo "  $0 rx --use-dpdk --mgmt-addr 192.168.100.2"
    exit 0
}

# Check if first argument is daemon type or help
if [ $# -eq 0 ]; then
    echo "Error: Daemon type (rx or tx) is required"
    show_usage
fi

# Parse daemon type
case $1 in
    rx|RX)
        DAEMON_TYPE="rx"
        shift
        ;;
    tx|TX)
        DAEMON_TYPE="tx"
        shift
        ;;
    -h|--help)
        show_usage
        ;;
    *)
        echo "Error: First argument must be 'rx' or 'tx'"
        echo "Use -h or --help for usage information"
        exit 1
        ;;
esac

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --buffer-scale)
            BUFFER_SCALE="$2"
            shift 2
            ;;
        --usrp-addr)
            USRP_ADDR="$2"
            shift 2
            ;;
        --use-dpdk)
            USE_DPDK=true
            shift
            ;;
        --mgmt-addr)
            MGMT_ADDR="$2"
            shift 2
            ;;
        -h|--help)
            show_usage
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage information"
            exit 1
            ;;
    esac
done

# Validate: if --use-dpdk is specified, --mgmt-addr is required
if [ "$USE_DPDK" = true ] && [ -z "$MGMT_ADDR" ]; then
    echo "Error: --mgmt-addr is required when --use-dpdk is specified"
    exit 1
fi

# Derive paths from the script's own location (works regardless of where the repo is cloned)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_PATH="${SCRIPT_DIR}/${DAEMON_TYPE}_daemon.py"

# Check if the daemon script exists
if [ ! -f "$DAEMON_PATH" ]; then
    echo "Error: Daemon script not found at: $DAEMON_PATH"
    exit 1
fi

# Build the Python command
PYTHON_CMD="python $DAEMON_PATH"
PYTHON_CMD="$PYTHON_CMD --buffer-scale $BUFFER_SCALE"
PYTHON_CMD="$PYTHON_CMD --usrp-addr $USRP_ADDR"

if [ "$USE_DPDK" = true ]; then
    PYTHON_CMD="$PYTHON_CMD --mgmt-addr $MGMT_ADDR --use-dpdk"
fi

# Display the command that will be executed
echo "=========================================="
echo "Starting ${DAEMON_TYPE^^} Daemon"
echo "=========================================="
echo "Configuration:"
echo "  User Home: $USER_HOME"
echo "  Daemon: ${DAEMON_TYPE}_daemon.py"
echo "  Buffer Scale: $BUFFER_SCALE"
echo "  USRP Address: $USRP_ADDR"
if [ "$USE_DPDK" = true ]; then
    echo "  DPDK: Enabled"
    echo "  Management Address: $MGMT_ADDR"
else
    echo "  DPDK: Disabled"
fi
echo ""
echo "Full command: taskset -c \"2-3\" chrt -f 80 $PYTHON_CMD"
echo "=========================================="
echo ""

# Create a temporary script that will be executed as root
TEMP_SCRIPT=$(mktemp /tmp/sdr_daemon_runner.XXXXXX.sh)

cat > "$TEMP_SCRIPT" << EOF
#!/bin/bash
cd ${SCRIPT_DIR}
source /etc/profile.d/conda.sh
conda activate toa-estimation-global

# Ensure output is not buffered for real-time display
export PYTHONUNBUFFERED=1

# Execute the command
taskset -c "2-3" chrt -f 80 $PYTHON_CMD
EOF

# Make the temporary script executable
chmod +x "$TEMP_SCRIPT"

# Execute as root using sudo su
echo "Switching to root and executing..."
sudo su -c "bash $TEMP_SCRIPT"

# Clean up the temporary script
rm -f "$TEMP_SCRIPT"