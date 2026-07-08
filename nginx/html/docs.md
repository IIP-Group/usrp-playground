# USRP Sandbox - Student Guide

Welcome! This page has everything you need to test your waveforms over the shared USRP.

---

## 1. What is this?

A shared server that transmits your baseband signal over a real USRP and returns what it received. You work locally (Python, MATLAB, ...); the server takes care of TX/RX.

The exact radio parameters (sample rate, carrier frequency, number of channels, limits) can change - always read them at runtime via `USRPClient.info()` instead of hardcoding them.

---

## 2. Installation

```bash
pip install git+https://github.com/IIP-Group/usrp-playground.git
```

This installs the Python package `usrp_benchmark` with the `USRPClient` class plus the `usrp-client` CLI tool. Requires Python 3.9 or newer.

Update later with:

```bash
pip install --upgrade git+https://github.com/IIP-Group/usrp-playground.git
```

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

# Build a test signal: a tone at fs/10 next to the carrier
fs = info['sample_rate_hz']
t  = np.arange(100_000) / fs
tx = (0.8 * np.exp(1j * 2 * np.pi * (fs/10) * t)).astype(np.complex64)

# Transmit and receive
rx = USRPClient.send(tx, verbose=True)
print(f"Received: {len(rx)} samples")
```

Tip: avoid a constant signal (`np.ones`) - at baseband it sits at 0 Hz, right on top of the receiver's DC offset and LO leakage. A tone with some frequency offset is much easier to find in the result.

---

## 5. The most important methods

| Call | Returns |
|---|---|
| `USRPClient.setup(host, port, token)` | - call once per session |
| `USRPClient.check()` | bool - server reachable? |
| `USRPClient.info()` | dict - sample rate, carrier, channels, limits |
| `USRPClient.send(signal)` | numpy array - received signal (SISO for 1-D input, MIMO for 2-D) |
| `USRPClient.send(signal, channel=1)` | like above, but over hardware channel 1 (1-D only) |
| `USRPClient.send_siso(signal, channel=0)` | 1-D in, 1-D out - explicit single-channel send |
| `USRPClient.send_mimo(signal)` | 2-D in, 2-D out - all channels at once |
| `USRPClient.listen(n)` | 1-D array - receive only, no transmission |
| `USRPClient.listen_siso(n, channel=0)` | 1-D array - receive only on a selectable channel |
| `USRPClient.listen_mimo(n)` | 2-D array `(n, channels)` - receive only, all channels |

Add `verbose=True` to any of them for live status output (queue position, progress).

`signal` must be a **complex numpy array** (IQ samples). It is converted to `complex64` internally. Keep the amplitude at or below 1.0.

---

## 6. Channel selection (SISO)

The testbed has more than one physical channel (different antenna ports / cabling - see the `channels` list in `info()`). By default everything runs over channel 0. To test over another channel:

```python
rx = USRPClient.send(tx, channel=1)            # or:
rx = USRPClient.send_siso(tx, channel=1)
```

`channel` is the index into the server's channel list. An invalid index is rejected with a clear error. Channel selection only applies to 1-D (SISO) signals - in MIMO, column i always drives channel i.

---

## 7. MIMO: several channels at once

Pass a 2-D array of shape `(n_samples, n_channels)`; column i is transmitted on channel i. The result has the same shape - row-aligned in time across channels:

```python
n = 100_000
t = np.arange(n) / fs
tone_a = 0.8 * np.exp(2j * np.pi * (+fs/10) * t)
tone_b = 0.8 * np.exp(2j * np.pi * (-fs/8)  * t)

