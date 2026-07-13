"""Single source of truth for per-USRP daemon endpoints.

Every USRP in the hardware inventory gets ONE daemon process that serves
the TX role and/or the RX role on separate ZMQ REP sockets. The ports are
derived deterministically from the device's position in the inventory
list, so the worker, the start scripts, the daemon agent and the daemons
themselves all agree without any extra configuration:

    usrps[i]  ->  tx_rep = 5600 + 4*i      (TX commands)
                  tx_pub = 5601 + 4*i      (TX async events)
                  rx_rep = 5602 + 4*i      (RX commands)
                  rx_pub = 5603 + 4*i      (RX heartbeat)

Editing the inventory (adding/removing USRPs) can therefore shift ports -
that is fine because every consumer re-reads the inventory live and the
daemons are restarted on inventory changes anyway.
"""
from __future__ import annotations

BASE_PORT = 5600
PORTS_PER_USRP = 4


def ports_for_index(i: int) -> dict:
    base = BASE_PORT + PORTS_PER_USRP * int(i)
    return {"tx_rep": base, "tx_pub": base + 1,
            "rx_rep": base + 2, "rx_pub": base + 3}


def normalize_role(role) -> str:
    """Map the inventory role field to 'tx', 'rx' or 'txrx'."""
    r = str(role or "").strip().lower()
    if r in ("tx", "rx"):
        return r
    return "txrx"


def endpoints_from_inventory(inventory: dict) -> dict:
    """Return {usrp_id: {index, identifier, type, role, tx_rep, ...}}."""
    out = {}
    for i, u in enumerate((inventory or {}).get("usrps", [])):
        uid = str(u.get("id", "")).strip()
        if not uid:
            continue
        out[uid] = {
            "index": i,
            "identifier": str(u.get("identifier") or u.get("args")
                              or u.get("serial") or "").strip(),
            "type": str(u.get("type", "")).strip(),
            "role": normalize_role(u.get("role")),
            **ports_for_index(i),
        }
    return out
