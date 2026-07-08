# Keep annotations lazy: `int | None` etc. would crash at import time on
# Python 3.9, which the package still supports (requires-python >= 3.9).
from __future__ import annotations

import json
import struct
import sys
import asyncio
import threading
import numpy as np
import websockets
import urllib.request
import urllib.error


# ---- MIMO wire format ------------------------------------------------------
# Must match usrp_testbed_library.mimo_format on the server.
_MIMO_MAGIC = b"MIMO\x00\x00\x00\x00"
_MIMO_HEADER_LEN = 16


def _is_mimo_blob(data: bytes) -> bool:
    return len(data) >= _MIMO_HEADER_LEN and data[:8] == _MIMO_MAGIC


def _encode_mimo(signal_2d: np.ndarray) -> bytes:
    arr = np.asarray(signal_2d, dtype=np.complex64)
    n_samples, n_channels = arr.shape
    header = _MIMO_MAGIC + struct.pack("<II", n_channels, n_samples)
    out = np.empty(n_channels * n_samples * 2, dtype=np.float32)
    for ch in range(n_channels):
        base = ch * n_samples * 2
        out[base + 0::2][:n_samples] = arr[:, ch].real
        out[base + 1::2][:n_samples] = arr[:, ch].imag
    return header + out.tobytes()


