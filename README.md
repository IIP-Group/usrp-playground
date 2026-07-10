# USRP Sandbox System

A distributed system for sending complex baseband signals over a real USRP/UHD wireless channel. Built for university lab courses where students submit IQ samples and receive the channel-impaired result. Supports single-channel (SISO) and multi-channel (MIMO) transmission, per-test channel selection, and receive-only capture (listen).

```
                                        Server (Docker)                    Host
                                  ┌──────────────────────────┐   ┌───────────────┐
  Student                         │  ┌──────────┐            │   │  TX daemon    │
  ┌──────────┐    WebSocket       │  │ FastAPI  │            │   │  RX daemon    │
  │ Python   │◄──────────────────►│  │ :8000    │            │   │  (UHD/USRP)   │
  │ Client   │  f32 in/out        │  └────┬─────┘            │   └───────▲───────┘
  └──────────┘                    │       │                  │           │ ZMQ
                                  │  ┌────┴─────┐  ┌──────┐  │           │
                                  │  │ Postgres │  │Worker├──┼───────────┘
                                  │  └──────────┘  └──────┘  │
                                  └──────────────────────────┘
```

## Quick Start (Server)

```bash
git clone https://github.com/IIP-Group/usrp-playground.git
cd usrp-playground
cp .env.example .env    # adjust if needed
docker compose up -d --build
```

The server runs on `http://localhost:8000`. The TX/RX daemons run on the host next to the USRPs (`./start-daemons.sh`).

To start the daemons automatically on every boot (recommended; the Docker services already restart on their own via `restart: unless-stopped`):

```bash
sudo ./deploy/install-daemons-service.sh   # once per machine
sudo systemctl start usrp-daemons
```

This is self-contained and portable - on a new machine, clone the repo, run `./setup-daemons.sh`, then the installer. It installs two services: `usrp-daemons` (TX/RX daemons + inventory helper) and `usrp-daemon-agent` (a small always-on bridge so the admin Hardware page can show daemon status and start/stop/restart them from the browser).

## Client Installation

### Option A: pip (recommended)

```bash
pip install git+https://github.com/IIP-Group/usrp-playground.git
```

### Option B: Standalone binary

