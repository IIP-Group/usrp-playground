import uhd
import numpy as np
from uhd.types import TimeSpec, TXMetadata, TXAsyncMetadata, TXMetadataEventCode as E
import zmq
import threading
import time
import logging
import h5py
import os
import argparse
import socket
try:
    from .usrp_common import (
        BaseUSRPDaemon, positive_float, buffer_scale_float, not_negative_float, int_list,
        validate_gain_dict, valid_ip, validate_sampling_rate, validate_carrier_frequency, validate_gain, validate_antenna,
        check_settings_mismatch, DEFAULT_TX_REP_ADDR, DEFAULT_TX_PUB_ADDR, DEFAULT_USRP_IP_ADDR,
        DEFAULT_TX_TIMEOUT, DEFAULT_LOCK_TIMEOUT, DEFAULT_POLL_PERIOD, DEFAULT_ASYNC_POLLING_RATE,
        FS_ATOL, FC_ATOL, G_ATOL
    )
except ImportError:
    from usrp_common import (
        BaseUSRPDaemon, positive_float, buffer_scale_float, not_negative_float, int_list,
        validate_gain_dict, valid_ip, validate_sampling_rate, validate_carrier_frequency, validate_gain, validate_antenna,
        check_settings_mismatch, DEFAULT_TX_REP_ADDR, DEFAULT_TX_PUB_ADDR, DEFAULT_USRP_IP_ADDR,
        DEFAULT_TX_TIMEOUT, DEFAULT_LOCK_TIMEOUT, DEFAULT_POLL_PERIOD, DEFAULT_ASYNC_POLLING_RATE,
        FS_ATOL, FC_ATOL, G_ATOL
    )

REP_ADDR = DEFAULT_TX_REP_ADDR
PUB_ADDR = DEFAULT_TX_PUB_ADDR
USRP_IP_ADDR = DEFAULT_USRP_IP_ADDR
POLLING_RATE = DEFAULT_ASYNC_POLLING_RATE

# RFNoC Configuration
RFNOC_CLEANUP_DELAY = 0.25  # Delay in seconds for RFNoC graph cleanup during channel reconfiguration

