"""
Common functionality for USRP RX and TX daemons.
"""
import uhd
import numpy as np
import zmq
import threading
import time
from contextlib import contextmanager
import os
import ipaddress
import logging

# Maximum network buffer size - imposed by the kernel
SIGNED_INT32_MAX = 2_147_483_647
MAX_NET_BUFF_SIZE = SIGNED_INT32_MAX// 2  # 1_073_741_823

# Default network addresses
DEFAULT_RX_REP_ADDR = "tcp://*:5555"
DEFAULT_RX_PUB_ADDR = "tcp://*:5556"
DEFAULT_TX_REP_ADDR = "tcp://*:5557"
DEFAULT_TX_PUB_ADDR = "tcp://*:5558"

# Default USRP IP address
DEFAULT_USRP_IP_ADDR = "192.168.10.2"

# Tolerance constants for checking actual USRP settings against requested ones
FS_ATOL = 1.0          # samples/s
FC_ATOL = 1.0           # Hz
G_ATOL = 0.05          # dB

# Timeout constants
DEFAULT_FLUSH_TIMEOUT = 1.0         # seconds
DEFAULT_RX_STREAM_TIMEOUT = 0.5     # seconds
DEFAULT_TX_TIMEOUT = 2.0            # seconds
DEFAULT_LOCK_TIMEOUT = 10.0         # seconds for GPSDO lock
DEFAULT_POLL_PERIOD = 0.5           # seconds for GPSDO polling
DEFAULT_ASYNC_POLLING_RATE = 0.05   # seconds for TX async polling


# Validation functions
def positive_int(value):
    """Custom argparse type for positive integers."""
    try:
        ivalue = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{value} is not a valid integer")
    if ivalue <= 0:
        raise ValueError(f"{value} is not a positive integer")
    return ivalue


def not_negative_int(value):
    """Custom argparse type for non-negative integers."""
    try:
        ivalue = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{value} is not a valid integer")
    if ivalue < 0:
        raise ValueError(f"{value} is not a non-negative integer")
    return ivalue


def positive_float(value):
    """Custom argparse type for positive floats."""
    try:
        fvalue = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{value} is not a valid float")
    if fvalue <= 0.0:
        raise ValueError(f"{value} is not a positive float")
    return fvalue


def buffer_scale_float(value):
    """Custom argparse type for buffer scaling factor."""
    try:
        fvalue = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{value} is not a valid buffer scale factor")
    if fvalue < 0.5 or fvalue > 8.0:
        raise ValueError(f"Buffer scale {value} is outside valid range (0.5-8.0)")
    return fvalue


def not_negative_float(value):
    """Custom argparse type for non-negative floats."""
    try:
        fvalue = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{value} is not a valid float")
    if fvalue < 0.0:
        raise ValueError(f"{value} is not a non-negative float")
    return fvalue


def int_list(v_list, *, non_negative=True, allow_empty=False):
    """Validate and convert a list of integers."""
    if isinstance(v_list, (list, tuple)):
        out = []
        for v in v_list:
            try:
                ivalue = int(v)
            except (ValueError, TypeError):
                raise ValueError(f"{v} is not a valid integer")
            if non_negative and ivalue < 0:
                raise ValueError(f"{v} is not a non-negative integer")
            elif not non_negative and ivalue <= 0:
                raise ValueError(f"{v} is not a positive integer")
            out.append(ivalue)
        if not out and not allow_empty:
            raise ValueError("List cannot be empty")
        return out
    else:
        raise ValueError(f"Value must be a list or tuple of integers")


def float_list(v_list, *, non_negative=False, allow_empty=False):
    """Validate and convert a list of floats."""
    if isinstance(v_list, (list, tuple)):
        out = []
        for v in v_list:
            try:
                fvalue = float(v)
            except (ValueError, TypeError):
                raise ValueError(f"{v} is not a valid float")
            if non_negative and fvalue < 0.0:
                raise ValueError(f"{v} is not a non-negative float")
            out.append(fvalue)
        if not out and not allow_empty:
            raise ValueError("List cannot be empty")
        return out
    else:
        raise ValueError(f"Value must be a list or tuple of floats")


