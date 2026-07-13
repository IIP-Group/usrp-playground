"""Combined per-device USRP daemon: ONE process per USRP, serving the TX
role and/or the RX role of that device.

A USRP can only be claimed by a single process, so a device that should
both transmit and receive (single-USRP setups) must host both roles in one
process. This daemon opens the device once and runs up to two request
loops in threads, each with its own ZMQ REP socket:

    --tx-rep-port   TX commands  (CONFIGURE_USRP/LOAD_SIGNAL/TRANSMIT_BURST)
    --rx-rep-port   RX commands  (CONFIGURE_USRP/FLUSH_RX/RECEIVE_TO_FILE)

Both loops share the MultiUSRP handle - UHD supports concurrent TX and RX
streaming on one device (standard full-duplex usage). From the worker's
point of view every USRP therefore exposes a TX endpoint and an RX
endpoint, no matter whether they live in one process (one shared device)
or in two (classic two-device setup).

Port assignment comes from endpoints.py, derived from the device's
position in the hardware inventory.
"""
import argparse
import logging
import threading
import time

import zmq

try:
    from . import tx_daemon as tx_mod
    from . import rx_daemon as rx_mod
    from .usrp_common import buffer_scale_float
except ImportError:
    import tx_daemon as tx_mod
    import rx_daemon as rx_mod
    from usrp_common import buffer_scale_float

OPEN_ATTEMPTS = 6
OPEN_BACKOFF_S = 5


def serve(handler, daemon, rep_addr, stop_event, label):
    """Generic REP loop: recv -> handle -> send, until stop_event is set."""
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REP)
    sock.bind(rep_addr)
    logging.info(f"[{label}] listening on {rep_addr}")
    try:
        while not stop_event.is_set():
            if sock.poll(500) == 0:
                continue
            request = sock.recv_json()
            try:
                response = handler(daemon, request)
            except Exception as e:      # handler already catches; belt+braces
                response = {"status": "ERROR", "error": str(e)}
            sock.send_json(response)
    finally:
        sock.close(linger=0)


def parse_arguments():
    p = argparse.ArgumentParser(
        description="Combined per-device USRP daemon (TX and/or RX role)")
    p.add_argument("--usrp-id", required=True,
                   help="Inventory id of this USRP (used for logging)")
    p.add_argument("--usrp-addr", required=True,
                   help="UHD identifier (serial=..., addr=..., name=...)")
    p.add_argument("--device-type", default="b200")
    p.add_argument("--mcr", type=float, default=0.0)
    p.add_argument("--buffer-scale", type=buffer_scale_float, default=1.0)
    p.add_argument("--roles", default="tx,rx",
                   help="Comma list of roles this daemon serves: tx, rx")
    p.add_argument("--tx-rep-port", type=int, default=None)
    p.add_argument("--tx-pub-port", type=int, default=None)
    p.add_argument("--rx-rep-port", type=int, default=None)
    p.add_argument("--rx-pub-port", type=int, default=None)
    return p.parse_args()


def main():
    args = parse_arguments()
    roles = {r.strip().lower() for r in args.roles.split(",") if r.strip()}
    if not roles & {"tx", "rx"}:
        raise ValueError(f"--roles must contain tx and/or rx, got {args.roles!r}")

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s - {args.usrp_id} - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )

    common = dict(
        usrp_addr=args.usrp_addr,
        mcr=args.mcr or None,
        device_type=args.device_type,
        buffer_scale=args.buffer_scale,
    )

    # Open the device ONCE (with retry - see tx/rx daemons for rationale),
    # then share the handle with the second role.
    tx_daemon = rx_daemon = None
    for attempt in range(1, OPEN_ATTEMPTS + 1):
        try:
            if "tx" in roles:
                tx_daemon = tx_mod.TXDaemon(
                    daemon_id=f"TX-{args.usrp_id}",
                    pub_addr=f"tcp://*:{args.tx_pub_port}",
                    **common,
                )
            if "rx" in roles:
                rx_daemon = rx_mod.RXDaemon(
                    pub_addr=f"tcp://*:{args.rx_pub_port}",
                    usrp=tx_daemon.usrp if tx_daemon is not None else None,
                    **common,
                )
            break
        except RuntimeError as e:
            if attempt == OPEN_ATTEMPTS:
                logging.error(
                    f"Could not open USRP after {OPEN_ATTEMPTS} attempts - giving up.")
                raise
            logging.warning(
                f"USRP open failed (attempt {attempt}/{OPEN_ATTEMPTS}): {e} - "
                f"retrying in {OPEN_BACKOFF_S}s")
            time.sleep(OPEN_BACKOFF_S)

    stop_event = threading.Event()
    threads = []
    if tx_daemon is not None:
        threads.append(threading.Thread(
            target=serve,
            args=(tx_mod.handle_request, tx_daemon,
                  f"tcp://*:{args.tx_rep_port}", stop_event, "tx"),
            daemon=True))
    if rx_daemon is not None:
        threads.append(threading.Thread(
            target=serve,
            args=(rx_mod.handle_request, rx_daemon,
                  f"tcp://*:{args.rx_rep_port}", stop_event, "rx"),
            daemon=True))
    for t in threads:
        t.start()

    logging.info(
        f"USRP daemon for '{args.usrp_id}' running "
        f"(roles: {', '.join(sorted(roles))})")

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=2)
        if tx_daemon is not None:
            tx_daemon.close()
        if rx_daemon is not None:
            rx_daemon.close()


if __name__ == "__main__":
    main()
