import uhd
import numpy as np
from uhd.types import TimeSpec, RXMetadata, RXMetadataErrorCode, StreamCMD, StreamMode
import zmq
import threading
import time
import os
import h5py
import logging
import argparse
try:
    from .usrp_common import (
        BaseUSRPDaemon, positive_int, positive_float, buffer_scale_float, not_negative_float, int_list, valid_ip,
        validate_sampling_rate, validate_carrier_frequency, validate_gain, validate_antenna,
        check_settings_mismatch, DEFAULT_RX_REP_ADDR, DEFAULT_RX_PUB_ADDR, DEFAULT_USRP_IP_ADDR,
        DEFAULT_FLUSH_TIMEOUT, DEFAULT_RX_STREAM_TIMEOUT, DEFAULT_LOCK_TIMEOUT, DEFAULT_POLL_PERIOD,
        FS_ATOL, FC_ATOL, G_ATOL
    )
except ImportError:
    from usrp_common import (
        BaseUSRPDaemon, positive_int, positive_float, buffer_scale_float, not_negative_float, int_list, valid_ip,
        validate_sampling_rate, validate_carrier_frequency, validate_gain, validate_antenna,
        check_settings_mismatch, DEFAULT_RX_REP_ADDR, DEFAULT_RX_PUB_ADDR, DEFAULT_USRP_IP_ADDR,
        DEFAULT_FLUSH_TIMEOUT, DEFAULT_RX_STREAM_TIMEOUT, DEFAULT_LOCK_TIMEOUT, DEFAULT_POLL_PERIOD,
        FS_ATOL, FC_ATOL, G_ATOL
    )

REP_ADDR = DEFAULT_RX_REP_ADDR
PUB_ADDR = DEFAULT_RX_PUB_ADDR
USRP_IP_ADDR = DEFAULT_USRP_IP_ADDR
FLUSH_TIMEOUT = DEFAULT_FLUSH_TIMEOUT
RX_STREAM_TIMEOUT = DEFAULT_RX_STREAM_TIMEOUT


