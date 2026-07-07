#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

echo "=========================================="
echo "  USRP Daemon Setup"
echo "=========================================="

if [ -d "$VENV_DIR" ]; then
    echo "Existing .venv found at ${VENV_DIR}"
    read -p "Recreate? [y/N] " answer
    if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
        rm -rf "$VENV_DIR"
    else
        echo "Keeping existing .venv, updating testbed library ..."
        source "${VENV_DIR}/bin/activate"
        pip install --upgrade -e "${SCRIPT_DIR}/usrp_testbed_library"
        echo "Done."
        exit 0
    fi
fi

echo "Creating .venv (with --system-site-packages for UHD access) ..."
python3 -m venv --system-site-packages "$VENV_DIR"
source "${VENV_DIR}/bin/activate"

echo "Installing usrp_testbed_library ..."
pip install --upgrade pip
pip install -e "${SCRIPT_DIR}/usrp_testbed_library"

echo ""
echo "=========================================="
echo "  Setup complete"
echo "=========================================="
echo ""
echo "Venv: ${VENV_DIR}"
echo ""
echo "Verify UHD is accessible:"
echo "  ${VENV_DIR}/bin/python -c 'import uhd; print(uhd.__version__)'"
echo ""
echo "Next: cp .env.example .env && nano .env && ./start.sh"
