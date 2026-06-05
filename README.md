# USRP Sandbox System

A distributed system for sending complex baseband signals through a simulated (later real) USRP/UHD wireless channel. Built for university lab courses where students submit IQ samples and receive the channel-impaired result.

```
                                        Server (Docker)
                                  ┌──────────────────────────┐
                                  │                          │
  Student                         │  ┌──────────┐           │
  ┌──────────┐    WebSocket       │  │ FastAPI  │           │
  │ Python   │◄──────────────────►│  │ :8000    │           │
  │ Client   │  f32 in/out        │  └────┬─────┘           │
  └──────────┘                    │       │                  │
                                  │  ┌────┴─────┐  ┌──────┐ │
                                  │  │ Postgres │  │Worker│ │
                                  │  │          │◄─┤AWGN  │ │
                                  │  └──────────┘  └──────┘ │
                                  └──────────────────────────┘
```

## Quick Start (Server)

```bash
git clone https://github.com/YOURUSER/USRP-Benchmark-System.git
cd USRP-Benchmark-System
cp .env.example .env    # adjust if needed
docker compose up -d --build
```

The server runs on `http://localhost:8000`.

## Client Installation

### Option A: pip (recommended)

```bash
pip install git+https://github.com/YOURUSER/USRP-Benchmark-System.git
```

### Option B: Standalone binary

Download the latest release for your platform from the [Releases](https://github.com/YOURUSER/USRP-Benchmark-System/releases) page.

## Usage

### CLI

```bash
usrp-client -i signal.f32 -o received.f32 -s localhost:8000 -t your-token
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
from usrp_benchmark import USRPClient
import numpy as np

USRPClient.setup(host="localhost", port=8000, token="your-token")

# Check server
assert USRPClient.check()

# Send complex baseband signal, receive channel-impaired version
tx = np.array([0.5+0.3j, -0.2+0.8j, 0.7-0.1j], dtype=np.complex64)
rx = USRPClient.send(tx)
```

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
| **worker** | Polls DB, processes signals through channel simulation |

### WebSocket Protocol (`ws://host:port/ws/run?auth_token=TOKEN`)

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
| `CHANNEL_SNR_DB` | `20` | AWGN channel SNR in dB |
| `MAX_UPLOAD_MB` | `200` | Max file size per upload |
| `MAX_SAMPLES` | `2500000` | Max complex samples per signal |
| `MAX_WS_CONNECTIONS` | `100` | Max concurrent WebSocket connections |
| `MAX_PENDING_TASKS` | `200` | Max tasks in queue |
| `TASK_TTL_HOURS` | `24` | Auto-delete tasks older than this |

## Channel Model

Currently a simple AWGN (Additive White Gaussian Noise) simulation:

```
y[n] = x[n] + noise[n]
```

The noise power is derived from the signal power and the configured SNR. This will later be replaced with real USRP/UHD full-duplex TX/RX over two antennas at 20 MHz sample rate.

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
