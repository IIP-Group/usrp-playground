# USRP X410 Testbed Control Library

Python library for controlling Ettus USRP X410 software-defined radios over 100GigE. Provides a daemon-based architecture for multi-channel transmission and reception, plus standalone scripts for simpler use cases.

## Architecture

```
                   ZMQ (REQ/REP + PUB/SUB)
Client Script  ─────────────────────────────>  TX Daemon  ──── UHD ────  USRP X410 (TX)
               ─────────────────────────────>  RX Daemon  ──── UHD ────  USRP X410 (RX)
```

The system uses a client/server model:

- **Daemons** (`tx_daemon.py`, `rx_daemon.py`) run on machines physically connected to the USRPs. They are launched via `run_daemon.sh`, which handles root privileges, real-time scheduling, and conda environment activation. The daemons expose a JSON-based ZMQ command interface and handle all UHD interactions.
- **Client scripts** (`simple_transmission.py`, `separate_usrp_transmission.py`) are the preferred way to run experiments. They orchestrate the full TX+RX workflow by sending commands to the daemons. They can run on any machine with network access to the daemon hosts.
- **Standalone scripts** (`tx_sync_standalone.py`, `rx_sync_standalone.py`) bypass the daemon architecture and interact with UHD directly. Useful for quick single-machine tests, but they lack the real-time scheduling, remote operation, and incremental reconfiguration features of the daemon approach.

## A Note on Channel Roles (sync / intf)

This library was originally built for testing synchronization algorithms in the presence of interference. As a result, TX channels are categorized into two **roles**:

- **Sync channels**: carry the signal of interest (e.g., a synchronization sequence)
- **Intf channels** (optional): carry an interfering signal on separate antennas

For general-purpose use, you can ignore the interference role entirely and treat sync channels as your primary TX channels. The key constraint is that **all channels within a role transmit the same waveform** -- the library does not support per-channel arbitrary waveforms. If you only need to transmit one signal from one or more antennas, use sync channels only and omit the interference arguments.

## Files

| File | Description |
|------|-------------|
| `usrp_common.py` | Shared library: `BaseUSRPDaemon` class, input validation, USRP configuration helpers, settings mismatch detection, GPSDO lock management |
| `tx_daemon.py` | TX daemon server. Loads signals from H5 files, supports sync + interference channels, timed burst transmission, async event monitoring (underflow, seq errors) |
| `rx_daemon.py` | RX daemon server. Multi-channel timed reception to H5 files, stream flushing, heartbeat publishing |
| `run_daemon.sh` | Daemon launcher script. Handles `sudo` escalation, real-time scheduling, CPU pinning, and conda environment activation. **This is the main entry point for starting daemons.** Linux only |
| `simple_transmission.py` | Client script for single-USRP experiments (one TX daemon + one RX daemon) |
| `separate_usrp_transmission.py` | Client script for multi-USRP experiments (separate sync TX, interference TX, and RX daemons on different machines) |
| `tx_sync_standalone.py` | Standalone TX script. Generates and transmits Zadoff-Chu sequences directly via UHD. No daemon required |
| `rx_sync_standalone.py` | Standalone RX script. Records samples to H5 file directly via UHD. No daemon required |
| `generate_zc_signal.m` | MATLAB script to generate pulse-shaped Zadoff-Chu sequences and save as H5 files for use with the TX daemon |

## Dependencies

- **Python**: `uhd` (UHD Python API), `numpy`, `pyzmq`, `h5py`
- **Standalone TX only**: `commpy` (for Zadoff-Chu sequence generation)
- **Signal generation**: MATLAB with Communications Toolbox (for `generate_zc_signal.m`)
- **Hardware**: Ettus USRP X410 with 100GigE connection
- **OS**: Linux (for `run_daemon.sh` and real-time scheduling)

## Starting the Daemons

The daemons must be launched via `run_daemon.sh` on each machine connected to a USRP. The script:

1. **Escalates to root** via `sudo` -- required for DPDK support and real-time thread scheduling
2. **Pins the process to CPU cores 2-3** via `taskset -c "2-3"` -- isolates the daemon from OS interrupts and other processes, reducing jitter
3. **Sets FIFO real-time priority 80** via `chrt -f 80` -- ensures the streaming thread is not preempted, which is critical to avoid RX overflows and TX underflows at high sampling rates
4. **Activates the conda environment** (`toa-estimation-global`) -- ensures the correct Python and UHD versions are used
5. **Derives its path automatically** from the script location -- works regardless of where the repo is cloned

### Usage

```bash
./run_daemon.sh [rx|tx] [OPTIONS]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--usrp-addr <ip>` | `192.168.10.2` | USRP device IP address (the 100GigE data interface) |
| `--buffer-scale <float>` | `1.0` | Buffer size scaling factor (range: 0.5-8.0). Increase if you see overflows at high rates. Values above ~4.0 may hit the kernel buffer limit without DPDK |
| `--use-dpdk` | disabled | Enable DPDK for high-performance networking. Bypasses the kernel network stack for lower latency and higher throughput. Requires `--mgmt-addr` |
| `--mgmt-addr <ip>` | - | Management interface IP (required with `--use-dpdk`). This is a separate network interface used for UHD control traffic when the data interface is handled by DPDK |

### Examples

```bash
# Basic RX daemon:
./run_daemon.sh rx --usrp-addr 192.168.10.2

# TX daemon with larger buffers (useful at high sampling rates):
./run_daemon.sh tx --usrp-addr 192.168.10.2 --buffer-scale 4.0

# RX daemon with DPDK enabled (highest performance):
./run_daemon.sh rx --usrp-addr 192.168.10.2 --use-dpdk --mgmt-addr 192.168.100.2
```

### Notes

- The conda environment name (`toa-estimation-global`) is hardcoded in `run_daemon.sh` (line 156). You will need to either create a conda environment with the same name, or change the name in the script. The required packages are `uhd`, `numpy`, `pyzmq`, and `h5py`. If DPDK support is needed, UHD must be built from source with DPDK enabled -- the globally installed UHD does not include DPDK. Without DPDK, the global UHD installation is sufficient.
- Once started, daemons run indefinitely and accept commands via ZMQ. Stop them with `Ctrl+C`.

## Running Experiments with Client Scripts

The client scripts are the **preferred way to orchestrate experiments**. They handle the full workflow: connect to daemons, configure the USRPs, load signals, coordinate TX/RX timing, and save results.

### Workflow