class RXDaemon(BaseUSRPDaemon):
    def __init__(self, usrp_addr, mgmt_addr=None, mcr=None,
                 device_type="x4xx", use_dpdk=False, buffer_scale=1.0):
        super().__init__(usrp_addr, mgmt_addr, mcr, device_type, use_dpdk, buffer_scale)

        self.channels = None
        self.rx_streamer = None

        # Configuration state for incremental updates
        self.current_config = {
            'fs': None,
            'fc': None,
            'channels': None,
            'G_RX': None,
            'antenna': None
        }
        # Per-channel configuration state
        self.channel_configs = {}

        self._publisher = self._context.socket(zmq.PUB)
        self._publisher.bind(PUB_ADDR)
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

        # Read current hardware state to initialize our tracking
        self._read_hardware_state()

    def _read_hardware_state(self):
        """Read current hardware configuration to initialize tracking state."""
        try:
            n_channels = self.usrp.get_rx_num_channels()

            # Read global sampling rate (use channel 0 as reference)
            if n_channels > 0:
                current_fs = self.usrp.get_rx_rate(0)
                self.current_config['fs'] = current_fs

            # Read per-channel settings
            for ch in range(n_channels):
                try:
                    current_fc = self.usrp.get_rx_freq(ch)
                    current_gain = self.usrp.get_rx_gain(ch)
                    current_antenna = self.usrp.get_rx_antenna(ch)

                    # Store in per-channel config
                    self.channel_configs[ch] = {
                        'fc': current_fc,
                        'gain': current_gain,
                        'antenna': current_antenna
                    }


                except Exception as e:
                    logging.warning(f"Could not read state for RX channel {ch}: {e}")

        except Exception as e:
            logging.warning(f"Could not read RX hardware state: {e}")
            # Continue with empty state - first configure will set everything

    def configure_usrp(self, fs, fc, requested_channels, G_RX, antenna="RX1"):

        with self._op_guard("configure_usrp"):
            def _antenna_for(ch):
                if isinstance(antenna, dict):
                    return antenna.get(ch, antenna.get(str(ch)))
                return antenna

            n_channels = self.usrp.get_rx_num_channels()
            available_channels = list(range(n_channels))

            if any(ch not in available_channels for ch in requested_channels):
                raise ValueError(f"Invalid channels {requested_channels}. Available channels: {available_channels}")

            # Check what has changed to minimize USRP reconfigurations
            new_config = {
                'fs': fs,
                'fc': fc,
                'channels': sorted(requested_channels),
                'G_RX': G_RX,
                'antenna': antenna
            }

            # Determine what needs to be updated (use tolerances for float comparisons)
            fs_changed = self.current_config['fs'] is None or not np.isclose(self.current_config['fs'], new_config['fs'], atol=FS_ATOL, rtol=0)
            fc_changed = self.current_config['fc'] is None or not np.isclose(self.current_config['fc'], new_config['fc'], atol=FC_ATOL, rtol=0)
            channels_changed = self.current_config['channels'] != new_config['channels']
            gain_changed = self.current_config['G_RX'] is None or not np.isclose(self.current_config['G_RX'], new_config['G_RX'], atol=G_ATOL, rtol=0)
            antenna_changed = self.current_config['antenna'] != new_config['antenna']

            logging.info(f"RX Configuration changes: fs={fs_changed}, fc={fc_changed}, channels={channels_changed}, gain={gain_changed}, antenna={antenna_changed}")


            # Validate new configuration
            validate_sampling_rate(self.usrp, fs, is_tx=False)
            for ch in requested_channels:
                validate_carrier_frequency(self.usrp, fc, ch, is_tx=False)
                validate_gain(self.usrp, G_RX, ch, is_tx=False)
                validate_antenna(self.usrp, _antenna_for(ch), ch, is_tx=False)

            # Update channel assignments
            self.channels = requested_channels

            actual_settings = {}

            # Only update sampling rate if it changed
            if fs_changed:
                self.usrp.set_rx_rate(fs, requested_channels[0])
            actual_fs = self.usrp.get_rx_rate(requested_channels[0])

            # Update per-channel settings with per-channel change detection
            for ch in requested_channels:

                # Get previous channel config
                prev_ch_config = self.channel_configs.get(ch, {})

                # Detect per-channel changes (use tolerances for float comparisons)
                prev_fc = prev_ch_config.get('fc')
                prev_gain = prev_ch_config.get('gain')
                ch_fc_changed = prev_fc is None or not np.isclose(prev_fc, fc, atol=FC_ATOL, rtol=0)
                ch_gain_changed = prev_gain is None or not np.isclose(prev_gain, G_RX, atol=G_ATOL, rtol=0)
                ch_antenna_changed = prev_ch_config.get('antenna') != _antenna_for(ch)

                # Only set frequency if it changed for this channel
                if ch_fc_changed:
                    tune_req = uhd.types.TuneRequest(fc)
                    self.usrp.set_rx_freq(tune_req, ch)
                actual_fc = self.usrp.get_rx_freq(ch)

                # Only set gain if it changed for this channel
                if ch_gain_changed:
                    self.usrp.set_rx_gain(G_RX, ch)
                actual_gain = self.usrp.get_rx_gain(ch)

                # Only set antenna if it changed for this channel
                if ch_antenna_changed:
                    self.usrp.set_rx_antenna(_antenna_for(ch), ch)
                actual_antenna = self.usrp.get_rx_antenna(ch)

                # Update per-channel config state
                self.channel_configs[ch] = {
                    'fc': fc,
                    'gain': G_RX,
                    'antenna': _antenna_for(ch)
                }

                actual_settings[ch] = {
                    "fs": actual_fs,
                    "fc": actual_fc,
                    "G_RX": actual_gain,
                    "antenna": actual_antenna
                }

            # Only recreate streamer if channels changed (most critical for RFNoC)
            if channels_changed or self.rx_streamer is None:
                if self.rx_streamer is not None:
                    del self.rx_streamer
                    self.rx_streamer = None

                # Use fc32 CPU format with sc16 over-the-wire format
                st = uhd.usrp.StreamArgs("fc32", "sc16")
                st.channels = requested_channels
                self.rx_streamer = self.usrp.get_rx_stream(st)

            # Update current configuration state
            self.current_config = new_config.copy()

            return actual_settings
    
    


    # Flush stale samples (parallels earlier standalone function; same timeout default)
    def flush_rx_stream(self, timeout=FLUSH_TIMEOUT):
        with self._op_guard("flush_rx_stream"):
            if self.rx_streamer is None or self.channels is None:
                return
            self.rx_streamer.issue_stream_cmd(StreamCMD(StreamMode.stop_cont))
            time.sleep(timeout)

            metadata = RXMetadata()
            chunk_size = self.rx_streamer.get_max_num_samps()
            rx_buffer = np.zeros((len(self.channels), chunk_size), dtype=np.complex64)

            while True:
                _ = self.rx_streamer.recv(rx_buffer, metadata, timeout)
                if metadata.error_code == RXMetadataErrorCode.timeout:
                    break

    
    
    def get_device_time(self):
        return self.usrp.get_time_now().get_real_secs()
        
    
    def receive_with_delay(self, delay, n_requested_samples, timeout=RX_STREAM_TIMEOUT):
        with self._op_guard("receive_with_delay"):
            if self.rx_streamer is None or self.channels is None:
                raise RuntimeError("USRP or RX streamer not configured. Please call CONFIGURE_USRP first.")

            n_ch = len(self.channels)
            rx_signal = np.zeros((n_ch, n_requested_samples), dtype=np.complex64)

            # B210 in multi-channel mode needs a streamer "warm-up": the very
            # first stream_cmd issued after creating a new RX streamer is
            # frequently lost, so the next recv() times out without ever
            # producing samples. Kicking off a tiny start_cont / stop_cont
            # cycle first makes the AD9361 path reliably honour the timed
            # num_done request that follows.
            if n_ch > 1:
                warmup_cmd = StreamCMD(StreamMode.start_cont)
                warmup_cmd.stream_now = False
                warmup_cmd.time_spec = TimeSpec(
                    self.usrp.get_time_now().get_real_secs() + 0.05
                )
                self.rx_streamer.issue_stream_cmd(warmup_cmd)
                warmup_md = RXMetadata()
                warmup_buf = np.zeros(
                    (n_ch, self.rx_streamer.get_max_num_samps()),
                    dtype=np.complex64,
                )
                # Brief streaming window to make sure the front-end actually
                # starts delivering samples before we issue the timed request.
                for _ in range(6):
                    self.rx_streamer.recv(warmup_buf, warmup_md, 0.15)
                    if warmup_md.error_code == RXMetadataErrorCode.none:
                        break
                self.rx_streamer.issue_stream_cmd(StreamCMD(StreamMode.stop_cont))
                # Drain whatever is still in flight so the next recv() starts
                # from a clean state.
                for _ in range(8):
                    self.rx_streamer.recv(warmup_buf, warmup_md, 0.05)
                    if warmup_md.error_code == RXMetadataErrorCode.timeout:
                        break

            rx_start_time = TimeSpec(self.usrp.get_time_now().get_real_secs() + delay)

            stream_cmd = StreamCMD(StreamMode.num_done)
            stream_cmd.num_samps = n_requested_samples
            stream_cmd.stream_now = False
            stream_cmd.time_spec = rx_start_time
            self.rx_streamer.issue_stream_cmd(stream_cmd)

            metadata = RXMetadata()
            chunk_size = self.rx_streamer.get_max_num_samps()

            rx_buffer = np.zeros((n_ch, chunk_size), dtype=np.complex64)

            samples_received = 0
            max_iterations = n_requested_samples // self.rx_streamer.get_max_num_samps() + 100  # Safety margin
            iteration_count = 0

            while samples_received < n_requested_samples:
                iteration_count += 1
                if iteration_count > max_iterations:
                    raise RuntimeError(f"RX timeout: received only {samples_received}/{n_requested_samples} samples after {iteration_count} iterations")

                # Use delay + timeout for first chunk, just timeout for subsequent chunks
                current_timeout = (delay + timeout) if samples_received == 0 else timeout
                n_rx_iter = self.rx_streamer.recv(rx_buffer, metadata, current_timeout)
                if metadata.error_code != RXMetadataErrorCode.none:
                    if metadata.error_code == RXMetadataErrorCode.timeout:
                        raise RuntimeError(f"RX stream timeout after receiving {samples_received}/{n_requested_samples} samples")
                    else:
                        raise RuntimeError(f"RX ERROR: {metadata.strerror()}")
                if n_rx_iter == 0:
                    continue

                n_to_copy = min(n_rx_iter, n_requested_samples - samples_received)
                for i in range(n_ch):
                    rx_signal[i, samples_received:samples_received + n_to_copy] = rx_buffer[i,:n_to_copy]
                samples_received += n_to_copy

            return rx_signal, samples_received
    
    
    # Lightweight heartbeat on PUB socket
    def _heartbeat_loop(self):
        while not self._stop.is_set():
            try:
                self._publisher.send_json({"event": "heartbeat", "ts": time.time()})
            except Exception:
                pass
            time.sleep(1.0)


    def close(self):
        self._stop.set()
        if self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.0)
        super().close()



