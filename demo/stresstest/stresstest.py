"""
Stresstest für den USRP-Playground-Server.

Liest MAX_QUEUE aus /health und startet N+1 Clients (einer über dem Limit),
gibt für jeden Client live aus, was er erlebt.

Aufruf:
    python stresstest.py --host 127.0.0.1 --port 80 --token <TOKEN>
    python stresstest.py --host ... --n 10           # explizit 10+1 Clients
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


def fetch_health(host: str, port: int, token: str) -> dict:
    for path in ("/health", "/api/health"):
        try:
            url = f"http://{host}:{port}{path}?auth_token={token}"
            with urllib.request.urlopen(url, timeout=3) as r:
                return json.loads(r.read())
        except Exception:
            continue
    return {}


def log(idx: int, msg: str):
    t = time.strftime("%H:%M:%S")
    print(f"{t} [#{idx:03d}] {msg}", flush=True)


async def one_client(idx: int, url: str, payload: bytes, total_timeout: float):
    log(idx, "connecting...")
    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, max_size=200 * 1024 * 1024,
                               open_timeout=10, close_timeout=2),
            timeout=15,
        )
    except websockets.InvalidStatus as e:
        log(idx, f"REJECTED at handshake (HTTP {e.response.status_code})")
        return
    except asyncio.TimeoutError:
        log(idx, "REJECTED: connect timeout")
        return
    except Exception as e:
        log(idx, f"REJECTED: {type(e).__name__}: {e}")
        return

    log(idx, "WebSocket open")
    deadline = time.monotonic() + total_timeout
    sent = False
    try:
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                log(idx, "no message in 10s - giving up")
                return
            except websockets.ConnectionClosed as e:
                code = getattr(e, "code", "?")
                reason = getattr(e, "reason", "") or ""
                log(idx, f"connection closed (code={code} reason={reason!r})")
                return

            if isinstance(msg, bytes):
                log(idx, f"DONE - received {len(msg):,} bytes back")
                return

            info = json.loads(msg)
            if "error" in info:
                log(idx, f"SERVER ERROR: {info['error']} - {info.get('message','')}")
                return

            mtype = info.get("message")
            if mtype == "info":
                fc = info.get("carrier_frequency_hz", 0) / 1e6
                sr = info.get("sample_rate_hz", 0) / 1e6
                log(idx, f"info: carrier={fc:.0f} MHz, rate={sr:.0f} MSps")
                # send signal once, right after info
                if not sent:
                    await ws.send(payload)
                    sent = True
                    log(idx, f"uploaded {len(payload):,} bytes")
            elif mtype == "queued":
                log(idx, f"queued - position {info.get('queue_position', '?')}, "
                         f"uid {info.get('uid','')[:8]}")
            elif mtype == "status":
                state = info.get("state", "?")
                pos = info.get("queue_position", "?")
                log(idx, f"status: state={state} pos={pos}")
            elif mtype == "done":
                log(idx, "server says: processing done, downloading...")
            else:
                log(idx, f"server: {info}")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def run(host: str, port: int, n: int | None, samples: int,
              total_timeout: float, ramp: float, token: str):
    url = f"ws://{host}:{port}/ws/run?auth_token={token}"
    payload = make_signal(samples)

    h = fetch_health(host, port, token)
    print("=" * 70)
    print(f"Health vor Test: {json.dumps(h, indent=2)}")
    print("=" * 70)

    if n is None:
        n = h.get("queue_max") or h.get("ws_max") or 5
        print(f"→ MAX_QUEUE laut /health = {n}, starte {n + 1} Clients (einer über Limit)\n")
    else:
        print(f"→ {n + 1} Clients, payload={len(payload):,} bytes\n")

    delay = ramp / max(n + 1, 1) if ramp > 0 else 0
    tasks = []
    for i in range(n + 1):
        tasks.append(asyncio.create_task(
            one_client(i, url, payload, total_timeout)
        ))
        if delay > 0:
            await asyncio.sleep(delay)

    await asyncio.gather(*tasks, return_exceptions=True)

    print()
    print("=" * 70)
    h2 = fetch_health(host, port, token)
    print(f"Health nach Test: {json.dumps(h2, indent=2)}")


def main():
    global TOKEN
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--n", type=int, default=None,
                   help="Anzahl Clients (Skript startet N+1). Default: aus /health auslesen.")
    p.add_argument("--samples", type=int, default=200_000)
    p.add_argument("--ramp", type=float, default=0.0,
                   help="Verteile Connect-Versuche über X Sekunden (0 = alle gleichzeitig)")
    p.add_argument("--total-timeout", type=float, default=120.0)
    p.add_argument("--token", default=TOKEN)
    args = p.parse_args()
    TOKEN = args.token
    asyncio.run(run(args.host, args.port, args.n, args.samples,
                    args.total_timeout, args.ramp, args.token))


if __name__ == "__main__":
    main()
