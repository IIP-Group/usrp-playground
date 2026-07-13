"""Host-side agent: lets the admin web UI start/stop the USRP daemons and
see whether they are alive - globally and per USRP.

Runs as its own systemd service (deploy/usrp-daemon-agent.service) so it
keeps working while the daemons themselves are stopped - it must never be
part of usrp-daemons.service.

Protocol (same shared-volume style as inventory_helper):
    * Entrypoint writes ${WATCH_DIR}/daemonctl_request.json:
        {"action": "start"|"stop"|"restart", "usrp": <id>|null, "ts": <epoch>}
      usrp=null targets ALL daemons (systemctl usrp-daemons); a USRP id
      targets only that device's daemon (start/stop scripts).
    * Agent executes and writes ${WATCH_DIR}/daemonctl_result.json:
        {"ok": bool, "action": ..., "usrp": ..., "output": "...", "ts": ...}
    * Every STATUS_INTERVAL seconds the agent refreshes
      ${WATCH_DIR}/daemon_status.json:
        {"agent_ts": <epoch>,
         "usrps": {<id>: {"running": bool, "pid": int|null, "role": "txrx",
                          "tx_responsive": bool|null,
                          "rx_responsive": bool|null}, ...},
         "inventory_helper": {"running": bool, "pid": int|null}}
      "running" = PID file exists and the process is alive.
      "*_responsive" = the endpoint answered a ZMQ PING within a short
      timeout (null while busy with a long operation - that is normal).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

try:
    from .endpoints import endpoints_from_inventory
except ImportError:
    from endpoints import endpoints_from_inventory

WATCH_DIR = Path(os.environ.get("DAEMONCTL_WATCH_DIR",
                                os.environ.get("INVENTORY_WATCH_DIR",
                                               "/data/inventory")))
REPO_DIR = Path(os.environ.get("REPO_DIR",
                               Path(__file__).resolve().parent.parent))
PID_DIR = Path(os.environ.get("PID_DIR", REPO_DIR / ".daemon_pids"))
SYSTEMD_UNIT = os.environ.get("DAEMONS_UNIT", "usrp-daemons")
STATUS_INTERVAL = float(os.environ.get("STATUS_INTERVAL_SEC", "2.0"))
PING_TIMEOUT_MS = int(os.environ.get("PING_TIMEOUT_MS", "400"))

REQUEST_FILE = WATCH_DIR / "daemonctl_request.json"
RESULT_FILE = WATCH_DIR / "daemonctl_result.json"
STATUS_FILE = WATCH_DIR / "daemon_status.json"
INVENTORY_FILE = WATCH_DIR / "inventory.json"


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def _read_inventory() -> dict:
    try:
        return json.loads(INVENTORY_FILE.read_text())
    except Exception:
        return {"usrps": [], "channels": []}


def _pid_running(pid_file: Path):
    try:
        pid = int(pid_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, PermissionError):
        return False, pid


def _ping(port: int):
    """PING a daemon REP socket. True/False, or None when the socket does
    not answer in time (daemon busy with a long op, or just gone - the PID
    check decides which)."""
    try:
        import zmq
    except ImportError:
        return None
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, PING_TIMEOUT_MS)
    sock.setsockopt(zmq.SNDTIMEO, PING_TIMEOUT_MS)
    try:
        sock.connect(f"tcp://127.0.0.1:{port}")
        sock.send_json({"op": "PING"})
        return sock.recv_json().get("status") == "OK"
    except zmq.error.Again:
        return None
    except Exception:
        return False
    finally:
        sock.close()


def refresh_status() -> None:
    eps = endpoints_from_inventory(_read_inventory())
    usrps = {}
    for uid, ep in eps.items():
        running, pid = _pid_running(PID_DIR / f"usrp_{uid}.pid")
        tx_resp = rx_resp = None
        if running:
            if ep["role"] in ("tx", "txrx"):
                tx_resp = _ping(ep["tx_rep"])
            if ep["role"] in ("rx", "txrx"):
                rx_resp = _ping(ep["rx_rep"])
        usrps[uid] = {"running": running, "pid": pid, "role": ep["role"],
                      "tx_responsive": tx_resp, "rx_responsive": rx_resp}
    inv_running, inv_pid = _pid_running(PID_DIR / "inventory.pid")
    _write_atomic(STATUS_FILE, {
        "agent_ts": time.time(),
        "usrps": usrps,
        "inventory_helper": {"running": inv_running, "pid": inv_pid},
    })


def _systemd_unit_available() -> bool:
    try:
        r = subprocess.run(["systemctl", "cat", SYSTEMD_UNIT],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _systemd_unit_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", SYSTEMD_UNIT],
                           timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _run(cmd) -> dict:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           cwd=str(REPO_DIR))
        out = (r.stdout or "") + (r.stderr or "")
        return {"ok": r.returncode == 0, "output": out[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "timed out"}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def run_action(action: str, usrp: str = None) -> dict:
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "action": action, "usrp": usrp,
                "output": f"unknown action '{action}'"}

    if usrp:
        # Per-USRP: use the scripts directly. The agent's systemd unit runs
        # with KillMode=process, so daemons it spawns survive agent
        # restarts; the next global restart re-homes them into the
        # usrp-daemons unit anyway.
        uid = str(usrp)
        if action in ("stop", "restart"):
            res = _run([str(REPO_DIR / "stop-daemons.sh"), uid])
            if action == "stop":
                return {"action": action, "usrp": uid, **res}
        res = _run([str(REPO_DIR / "start-daemons.sh"), uid])
        return {"action": action, "usrp": uid, **res}

    # Global: via systemd when installed, otherwise the scripts.
    if _systemd_unit_available():
        # "start" while the unit is already active would be a no-op even
        # when individual daemons have died (oneshot + RemainAfterExit).
        # Treat it as restart so the Start button always heals the setup.
        if action == "start" and _systemd_unit_active():
            action_cmd = "restart"
        else:
            action_cmd = action
        res = _run(["systemctl", action_cmd, SYSTEMD_UNIT])
        return {"action": action, "usrp": None, **res}

    if action == "restart":
        r1 = _run([str(REPO_DIR / "stop-daemons.sh")])
        r2 = _run([str(REPO_DIR / "start-daemons.sh")])
        return {"ok": r1["ok"] and r2["ok"], "action": action, "usrp": None,
                "output": r1["output"] + "\n" + r2["output"]}
    script = "start-daemons.sh" if action == "start" else "stop-daemons.sh"
    res = _run([str(REPO_DIR / script)])
    return {"action": action, "usrp": None, **res}


def handle_request() -> None:
    try:
        req = json.loads(REQUEST_FILE.read_text())
    except Exception:
        REQUEST_FILE.unlink(missing_ok=True)
        return
    REQUEST_FILE.unlink(missing_ok=True)
    action = str(req.get("action", ""))
    usrp = req.get("usrp") or None
    print(f"[daemonctl] executing '{action}' (usrp={usrp})", flush=True)
    result = run_action(action, usrp=usrp)
    result["ts"] = time.time()
    result["request_ts"] = req.get("ts")
    _write_atomic(RESULT_FILE, result)
    print(f"[daemonctl] {action}: ok={result.get('ok')}", flush=True)


def main() -> None:
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[daemon-agent] watching {WATCH_DIR}, repo {REPO_DIR}", flush=True)
    last_status = 0.0
    while True:
        try:
            if REQUEST_FILE.exists():
                handle_request()
                refresh_status()
                last_status = time.monotonic()
            elif time.monotonic() - last_status >= STATUS_INTERVAL:
                refresh_status()
                last_status = time.monotonic()
        except Exception as e:
            print(f"[daemon-agent] error: {e}", flush=True)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