def _decode_mimo(data: bytes) -> np.ndarray:
    n_channels, n_samples = struct.unpack("<II", data[8:16])
    payload = data[_MIMO_HEADER_LEN:_MIMO_HEADER_LEN + n_channels * n_samples * 8]
    raw = np.frombuffer(payload, dtype=np.float32)
    out = np.empty((n_samples, n_channels), dtype=np.complex64)
    for ch in range(n_channels):
        base = ch * n_samples * 2
        I = raw[base + 0:base + n_samples * 2:2]
        Q = raw[base + 1:base + n_samples * 2:2]
        out[:, ch] = I + 1j * Q
    return out


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
            return cls._info
        except Exception:
            # Don't cache the failure - the next call retries, so one
            # network hiccup doesn't stick as "all values are 0" forever.
            return {}

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
    def send(cls, signal: np.ndarray, channel: int = 0,
             verbose: bool = False) -> np.ndarray:
        """Send `signal` to the server and return what came back.

        * 1-D complex array → SISO. Returns 1-D complex64. `channel`
          selects which hardware channel the test runs over (index into
          the server's channel list, default 0).
        * 2-D array of shape (n_samples, n_channels) → MIMO. Each column
          is one channel's IQ stream; the returned array has the same
          shape. The server must have MIMO enabled. `channel` does not
          apply here - column i always drives channel i.
        """
        arr = np.asarray(signal)
        if arr.ndim == 1:
            arr = arr.astype(np.complex64)
            raw = np.empty(arr.size * 2, dtype=np.float32)
            raw[0::2] = arr.real
            raw[1::2] = arr.imag
            payload = raw.tobytes()
            if channel:
                handshake = {"mode": "siso", "channel": int(channel)}
            else:
                handshake = None
        elif arr.ndim == 2:
            if channel:
                raise ValueError(
                    "channel= only applies to 1-D (SISO) signals. In MIMO "
                    "column i always drives channel i; to test one single "
                    "channel use send_siso(signal, channel=...)."
                )
            n_samples, n_channels = arr.shape
            payload = _encode_mimo(arr)
            handshake = {"mode": "mimo", "channels": int(n_channels)}
        else:
            raise ValueError(f"signal must be 1-D or 2-D, got {arr.ndim}-D")

        result_bytes = _run_async(
            cls._ws_send(payload, handshake=handshake, verbose=verbose)
        )

        if _is_mimo_blob(result_bytes):
            return _decode_mimo(result_bytes)
        raw_out = np.frombuffer(result_bytes, dtype=np.float32)
        return raw_out[0::2] + 1j * raw_out[1::2]

    @classmethod
    def send_siso(cls, signal: np.ndarray, channel: int = 0,
                  verbose: bool = False) -> np.ndarray:
        """Send a single-channel (SISO) signal over a selectable channel.

        `signal` must be 1-D (a column/row vector of shape (N,1)/(1,N) is
        accepted and flattened). `channel` is the index into the server's
        channel list; the full TX/RX chain of that channel is used and
        the usual begin/end guard applies. Returns 1-D complex64.
        """
        arr = np.asarray(signal)
        if arr.ndim == 2 and 1 in arr.shape:
            arr = arr.ravel()
        if arr.ndim != 1:
            raise ValueError(
                f"send_siso expects a 1-D signal, got shape "
                f"{np.asarray(signal).shape}. For multi-channel use send_mimo()."
            )
        return cls.send(arr, channel=channel, verbose=verbose)

    @classmethod
    def send_mimo(cls, signal: np.ndarray, verbose: bool = False) -> np.ndarray:
        """Send a multi-channel (MIMO) signal.

        `signal` must be 2-D with shape (n_samples, n_channels); column i
        drives channel i. Returns an array of the same shape. To test one
        single channel use send_siso(signal, channel=...) instead of an
        all-zero column - SISO configures only that channel.
        """
        arr = np.asarray(signal)
        if arr.ndim != 2:
            raise ValueError(
                f"send_mimo expects shape (n_samples, n_channels), got "
                f"{arr.ndim}-D. For a single channel use send_siso()."
            )
        return cls.send(arr, verbose=verbose)

    @classmethod
    def listen(cls, n_samples: int, channel: int = 0,
               channels: int | None = None, verbose: bool = False) -> np.ndarray:
        """Receive only: capture `n_samples` from the radio without
        transmitting anything, and return the captured IQ vector.

        * channels=None or 1 → SISO listen on `channel` (index into the
          server's channel list, default 0). Returns 1-D complex64.
        * channels>=2 → MIMO listen on channels 0..channels-1. Returns
          shape (n_samples, channels). The server must have MIMO enabled.
        """
        n = int(n_samples)
        if n <= 0:
            raise ValueError("n_samples must be positive")
        n_ch = 1 if channels is None else int(channels)
        if n_ch < 1:
            raise ValueError("channels must be >= 1")
        if n_ch > 1:
            if channel:
                raise ValueError(
                    "channel= only applies to SISO listen. MIMO listen "
                    "always captures channels 0..N-1; to listen on one "
                    "single channel use listen_siso(n, channel=...)."
                )
            handshake = {"mode": "listen", "n_samples": n, "channels": n_ch}
        else:
            handshake = {"mode": "listen", "n_samples": n,
                         "channel": int(channel)}

        result_bytes = _run_async(
            cls._ws_send(None, handshake=handshake, verbose=verbose)
        )

        if _is_mimo_blob(result_bytes):
            return _decode_mimo(result_bytes)
        raw_out = np.frombuffer(result_bytes, dtype=np.float32)
        return raw_out[0::2] + 1j * raw_out[1::2]

    @classmethod
    def listen_siso(cls, n_samples: int, channel: int = 0,
                    verbose: bool = False) -> np.ndarray:
        """Capture `n_samples` on one selectable channel (no transmission).

        Returns 1-D complex64 of length n_samples.
        """
        return cls.listen(n_samples, channel=channel, verbose=verbose)

    @classmethod
    def listen_mimo(cls, n_samples: int, channels: int | None = None,
                    verbose: bool = False) -> np.ndarray:
        """Capture `n_samples` on all channels at once (no transmission).

        `channels` defaults to the server's mimo_max_channels. Returns
        shape (n_samples, channels).
        """
        if channels is None:
            channels = int(cls._fetch_info().get("mimo_max_channels", 2) or 2)
        channels = int(channels)
        if channels < 2:
            raise ValueError(
                "listen_mimo needs channels >= 2. For a single channel "
                "use listen_siso(n, channel=...)."
            )
        return cls.listen(n_samples, channels=channels, verbose=verbose)

    @classmethod
    async def _ws_send(cls, data: bytes | None, handshake: dict | None = None,
                       verbose: bool = False) -> bytes:
        url = f"ws://{cls._base_url()}/ws/run?auth_token={cls._token}"
        try:
            return await cls._ws_session(url, data, handshake, verbose)
        except websockets.exceptions.ConnectionClosed as e:
            # Translate an abrupt close into something a student can act on.
            rcvd = getattr(e, "rcvd", None)
            code = getattr(rcvd, "code", None)
            reason = getattr(rcvd, "reason", "") or ""
            if code == 1013 or "queue" in reason.lower():
                raise RuntimeError(
                    "Server busy: the queue is full or you have too many "
                    "parallel connections. Try again in a moment."
                ) from None
            detail = reason or (f"close code {code}" if code else "no reason given")
            raise RuntimeError(
                f"Server closed the connection unexpectedly ({detail}). "
                f"Try again; if it persists the server may be overloaded."
            ) from None
        except websockets.exceptions.InvalidHandshake as e:
            # Rejected before the WebSocket was accepted (queue full /
            # per-IP cap show up as HTTP 403 here).
            status = (getattr(getattr(e, "response", None), "status_code", None)
                      or getattr(e, "status_code", None))
            if status == 403:
                raise RuntimeError(
                    "Server busy: the queue is full or you have too many "
                    "parallel connections from your IP. Try again in a moment."
                ) from None
            raise RuntimeError(
                "Could not connect to the server"
                + (f" (HTTP {status})" if status else "")
                + ". Is it running?"
            ) from None
        except OSError as e:
            raise RuntimeError(
                f"Could not reach the server at {cls._base_url()}: {e}"
            ) from None

    @classmethod
    async def _ws_session(cls, url: str, data: bytes | None,
                          handshake: dict | None, verbose: bool) -> bytes:
        async with websockets.connect(url, max_size=200 * 1024 * 1024) as ws:
            if handshake is not None:
                await ws.send(json.dumps(handshake))
                if verbose:
                    print(f"[handshake] {handshake}")
            if data is not None:
                await ws.send(data)
                if verbose:
                    print(f"[upload] Sent {len(data):,} bytes")

            result_size = None

            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    if verbose:
                        total = len(msg)
                        print(f"\r[download] {total:,} bytes received", end="")
                        print(f" - {total // 8:,} samples")
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

                elif msg_type == "ack":
                    print(f"[ack] mode={info.get('mode')} accepted")

                elif msg_type == "queued":
                    pos = info.get("queue_position", 0)
                    uid = info.get("uid", "")[:8]
                    if pos == 0:
                        print(f"[queued] Task {uid}... - next in line")
                    else:
                        print(f"[queued] Task {uid}... - {pos} task(s) ahead")

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