def get_primary_ip(remote_host="8.8.8.8", remote_port=80):
    """
    Returns the primary IP address of the machine, i.e.
    the one used to reach `remote_host` (default: Google DNS).
    No packets are actually sent.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_host, remote_port))
        return s.getsockname()[0]
    finally:
        s.close()


class TXDaemon(BaseUSRPDaemon):
    def __init__(self, usrp_addr, mgmt_addr=None, mcr=None,
                 device_type="x4xx", use_dpdk=False, buffer_scale=1.0, daemon_id=None):
        super().__init__(usrp_addr, mgmt_addr, mcr, device_type, use_dpdk, buffer_scale)
        self.daemon_id = daemon_id or f"TX-{get_primary_ip()}"

        self.sync_channels = None
        self.intf_channels = None
        self.channels = None

        # Configuration state for incremental updates
        self.current_config = {
            'fs': None,
            'fc': None,
            'channels': None,
            'channel_gains': None,
            'antenna': None
        }
        # Per-channel configuration state
        self.channel_configs = {}

        # Signal file paths and loaded data
        self.sync_signal_path = None
        self.intf_signal_path = None
        self.sync_signal_data = None
        self.intf_signal_data = None

        # Final assembled signal matrix
        self.tx_signal = None
        self.signal_samples = 0
        self.tx_streamer = None

        self._publisher = self._context.socket(zmq.PUB)
        self._publisher.bind(PUB_ADDR)
        self._async_thread = threading.Thread(target=self._async_loop, daemon=True)
        self._async_thread.start()

        # Read current hardware state to initialize our tracking
        self._read_hardware_state()

    def _read_hardware_state(self):
        """Read current hardware configuration to initialize tracking state."""
        try:
            n_channels = self.usrp.get_tx_num_channels()

            # Read global sampling rate (use channel 0 as reference)
            if n_channels > 0:
                current_fs = self.usrp.get_tx_rate(0)
                self.current_config['fs'] = current_fs

            # Read per-channel settings
            for ch in range(n_channels):
                try:
                    current_fc = self.usrp.get_tx_freq(ch)
                    current_gain = self.usrp.get_tx_gain(ch)
                    current_antenna = self.usrp.get_tx_antenna(ch)

                    # Store in per-channel config (role unknown at startup)
                    self.channel_configs[ch] = {
                        'fc': current_fc,
                        'gain': current_gain,
                        'antenna': current_antenna,
                        'role': None  # Will be set during first configure_usrp
                    }


                except Exception as e:
                    logging.warning(f"Could not read state for TX channel {ch}: {e}")

        except Exception as e:
            logging.warning(f"Could not read hardware state: {e}")
            # Continue with empty state - first configure will set everything

    def configure_usrp(self, fs, fc, sync_channels, intf_channels, channel_gains, antenna="TX/RX0", channel_powers=None):

        with self._op_guard("configure_usrp"):
            # `antenna` may be a single string (applied to every channel) or
            # a dict {ch: name} for per-channel antennas (B210 MIMO needs
            # TX/RX for ch 0 and TX/RX2 for ch 1). Normalise to a dict so
            # the rest of the body can index by channel uniformly.
            def _antenna_for(ch):
                if isinstance(antenna, dict):
                    return antenna.get(ch, antenna.get(str(ch)))
                return antenna

            # Validate that at least one type of channel is specified
            if not sync_channels and not intf_channels:
                raise ValueError("Must specify either sync_channels or intf_channels (or both)")

            n_channels = self.usrp.get_tx_num_channels()
            available_channels = list(range(n_channels))

            # Build requested channels list, filtering out None values
            requested_channels = []
            if sync_channels:
                requested_channels.extend(sync_channels)
            if intf_channels:
                requested_channels.extend(intf_channels)

            if any(ch not in available_channels for ch in requested_channels):
                raise ValueError(f"Invalid channels {requested_channels}. Available channels: {available_channels}")

            if intf_channels and sync_channels and any(ch in intf_channels for ch in sync_channels):
                raise ValueError(f"Synchronization channels {sync_channels} and interference channels {intf_channels} cannot overlap")

            # Check that gains are provided for all requested channels
            if channel_gains is None or any(ch not in channel_gains for ch in requested_channels):
                raise ValueError(f"Must provide gain settings for all requested channels: {requested_channels}")

            # Note: This validation prevents channel conflicts within the same USRP.
            # Different USRPs can use the same channel numbers without conflict.

            # Check what has changed to minimize USRP reconfigurations
            new_config = {
                'fs': fs,
                'fc': fc,
                'channels': sorted(requested_channels),
                'channel_gains': channel_gains,
                'antenna': antenna
            }

            # Determine what needs to be updated (use tolerances for float comparisons)
            fs_changed = self.current_config['fs'] is None or not np.isclose(self.current_config['fs'], new_config['fs'], atol=FS_ATOL, rtol=0)
            fc_changed = self.current_config['fc'] is None or not np.isclose(self.current_config['fc'], new_config['fc'], atol=FC_ATOL, rtol=0)
            channels_changed = self.current_config['channels'] != new_config['channels']
            old_gains = self.current_config['channel_gains']
            gains_changed = (old_gains is None or old_gains.keys() != new_config['channel_gains'].keys() or
                             any(not np.isclose(old_gains[ch], new_config['channel_gains'][ch], atol=G_ATOL, rtol=0) for ch in old_gains))
            antenna_changed = self.current_config['antenna'] != new_config['antenna']

            logging.info(f"Configuration changes: fs={fs_changed}, fc={fc_changed}, channels={channels_changed}, gains={gains_changed}, antenna={antenna_changed}")


            # Validate new configuration
            validate_sampling_rate(self.usrp, fs, is_tx=True)
            for ch in requested_channels:
                validate_carrier_frequency(self.usrp, fc, ch, is_tx=True)
                g = channel_gains.get(ch, None)
                validate_gain(self.usrp, g, ch, is_tx=True)
                validate_antenna(self.usrp, _antenna_for(ch), ch, is_tx=True)

            # Update channel assignments
            self.sync_channels = sync_channels
            self.intf_channels = intf_channels
            self.channels = requested_channels

            # Auto-rebuild signal matrix if signals are loaded and channel config changed
            if (self.sync_signal_data is not None or self.intf_signal_data is not None) and channels_changed:
                logging.info("Channel configuration changed - rebuilding transmission signal matrix")
                self._assemble_tx_signal()

            actual_settings = {}

            # Only update sampling rate if it changed
            if fs_changed:
                self.usrp.set_tx_rate(fs, requested_channels[0])
            actual_fs = self.usrp.get_tx_rate(requested_channels[0])

            # Update per-channel settings with per-channel change detection
            for ch in requested_channels:

                role = "synchronization" if sync_channels and ch in sync_channels else "interference"
                expected_gain = channel_gains.get(ch, None)

                # Get previous channel config
                prev_ch_config = self.channel_configs.get(ch, {})

                # Detect per-channel changes (use tolerances for float comparisons)
                prev_fc = prev_ch_config.get('fc')
                prev_gain = prev_ch_config.get('gain')
                ch_fc_changed = prev_fc is None or not np.isclose(prev_fc, fc, atol=FC_ATOL, rtol=0)
                ch_gain_changed = prev_gain is None or not np.isclose(prev_gain, expected_gain, atol=G_ATOL, rtol=0)
                ch_antenna_changed = prev_ch_config.get('antenna') != _antenna_for(ch)

                # Only set frequency if it changed for this channel
                if ch_fc_changed:
                    tune_req = uhd.types.TuneRequest(fc)
                    self.usrp.set_tx_freq(tune_req, ch)
                actual_fc = self.usrp.get_tx_freq(ch)

                # Power-reference mode: caller passed an absolute target dBm
                # for this channel. Use UHD's calibrated set_tx_power_reference
                # so we know what comes out of the SMA. Falls back to gain mode
                # if no power was supplied OR if the firmware/cal table does
                # not support the power API.
                expected_power_dbm = (channel_powers or {}).get(ch)
                actual_power_dbm = None
                used_power_api = False
                if expected_power_dbm is not None:
                    try:
                        if ch_gain_changed:
                            self.usrp.set_tx_power_reference(float(expected_power_dbm), ch)
                        actual_power_dbm = self.usrp.get_tx_power_reference(ch)
                        used_power_api = True
                    except Exception as e:
                        logging.warning(
                            f"set_tx_power_reference unavailable on ch {ch} "
                            f"({e}); falling back to set_tx_gain"
                        )
                if not used_power_api and ch_gain_changed:
                    self.usrp.set_tx_gain(expected_gain, ch)
                actual_gain = self.usrp.get_tx_gain(ch)

                # Only set antenna if it changed for this channel
                if ch_antenna_changed:
                    self.usrp.set_tx_antenna(_antenna_for(ch), ch)
                actual_antenna = self.usrp.get_tx_antenna(ch)


                # Update per-channel config state
                self.channel_configs[ch] = {
                    'fc': fc,
                    'gain': expected_gain,
                    'power_dbm': expected_power_dbm,
                    'antenna': _antenna_for(ch),
                    'role': role
                }

                actual_settings[ch] = {
                    "role": role,
                    "fs": actual_fs,
                    "fc": actual_fc,
                    "G_TX": actual_gain,
                    "P_TX_DBM": actual_power_dbm,
                    "antenna": actual_antenna
                }

            # Only recreate streamer if channels changed (most critical for RFNoC)
            if channels_changed or self.tx_streamer is None:
                try:
                    if self.tx_streamer is not None:
                        # Force cleanup before reconfiguration
                        del self.tx_streamer
                        self.tx_streamer = None

                        # Give RFNoC time to clean up graph connections
                        time.sleep(RFNOC_CLEANUP_DELAY)

                    st = uhd.usrp.StreamArgs("fc32", "sc16")
                    st.channels = requested_channels
                    self.tx_streamer = self.usrp.get_tx_stream(st)

                    logging.info(f"Successfully reconfigured TX streamer for channels {requested_channels}")

                except Exception as e:
                    logging.error(f"Failed to reconfigure channels {requested_channels}: {e}")
                    logging.error("Channel reconfiguration failed due to RFNoC graph conflicts - this is a known limitation")
                    raise RuntimeError(f"Channel reconfiguration failed: {e}. Please restart the daemon to clear RFNoC graph state.")

            # Update current configuration state
            self.current_config = new_config.copy()

            return actual_settings
    
    

        
        
    def load_signal(self, sync_signal_path=None, intf_signal_path=None):
        """Load signal data from H5 files and prepare for transmission."""

        with self._op_guard("load_signal"):

            if not self.sync_channels and not self.intf_channels:
                raise RuntimeError("USRP not configured. Please call configure_usrp() before loading signals.")

            # Validate that at least one signal is provided
            if sync_signal_path is None and intf_signal_path is None:
                raise ValueError("Must provide either sync_signal_path or intf_signal_path (or both)")

            # Clear any previous signal data
            self.sync_signal_path = None
            self.sync_signal_data = None
            self.intf_signal_path = None
            self.intf_signal_data = None
            self.tx_signal = None
            self.signal_samples = 0

            # Load synchronization signal (optional)
            if sync_signal_path is not None:
                if not self.sync_channels:
                    raise ValueError("Cannot load sync signal without sync_channels configured")
                self.sync_signal_path = sync_signal_path
                self.sync_signal_data = self._load_signal_file(sync_signal_path, "synchronization")
            else:
                self.sync_signal_path = None
                self.sync_signal_data = None

            # Load interference signal (optional)
            if intf_signal_path is not None:
                if not self.intf_channels:
                    raise ValueError("Cannot load interference signal without intf_channels configured")
                self.intf_signal_path = intf_signal_path
                self.intf_signal_data = self._load_signal_file(intf_signal_path, "interference")
            else:
                self.intf_signal_path = None
                self.intf_signal_data = None

            # Assemble final transmission signal matrix
            self._assemble_tx_signal()

            return {
                "loaded_sync_samples": len(self.sync_signal_data) if self.sync_signal_data is not None else 0,
                "loaded_intf_samples": len(self.intf_signal_data) if self.intf_signal_data is not None else 0,
                "total_samples": self.signal_samples,
                "channels_configured": len(self.channels),
                "sync_channels": self.sync_channels if self.sync_channels else [],
                "intf_channels": self.intf_channels if self.intf_channels else []
            }

    def _load_signal_file(self, file_path, signal_type):
        """Load and validate a signal file."""
        # Check if file exists
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Signal file not found: {file_path}")

        # Check file extension
        if not file_path.endswith('.h5'):
            raise ValueError(f"Signal file must be .h5 format: {file_path}")

        try:
            with h5py.File(file_path, 'r') as f:
                if 'tx_signal' not in f:
                    raise ValueError(f"Signal file missing 'tx_signal' dataset: {file_path}")

                signal_data = f['tx_signal'][:]

                # Multichannel mode: dataset is (n_channels, n_samples). Each
                # row is one channel's signal. _assemble_tx_signal will place
                # row i on sync_channels[i] / intf_channels[i] (in order).
                multichannel = bool(f['/tx_signal'].attrs.get('multichannel', False))
                if multichannel:
                    if signal_data.ndim != 2:
                        raise ValueError(
                            f"multichannel=True but signal is {signal_data.ndim}D, "
                            f"expected (n_channels, n_samples)"
                        )
                    if signal_data.dtype != np.complex64:
                        signal_data = signal_data.astype(np.complex64)
                    if signal_data.size == 0:
                        raise ValueError(f"Empty {signal_type} signal in file: {file_path}")
                    if np.all(signal_data == 0):
                        raise ValueError(f"All-zero {signal_type} signal in file: {file_path}")
                    return signal_data  # 2D

                # Check if data is in interleaved real/imag format (MATLAB compatibility)
                if 'complex_format' in f['/tx_signal'].attrs:
                    complex_format = f['/tx_signal'].attrs['complex_format']
                    if isinstance(complex_format, bytes):
                        complex_format = complex_format.decode('utf-8')

                    if complex_format == 'interleaved_real_imag':
                        # Convert from interleaved format to complex
                        # Handle both [2, N] and [N, 2] orientations
                        if signal_data.shape[0] == 2:
                            # Format: [2, N] - [real_part; imag_part]
                            real_part = signal_data[0, :]
                            imag_part = signal_data[1, :]
                            signal_data = real_part + 1j * imag_part
                        elif signal_data.shape[1] == 2:
                            # Format: [N, 2] - [real_part, imag_part] (MATLAB transpose)
                            real_part = signal_data[:, 0]
                            imag_part = signal_data[:, 1]
                            signal_data = real_part + 1j * imag_part
                        else:
                            raise ValueError(f"Invalid interleaved format shape: {signal_data.shape}. Expected [2,N] or [N,2].")
                else:
                    # No interleaved format - validate dimensions before conversion
                    if signal_data.ndim == 2:
                        rows, cols = signal_data.shape
                        if rows != 1 and cols != 1:
                            raise ValueError(f"Signal must be 1D vector or 2D vector (Nx1 or 1xN). Got shape {signal_data.shape} in file: {file_path}")

                # Ensure it's complex64
                if signal_data.dtype != np.complex64:
                    signal_data = signal_data.astype(np.complex64)

                # Enforce 1D vector (flatten if needed) - only for data that wasn't interleaved
                if signal_data.ndim == 2:
                    signal_data = signal_data.flatten()
                elif signal_data.ndim != 1:
                    raise ValueError(f"Signal must be 1D or 2D vector. Got {signal_data.ndim}D array in file: {file_path}")

                # Check for empty or zero signals
                if signal_data.size == 0:
                    raise ValueError(f"Empty {signal_type} signal in file: {file_path}")

                if np.all(signal_data == 0):
                    raise ValueError(f"All-zero {signal_type} signal in file: {file_path}")

                return signal_data

        except Exception as e:
            if isinstance(e, (FileNotFoundError, ValueError)):
                raise
            else:
                raise ValueError(f"Error reading {signal_type} signal file {file_path}: {str(e)}")

    def _assemble_tx_signal(self):
        """Assemble the final multi-channel transmission signal matrix.

        Each of `sync_signal_data` / `intf_signal_data` may be either:
          * 1-D — the same signal is replicated onto every channel in the
            corresponding list (legacy / SISO behaviour).
          * 2-D (n_channels, n_samples) — row i goes to the i-th channel
            of the list, enabling true per-channel MIMO transmission.
        """
        def _len(arr):
            if arr is None:
                return 0
            return arr.shape[-1] if arr.ndim >= 1 else 0

        sync_length = _len(self.sync_signal_data)
        intf_length = _len(self.intf_signal_data)

        self.signal_samples = max(sync_length, intf_length)
        if self.signal_samples == 0:
            raise RuntimeError("No signal data loaded for transmission")

        all_channels = []
        if self.sync_channels:
            all_channels.extend(self.sync_channels)
        if self.intf_channels:
            all_channels.extend(self.intf_channels)
        if not all_channels:
            raise RuntimeError("No channels configured for signal assembly")

        max_channel = max(all_channels)
        self.tx_signal = np.zeros((max_channel + 1, self.signal_samples), dtype=np.complex64)

        def _place(signal_data, channel_list, length):
            if signal_data is None or not channel_list:
                return
            if signal_data.ndim == 2:
                n_rows = signal_data.shape[0]
                if n_rows != len(channel_list):
                    raise ValueError(
                        f"multichannel signal has {n_rows} rows but channel list "
                        f"has {len(channel_list)} entries"
                    )
                for i, ch in enumerate(channel_list):
                    self.tx_signal[ch, :length] = signal_data[i]
            else:
                for ch in channel_list:
                    self.tx_signal[ch, :length] = signal_data

        _place(self.sync_signal_data, self.sync_channels, sync_length)
        _place(self.intf_signal_data, self.intf_channels, intf_length)
    
    
    def get_device_time(self):
        return self.usrp.get_time_now().get_real_secs()
        
    
    def transmit_with_delay(self, delay, timeout=None):

        with self._op_guard("transmit_with_delay"):

            if self.tx_streamer is None or self.tx_signal is None:
                raise RuntimeError("USRP or transmission signal not configured. Please call configure_usrp() and load_signal() before starting transmission.")


            chunk_size = self.tx_streamer.get_max_num_samps()
            tx_signal_channels, n_requested_samples = self.tx_signal.shape

            # Verify channel indices are valid
            max_channel = max(self.channels)
            if max_channel >= tx_signal_channels:
                raise RuntimeError(f"TX signal has {tx_signal_channels} channels but channel {max_channel} is requested")

            # Use default timeout for individual send operations if not provided
            if timeout is None:
                timeout = DEFAULT_TX_TIMEOUT

            samples_transmitted = 0

            # Schedule transmission start time according to the initial delay
            first_chunk = True
            tx_start_time = TimeSpec(self.usrp.get_time_now().get_real_secs() + delay)

            while samples_transmitted < n_requested_samples:

                final_sample_idx = min(samples_transmitted + chunk_size, n_requested_samples)

                # Efficient vectorized copy: extract all active channels at once
                tx_buffer = self.tx_signal[self.channels, samples_transmitted:final_sample_idx]

                metadata = TXMetadata()
                metadata.start_of_burst = first_chunk
                metadata.end_of_burst = (final_sample_idx == n_requested_samples)
                metadata.has_time_spec = first_chunk
                if first_chunk:
                    metadata.time_spec = tx_start_time
                    first_chunk = False
                
                n_tx_iter = self.tx_streamer.send(tx_buffer, metadata, timeout)
                if n_tx_iter == 0:
                    raise RuntimeError("TX stream timed out. No samples were sent.")
                
                samples_transmitted += n_tx_iter
                
            return samples_transmitted
    
    
    # Telemetry loop
    def _async_loop(self):
        
        metadata = TXAsyncMetadata()
        while not self._stop.is_set():
            try:
                if self.tx_streamer is None:
                    time.sleep(POLLING_RATE)
                    continue  
                
                msg_received = self.tx_streamer.recv_async_msg(metadata, POLLING_RATE)
                if not msg_received:
                    continue
                
                # Handle events
                event = metadata.event_code
                
                ev_name = None

                if event in (E.underflow, getattr(E, "underflow_in_packet", E.underflow)):
                    ev_name = "underflow"
                elif event in (E.seq_error, getattr(E, "seq_error_in_burst", E.seq_error)):
                    ev_name = "seq_error"
                elif event == E.time_error:
                    ev_name = "time_error"
                elif event == E.burst_ack:
                    ev_name = "burst_ack"
                    
                if ev_name is not None:
                    self._publisher.send_json({
                        "event": ev_name,
                        "ts": time.time(),
                        "ch": getattr(metadata, "channel", None),
                        "daemon_id": self.daemon_id
                    })
            
            except Exception as e:
                self._publisher.send_json({
                    "event": "daemon_error",
                    "error": str(e),
                    "ts": time.time(),
                    "daemon_id": self.daemon_id
                })
                
                
    def close(self):
        self._stop.set()
        if self._async_thread.is_alive():
            self._async_thread.join(timeout=1.0)
        super().close()


def parse_arguments():
    """Parse command line arguments for TX daemon.

    All options can also be supplied via environment variables:
      USRP_TX_ADDR    → --usrp-addr   (IP, serial number, or full key=value)
      USRP_DEVICE_TYPE→ --device-type (b200 | x4xx | x3xx | …)
      MASTER_CLOCK_RATE→ --mcr        (Hz; 0 = let UHD choose)
    """
    import os as _os
    parser = argparse.ArgumentParser(description="TX Daemon for USRP-based SDR transmission")
    parser.add_argument(
        '--usrp-addr', '-a',
        default=_os.environ.get("USRP_TX_ADDR", DEFAULT_USRP_IP_ADDR),
        help=(
            "USRP device address. Can be:\n"
            "  • IP address          → addr=<ip>   (X3xx/X4xx network)\n"
            "  • Serial number       → serial=<sn> (B2xx USB)\n"
            "  • Full UHD key=value  e.g. 'serial=12345678' or 'name=mydev'\n"
            "  Env: USRP_TX_ADDR (default: %(default)s)"
        ),
    )
    parser.add_argument(
        '--device-type', '-t',
        default=_os.environ.get("USRP_DEVICE_TYPE", "x4xx"),
        help=(
            "UHD device type string: 'b200' for B200/B210 USB, 'x4xx' for X410/X440,\n"
            "'x3xx' for X310/X300, etc. Controls buffer strategy and frame sizes.\n"
            "Env: USRP_DEVICE_TYPE (default: %(default)s)"
        ),
    )
    parser.add_argument('--mgmt-addr', '-m', type=valid_ip,
                       help="Management interface IP (X4xx only, required with --use-dpdk)")
    parser.add_argument('--use-dpdk', action='store_true',
                       help="Enable DPDK (network devices only; requires --mgmt-addr)")
    parser.add_argument(
        '--mcr', type=float,
        default=float(_os.environ.get("MASTER_CLOCK_RATE", 0) or 0),
        help=(
            "Master clock rate in Hz. 0 = let UHD choose (good default for B2xx).\n"
            "X4xx typically needs 250000000. Env: MASTER_CLOCK_RATE (default: %(default).0f)"
        ),
    )
    parser.add_argument('--buffer-scale', type=buffer_scale_float, default=1.0,
                       help="Buffer size scaling factor (0.5–8.0, network devices only)")
    return parser.parse_args()


def main():
    args = parse_arguments()

    if args.use_dpdk and args.mgmt_addr is None:
        raise ValueError("--mgmt-addr is required when --use-dpdk is set")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()],
    )

    context = zmq.Context.instance()
    reply = context.socket(zmq.REP)
    reply.bind(REP_ADDR)
    daemon_id = f"TX-{get_primary_ip()}"
    tx_daemon = TXDaemon(
        usrp_addr=args.usrp_addr,
        mgmt_addr=args.mgmt_addr,
        mcr=args.mcr or None,
        device_type=args.device_type,
        use_dpdk=args.use_dpdk,
        buffer_scale=args.buffer_scale,
        daemon_id=daemon_id,
    )

    logging.info(f"TX Daemon is running. Publishing events on {PUB_ADDR} and listening for commands on {REP_ADDR}")

    try:
        while True:
            request = reply.recv_json()
            
            try:
                op = request.get("op", None)
                
                if op == "PING":
                    reply.send_json({"status":"OK"})
                    
                elif op == "GET_TIME":
                    reply.send_json({"status":"OK", "device_time":tx_daemon.get_device_time()})
                    
                elif op == "CONFIGURE_USRP":

                    required_params = ["fs", "fc", "G_TX"]
                    missing_params = [p for p in required_params if p not in request]
                    if missing_params:
                        error_msg = f"Missing parameters: {missing_params}"
                        logging.error(f"CONFIGURE_USRP failed: {error_msg}")
                        reply.send_json({"status":"ERROR", "error":error_msg})
                        continue

                    fs_req  = positive_float(request["fs"])
                    fc_req  = positive_float(request["fc"])
                    sc_req  = int_list(request["sync_channels"], non_negative=True, allow_empty=True) if "sync_channels" in request else []
                    ic_req  = int_list(request["intf_channels"], non_negative=True, allow_empty=True) if "intf_channels" in request else []

                    # Validate G_TX as a dictionary with required channels
                    all_channels = sc_req + ic_req
                    g_tx = validate_gain_dict(request["G_TX"], all_channels)

                    ant_req = request.get("antenna", "TX/RX0")

                    # Optional power-reference targets in dBm. Caller sends a
                    # dict {channel: dbm} via P_TX_DBM. If present, the daemon
                    # uses set_tx_power_reference() (calibrated) instead of
                    # set_tx_gain() (relative) for those channels.
                    p_tx_dbm = None
                    if "P_TX_DBM" in request and request["P_TX_DBM"] is not None:
                        raw = request["P_TX_DBM"]
                        if isinstance(raw, dict):
                            p_tx_dbm = {int(k): float(v) for k, v in raw.items()}
                        else:
                            p_tx_dbm = {ch: float(raw) for ch in all_channels}

                    actual_settings = tx_daemon.configure_usrp(
                        fs=fs_req, fc=fc_req, sync_channels=sc_req, intf_channels=ic_req,
                        channel_gains=g_tx, antenna=ant_req, channel_powers=p_tx_dbm
                    )

                    requested_params = {
                        "fs": fs_req,
                        "fc": fc_req,
                        "sync_channels": sc_req,
                        "intf_channels": ic_req,
                        "G_TX": g_tx,
                        "antenna": ant_req
                    }
                    mismatches = check_settings_mismatch(actual_settings, requested_params)

                    if mismatches:
                        reply.send_json({
                            "status": "MISMATCH",
                            "settings": actual_settings,
                            "mismatches": mismatches
                        })
                    else:
                        reply.send_json({
                            "status": "OK",
                            "settings": actual_settings
                        })
                    
                    
                elif op == "LOAD_SIGNAL":

                    # Check that at least one signal path is provided
                    if "sync_signal_path" not in request and "intf_signal_path" not in request:
                        error_msg = "Must provide either sync_signal_path or intf_signal_path (or both)"
                        logging.error(f"LOAD_SIGNAL failed: {error_msg}")
                        reply.send_json({"status":"ERROR", "error":error_msg})
                        continue

                    sync_signal_path = str(request["sync_signal_path"]) if "sync_signal_path" in request else None
                    intf_signal_path = str(request["intf_signal_path"]) if "intf_signal_path" in request else None

                    signal_info = tx_daemon.load_signal(
                        sync_signal_path=sync_signal_path,
                        intf_signal_path=intf_signal_path
                    )

                    reply.send_json({
                        "status": "OK",
                        "signal_info": signal_info
                    })
                    
                elif op == "TRANSMIT_BURST":

                    if "delay" not in request:
                        error_msg = "Missing parameter: delay"
                        logging.error(f"TRANSMIT_BURST failed: {error_msg}")
                        reply.send_json({"status":"ERROR", "error":error_msg})
                        continue

                    # Use provided timeout or let the method calculate it automatically
                    timeout_param = request.get("timeout")
                    timeout = positive_float(timeout_param) if timeout_param is not None else None

                    sent = tx_daemon.transmit_with_delay(
                        delay=not_negative_float(request["delay"]),
                        timeout=timeout
                    )
                    reply.send_json({"status":"OK", "samples_sent":sent})

                elif op == "CHANGE_REFERENCE":
                    if "source" not in request:
                        error_msg = "Missing parameter: source"
                        logging.error(f"CHANGE_REFERENCE failed: {error_msg}")
                        reply.send_json({"status":"ERROR", "error":error_msg})
                        continue
                    source = str(request["source"]).lower()
                    if source not in ["internal", "gpsdo"]:
                        error_msg = "Unsupported source: must be 'internal' or 'gpsdo'"
                        logging.error(f"CHANGE_REFERENCE failed: {error_msg}")
                        reply.send_json({"status":"ERROR", "error":error_msg})
                        continue
                    
                    lock_timeout = positive_float(request.get("timeout", DEFAULT_LOCK_TIMEOUT))
                    poll_period = positive_float(request.get("poll_period", DEFAULT_POLL_PERIOD))
                    mboard = int(request.get("mboard", 0))    
                
        
                    tx_daemon.change_time_and_clock_source(
                        mboard=mboard,
                        source=source,
                        lock_timeout=lock_timeout,
                        poll_period=poll_period)
                    
                    reply.send_json({"status":"OK"})
                    
                else:
                    error_msg = f"Invalid operation: {op}"
                    logging.error(f"Request failed: {error_msg}")
                    reply.send_json({"status":"ERROR", "error":error_msg})
            except Exception as e:
                error_msg = str(e)
                logging.error(f"Request failed with exception: {error_msg}")
                reply.send_json({"status":"ERROR", "error":error_msg})
    except KeyboardInterrupt:
        pass   
    finally:
        tx_daemon.close()
        reply.close(linger=0)     
                    
                    
if __name__ == "__main__":
    main()