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

    # Validate USRPs (id + serial unique, mandatory).
    seen_ids: set[str] = set()
    seen_serials: set[str] = set()
    cleaned_usrps = []
    for u in usrps:
        uid = str(u.get("id", "")).strip()
        if not uid:
            raise ValueError("Each USRP needs a non-empty 'id'.")
        if uid in seen_ids:
            raise ValueError(f"Duplicate USRP id: {uid!r}.")
        serial = str(u.get("serial", "")).strip()
        if not serial:
            raise ValueError(f"USRP {uid!r}: serial is required.")
        if serial in seen_serials:
            raise ValueError(f"Duplicate serial across USRPs: {serial!r}.")
        seen_ids.add(uid); seen_serials.add(serial)
        ports = [str(p).strip() for p in (u.get("ports") or []) if str(p).strip()]
        cleaned_usrps.append({
            "id":      uid,
            "label":   str(u.get("label", uid)).strip(),
            "serial":  serial,
            "type":    str(u.get("type", "b200")).strip(),
            "role":    str(u.get("role", "")).strip(),    # "tx" / "rx" / ""
            "ports":   ports,
            "args":    str(u.get("args", "")).strip(),
        })

    # Validate channels (TX-USRP/port → RX-USRP/port, refs must exist).
    usrp_index = {u["id"]: u for u in cleaned_usrps}
    cleaned_channels = []
    seen_ch_ids: set[str] = set()
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
        cleaned_channels.append({
            "id":    cid,
            "label": str(c.get("label", cid)).strip(),
            "tx":    {"usrp": tx_id, "port": tx_port},
            "rx":    {"usrp": rx_id, "port": rx_port},
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