def validate_gain_dict(gain_dict, required_channels):
    """Validate gain dictionary has required channel keys with non-negative float values.

    Args:
        gain_dict: Dictionary mapping channel numbers to gain values
        required_channels: List of channel numbers that must be present

    Returns:
        Dictionary with validated channel:gain pairs

    Raises:
        ValueError: If gain_dict is not a dict, missing required channels, or has invalid gain values
    """
    if not isinstance(gain_dict, dict):
        raise ValueError("G_TX must be a dictionary mapping channel numbers to gain values")

    # Convert all keys to integers and values to floats
    validated = {}
    for key, value in gain_dict.items():
        try:
            channel = int(key)
        except (ValueError, TypeError):
            raise ValueError(f"Channel key '{key}' is not a valid integer")

        try:
            gain = float(value)
            if gain < 0.0:
                raise ValueError(f"Gain for channel {channel} must be non-negative, got {gain}")
        except (ValueError, TypeError):
            raise ValueError(f"Gain value '{value}' for channel {channel} is not a valid float")

        validated[channel] = gain

    # Check all required channels are present
    missing_channels = [ch for ch in required_channels if ch not in validated]
    if missing_channels:
        raise ValueError(f"G_TX missing required channels: {missing_channels}")

    return validated


def valid_path(path: str):
    if not path.endswith(".h5"):
        raise ValueError(f"File must have a .h5 extension: {path!r}")
    return path
    
    
def valid_ip(s: str):
    # Allow common hostname aliases
    allowed_hostnames = {"localhost", "127.0.0.1", "::1"}
    if s.lower() in allowed_hostnames:
        return s
    try:
        ipaddress.ip_address(s)
        return s
    except ValueError as e:
        raise ValueError(f"Invalid IP address or hostname: {s!r}") from e


def valid_port(value):
    """Custom argparse type for valid port numbers (excluding reserved ranges)."""
    ivalue = not_negative_int(value)

    if ivalue == 0 or ivalue > 65535:
        raise ValueError(f"Port {value} is outside valid range (1-65535)")

    if ivalue <= 1023:
        raise ValueError(f"Port {value} is in the well-known/system port range (1-1023)")

    return ivalue


def get_bool_sensor_state(usrp, sensor_name, mboard=0):
    """Get boolean sensor state from USRP."""
    return usrp.get_mboard_sensor(sensor_name, mboard).to_bool()


def find_closest_values(target_value, available_values, max_closest=2):
    """Find the closest values to a target from available options.

    Args:
        target_value: The desired value
        available_values: Array or list of available values
        max_closest: Maximum number of closest values to return (default: 2)

    Returns:
        List of closest values, sorted by proximity to target
    """
    available_array = np.asarray(available_values)

    # Handle edge cases
    if len(available_array) == 0:
        return []
    if len(available_array) == 1:
        return [available_array[0]]

    # Calculate distances and find closest indices
    distances = np.abs(available_array - target_value)
    closest_indices = np.argsort(distances)[:max_closest]

    # Return the closest values, sorted by proximity
    closest_values = available_array[closest_indices]
    return closest_values.tolist()


def validate_sampling_rate(usrp, fs, is_tx=False):
    """Validate requested sampling frequency against USRP capabilities."""
    fs_range = usrp.get_tx_rates() if is_tx else usrp.get_rx_rates()
    available_fs = np.arange(fs_range.start(), fs_range.stop()+fs_range.step(), fs_range.step())
    if not np.any(np.isclose(fs, available_fs)):
        mode = "TX" if is_tx else "RX"
        closest_rates = find_closest_values(fs, available_fs)
        if len(closest_rates) == 1:
            raise ValueError(f"Invalid {mode} sampling rate {fs}. Closest available rate: {closest_rates[0]}")
        else:
            raise ValueError(f"Invalid {mode} sampling rate {fs}. Closest available rates: {closest_rates[0]}, {closest_rates[1]}")


