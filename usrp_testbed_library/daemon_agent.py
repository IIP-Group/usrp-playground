"""Host-side agent: lets the admin web UI start/stop the USRP daemons and
see whether they are alive.

Runs as its own systemd service (deploy/usrp-daemon-agent.service) so it
keeps working while the daemons themselves are stopped - it must never be
part of usrp-daemons.service.

Protocol (same shared-volume style as inventory_helper):
    * Entrypoint writes ${WATCH_DIR}/daemonctl_request.json:
        {"action": "start"|"stop"|"restart", "ts": <epoch>}
    * Agent executes the action (systemctl usrp-daemons when installed,
      otherwise the repo's start/stop scripts) and writes
      ${WATCH_DIR}/daemonctl_result.json:
        {"ok": bool, "action": ..., "output": "...", "ts": <epoch>}
    * Every STATUS_INTERVAL seconds the agent refreshes
      ${WATCH_DIR}/daemon_status.json:
        {"agent_ts": <epoch>, "daemons": {
            "tx":        {"running": bool, "pid": int|null, "responsive": bool|null},
            "rx":        {...},
            "inventory": {"running": bool, "pid": int|null, "responsive": null}}}
      "running" = PID file exists and the process is alive.
      "responsive" = the daemon answered a ZMQ PING within a short timeout
      (null while it is busy with a long operation - that is normal).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

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

DAEMON_PORTS = {"tx": 5557, "rx": 5555}


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def _pid_running(name: str):
    pid_file = PID_DIR / f"{name}.pid"
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
    """PING the daemon's ZMQ REP socket. Returns True/False, or None when
    the socket connects but does not answer in time (daemon busy)."""
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
        return None          # alive but busy, or gone - PID check decides
    except Exception:
        return False
    finally:
        sock.close()


def refresh_status() -> None:
    daemons = {}
    for name in ("tx", "rx", "inventory"):
        running, pid = _pid_running(name)
        responsive = None
        if running and name in DAEMON_PORTS:
            responsive = _ping(DAEMON_PORTS[name])
        daemons[name] = {"running": running, "pid": pid,
                         "responsive": responsive}
    _write_atomic(STATUS_FILE, {"agent_ts": time.time(), "daemons": daemons})


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


def run_action(action: str) -> dict:
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "action": action,
                "output": f"unknown action '{action}'"}

    if _systemd_unit_available():
        # "start" while the unit is already active would be a no-op even
        # when an individual daemon has died (oneshot + RemainAfterExit).
        # Treat it as restart so the Start button always heals the setup.
        if action == "start" and _systemd_unit_active():
            action = "restart"
        cmd = ["systemctl", action, SYSTEMD_UNIT]
    else:
        # Fallback for machines without the systemd unit installed.
        script = {"start": "start-daemons.sh", "stop": "stop-daemons.sh"}
        if action == "restart":
            r1 = run_action("stop")
            r2 = run_action("start")
            return {"ok": r1["ok"] and r2["ok"], "action": "restart",
                    "output": r1["output"] + "\n" + r2["output"]}
        cmd = [str(REPO_DIR / script[action])]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           cwd=str(REPO_DIR))
        out = (r.stdout or "") + (r.stderr or "")
        return {"ok": r.returncode == 0, "action": action,
                "output": out[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "action": action, "output": "timed out"}
    except Exception as e:
        return {"ok": False, "action": action, "output": str(e)}


def handle_request() -> None:
    try:
        req = json.loads(REQUEST_FILE.read_text())
    except Exception:
        REQUEST_FILE.unlink(missing_ok=True)
        return
    REQUEST_FILE.unlink(missing_ok=True)
    action = str(req.get("action", ""))
    print(f"[daemonctl] executing '{action}'", flush=True)
    result = run_action(action)
    result["ts"] = time.time()
    result["request_ts"] = req.get("ts")
    _write_atomic(RESULT_FILE, result)
    print(f"[daemonctl] {action}: ok={result['ok']}", flush=True)


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