1. **Start daemons** on all machines involved (see above)
2. **Prepare your TX signal** as an H5 file (see [H5 File Formats](#h5-file-formats))
3. **Run the client script** from any machine with network access to the daemon hosts

The client scripts handle timing automatically: RX starts recording first, then TX begins after a configurable delay (`--tx-rx-delay-diff`), ensuring the receiver captures the full transmission with margin on both sides.

### Single-USRP Experiments (`simple_transmission.py`)

Use this when TX and RX are on the same USRP or co-located USRPs controlled by one pair of daemons.

```bash
python simple_transmission.py \
    --tx-sync-channel 0 \
    --tx-sync-gains 30.0 \
    --rx-channels 0 1 2 3 \
    --rx-gain 20.0 \
    --sampling-rate 62.5e6 \
    --carrier-frequency 5.8e9 \
    --sync-signal-file signals/sync_signal.h5 \
    --output-file recording.h5 \
    --tx-address 192.168.1.10 \
    --rx-address 192.168.1.20
```

**Multi-channel TX** (same signal from multiple antennas):

```bash
python simple_transmission.py \
    --tx-sync-channel 0 1 \
    --tx-sync-gains 30.0 25.0 \
    --rx-channels 0 1 2 3 \
    ...
```

**With interference** (sync + interference on the same USRP):

```bash
python simple_transmission.py \
    --tx-sync-channel 0 \
    --tx-sync-gains 30.0 \
    --tx-intf-channel 1 2 \
    --tx-intf-gains 15.0 15.0 \
    --sync-signal-file signals/sync_signal.h5 \
    --intf-signal-file signals/intf_signal.h5 \
    ...
```

### Multi-USRP Experiments (`separate_usrp_transmission.py`)

Use this when sync TX, interference TX, and RX are on **separate USRP devices** controlled by separate daemons on different machines.

```bash
python separate_usrp_transmission.py \
    --tx-sync-channel 0 \
    --tx-sync-gains 30.0 \
    --tx-intf-channel 0 \
    --tx-intf-gains 15.0 \
    --rx-channels 0 1 2 3 \
    --rx-gain 20.0 \
    --sampling-rate 62.5e6 \
    --carrier-frequency 5.8e9 \
    --sync-signal-file signals/sync_signal.h5 \
    --intf-signal-file signals/intf_signal.h5 \
    --output-file recording.h5 \
    --tx-sync-address 129.132.24.220 \
    --tx-intf-address 129.132.24.198 \
    --rx-address 129.132.24.214
```

When interference is present, the timing is staggered: interference TX starts first, then RX, then sync TX -- ensuring the interference is already active when the receiver starts capturing.

**Without interference** (just sync TX + RX on separate machines):

```bash
python separate_usrp_transmission.py \
    --tx-sync-channel 0 \
    --tx-sync-gains 30.0 \
    --rx-channels 0 1 2 3 \
    --rx-gain 20.0 \
    --sampling-rate 62.5e6 \
    --carrier-frequency 5.8e9 \
    --sync-signal-file signals/sync_signal.h5 \
    --output-file recording.h5 \
    --tx-sync-address 129.132.24.220 \
    --rx-address 129.132.24.214
```

### Key Client Parameters

| Parameter | Description |
|-----------|-------------|
| `--tx-sync-channel` | TX channel(s) for the primary signal. Accepts multiple values |
| `--tx-sync-gains` | Per-channel TX gain in dB (one value per sync channel) |
| `--tx-intf-channel` | TX channel(s) for the interference signal (optional) |
| `--tx-intf-gains` | Per-channel interference TX gain in dB |
| `--rx-channels` | RX channels to record from |
| `--rx-gain` | RX gain in dB (shared across all RX channels) |
| `--sampling-rate` / `--tx-sampling-rate` / `--rx-sampling-rate` | Sampling rate. Use `--sampling-rate` for both, or specify TX/RX independently |
| `--carrier-frequency` | Carrier frequency in Hz |
| `--sync-signal-file` | H5 file with the primary TX waveform |
| `--intf-signal-file` | H5 file with the interference waveform (optional) |
| `--output-file` | Path for the recorded RX signal (.h5) |
| `--tx-rx-delay-diff` | Delay between RX start and TX start in seconds (default: 0.1s) |

## Standalone Scripts (Quick Tests)

The standalone scripts talk directly to UHD without daemons. They are useful for quick verification on the machine connected to the USRP, but they lack real-time scheduling, remote operation, and incremental reconfiguration.

```bash
# TX (generates and transmits Zadoff-Chu sequences directly):
python tx_sync_standalone.py \
    --sync-channel 0 \
    --intf-channel 1 \
    --sampling-rate 62.5e6 \
    --carrier-frequency 5.8e9 \
    --sync-gain 30.0 \
    --intf-gain 0.0 \
    --sequence-repetitions 1000

# RX (records to file):
python rx_sync_standalone.py \
    --number-samples 1000000 \
    --sampling-rate 62.5e6 \
    --carrier-frequency 5.8e9 \
    --gain 20.0 \
    --channels 0 1 2 3 \
    --output-file recording.h5
```

## Daemon Command Reference

### TX Daemon (default port 5557)

| Command | Required Parameters | Optional | Description |
|---------|-------------------|----------|-------------|
| `PING` | - | - | Connectivity test |
| `GET_TIME` | - | - | Returns USRP device time |
| `CONFIGURE_USRP` | `fs`, `fc`, `G_TX` (dict: channel->gain) | `sync_channels`, `intf_channels`, `antenna` | Configure radio parameters. `G_TX` must include an entry for every channel in sync + intf |
| `LOAD_SIGNAL` | at least one of `sync_signal_path`, `intf_signal_path` | - | Load waveform(s) from H5 files. The same waveform is replicated across all channels of that role |
| `TRANSMIT_BURST` | `delay` | `timeout` | Transmit loaded signal after delay (seconds relative to current USRP time) |
| `CHANGE_REFERENCE` | `source` ("internal" or "gpsdo") | `timeout`, `poll_period`, `mboard` | Switch clock/time reference |

### RX Daemon (default port 5555)

| Command | Required Parameters | Optional | Description |
|---------|-------------------|----------|-------------|
| `PING` | - | - | Connectivity test |
| `GET_TIME` | - | - | Returns USRP device time |
| `CONFIGURE_USRP` | `fs`, `fc`, `channels`, `G_RX` | `antenna` | Configure radio parameters. `G_RX` is a single value applied to all channels |
| `FLUSH_RX` | - | - | Drain stale samples from RX buffer |
| `RECEIVE_TO_FILE` | `n_samples`, `delay`, `path` | `timeout` | Record samples to H5 file. Delay is relative to current USRP time |
| `CHANGE_REFERENCE` | `source` ("internal" or "gpsdo") | `timeout`, `poll_period`, `mboard` | Switch clock/time reference |

## H5 File Formats

### TX Signal (input)

The TX daemon expects H5 files with a `/tx_signal` dataset containing a 1D complex baseband waveform. Two formats are supported:

- **MATLAB interleaved**: shape `[2, N]` or `[N, 2]` with real and imaginary parts in separate rows/columns. Must have attribute `complex_format = "interleaved_real_imag"`. This is what `generate_zc_signal.m` produces.
- **Python native**: shape `[N]` with `complex64` dtype. Write directly with `h5py`.

Any waveform can be used -- the library transmits whatever is in the file. The `generate_zc_signal.m` script is provided as an example for generating pulse-shaped Zadoff-Chu sequences, but you can substitute any signal.

### RX Signal (output)

The RX daemon writes H5 files with:
- `/rx_signal`: shape `[n_channels, n_samples]`, dtype `complex64`
- Attributes: `fs`, `fc`, `gain_dB`, `channels`, `n_channels`, `otw`, `cpu`

## Limitations

- **No per-channel waveforms.** All sync channels transmit the same waveform; all interference channels transmit the same waveform. To transmit different signals per antenna, you would need to extend `_assemble_tx_signal` in `tx_daemon.py`.
- **Shared RX gain.** A single `G_RX` value is applied to all RX channels. Per-channel RX gains are not supported.
- **Shared carrier frequency and antenna type.** All TX channels use the same `fc` and antenna; same for RX.
- **X410 specific.** Buffer sizes, frame sizes (8958 bytes for 100GigE), and antenna names ("TX/RX0", "RX1") are configured for the Ettus X410. Other USRP models may require adjustments in `usrp_common.py`.
- **Linux only for daemons.** The `run_daemon.sh` launcher uses `taskset`, `chrt`, and `getent`, which are Linux utilities. The Python scripts themselves are cross-platform, but optimal streaming performance requires real-time scheduling.

## Network Configuration

The daemons use ZMQ sockets on the following default ports:

| Service | REP (commands) | PUB (events/heartbeat) |
|---------|---------------|------------------------|
| RX Daemon | 5555 | 5556 |
| TX Daemon | 5557 | 5558 |

The TX daemon publishes async events (underflow, seq_error, burst_ack) on its PUB socket. The RX daemon publishes periodic heartbeats. Client scripts subscribe to the TX PUB socket to monitor transmission health in real time.

Ports are configurable via CLI arguments on the client scripts (`--tx-rep-port`, `--tx-pub-port`, `--rx-rep-port`, `--rx-pub-port`).
