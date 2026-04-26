"""
Stresstest für den USRP-Benchmark-Server.

Öffnet N parallele WebSocket-Verbindungen, lädt jeweils ein Signal hoch
und beobachtet:
  * wie viele Verbindungen gleichzeitig akzeptiert werden  (MAX_WS)
  * wie lang die Warteschlange werden darf                 (max_pending)
  * wie viele Tasks aktuell pending sind (live via /health, falls erreichbar)

Aufruf:
    python stresstest.py --host localhost --port 8000 --n 500 --samples 200000
"""
import argparse
import asyncio
import json
import time
import urllib.request
import numpy as np
import websockets


TOKEN = "default-bench-token-2024"


def make_signal(n_samples: int) -> bytes:
    sig = np.exp(1j * 2 * np.pi * 1e6 * np.arange(n_samples) / 25e6).astype(np.complex64)
    raw = np.empty(len(sig) * 2, dtype=np.float32)
    raw[0::2] = sig.real
    raw[1::2] = sig.imag
    return raw.tobytes()


_health_path = None  # cached: which path actually returns 200


def fetch_health(host: str, port: int) -> dict:
    global _health_path
    candidates = [_health_path] if _health_path else ["/health", "/api/health"]
    for path in candidates:
        if not path:
            continue
        url = f"http://{host}:{port}{path}?auth_token={TOKEN}"
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                data = json.loads(r.read())
                _health_path = path
                return data
        except Exception:
            continue
    return {}


class Stats:
    def __init__(self):
        self.connected = 0
        self.peak_connected = 0
        self.queued = 0
        self.peak_queue_pos = 0
        self.done = 0
        self.errors_seen: dict[str, int] = {}

    def err(self, key: str):
        self.errors_seen[key] = self.errors_seen.get(key, 0) + 1

    def inc_conn(self):
        self.connected += 1
        if self.connected > self.peak_connected:
            self.peak_connected = self.connected

    def dec_conn(self):
        self.connected -= 1


async def one_client(idx: int, url: str, payload: bytes, stats: Stats,
                     total_timeout: float, msg_timeout: float):
    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, max_size=200 * 1024 * 1024,
                               open_timeout=10, close_timeout=2),
            timeout=15,
        )
    except Exception as e:
        stats.err(f"connect:{type(e).__name__}")
        return

    stats.inc_conn()
    got_any_msg = False
    try:
        try:
            await ws.send(payload)
        except Exception as e:
            stats.err(f"send:{type(e).__name__}")
            return

        deadline = time.monotonic() + total_timeout
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=msg_timeout)
            except asyncio.TimeoutError:
                stats.err("recv_timeout")
                return
            except websockets.ConnectionClosed:
                stats.err("closed_without_msg" if not got_any_msg else "closed_mid_stream")
                return
            got_any_msg = True
            if isinstance(msg, bytes):
                stats.done += 1
                return
            info = json.loads(msg)
            if "error" in info:
                stats.err(f"server:{info['error']}")
                return
            mtype = info.get("message")
            if mtype == "queued":
                stats.queued += 1
                pos = info.get("queue_position", 0)
                if pos > stats.peak_queue_pos:
                    stats.peak_queue_pos = pos
            elif mtype == "status":
                pos = info.get("queue_position", 0)
                if pos > stats.peak_queue_pos:
                    stats.peak_queue_pos = pos
        stats.err("total_timeout")
    finally:
        stats.dec_conn()
        try:
            await ws.close()
        except Exception:
            pass


