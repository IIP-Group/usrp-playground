#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
    echo "Error: .env not found. Copy .env.example to .env and configure it."
    exit 1
fi

set -a
source .env
set +a

mkdir -p data/tasks/input data/tasks/output data/signals data/postgres logs

echo "=========================================="
echo "  USRP Playground"
echo "=========================================="

if [ "$USE_REAL_USRP" = "true" ]; then
    echo "  Mode: REAL USRP"
    echo ""
    echo "Starting daemons ..."
    bash "${SCRIPT_DIR}/start-daemons.sh"
    echo ""
else
    echo "  Mode: AWGN Simulation"
    echo ""
fi

echo "Starting Docker containers (GHCR: ${GHCR_OWNER}) ..."
sudo docker compose -f docker-compose.prod.yml up -d

echo ""
echo "=========================================="
echo "  System running"
echo "=========================================="
echo "  API:  http://localhost:${ENTRYPOINT_PORT:-8000}"
echo "  Info: http://localhost:${ENTRYPOINT_PORT:-8000}/info?auth_token=${DEFAULT_AUTH_TOKEN}"
echo ""
echo "  Stop: ./stop.sh"
echo "=========================================="