def parse_arguments():
    """Parse command line arguments for RX daemon.

    All options can also be supplied via environment variables:
      USRP_RX_ADDR    → --usrp-addr   (IP, serial number, or full key=value)
      USRP_DEVICE_TYPE→ --device-type (b200 | x4xx | x3xx | …)
      MASTER_CLOCK_RATE→ --mcr        (Hz; 0 = let UHD choose)
    """
    import os as _os
    parser = argparse.ArgumentParser(description="RX Daemon for USRP-based SDR reception")
    parser.add_argument(
        '--usrp-addr', '-a',
        default=_os.environ.get("USRP_RX_ADDR", DEFAULT_USRP_IP_ADDR),
        help=(
            "USRP device address. Can be:\n"
            "  • IP address          → addr=<ip>   (X3xx/X4xx network)\n"
            "  • Serial number       → serial=<sn> (B2xx USB)\n"
            "  • Full UHD key=value  e.g. 'serial=12345678' or 'name=mydev'\n"
            "  Env: USRP_RX_ADDR (default: %(default)s)"
        ),
    )
    parser.add_argument(
        '--device-type', '-t',
        default=_os.environ.get("USRP_DEVICE_TYPE", "x4xx"),
        help=(
            "UHD device type string: 'b200' for B200/B210 USB, 'x4xx' for X410/X440,\n"
            "'x3xx' for X310/X300, etc.\n"
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
                       help="Buffer size scaling factor (0.5-8.0, network devices only)")
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
    rx_daemon = RXDaemon(
        usrp_addr=args.usrp_addr,
        mgmt_addr=args.mgmt_addr,
        mcr=args.mcr or None,
        device_type=args.device_type,
        use_dpdk=args.use_dpdk,
        buffer_scale=args.buffer_scale,
    )

    logging.info(f"RX Daemon is running. Publishing events on {PUB_ADDR} and listening for commands on {REP_ADDR}")

    try:
        while True:
            request = reply.recv_json()
            try:
                op = request.get("op", None)

                if op == "PING":
                    reply.send_json({"status": "OK"})

                elif op == "GET_TIME":
                    reply.send_json({"status": "OK", "device_time": rx_daemon.get_device_time()})

                elif op == "CONFIGURE_USRP":
                    required_params = ["fs", "fc", "channels", "G_RX"]
                    missing_params = [p for p in required_params if p not in request]
                    if missing_params:
                        error_msg = f"Missing parameters: {missing_params}"
                        logging.error(f"CONFIGURE_USRP failed: {error_msg}")
                        reply.send_json({"status":"ERROR", "error":error_msg})
                        continue
                    
                    fs_req  = positive_float(request["fs"])
                    fc_req  = positive_float(request["fc"])
                    g_req   = not_negative_float(request["G_RX"])
                    ch_req  = int_list(request["channels"], non_negative=True)
                    ant_req = request.get("antenna", "RX1")

                    actual_settings = rx_daemon.configure_usrp(
                        fs=fs_req, fc=fc_req, G_RX=g_req, requested_channels=ch_req, antenna=ant_req
                    )

                    requested_params = {
                        "fs": fs_req,
                        "fc": fc_req,
                        "G_RX": g_req,
                        "antenna": ant_req
                    }
                    mismatches = check_settings_mismatch(actual_settings, requested_params)

                    if mismatches:
                        reply.send_json({"status": "MISMATCH", "settings": actual_settings, "mismatches": mismatches})
                    else:
                        reply.send_json({"status": "OK", "settings": actual_settings})

                elif op == "FLUSH_RX":
                    rx_daemon.flush_rx_stream()
                    reply.send_json({"status": "OK"})

                elif op == "RECEIVE_TO_FILE":
                    required_params = ["n_samples", "delay", "path"]
                    missing_params = [p for p in required_params if p not in request]
                    if missing_params:
                        error_msg = f"Missing parameters: {missing_params}"
                        logging.error(f"RECEIVE_TO_FILE failed: {error_msg}")
                        reply.send_json({"status": "ERROR", "error": error_msg})
                        continue
                    n_samples = positive_int(request["n_samples"])
                    delay   = not_negative_float(request["delay"])
                    timeout = positive_float(request.get("timeout", RX_STREAM_TIMEOUT))
                    path    = str(request["path"])

                    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                    data, n_recv = rx_daemon.receive_with_delay(delay, n_samples, timeout=timeout)
                    if n_recv != n_samples:
                        error_msg = f"Received {n_recv} samples, expected {n_samples}"
                        logging.error(f"RECEIVE_TO_FILE failed: {error_msg}")
                        reply.send_json({"status": "ERROR", "error": error_msg})
                    else:
                        with h5py.File(path, "w") as f:
                            # Save RX signal as native complex64 (Python-friendly format)
                            f.create_dataset("rx_signal", data=data, compression=None)

                            ch0 = rx_daemon.channels[0]
                            f["rx_signal"].attrs.update({
                                "fs": rx_daemon.usrp.get_rx_rate(ch0),
                                "fc": rx_daemon.usrp.get_rx_freq(ch0),
                                "gain_dB": rx_daemon.usrp.get_rx_gain(ch0),
                                "otw": "sc16",
                                "cpu": "fc32",
                                "channels": rx_daemon.channels,
                                "n_channels": len(rx_daemon.channels),
                                "description": "Multi-channel RX signal (native complex64)"
                            })
                        reply.send_json({"status": "OK", "samples_received": n_recv, "path": path})

                elif op == "CHANGE_REFERENCE":
                    if "source" not in request:
                        error_msg = "Missing parameter: source"
                        logging.error(f"CHANGE_REFERENCE failed: {error_msg}")
                        reply.send_json({"status": "ERROR", "error": error_msg})
                        continue
                    src = str(request["source"]).lower()
                    if src not in ["internal", "gpsdo"]:
                        error_msg = "Unsupported source: must be 'internal' or 'gpsdo'"
                        logging.error(f"CHANGE_REFERENCE failed: {error_msg}")
                        reply.send_json({"status": "ERROR", "error": error_msg})
                        continue
                    lock_timeout = positive_float(request.get("timeout", DEFAULT_LOCK_TIMEOUT))
                    poll_period  = positive_float(request.get("poll_period", DEFAULT_POLL_PERIOD))
                    mboard       = int(request.get("mboard", 0))

                    rx_daemon.change_time_and_clock_source(
                        mboard=mboard, source=src, lock_timeout=lock_timeout, poll_period=poll_period
                    )
                    reply.send_json({"status": "OK"})

                else:
                    error_msg = f"Invalid operation: {op}"
                    logging.error(f"Request failed: {error_msg}")
                    reply.send_json({"status": "ERROR", "error": error_msg})

            except Exception as e:
                error_msg = str(e)
                logging.error(f"Request failed with exception: {error_msg}")
                reply.send_json({"status": "ERROR", "error": error_msg})
    except KeyboardInterrupt:
        pass
    finally:
        rx_daemon.close()
        reply.close(linger=0)

if __name__ == "__main__":
    main()