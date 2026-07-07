#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Stopping Docker containers ..."
sudo docker compose -f docker-compose.prod.yml down

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

if [ "$USE_REAL_USRP" = "true" ]; then
    bash "${SCRIPT_DIR}/stop-daemons.sh"
fi

echo "System stopped."
