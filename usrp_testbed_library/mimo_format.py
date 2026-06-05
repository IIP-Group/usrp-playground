"""
Shared wire format for multi-channel (MIMO) IQ blobs.

A SISO blob is just float32 interleaved (I0, Q0, I1, Q1, ...) - unchanged
from the original protocol, so old clients keep working byte-for-byte.

A MIMO blob is prefixed with a 16-byte header so the receiver knows it
is multi-channel data:

    offset  size  field
    ──────  ────  ──────────────────────────────────────────────
       0      8   magic, b"MIMO\\x00\\x00\\x00\\x00"
       8      4   n_channels  (uint32 little-endian, ≥ 1)
      12      4   n_samples   (uint32 little-endian, per channel)
      16    …     channel-sequential samples:
                  ch0_I0, ch0_Q0, ch0_I1, ch0_Q1, …,    (n_samples pairs)
                  ch1_I0, ch1_Q0, …                      (n_samples pairs)
                  …                                      (n_channels times)

Total size after the header: n_channels * n_samples * 8 bytes (float32 IQ).

The magic was chosen so that the first 8 bytes never look like plausible
normalised IQ samples - interpreting the magic as two float32 yields
3.44e9 and 0.0, far outside the [-1, 1] range an ADC produces.
"""
from __future__ import annotations

import struct
import numpy as np

MIMO_MAGIC = b"MIMO\x00\x00\x00\x00"
MIMO_HEADER_LEN = 16


def is_mimo_blob(data: bytes) -> bool:
    return len(data) >= MIMO_HEADER_LEN and data[:8] == MIMO_MAGIC


def encode_mimo(signal_2d: np.ndarray) -> bytes:
    """Encode a (n_samples, n_channels) complex array into a MIMO blob."""
    arr = np.asarray(signal_2d)
    if arr.ndim != 2:
        raise ValueError(f"encode_mimo expects 2D, got {arr.ndim}D")
    n_samples, n_channels = arr.shape
    if n_channels < 1 or n_samples < 1:
        raise ValueError(f"invalid MIMO shape: {arr.shape}")
    arr = arr.astype(np.complex64)
    header = MIMO_MAGIC + struct.pack("<II", n_channels, n_samples)
    # channel-sequential: for each ch, interleave I/Q
    out = np.empty(n_channels * n_samples * 2, dtype=np.float32)
    for ch in range(n_channels):
        col = arr[:, ch]
        base = ch * n_samples * 2
        out[base + 0::2][:n_samples] = col.real
        out[base + 1::2][:n_samples] = col.imag
    return header + out.tobytes()


def decode_mimo(data: bytes) -> np.ndarray:
    """Decode a MIMO blob into a (n_samples, n_channels) complex64 array."""
    if not is_mimo_blob(data):
        raise ValueError("not a MIMO blob")
    n_channels, n_samples = struct.unpack("<II", data[8:16])
    expected = n_channels * n_samples * 8
    payload = data[MIMO_HEADER_LEN:]
    if len(payload) < expected:
        raise ValueError(
            f"MIMO blob truncated: header says {expected} bytes, got {len(payload)}"
        )
    raw = np.frombuffer(payload[:expected], dtype=np.float32)
    out = np.empty((n_samples, n_channels), dtype=np.complex64)
    for ch in range(n_channels):
        base = ch * n_samples * 2
        I = raw[base + 0:base + n_samples * 2:2]
        Q = raw[base + 1:base + n_samples * 2:2]
        out[:, ch] = I + 1j * Q
    return out


def encode_siso(signal_1d: np.ndarray) -> bytes:
    """Encode a 1-D complex array as a raw IQ blob (no header)."""
    arr = np.asarray(signal_1d, dtype=np.complex64).ravel()
    raw = np.empty(arr.size * 2, dtype=np.float32)
    raw[0::2] = arr.real
    raw[1::2] = arr.imag
    return raw.tobytes()


def decode_siso(data: bytes) -> np.ndarray:
    """Decode a header-less SISO blob into a 1-D complex64 array."""
    raw = np.frombuffer(data, dtype=np.float32)
    if raw.size % 2 != 0:
        raw = raw[:-1]
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def encode_any(signal: np.ndarray) -> bytes:
    """Encode either a 1-D (SISO) or 2-D (MIMO) signal.

    A 2-D array of shape (n_samples, 1) still goes through the MIMO encoder
    so the receiver can tell it apart from a plain 1-D SISO blob.
    """
    arr = np.asarray(signal)
    if arr.ndim == 1:
        return encode_siso(arr)
    if arr.ndim == 2:
        return encode_mimo(arr)
    raise ValueError(f"unsupported ndim={arr.ndim}; expected 1 or 2")


def decode_any(data: bytes) -> np.ndarray:
    """Decode a blob and return either 1-D (SISO) or 2-D (MIMO) complex64."""
    if is_mimo_blob(data):
        return decode_mimo(data)
    return decode_siso(data)