def validate_carrier_frequency(usrp, fc, channel, is_tx=False):
    """Validate requested carrier frequency against USRP capabilities."""
    fc_range = usrp.get_tx_freq_range(channel) if is_tx else usrp.get_rx_freq_range(channel)
    if not (fc_range.start() <= fc <= fc_range.stop()):
        mode = "TX" if is_tx else "RX"
        # For continuous ranges, show bounds as closest values
        if fc < fc_range.start():
            raise ValueError(f"Invalid {mode} carrier frequency {fc}. Below valid range. Minimum frequency: {fc_range.start()}")
        elif fc > fc_range.stop():
            raise ValueError(f"Invalid {mode} carrier frequency {fc}. Above valid range. Maximum frequency: {fc_range.stop()}")
        else:
            raise ValueError(f"Invalid {mode} carrier frequency {fc}. Valid range: {fc_range.start()} - {fc_range.stop()}")


def validate_gain(usrp, gain, channel, is_tx=False):
    """Validate requested gain against USRP capabilities."""
    gain_range = usrp.get_tx_gain_range(channel) if is_tx else usrp.get_rx_gain_range(channel)
    available_gains = np.arange(gain_range.start(), gain_range.stop()+gain_range.step(), gain_range.step())
    if not np.any(np.isclose(gain, available_gains)):
        mode = "TX" if is_tx else "RX"
        closest_gains = find_closest_values(gain, available_gains)
        if len(closest_gains) == 1:
            raise ValueError(f"Invalid {mode} gain {gain} dB. Closest available gain: {closest_gains[0]} dB")
        else:
            raise ValueError(f"Invalid {mode} gain {gain} dB. Closest available gains: {closest_gains[0]} dB, {closest_gains[1]} dB")


def validate_antenna(usrp, antenna, channel, is_tx=False):
    """Validate requested antenna against USRP capabilities."""
    available_antennas = usrp.get_tx_antennas(channel) if is_tx else usrp.get_rx_antennas(channel)
    antenna_type = "TX" if is_tx else "RX"
    valid_antennas = [a for a in available_antennas if antenna_type in a.upper()]

    if antenna not in valid_antennas:
        # For antennas, show all valid options since there are typically only a few
        antenna_list = ", ".join(f"'{a}'" for a in valid_antennas)
        raise ValueError(f"Invalid antenna '{antenna}' for channel {channel}. Available {antenna_type} antennas: {antenna_list}")


def change_time_and_clock_source(usrp, mboard=0, source="internal",
                                lock_timeout=DEFAULT_LOCK_TIMEOUT,
                                poll_period=DEFAULT_POLL_PERIOD):
    """Change time and clock source with GPSDO lock verification."""
    source = str(source).lower()
    if source not in ("internal", "gpsdo"):
        raise ValueError(f"Source not supported: {source!r}")

    current_clock_source = usrp.get_clock_source(mboard)
    current_time_source = usrp.get_time_source(mboard)

    if source == current_clock_source and source == current_time_source:
        return  # No change needed


    # If switching to GPSDO, validate availability first
    if source == "gpsdo":
        # Check if GPSDO is available before making any changes
        try:
            usrp.get_mboard_sensor("gps_locked", mboard)
        except Exception:
            raise RuntimeError("GPSDO not available on this device")

        # Check if PPS sensor is available
        pps_sensor_available = False
        try:
            usrp.get_mboard_sensor("pps_detected", mboard)
            pps_sensor_available = True
        except Exception:
            pass

    # Now make the actual changes
    usrp.set_clock_source(source, mboard)
    usrp.set_time_source(source, mboard)

    # If we switched to GPSDO, wait for lock
    if source == "gpsdo":
        gps_locked = False
        reference_locked = False
        pps_detected = True  # Default for when sensor not available

        t0 = time.time()
        while time.time() - t0 < lock_timeout:
            gps_locked = get_bool_sensor_state(usrp, "gps_locked", mboard)
            reference_locked = get_bool_sensor_state(usrp, "ref_locked", mboard)

            # Check all required conditions
            all_locked = gps_locked and reference_locked
            if pps_sensor_available:
                pps_detected = get_bool_sensor_state(usrp, "pps_detected", mboard)
                all_locked = all_locked and pps_detected

            if all_locked:
                break
            else:
                time.sleep(poll_period)

        # Use the last values from the loop - no need to poll again
        final_check = gps_locked and reference_locked
        if pps_sensor_available:
            final_check = final_check and pps_detected

        if not final_check:
            # Restore previous settings (time source first to avoid warnings)
            usrp.set_time_source(current_time_source, mboard)
            usrp.set_clock_source(current_clock_source, mboard)

            raise RuntimeError(
                f"Timeout waiting for GPSDO lock. GPS LOCKED: {gps_locked}, REF LOCKED: {reference_locked}, "
                f"PPS DETECTED: {pps_detected}. Clock and time source restored to previous values."
            )


