#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

echo "=========================================="
echo "  USRP Daemon Setup"
echo "=========================================="

# Write the location of the system-wide UHD python bindings into a .pth
# file inside the venv. This makes `import uhd` work in the venv
# PERMANENTLY - for every user, under sudo and under systemd - without any
# PYTHONPATH tricks. Needed because from-source UHD installs land in
# /usr/local/lib/pythonX.Y/site-packages, which a venv does not include
# even with --system-site-packages.
link_uhd_into_venv() {
    local uhd_site venv_site
    uhd_site=$(python3 -c 'import uhd, os; print(os.path.dirname(os.path.dirname(uhd.__file__)))' 2>/dev/null || true)
    if [ -z "$uhd_site" ]; then
        echo ""
        echo "ERROR: the system python3 cannot import uhd."
        echo "       Install UHD with Python bindings first (apt or from source),"
        echo "       verify with:  python3 -c 'import uhd; print(uhd.__version__)'"
        echo "       then re-run this script."
        return 1
    fi
    venv_site=$("${VENV_DIR}/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    echo "$uhd_site" > "${venv_site}/_uhd_location.pth"
    echo "Linked UHD into venv: ${uhd_site}"
}

verify_venv() {
    if "${VENV_DIR}/bin/python" - <<'PYEOF'
import uhd, zmq, numpy, h5py
print(f"  uhd {uhd.__version__} | zmq | numpy | h5py: all importable")
PYEOF
    then
        echo "Venv check: OK"
    else
        echo ""
        echo "ERROR: the venv is missing packages the daemons need (see above)."
        echo "       Re-run this script and recreate the venv when asked."
        exit 1
    fi
}

if [ -d "$VENV_DIR" ]; then
    echo "Existing .venv found at ${VENV_DIR}"
    read -p "Recreate? [y/N] " answer
    if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
        rm -rf "$VENV_DIR"
    else
        echo "Keeping existing .venv, repairing UHD link + updating library ..."
        link_uhd_into_venv
        source "${VENV_DIR}/bin/activate"
        pip install --upgrade -e "${SCRIPT_DIR}/usrp_testbed_library"
        verify_venv
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

link_uhd_into_venv
verify_venv

echo ""
echo "=========================================="
echo "  Setup complete"
echo "=========================================="
echo ""
echo "Venv: ${VENV_DIR}"
echo ""
echo "Next: cp .env.example .env && nano .env && ./start.sh"