tx = np.stack([tone_a, tone_b], axis=1).astype(np.complex64)   # (n, 2)
rx = USRPClient.send(tx)                                       # (n_rx, 2)
```

MIMO must be enabled on the server (`info()['mimo_enabled']`); the maximum number of channels is `info()['mimo_max_channels']`.

---

## 8. Listen: receive without transmitting

Capture raw samples from the receiver without sending anything - useful to inspect the band (there is plenty of WiFi around 2.4 GHz!), measure the noise floor, or record someone else's transmission in a lab exercise:

```python
rx  = USRPClient.listen(200_000)                  # channel 0
rx  = USRPClient.listen_siso(200_000, channel=1)  # a specific channel
rx2 = USRPClient.listen_mimo(200_000)             # all channels, shape (n, channels)
```

You get exactly `n` samples back. No guards apply - the capture starts as soon as your task runs.

---

## 9. What happens to your signal

1. **Upload** - your signal is sent to the server as binary
2. **Queue** - if other tasks are ahead of you, you wait
3. **Duty cycle check** - the server makes sure we stay below the TX time quota
4. **Listen Before Talk** - short RX check: is the channel free? If not, back off and retry
5. **TX + RX simultaneously** - the signal is transmitted while RX captures
6. **Download** - the received signal comes back as binary

The result contains **guard regions** (just noise) before and after your burst. Their lengths are drawn **randomly** per test from the ranges configured on the server (see `begin_guard_min_sec` / `begin_guard_max_sec` etc. in `info()`). So do not assume your signal starts at a fixed sample - detecting where it starts (synchronization) is part of the exercise. A robust approach: find your tone in the spectrum first, filter narrowband around it, then threshold the envelope.

---

## 10. CLI tool (no Python required)

If your signal is generated in another language (MATLAB, C, GNU Radio) or you just want to push a file through quickly, after `pip install` the `usrp-client` command is available in your terminal.

**File format:** `.f32` - interleaved float32, in the order `real, imag, real, imag, ...`. This is the same format used by GNU Radio and most SDR tools. (Multi-channel files carry a small 16-byte header; the Python API handles that for you.)

**Usage:**

```bash
# send a file (as before)
usrp-client -i input.f32 -o output.f32 -s 129.132.24.210:80 -t YOUR-TOKEN-HERE

# send over hardware channel 1
usrp-client -i input.f32 -c 1 -s 129.132.24.210:80 -t YOUR-TOKEN-HERE

# receive only: capture 100000 samples, no transmission
usrp-client --listen 100000 -o capture.f32 -s 129.132.24.210:80 -t YOUR-TOKEN-HERE

# receive only on all channels at once (MIMO)
usrp-client --listen 100000 --channels 2 -o capture.f32 -s 129.132.24.210:80 -t YOUR-TOKEN-HERE
```

**Arguments:**

| Flag | Meaning | Default |
|---|---|---|
| `-i`, `--input` | Path to input file (`.f32`, interleaved IQ) | required unless `--listen` |
| `-o`, `--output` | Path for the received file | `output.f32` |
| `-s`, `--server` | Server address `host:port` | `localhost:8000` |
| `-t`, `--token` | Auth token from your email | default-bench-token |
| `-c`, `--channel` | Hardware channel for SISO send/listen | `0` |
| `-l`, `--listen` | Receive only: capture N samples, no input file | off |
| `--channels` | With `--listen`: capture this many channels (MIMO) | `1` |

While running you get status updates on stdout: `[upload]`, `[queued]`, `[waiting]`, `[running]`, `[done]`, `[result]`.

**Example: produce f32 from numpy** (if you have a signal in Python and want to send it via the CLI):

```python
import numpy as np
fs = 1e6                      # use the value from USRPClient.info()
sig = np.exp(1j * 2 * np.pi * (fs/10) * np.arange(100_000) / fs).astype(np.complex64)
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
fs  = 1e6;                                 % use the value from /info
sig = exp(1i*2*pi*(fs/10)*(0:99999)/fs);
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

## 11. Common errors

| Error | Meaning | Fix |
|---|---|---|
| `auth_failed` | Wrong token | Re-copy the token from your email |
| `queue_full` / "Server busy" | Too many tasks queued | Wait, retry later |
| `file_too_large` | Upload larger than the server limit | Send a smaller signal or multiple smaller bursts |
| `too_many_samples` | More samples per channel than `info()['max_samples']` | Shorten the signal |
| `bad_payload` | Malformed upload (empty signal, broken MIMO header, ...) | Check how you build the array/file |
| `bad_handshake` | Invalid request (unknown channel index, bad n_samples, ...) | Check channel index against `info()['channels']` |
| `mimo_disabled` | 2-D signal sent while MIMO is off on the server | Ask the assistant, or send SISO |
| `Unknown mode 'listen'` | Server runs an older version without listen | Ask the assistant to update the server |
| `SLEEPING ZZZZ` | Server is currently off (maintenance) | Try later |
| `Signal too long for the duty-cycle limit` | One burst is longer than the whole TX time quota | Use a shorter signal |
| `Duty cycle limit` | TX time quota temporarily used up | Server waits automatically |
| `Listen Before Talk failed` | Channel was busy 10x in a row | Try later, someone else may be very active |

---

## 12. Questions / bugs

GitHub Issues: [github.com/IIP-Group/usrp-playground/issues](https://github.com/IIP-Group/usrp-playground/issues)

---

<p class="text-dim">Last updated: July 2026 · <a href="https://github.com/IIP-Group/usrp-playground">Source on GitHub</a></p>