# ---------------------------------------------------------------------------
#  Device-type helpers
# ---------------------------------------------------------------------------

# Device type prefixes that use the network (Ethernet / 100GigE) transport.
# Everything else is assumed to be USB (B2xx family).
_NETWORK_PREFIXES = ("x3", "x4", "n2", "n3")


def _is_network_device(device_type: str) -> bool:
    """Return True for X3xx/X4xx/N2xx/N3xx (network); False for B2xx (USB)."""
    return device_type.strip().lower().startswith(_NETWORK_PREFIXES)


def _build_usrp_args(usrp_addr: str,
                     device_type: str,
                     mcr: float,
                     buffer_scale: float,
                     mgmt_addr: str = None,
                     use_dpdk: bool = False) -> str:
    """Build a UHD device-args string that works for both USB and network USRPs.

    Address auto-prefixing:
      - If *usrp_addr* already contains a key (``serial=``, ``addr=``,
        ``name=``, ``resource=``), it is passed through unchanged.
      - Otherwise, network devices get ``addr=<value>`` and USB devices get
        ``serial=<value>`` (an empty string means "first available device").

    Buffer/frame-size tuning is applied only for network devices.  USB devices
    (B210, B200mini, …) work best with UHD defaults and have no concept of
    Ethernet frame sizing or DPDK.
    """
    dtype = device_type.strip().lower()
    network = _is_network_device(dtype)

    # --- address prefix ----------------------------------------------------
    addr = (usrp_addr or "").strip()
    _known_keys = ("serial=", "addr=", "name=", "resource=")
    if addr and not any(addr.startswith(k) for k in _known_keys):
        addr = f"addr={addr}" if network else f"serial={addr}"

    parts: list[str] = []
    if addr:
        parts.append(addr)
    parts.append(f"type={dtype}")

    # --- network-specific tuning -------------------------------------------
    if network:
        scaled_buff = min(int(268_435_456 * buffer_scale), MAX_NET_BUFF_SIZE)
        scaled_frames = int(512 * buffer_scale)
        scaled_mbufs  = int(8_192 * buffer_scale)

        # Network kernels can reject oversized buffers without DPDK
        if not use_dpdk and scaled_buff > MAX_NET_BUFF_SIZE:
            logging.warning(
                f"Buffer size {scaled_buff} exceeds kernel limit "
                f"{MAX_NET_BUFF_SIZE}; clamping. Use DPDK for higher scaling."
            )
            scaled_buff = MAX_NET_BUFF_SIZE

        parts += [
            "clock_source=internal",
            "time_source=internal",
        ]
        if mcr:
            parts.append(f"master_clock_rate={int(mcr)}")
        parts += [
            f"send_buff_size={scaled_buff}",
            f"recv_buff_size={scaled_buff}",
            "send_frame_size=8958",      # max for 100GigE
            "recv_frame_size=8958",
            f"num_recv_frames={scaled_frames}",
            f"num_send_frames={scaled_frames}",
        ]
        if mgmt_addr:
            parts.append(f"mgmt_addr={mgmt_addr}")
        if use_dpdk:
            parts.append("use_dpdk=1")
            parts.append(f"dpdk_num_mbufs={scaled_mbufs}")
    else:
        # USB device (B210, B200, B200mini, …)
        # No Ethernet frame/buffer tuning, no DPDK.  Pass MCR only if the
        # caller explicitly set one (0 / None → let UHD choose).
        if mcr:
            parts.append(f"master_clock_rate={int(mcr)}")

    return ",".join(parts)