async def monitor(host: str, port: int, stats: Stats, stop: asyncio.Event):
    while not stop.is_set():
        h = fetch_health(host, port)
        pending = h.get("pending_tasks", "?")
        ws_conn = h.get("ws_connections", "?")
        usrp = h.get("usrp_state", "?")
        print(f"[live] srv: ws={ws_conn} pending={pending} usrp={usrp}  "
              f"| client: open={stats.connected} done={stats.done} "
              f"queued={stats.queued} peakQpos={stats.peak_queue_pos} "
              f"errs={sum(stats.errors_seen.values())}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass


async def run(host: str, port: int, n: int, ramp: float,
              total_timeout: float, msg_timeout: float, samples: int):
    url = f"ws://{host}:{port}/ws/run?auth_token={TOKEN}"
    payload = make_signal(samples)
    stats = Stats()
    stop = asyncio.Event()

    print(f"[start] target={url}")
    print(f"[start] n={n} ramp={ramp}s payload={len(payload):,} bytes ({samples:,} samples)")
    h0 = fetch_health(host, port)
    if h0:
        print(f"[start] health (über {_health_path}): {h0}")
    else:
        print("[start] /health nicht erreichbar — laufe trotzdem, ohne Live-Server-Werte")

    mon = asyncio.create_task(monitor(host, port, stats, stop))

    tasks = []
    delay = ramp / max(n, 1)
    t0 = time.monotonic()
    for i in range(n):
        tasks.append(asyncio.create_task(
            one_client(i, url, payload, stats, total_timeout, msg_timeout)
        ))
        if delay > 0:
            await asyncio.sleep(delay)

    await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - t0

    stop.set()
    await mon

    h1 = fetch_health(host, port)
    print()
    print("=" * 60)
    print(f"[done] Dauer: {elapsed:.1f}s")
    if h1:
        print(f"[done] health nach Test: {h1}")
    print()
    print("--- Ergebnisse ---")
    print(f"  versuchte Clients          : {n}")
    print(f"  Peak gleichzeitig offen    : {stats.peak_connected}")
    print(f"  Tasks erfolgreich done     : {stats.done}")
    print(f"  Tasks 'queued' bestätigt   : {stats.queued}")
    print(f"  Peak queue_position        : {stats.peak_queue_pos}")

    server_errs = {k: v for k, v in stats.errors_seen.items() if k.startswith("server:")}
    other_errs = {k: v for k, v in stats.errors_seen.items() if not k.startswith("server:")}
    too_many = server_errs.get("server:too_many_connections", 0)
    queue_full = server_errs.get("server:queue_full", 0)
    closed_without = other_errs.get("closed_without_msg", 0)

    print()
    print("--- Server-Fehler (explizit gemeldet) ---")
    if server_errs:
        for k, v in sorted(server_errs.items(), key=lambda x: -x[1]):
            print(f"    {k:<40} {v}")
    else:
        print("    keine")

    print()
    print("--- Sonstige Fehler / Verbindungsabbrüche ---")
    if other_errs:
        for k, v in sorted(other_errs.items(), key=lambda x: -x[1]):
            print(f"    {k:<40} {v}")
    else:
        print("    keine")

    print()
    print("--- Interpretation ---")
    print(f"  MAX_WS  ≈ {stats.peak_connected}   (Peak gleichzeitig offene WS)")
    if too_many:
        print(f"          + {too_many} explizite 'too_many_connections'-Closes")
    if closed_without:
        print(f"          + {closed_without} Verbindungen ohne Nachricht geschlossen")
        print("          → wahrscheinlich auch Limit-Treffer (Server schließt sofort)")
    print(f"  max_pending  ≈ {stats.queued + queue_full}   "
          f"(akzeptierte Queue-Plätze {stats.queued} + abgelehnt {queue_full})")
    print(f"  längste beobachtete Warteliste : {stats.peak_queue_pos}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--n", type=int, default=500, help="Anzahl paralleler Clients")
    p.add_argument("--ramp", type=float, default=2.0,
                   help="Verteile Connect-Versuche über X Sekunden (0 = alle gleichzeitig)")
    p.add_argument("--total-timeout", type=float, default=60.0,
                   help="Max. Wartezeit pro Client gesamt (s)")
    p.add_argument("--msg-timeout", type=float, default=8.0,
                   help="Max. Wartezeit zwischen zwei Server-Nachrichten (s)")
    p.add_argument("--samples", type=int, default=200_000,
                   help="Signal-Größe pro Upload — größer ⇒ Queue baut sich auf")
    args = p.parse_args()
    asyncio.run(run(args.host, args.port, args.n, args.ramp,
                    args.total_timeout, args.msg_timeout, args.samples))


if __name__ == "__main__":
    main()
