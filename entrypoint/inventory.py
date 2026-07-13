"""
USRP inventory management. Persists a list of USRP devices and the channel
mappings between them (TX port → RX port) into a JSON file on the shared
volume. The host-side inventory_helper.py runs `uhd_find_devices` on
demand and writes its result alongside, which the API exposes to the UI.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

_DIR = Path(os.environ.get("INVENTORY_DIR", "/data/inventory"))
_INVENTORY = _DIR / "inventory.json"
_DISCOVER_REQUEST = _DIR / "discover_request"
_DISCOVER_RESULT = _DIR / "discover_result.json"
# Daemon start/stop/status - served by the host-side daemon_agent.py
# (deploy/usrp-daemon-agent.service), same shared-volume protocol as
# the discovery helper.
_DAEMON_STATUS = _DIR / "daemon_status.json"
_DAEMONCTL_REQUEST = _DIR / "daemonctl_request.json"
_DAEMONCTL_RESULT = _DIR / "daemonctl_result.json"
_AGENT_STALE_SEC = 8.0
_lock = Lock()

# Default empty inventory shape.
_EMPTY = {"usrps": [], "channels": []}


def _ensure_dir() -> None:
    _DIR.mkdir(parents=True, exist_ok=True)


def read_inventory() -> dict:
    """Return the persisted inventory, or an empty skeleton."""
    _ensure_dir()
    if not _INVENTORY.exists():
        return {"usrps": [], "channels": []}
    try:
        data = json.loads(_INVENTORY.read_text())
        data.setdefault("usrps", [])
        data.setdefault("channels", [])
        return data
    except Exception:
        return {"usrps": [], "channels": []}


def write_inventory(data: dict) -> dict:
    """Validate, normalise and atomically write the inventory file."""
    _ensure_dir()
    usrps = list(data.get("usrps") or [])
    channels = list(data.get("channels") or [])

    # Validate USRPs (id + identifier unique, both mandatory).
    # `identifier` is whatever UHD needs to find the device - a free-form
    # string that may contain `serial=…`, `addr=…`, `name=…`, or any
    # combination separated by commas. We don't try to parse it.
    seen_ids: set[str] = set()
    seen_ident: set[str] = set()
    cleaned_usrps = []
    for u in usrps:
        uid = str(u.get("id", "")).strip()
        if not uid:
            raise ValueError("Each USRP needs a non-empty 'id'.")
        if uid in seen_ids:
            raise ValueError(f"Duplicate USRP id: {uid!r}.")
        # Backward compat: older inventories had separate `serial` / `args`.
        identifier = str(u.get("identifier")
                         or u.get("args")
                         or u.get("serial")
                         or "").strip()
        if not identifier:
            raise ValueError(
                f"USRP {uid!r}: 'identifier' is required "
                f"(e.g. 'serial=3485538', 'addr=192.168.10.2', 'name=myusrp')."
            )
        if identifier in seen_ident:
            raise ValueError(f"Duplicate identifier across USRPs: {identifier!r}.")
        seen_ids.add(uid); seen_ident.add(identifier)
        ports = [str(p).strip() for p in (u.get("ports") or []) if str(p).strip()]
        cleaned_usrps.append({
            "id":         uid,
            "label":      str(u.get("label", uid)).strip(),
            "identifier": identifier,
            "type":       str(u.get("type", "")).strip(),
            "role":       str(u.get("role", "")).strip(),    # "tx" / "rx" / "txrx" / ""
            "ports":      ports,
        })

    # Validate channels (TX-USRP/port → RX-USRP/port, refs must exist).
    usrp_index = {u["id"]: u for u in cleaned_usrps}
    cleaned_channels = []
    seen_ch_ids: set[str] = set()

    def _num(value, default=None):
        if value in ("", None):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _chan(value):
        """Optional device channel index (0/1 on a B210). Empty = auto."""
        if value in ("", None):
            return None
        try:
            iv = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Device channel index must be an integer, got {value!r}.")
        if iv < 0:
            raise ValueError(f"Device channel index must be >= 0, got {iv}.")
        return iv

    for c in channels:
        cid = str(c.get("id", "")).strip()
        if not cid:
            raise ValueError("Each channel needs a non-empty 'id'.")
        if cid in seen_ch_ids:
            raise ValueError(f"Duplicate channel id: {cid!r}.")
        seen_ch_ids.add(cid)
        tx = c.get("tx") or {}
        rx = c.get("rx") or {}
        tx_id, tx_port = str(tx.get("usrp", "")).strip(), str(tx.get("port", "")).strip()
        rx_id, rx_port = str(rx.get("usrp", "")).strip(), str(rx.get("port", "")).strip()
        for side, uid, port in [("tx", tx_id, tx_port), ("rx", rx_id, rx_port)]:
            if uid not in usrp_index:
                raise ValueError(
                    f"Channel {cid!r}: {side}.usrp {uid!r} is not in the USRP list."
                )
            if port and port not in usrp_index[uid]["ports"]:
                raise ValueError(
                    f"Channel {cid!r}: port {port!r} not declared on USRP {uid!r}."
                )
            # Role check: a channel may only transmit over a USRP whose
            # daemon serves the TX role, and receive over one serving RX
            # (empty role = txrx = both).
            role = (usrp_index[uid].get("role") or "").strip().lower()
            if side == "tx" and role == "rx":
                raise ValueError(
                    f"Channel {cid!r}: USRP {uid!r} has role 'rx' and "
                    f"cannot be used as TX side."
                )
            if side == "rx" and role == "tx":
                raise ValueError(
                    f"Channel {cid!r}: USRP {uid!r} has role 'tx' and "
                    f"cannot be used as RX side."
                )
        cleaned_channels.append({
            "id":    cid,
            "label": str(c.get("label", cid)).strip(),
            "tx":    {
                "usrp":      tx_id,
                "port":      tx_port,
                "chan":      _chan(tx.get("chan")),
                "gain_db":   _num(tx.get("gain_db")),
                "power_dbm": _num(tx.get("power_dbm")),
            },
            "rx":    {
                "usrp":    rx_id,
                "port":    rx_port,
                "chan":    _chan(rx.get("chan")),
                "gain_db": _num(rx.get("gain_db")),
            },
        })

    out = {"usrps": cleaned_usrps, "channels": cleaned_channels}
    with _lock:
        tmp = _INVENTORY.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2))
        os.replace(tmp, _INVENTORY)
    return out


def trigger_discovery() -> None:
    """Ask the host-side helper to run uhd_find_devices."""
    _ensure_dir()
    _DISCOVER_REQUEST.touch()


def latest_discovery() -> dict:
    """Return the last discover_result.json contents, or a stub."""
    _ensure_dir()
    if not _DISCOVER_RESULT.exists():
        return {"timestamp": None, "devices": [], "error":
                "Discovery helper not running. Start it via ./start-daemons.sh on the host."}
    try:
        return json.loads(_DISCOVER_RESULT.read_text())
    except Exception as e:
        return {"timestamp": None, "devices": [],
                "error": f"Could not parse discovery result: {e}"}


def wait_for_discovery(timeout_s: float = 6.0, poll: float = 0.25) -> dict:
    """Trigger discovery and wait until the result file is fresher than now."""
    _ensure_dir()
    start_mtime = _DISCOVER_RESULT.stat().st_mtime if _DISCOVER_RESULT.exists() else 0
    trigger_discovery()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _DISCOVER_RESULT.exists() and _DISCOVER_RESULT.stat().st_mtime > start_mtime:
            return latest_discovery()
        time.sleep(poll)
    return latest_discovery()


# ---- Daemon control (host agent bridge) ------------------------------------

def daemon_status() -> dict:
    """Daemon liveness as reported by the host agent, plus agent liveness.

    `agent_online` is False when the status file is missing or stale -
    that means the usrp-daemon-agent service is not running on the host.
    """
    _ensure_dir()
    try:
        data = json.loads(_DAEMON_STATUS.read_text())
    except Exception:
        data = {"daemons": {}}
    age = time.time() - float(data.get("agent_ts", 0) or 0)
    data["agent_online"] = age < _AGENT_STALE_SEC
    data["status_age_sec"] = round(age, 1)
    try:
        data["last_result"] = json.loads(_DAEMONCTL_RESULT.read_text())
    except Exception:
        data["last_result"] = None
    return data


def request_daemon_action(action: str, usrp: str = None,
                          wait_s: float = 20.0, poll: float = 0.3) -> dict:
    """Ask the host agent to start/stop/restart daemons.

    `usrp=None` targets ALL daemons; a USRP id targets only that device's
    daemon. Waits up to `wait_s` for the result; a start can take longer
    (USRP init), in which case {"status": "pending"} is returned and the
    UI keeps watching daemon_status().
    """
    _ensure_dir()
    status = daemon_status()
    if not status.get("agent_online"):
        return {"status": "agent_offline",
                "message": "Daemon agent is not running on the host. "
                           "Install it once with: "
                           "sudo ./deploy/install-daemons-service.sh"}
    ts = time.time()
    tmp = _DAEMONCTL_REQUEST.with_suffix(".tmp")
    tmp.write_text(json.dumps({"action": action, "usrp": usrp, "ts": ts}))
    os.replace(tmp, _DAEMONCTL_REQUEST)

    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            res = json.loads(_DAEMONCTL_RESULT.read_text())
            if res.get("request_ts") == ts:
                res["status"] = "done"
                return res
        except Exception:
            pass
        time.sleep(poll)
    return {"status": "pending", "action": action,
            "message": "Still working (USRP init can take a while) - "
                       "the status below updates automatically."}
