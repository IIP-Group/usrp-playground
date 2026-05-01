import json
import sys
import asyncio
import threading
import numpy as np
import websockets
import urllib.request
import urllib.error


def _run_async(coro):
    """Run a coroutine, even if called from within a running event loop (Jupyter)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result = {}

    def runner():
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as e:
            result["exc"] = e

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "exc" in result:
        raise result["exc"]
    return result["value"]


class USRPClient:
    _host = None
    _port = None
    _token = None
    _info = None

    @classmethod
    def setup(cls, host="localhost", port=8000, token="default-bench-token-2024"):
        cls._host = host
        cls._port = port
        cls._token = token
        cls._info = None

    @classmethod
    def _base_url(cls):
        if cls._host is None:
            raise RuntimeError("Call USRPClient.setup() first")
        return f"{cls._host}:{cls._port}"

    @classmethod
    def _fetch_info(cls):
        if cls._info is not None:
            return cls._info
        try:
            url = f"http://{cls._base_url()}/info?auth_token={cls._token}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                cls._info = json.loads(resp.read())
        except Exception:
            cls._info = {}
        return cls._info

    @classmethod
    def check(cls) -> bool:
        try:
            url = f"http://{cls._base_url()}/health?auth_token={cls._token}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                return data.get("status") == "ok"
        except Exception:
            return False

    @classmethod
    @property
    def carrier_frequency(cls) -> int:
        return cls._fetch_info().get("carrier_frequency_hz", 0)

    @classmethod
    @property
    def sample_rate(cls) -> int:
        return cls._fetch_info().get("sample_rate_hz", 0)

    @classmethod
    @property
    def bandwidth(cls) -> int:
        return cls._fetch_info().get("bandwidth_hz", 0)

    @classmethod
    @property
    def tx_gain(cls) -> int:
        return cls._fetch_info().get("tx_gain_db", 0)

    @classmethod
    @property
    def rx_gain(cls) -> int:
        return cls._fetch_info().get("rx_gain_db", 0)

    @classmethod
    @property
    def max_samples(cls) -> int:
        return cls._fetch_info().get("max_samples", 0)

    @classmethod
    def info(cls) -> dict:
        return cls._fetch_info()

    @classmethod
    def send(cls, signal: np.ndarray, verbose: bool = False) -> np.ndarray:
        signal = np.asarray(signal, dtype=np.complex64)
        raw = np.empty(len(signal) * 2, dtype=np.float32)
        raw[0::2] = signal.real
        raw[1::2] = signal.imag
        result_bytes = _run_async(cls._ws_send(raw.tobytes(), verbose=verbose))
        raw_out = np.frombuffer(result_bytes, dtype=np.float32)
        return raw_out[0::2] + 1j * raw_out[1::2]

    @classmethod
    async def _ws_send(cls, data: bytes, verbose: bool = False) -> bytes:
        url = f"ws://{cls._base_url()}/ws/run?auth_token={cls._token}"
        async with websockets.connect(url, max_size=200 * 1024 * 1024) as ws:
            await ws.send(data)
            if verbose:
                print(f"[upload] Sent {len(data):,} bytes ({len(data) // 8:,} samples)")

            result_size = None

            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    if verbose:
                        total = len(msg)
                        print(f"\r[download] {total:,} bytes received", end="")
                        print(f" — {total // 8:,} samples")
                    return msg

                info = json.loads(msg)

                if "error" in info:
                    if verbose:
                        print(f"\r[error] {info['error']}: {info.get('message', '')}")
                    raise RuntimeError(f"Server error: {info['message']}")

                if not verbose:
                    if info.get("message") == "info":
                        cls._info = {k: v for k, v in info.items() if k != "message"}
                    continue

                msg_type = info.get("message")

                if msg_type == "info":
                    cls._info = {k: v for k, v in info.items() if k != "message"}
                    fc = info.get("carrier_frequency_hz", 0) / 1e6
                    bw = info.get("bandwidth_hz", 0) / 1e6
                    sr = info.get("sample_rate_hz", 0) / 1e6
                    print(f"[info] USRP | Carrier: {fc:.0f} MHz | BW: {bw:.0f} MHz | Rate: {sr:.0f} MSps")

                elif msg_type == "queued":
                    pos = info.get("queue_position", 0)
                    uid = info.get("uid", "")[:8]
                    if pos == 0:
                        print(f"[queued] Task {uid}... — next in line")
                    else:
                        print(f"[queued] Task {uid}... — {pos} task(s) ahead")

                elif msg_type == "status":
                    state = info.get("state", "?")
                    pos = info.get("queue_position", 0)
                    if state == "PD":
                        if pos == 0:
                            print(f"\r[waiting] Next in line...", end="")
                        else:
                            print(f"\r[waiting] {pos} task(s) ahead...", end="")
                    elif state == "R":
                        print(f"\r[running] Processing signal...            ", end="")
                    elif state == "D":
                        print(f"\r[done] Processing complete                ")

                elif msg_type == "done":
                    print("[download] Receiving result...")

                else:
                    print(f"[server] {json.dumps(info)}")
