# USRP Sandbox - Student Guide

Welcome! This page has everything you need to test your waveforms over the shared USRP.

---

## 1. What is this?

A shared server that transmits your baseband signal over a real USRP X410 and returns what it received. You work locally (Python, MATLAB, ...); the server takes care of TX/RX.

- Two USRP X410, connected via 10 dB attenuator
- Master clock: 250 MHz
- Default sample rate: 31.25 MHz (250 MHz / 8)
- Carrier: 2.4 GHz (configurable)

---

## 2. Installation

```bash
pip install git+https://github.com/IIP-Group/usrp-playground.git
```

This installs the Python package `usrp_benchmark` with the `USRPClient` class plus the `usrp-client` CLI tool.

Recommended: a fresh virtual environment.

---

## 3. Your token

You received a personal token by email (it starts with your ETH-ID). Use it to authenticate against the server.

**Important:** do not share it. If your token leaks or is misused, contact the assistant.

---

## 4. Minimal example (Python)

```python
import numpy as np
from usrp_benchmark import USRPClient

USRPClient.setup(
    host="129.132.24.210",
    port=80,
    token="YOUR-TOKEN-HERE",
)

# Server reachable?
print("Server OK:", USRPClient.check())

# Query radio info
info = USRPClient.info()
print(f"Sample Rate: {info['sample_rate_hz']/1e6} MHz")
print(f"Carrier:     {info['carrier_frequency_hz']/1e9} GHz")

# Build a test signal (1 MHz complex sine)
fs = info['sample_rate_hz']
t  = np.arange(100_000) / fs
tx = np.exp(1j * 2 * np.pi * 1e6 * t).astype(np.complex64)

# Transmit and receive
rx = USRPClient.send(tx, verbose=True)
print(f"Received: {len(rx)} samples")
```

---

## 5. The most important methods

| Call | Returns |
|---|---|
| `USRPClient.setup(host, port, token)` | - call once per session |
| `USRPClient.check()` | bool - server reachable? |
| `USRPClient.info()` | dict - sample rate, carrier, gains, limits |
| `USRPClient.send(signal)` | numpy array - received signal |
| `USRPClient.send(signal, verbose=True)` | numpy array - with live status output |

`signal` must be a **complex numpy array** (IQ samples). It is converted to `complex64` internally.

---

## 6. What happens to your signal

1. **Upload** - your signal is sent to the server as binary
2. **Queue** - if other tasks are ahead of you, you wait
3. **Duty cycle check** - the server makes sure we stay below the TX time quota (10% / 60 s window)
4. **Listen Before Talk** - short RX check: is the channel free? If not, back off and retry
5. **TX + RX simultaneously** - the signal is transmitted while RX captures
6. **Download** - the received signal comes back as binary

The result contains **guard regions** before and after your signal (~100 ms with default settings). This guarantees you capture the whole burst even with small timing drift.

---

## 7. CLI tool (no Python required)

If your signal is generated in another language (MATLAB, C, GNU Radio) or you just want to push a file through quickly, after `pip install` the `usrp-client` command is available in your terminal.

**File format:** `.f32` - interleaved float32, in the order `real, imag, real, imag, ...`. This is the same format used by GNU Radio and most SDR tools.

**Usage:**

```bash
usrp-client -i input.f32 -o output.f32 \
            -s 129.132.24.210:80 \
            -t YOUR-TOKEN-HERE
```

**Arguments:**

| Flag | Meaning | Default |
|---|---|---|
| `-i`, `--input` | Path to input file (`.f32`, interleaved IQ) | - (required) |
| `-o`, `--output` | Path for the received file | `output.f32` |
| `-s`, `--server` | Server address `host:port` | `localhost:8000` |
| `-t`, `--token` | Auth token from your email | default-bench-token |

While running you get status updates on stdout: `[upload]`, `[queued]`, `[waiting]`, `[running]`, `[done]`, `[result]`.

**Example: produce f32 from numpy** (if you have a signal in Python and want to send it via the CLI):

```python
import numpy as np
sig = np.exp(1j * 2 * np.pi * 1e6 * np.arange(100_000) / 25e6).astype(np.complex64)
raw = np.empty(len(sig)*2, dtype=np.float32)
raw[0::2] = sig.real
raw[1::2] = sig.imag
raw.tofile("input.f32")
```

And read it back:

```python
import numpy as np
raw = np.fromfile("output.f32", dtype=np.float32)
rx = raw[0::2] + 1j * raw[1::2]
```

**MATLAB:**

```matlab
% write
sig = exp(1i*2*pi*1e6*(0:99999)/25e6);
fid = fopen('input.f32','wb');
fwrite(fid, [real(sig); imag(sig)], 'float32');   % interleaved
fclose(fid);

% read
fid = fopen('output.f32','rb');
raw = fread(fid, Inf, 'float32');
fclose(fid);
rx  = raw(1:2:end) + 1i*raw(2:2:end);
```

---

## 8. Common errors

| Error | Meaning | Fix |
|---|---|---|
| `auth_failed` | Wrong token | Re-copy the token from your email |
| `queue_full` | Too many tasks queued | Wait, retry later |
| `file_too_large` | Signal too large (>200 MB) | Send a smaller signal or multiple smaller bursts |
| `SLEEPING ZZZZ` | Server is currently off (maintenance) | Try later |
| `Duty cycle limit` | TX time quota used up | Server waits automatically |
| `Listen Before Talk failed` | Channel was busy 10× in a row | Try later, someone else may be very active |

---

## 9. Questions / bugs

GitHub Issues: [github.com/IIP-Group/usrp-playground/issues](https://github.com/IIP-Group/usrp-playground/issues)

Or contact the assistant directly (see your token email).

---

<p class="text-dim">Last updated: April 2026 · <a href="https://github.com/IIP-Group/usrp-playground">Source on GitHub</a></p>