Download the latest release for your platform from the [Releases](https://github.com/IIP-Group/usrp-playground/releases) page.

## Usage

### CLI

```bash
usrp-client -i signal.f32 -o received.f32 -s localhost:8000 -t your-token

# send over hardware channel 1 (SISO)
usrp-client -i signal.f32 -c 1 -s localhost:8000 -t your-token

# receive only: capture 100000 samples without transmitting
usrp-client --listen 100000 -o capture.f32 -s localhost:8000 -t your-token

# receive only on all channels at once (MIMO)
usrp-client --listen 100000 --channels 2 -o capture.f32 -s localhost:8000 -t your-token
```

```
[upload] Sent signal.f32 (8000 bytes)
[queued] Task a1b2c3d4-... - 2 task(s) ahead in queue
[waiting] Queue position: 1 task(s) ahead
[running] Processing your signal...
[done] Task finished
[done] Processing complete, receiving file...
[result] Saved to received.f32 (8000 bytes)
```

### Python API

```python
from usrp_playground import USRPClient
import numpy as np

client = USRPClient.setup(host="localhost", port=8000, token="your-token")

# Check server
assert client.check()

# Send complex baseband signal, receive channel-impaired version
tx = np.array([0.5+0.3j, -0.2+0.8j, 0.7-0.1j], dtype=np.complex64)
rx = client.send(tx)

# SISO over a selectable hardware channel
rx = client.send(tx, channel=1)          # same as send_siso(tx, channel=1)

# MIMO: shape (n_samples, n_channels), column i drives channel i
tx2 = np.stack([tx, 2 * tx], axis=1)
rx2 = client.send_mimo(tx2)              # returns (n_rx, n_channels)

# Receive only - no transmission
rx  = client.listen(100_000)                   # channel 0
rx  = client.listen_siso(100_000, channel=1)   # specific channel
rx2 = client.listen_mimo(100_000)              # all channels, (n, channels)
```

See `demo/python/api_tour.ipynb` for a runnable tour of the whole API, and the hosted docs page (`/docs.html`) for the student guide.

### Creating a test signal

```bash
python3 -c "import struct; open('test.f32','wb').write(struct.pack('8f',0.1,-0.2,0.3,-0.4,0.5,-0.6,0.7,-0.8))"
usrp-client -i test.f32
```

## File Format

Raw interleaved float32 IQ samples. No header, no metadata.

```
[I₀ float32][Q₀ float32][I₁ float32][Q₁ float32] ...
```

In numpy:

```python
# Write
signal = np.array([0.5+0.3j, -0.2+0.8j], dtype=np.complex64)
signal.view(np.float32).tofile("signal.f32")

# Read
raw = np.fromfile("signal.f32", dtype=np.float32)
signal = raw[0::2] + 1j * raw[1::2]
```

## Architecture

| Service | Description |
|---|---|
| **db** | PostgreSQL - tokens, task queue, audit logs |
| **entrypoint** | FastAPI - WebSocket endpoint, auth, task creation |
| **worker** | Polls DB, drives the TX/RX daemons (real USRPs) over ZMQ |

### WebSocket Protocol (`ws://host:port/ws/run?auth_token=TOKEN`)

The client may send an optional JSON text frame before the binary payload:

```
{"mode": "siso", "channel": 1}             SISO over hardware channel 1
{"mode": "mimo", "channels": 2}            multi-channel upload (MIMO header required)
{"mode": "listen", "n_samples": 100000}    receive only - NO binary payload follows
{"mode": "listen", "n_samples": 100000, "channels": 2}    MIMO listen
```

Without a handshake, a plain binary frame is treated as a SISO upload on channel 0 (fully backward compatible).

```
Client                              Server
  │── [binary: f32 data] ──────────►│
  │◄── {"message":"queued",         │
  │      "uid":"...",               │
  │      "state":"PD",              │
  │      "queue_position":3}        │
  │◄── {"message":"status",         │
  │      "state":"PD",              │  every 2s
  │      "queue_position":1}        │
  │◄── {"message":"status",         │
  │      "state":"R",               │
  │      "queue_position":0}        │
  │◄── {"message":"status",         │
  │      "state":"D",               │
  │      "queue_position":0}        │
  │◄── {"message":"done"}           │
  │◄── [binary: f32 result]         │
```

Error responses: `{"error": "error_code", "message": "description"}`

### Task States

| State | Meaning |
|---|---|
| `PD` | Pending - waiting in queue |
| `R` | Running - being processed |
| `D` | Done - result ready |

## Configuration (.env)

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_AUTH_TOKEN` | `default-bench-token-2024` | Built-in auth token |
| `MAX_UPLOAD_MB` | `200` | Max file size per upload |
| `MAX_SAMPLES` | `2500000` | Max complex samples per signal (per channel) |
| `MAX_QUEUE` | `100` | Max concurrent queue slots (WebSocket connections) |
| `MAX_QUEUE_PER_IP` | `5` | Max queue slots per client IP |
| `TASK_TTL_HOURS` | `24` | Auto-delete tasks older than this |
| `MIMO_ENABLED` | `false` | Allow multi-channel (MIMO) uploads |
| `BEGIN_GUARD_MIN_SEC` / `..MAX..` | `0.1` | Range the pre-signal guard is drawn from |
| `END_GUARD_MIN_SEC` / `..MAX..` | `0.1` | Range the post-signal guard is drawn from |

Most of these can also be changed at runtime on the admin Settings page; the worker picks changes up live.

## RF Path

The worker drives two UHD daemons (TX and RX) on the host over ZMQ. Each test transmits the uploaded signal over the real USRP hardware while the RX side captures - including random guard intervals before and after the burst, a duty-cycle quota, and Listen-Before-Talk. Channel routing (antenna ports, gains) comes from the Hardware Inventory page.

### Round-trip latency

For an empty queue the per-task overhead budget is roughly: worker pickup (<=0.2 s, `WORKER_POLL_INTERVAL_SEC`) + LBT sense (~0.4 s, tunable via the LBT settings) + scheduling delay (`INITIAL_DELAY`, default 0.5 s) + guards + signal airtime + result detection (<=0.25 s adaptive status polling). Expect ~2 s plus airtime with defaults. To trim further: disable LBT for cable-only setups, lower `INITIAL_DELAY`, and shrink the guard ranges. Getting below ~1 s end-to-end would additionally need an event-driven task queue (e.g. Postgres LISTEN/NOTIFY) instead of polling - not implemented yet.

## Health Check

```bash
curl "http://localhost:8000/health?auth_token=your-token"
# {"status": "ok", "pending_tasks": 3, "ws_connections": 12}
```

## Releases

Standalone client binaries are built automatically via GitHub Actions for Linux, macOS, and Windows when a version tag is pushed:

```bash
git tag v0.1.0
git push origin v0.1.0
```