class BaseUSRPDaemon:
    """Base class for USRP daemons with common functionality."""

    def __init__(self, usrp_addr, mgmt_addr=None, mcr=None,
                 device_type="x4xx", use_dpdk=False, buffer_scale=1.0):

        usrp_args = _build_usrp_args(
            usrp_addr=usrp_addr,
            device_type=device_type,
            mcr=mcr or 0,
            buffer_scale=buffer_scale,
            mgmt_addr=mgmt_addr,
            use_dpdk=use_dpdk,
        )

        logging.info(f"Opening USRP with args: {usrp_args}")

        try:
            self.usrp = uhd.usrp.MultiUSRP(usrp_args)
            logging.info("USRP device opened successfully")
        except Exception as e:
            logging.error("Failed to open USRP device. Error: %s", e)
            raise

        self._op_lock = threading.Lock()
        self._stop = threading.Event()
        self._context = zmq.Context()
        self._publisher = None

    @contextmanager
    def _op_guard(self, op_name: str):
        """Context manager for operation locking."""
        if not self._op_lock.acquire(blocking=False):
            raise RuntimeError(f"{op_name} denied: device busy (another operation is in progress).")
        try:
            yield
        finally:
            self._op_lock.release()

    def get_device_time(self):
        """Get current USRP device time."""
        return self.usrp.get_time_now().get_real_secs()

    def change_time_and_clock_source(self, mboard=0, source="internal",
                                   lock_timeout=DEFAULT_LOCK_TIMEOUT,
                                   poll_period=DEFAULT_POLL_PERIOD):
        """Change time and clock source with operation locking."""
        with self._op_guard("change_time_and_clock_source"):
            change_time_and_clock_source(self.usrp, mboard, source, lock_timeout, poll_period)

    def close(self):
        """Clean up resources."""
        self._stop.set()
        if self._publisher:
            try:
                self._publisher.close(linger=0)
            except zmq.ZMQError as e:
                logging.warning(f"Error closing publisher socket: {e}")
        try:
            self._context.term()
        except zmq.ZMQError as e:
            logging.warning(f"Error terminating context: {e}")


def check_settings_mismatch(actual_settings, requested_params):
    """Check for mismatches between actual and requested USRP settings."""
    mismatches = {}

    for ch, settings in actual_settings.items():
        differences = {}

        if not np.isclose(settings["fs"], requested_params["fs"], rtol=0, atol=FS_ATOL):
            differences["fs"] = (settings["fs"], requested_params["fs"])

        if not np.isclose(settings["fc"], requested_params["fc"], rtol=0, atol=FC_ATOL):
            differences["fc"] = (settings["fc"], requested_params["fc"])

        # Handle different gain parameter names and values per channel
        if "G_RX" in settings:
            requested_gain = requested_params["G_RX"]
            if requested_gain is not None and not np.isclose(settings["G_RX"], requested_gain, rtol=0, atol=G_ATOL):
                differences["G_RX"] = (settings["G_RX"], requested_gain)
        elif "G_TX" in settings:
            # TX gain: use dict with channel as key
            requested_gain = requested_params["G_TX"].get(ch)
            if requested_gain is not None and not np.isclose(settings["G_TX"], requested_gain, rtol=0, atol=G_ATOL):
                differences["G_TX"] = (settings["G_TX"], requested_gain)

        # Antenna may have been requested as a single string (broadcast to
        # every channel) or a per-channel dict. Resolve to the value that
        # was actually asked for on THIS channel before comparing.
        req_ant = requested_params["antenna"]
        if isinstance(req_ant, dict):
            req_ant = req_ant.get(ch, req_ant.get(str(ch)))
        if settings["antenna"] != req_ant:
            differences["antenna"] = (settings["antenna"], req_ant)

        if differences:
            mismatches[ch] = differences

    return mismatches