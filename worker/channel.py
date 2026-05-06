import os
import threading
import time
import random
import logging
import numpy as np

try:
    from usrp_testbed_library.constants import (
        DEFAULT_TX_REP_PORT,
        DEFAULT_RX_REP_PORT,
    )
except ImportError:
    DEFAULT_TX_REP_PORT = 5557
    DEFAULT_RX_REP_PORT = 5555

logger = logging.getLogger("channel")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)

# ---- .env file reader (live, picks up UI changes without restart) -----------
_ENV_FILE = "/app/.env"
_file_cache: dict = {}
_file_cache_ts: float = 0.0
_file_lock = threading.Lock()
_CACHE_TTL = 2.0


def _parse_env_file(path: str) -> dict:
    result = {}
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key, _, rest = stripped.partition("=")
                key = key.strip()
                if " #" in rest:
                    rest = rest[:rest.index(" #")]
                result[key] = rest.strip()
    except FileNotFoundError:
        pass
    return result


def _file_get(key: str) -> str | None:
    global _file_cache, _file_cache_ts
    now = time.monotonic()
    with _file_lock:
        if now - _file_cache_ts > _CACHE_TTL:
            _file_cache = _parse_env_file(_ENV_FILE)
            _file_cache_ts = now
        return _file_cache.get(key)


_LOCKED_KEYS = {"CARRIER_FREQUENCY_HZ", "BANDWIDTH_HZ"}
_LOCKED_CARRIER_HZ = 2_441_750_000

# B210 with auto MCR lands on 32 MHz; valid rates are 32/integer.
# Use a per-device-type SAMPLE_RATE_HZ_<DTYPE> override before falling
# back to SAMPLE_RATE_HZ (default 25 MHz suits X4xx but not B210).
_B200_DEFAULT_SAMPLE_RATE = 16_000_000   # 32 MHz MCR ÷ 2


def _get_sample_rate() -> float:
    dtype = (_file_get("USRP_DEVICE_TYPE") or os.getenv("USRP_DEVICE_TYPE", "x4xx")).lower()
    dtype_key = dtype.upper().replace("-", "_")
    specific = _file_get(f"SAMPLE_RATE_HZ_{dtype_key}") or os.getenv(f"SAMPLE_RATE_HZ_{dtype_key}")
    if specific:
        return float(specific)
    default = _B200_DEFAULT_SAMPLE_RATE if dtype.startswith("b2") else 25_000_000
    return _get("SAMPLE_RATE_HZ", default, float)


def _get(key: str, default, type_=str):
    if key in _LOCKED_KEYS:
        if key == "CARRIER_FREQUENCY_HZ":
            val = _LOCKED_CARRIER_HZ
        else:
            val = _file_get(key) or os.getenv(key)
    else:
        val = _file_get(key) or os.getenv(key)
    if val is None or val == "":
        return default
    if type_ is int:   return int(float(val))
    if type_ is float: return float(val)
    if type_ is bool:  return str(val).strip().lower() in ("1","true","yes","on")
    return val


def _get_guard() -> tuple[float, float]:
    """Draw a random guard time from uniform distributions."""
    b_min = _get("BEGIN_GUARD_MIN_SEC", 0.1, float)
    b_max = _get("BEGIN_GUARD_MAX_SEC", b_min, float)
    e_min = _get("END_GUARD_MIN_SEC", 0.1, float)
    e_max = _get("END_GUARD_MAX_SEC", e_min, float)
    begin = random.uniform(min(b_min, b_max), max(b_min, b_max))
    end   = random.uniform(min(e_min, e_max), max(e_min, e_max))
    return begin, end


# ---- Static (container-only) values ----------------------------------------
ANTENNA_TX = _get("ANTENNA_TX", "TX/RX0")
ANTENNA_RX = _get("ANTENNA_RX", "RX1")
TX_CHANNEL = int(os.getenv("TX_CHANNEL", "0"))
RX_CHANNEL = int(os.getenv("RX_CHANNEL", "0"))

TX_DAEMON_HOST = os.getenv("TX_DAEMON_HOST", "host.docker.internal")
TX_DAEMON_PORT = int(os.getenv("TX_DAEMON_PORT", "5557"))
RX_DAEMON_HOST = os.getenv("RX_DAEMON_HOST", "host.docker.internal")
RX_DAEMON_PORT = int(os.getenv("RX_DAEMON_PORT", "5555"))

SIGNAL_DIR = os.getenv("SIGNAL_DIR", "/data/signals")
SIGNAL_DIR_HOST = os.getenv("SIGNAL_DIR_HOST", SIGNAL_DIR)

OPERATION_TIMEOUT_MARGIN = 5.0

CONNECTIVITY_TIMEOUT_MS = 2000
CONFIGURE_TIMEOUT_MS = 5000
SIGNAL_LOAD_TIMEOUT_MS = 10000


class USRPChannel:

    def __init__(self):
        self._tx_context = None
        self._tx_req = None
        self._rx_context = None
        self._rx_req = None
        self._configured = False
        self._tx_history = []

    def _connect(self):
        if self._tx_req is not None:
            return

        import zmq

        self._tx_context = zmq.Context()
        self._tx_req = self._tx_context.socket(zmq.REQ)
        self._tx_req.connect(f"tcp://{TX_DAEMON_HOST}:{TX_DAEMON_PORT}")
        self._tx_req.setsockopt(zmq.RCVTIMEO, CONNECTIVITY_TIMEOUT_MS)

        self._rx_context = zmq.Context()
        self._rx_req = self._rx_context.socket(zmq.REQ)
        self._rx_req.connect(f"tcp://{RX_DAEMON_HOST}:{RX_DAEMON_PORT}")
        self._rx_req.setsockopt(zmq.RCVTIMEO, CONNECTIVITY_TIMEOUT_MS)

        self._tx_req.send_json({"op": "PING"})
        resp = self._tx_req.recv_json()
        if resp.get("status") != "OK":
            raise RuntimeError(f"TX daemon ping failed: {resp}")

        self._rx_req.send_json({"op": "PING"})
        resp = self._rx_req.recv_json()
        if resp.get("status") != "OK":
            raise RuntimeError(f"RX daemon ping failed: {resp}")

        logger.info("Connected to TX daemon at %s:%s", TX_DAEMON_HOST, TX_DAEMON_PORT)
        logger.info("Connected to RX daemon at %s:%s", RX_DAEMON_HOST, RX_DAEMON_PORT)

        os.makedirs(SIGNAL_DIR, exist_ok=True)

    def _configure(self):
        """(Re-)configure both USRPs with current settings. Always runs so that
        changes made in the admin panel propagate to the hardware."""
        import zmq

        self._connect()

        fs = _get_sample_rate()
        fc = _get("CARRIER_FREQUENCY_HZ", 2_400_000_000, float)
        # TX_POWER_DBM (calibrated absolute output) wins over TX_GAIN_DB
        # (relative). Both are forwarded so the daemon can pick power-API
        # when supported and fall back to gain otherwise.
        tx_power_dbm = _get("TX_POWER_DBM", None, float)
        tx_gain = _get("TX_GAIN_DB", 30.0, float)
        rx_gain = _get("RX_GAIN_DB", 30.0, float)
        antenna_tx = _get("ANTENNA_TX", "TX/RX0")
        antenna_rx = _get("ANTENNA_RX", "RX1")

        tx_cmd = {
            "op": "CONFIGURE_USRP",
            "fs": fs, "fc": fc,
            "sync_channels": [TX_CHANNEL],
            "intf_channels": [],
            "G_TX": {str(TX_CHANNEL): tx_gain},
            "antenna": antenna_tx,
        }
        if tx_power_dbm is not None:
            tx_cmd["P_TX_DBM"] = {str(TX_CHANNEL): float(tx_power_dbm)}
        self._tx_req.setsockopt(zmq.RCVTIMEO, CONFIGURE_TIMEOUT_MS)
        self._tx_req.send_json(tx_cmd)
        resp = self._tx_req.recv_json()
        status = resp.get("status")
        if status == "ERROR":
            raise RuntimeError(f"TX configure failed: {resp.get('error')}")
        if status == "MISMATCH":
            # Actual USRP settings differ from what we requested → downstream
            # timing/frequency math would silently be wrong. Abort loudly.
            raise RuntimeError(
                f"TX configure mismatch — the USRP could not apply the requested "
                f"settings exactly. Mismatches: {resp.get('mismatches')}"
            )
        # >>> DIAGNOSTIC (3x-frequency-bug) — REMOVE WHEN DONE
        logger.info("[DIAG] TX configure actual: %s", resp.get("settings"))
        # <<< DIAGNOSTIC

        rx_cmd = {
            "op": "CONFIGURE_USRP",
            "fs": fs, "fc": fc,
            "channels": [RX_CHANNEL],
            "G_RX": rx_gain,
            "antenna": antenna_rx,
        }
        self._rx_req.setsockopt(zmq.RCVTIMEO, CONFIGURE_TIMEOUT_MS)
        self._rx_req.send_json(rx_cmd)
        resp = self._rx_req.recv_json()
        status = resp.get("status")
        if status == "ERROR":
            raise RuntimeError(f"RX configure failed: {resp.get('error')}")
        if status == "MISMATCH":
            raise RuntimeError(
                f"RX configure mismatch — the USRP could not apply the requested "
                f"settings exactly. Mismatches: {resp.get('mismatches')}"
            )
        # >>> DIAGNOSTIC (3x-frequency-bug) — REMOVE WHEN DONE
        logger.info("[DIAG] RX configure actual: %s  (requested fs=%s, fc=%s)",
                    resp.get("settings"), fs, fc)
        # <<< DIAGNOSTIC

        self._configured = True
        self._current_fs = fs

    def _to_host_path(self, container_path):
        return str(container_path).replace(SIGNAL_DIR, SIGNAL_DIR_HOST, 1)

    def _check_duty_cycle(self, signal_duration):
        if not _get("DUTY_CYCLE_ENABLED", True, bool):
            return True, 0.0
        duty_max = _get("DUTY_CYCLE_MAX_PERCENT", 10.0, float) / 100.0
        duty_window = _get("DUTY_CYCLE_WINDOW_SEC", 60.0, float)
        now = time.time()
        cutoff = now - duty_window
        self._tx_history = [(t, d) for t, d in self._tx_history if t > cutoff]
        total_tx_time = sum(d for _, d in self._tx_history)
        max_allowed = duty_max * duty_window
        available = max_allowed - total_tx_time

        if signal_duration > available:
            wait_time = signal_duration - available + 1.0
            logger.warning(
                "Duty cycle limit: %.2fs used / %.2fs allowed in %ds window. "
                "Need to wait %.1fs",
                total_tx_time, max_allowed, int(duty_window), wait_time
            )
            return False, wait_time
        logger.info(
            "Duty cycle OK: %.2fs used + %.4fs new / %.2fs allowed",
            total_tx_time, signal_duration, max_allowed
        )
        return True, 0.0

    def _listen_before_talk(self):
        if not _get("LBT_ENABLED", True, bool):
            return

        import zmq
        import h5py

        sense_samples = _get("LBT_SENSE_SAMPLES", 200000, int)
        threshold = _get("LBT_THRESHOLD_DBFS", -50.0, float)
        max_retries = _get("LBT_MAX_RETRIES", 10, int)
        backoff = _get("LBT_BACKOFF_SEC", 1.0, float)
        fs = _get_sample_rate()

        for attempt in range(max_retries):
            self._rx_req.setsockopt(zmq.RCVTIMEO, 5000)
            self._rx_req.send_json({"op": "FLUSH_RX"})
            self._rx_req.recv_json()

            sense_file = os.path.join(SIGNAL_DIR, "lbt_sense.h5")
            host_sense_file = self._to_host_path(sense_file)
            sense_duration = sense_samples / fs
            timeout_ms = int((sense_duration + OPERATION_TIMEOUT_MARGIN) * 1000)

            self._rx_req.setsockopt(zmq.RCVTIMEO, timeout_ms)
            self._rx_req.send_json({
                "op": "RECEIVE_TO_FILE",
                "n_samples": sense_samples,
                "path": host_sense_file,
                "delay": 0.5,
            })
            resp = self._rx_req.recv_json()

            if resp.get("status") != "OK":
                logger.warning("LBT sense failed: %s", resp.get("error"))
                time.sleep(backoff)
                continue

            try:
                with h5py.File(sense_file, "r") as f:
                    rx_data = f["rx_signal"][:]
                    if rx_data.ndim == 2:
                        rx_data = rx_data[0]
                    power = float(np.mean(np.abs(rx_data) ** 2))
                    power_dbfs = 10 * np.log10(power + 1e-20)
            except Exception as e:
                logger.warning("LBT: could not read sense file: %s", e)
                time.sleep(backoff)
                continue
            finally:
                try:
                    os.unlink(sense_file)
                except OSError:
                    pass

            logger.info("LBT sense: %.1f dBFS (threshold: %.1f dBFS)",
                        power_dbfs, threshold)

            if power_dbfs < threshold:
                logger.info("LBT: channel clear")
                return

            logger.warning("LBT: channel busy (attempt %d/%d), backing off %.1fs",
                           attempt + 1, max_retries, backoff)
            time.sleep(backoff)

        raise RuntimeError(
            f"Listen Before Talk failed: channel busy after {max_retries} retries"
        )

    def _reset_sockets(self):
        """Throw away the ZMQ REQ sockets — next call to `_connect` recreates
        them. Use when an exception leaves the REQ socket in an illegal state
        (sent without matching recv)."""
        import zmq
        for attr in ("_tx_req", "_rx_req"):
            sock = getattr(self, attr, None)
            if sock is not None:
                try:
                    sock.setsockopt(zmq.LINGER, 0)
                    sock.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._configured = False

    def send_and_receive(self, signal):
        import zmq
        import h5py

        try:
            self._configure()
        except Exception:
            self._reset_sockets()
            raise

        fs = _get_sample_rate()
        initial_delay = _get("INITIAL_DELAY", 1.0, float)
        begin_guard, end_guard = _get_guard()
        self.last_guard = (begin_guard, end_guard)

        n_samples = len(signal)
        signal_duration = n_samples / fs

        while True:
            allowed, wait_time = self._check_duty_cycle(signal_duration)
            if allowed:
                break
            time.sleep(wait_time)

        try:
            self._listen_before_talk()
        except Exception:
            self._reset_sockets()
            raise

        uid = f"{int(time.time() * 1000)}_{os.getpid()}"
        tx_file = os.path.join(SIGNAL_DIR, f"tx_{uid}.h5")
        rx_file = os.path.join(SIGNAL_DIR, f"rx_{uid}.h5")
        host_tx_file = self._to_host_path(tx_file)
        host_rx_file = self._to_host_path(rx_file)

        tx_cmd_sent = False
        rx_cmd_sent = False
        try:
            with h5py.File(tx_file, "w") as f:
                f.create_dataset("tx_signal", data=signal.astype(np.complex64))

            self._tx_req.setsockopt(zmq.RCVTIMEO, SIGNAL_LOAD_TIMEOUT_MS)
            self._tx_req.send_json({
                "op": "LOAD_SIGNAL",
                "sync_signal_path": host_tx_file
            })
            resp = self._tx_req.recv_json()
            if resp.get("status") != "OK":
                raise RuntimeError(f"TX LOAD_SIGNAL failed: {resp.get('error')}")

            signal_info = resp.get("signal_info", {})
            logger.info(
                "TX signal loaded: %d samples  |  guard: begin=%.4fs end=%.4fs",
                signal_info.get("total_samples", 0), begin_guard, end_guard
            )

            # RX starts `begin_guard` seconds before TX, continues `end_guard`
            # seconds after TX end.
            rx_start_delay = initial_delay
            tx_start_delay = initial_delay + begin_guard
            rx_duration = begin_guard + signal_duration + end_guard
            rx_samples_needed = int(np.round(rx_duration * fs))

            self._rx_req.setsockopt(zmq.RCVTIMEO, 5000)
            self._rx_req.send_json({"op": "FLUSH_RX"})
            self._rx_req.recv_json()

            rx_timeout_ms = int(
                (rx_start_delay + rx_duration + OPERATION_TIMEOUT_MARGIN) * 1000
            )
            self._rx_req.setsockopt(zmq.RCVTIMEO, rx_timeout_ms)
            self._rx_req.send_json({
                "op": "RECEIVE_TO_FILE",
                "n_samples": rx_samples_needed,
                "path": host_rx_file,
                "delay": rx_start_delay
            })
            rx_cmd_sent = True

            tx_timeout_ms = int(
                (tx_start_delay + signal_duration + OPERATION_TIMEOUT_MARGIN) * 1000
            )
            self._tx_req.setsockopt(zmq.RCVTIMEO, tx_timeout_ms)
            self._tx_req.send_json({
                "op": "TRANSMIT_BURST",
                "delay": tx_start_delay
            })
            tx_cmd_sent = True

            tx_resp = self._tx_req.recv_json()
            tx_cmd_sent = False
            if tx_resp.get("status") != "OK":
                raise RuntimeError(
                    f"TRANSMIT_BURST failed: {tx_resp.get('error')}"
                )
            logger.info("TX done: %d samples sent", tx_resp.get("samples_sent", 0))

            rx_resp = self._rx_req.recv_json()
            rx_cmd_sent = False
            if rx_resp.get("status") != "OK":
                raise RuntimeError(
                    f"RECEIVE_TO_FILE failed: {rx_resp.get('error')}"
                )
            logger.info(
                "RX done: %d samples received",
                rx_resp.get("samples_received", 0)
            )

            self._tx_history.append((time.time(), signal_duration))

            with h5py.File(rx_file, "r") as f:
                rx_data = f["rx_signal"][:]
                if rx_data.ndim == 2:
                    rx_data = rx_data[0]
                # >>> DIAGNOSTIC (3x-frequency-bug) — REMOVE WHEN DONE
                attrs = dict(f["rx_signal"].attrs)
                logger.info(
                    "[DIAG] RX file: %d samples, actual fs=%s, fc=%s "
                    "(configured fs=%s, requested rx_samples=%s, rx_duration=%.4fs)",
                    len(rx_data), attrs.get("fs"), attrs.get("fc"),
                    fs, rx_samples_needed, rx_duration,
                )
                # <<< DIAGNOSTIC

            return rx_data.astype(np.complex64)

        except Exception:
            # A command was sent but no reply consumed → REQ socket is in
            # illegal state for the next send. Drain what we can, then reset
            # anything still stuck so the next task can proceed.
            if tx_cmd_sent:
                try:
                    self._tx_req.setsockopt(zmq.RCVTIMEO, 1000)
                    self._tx_req.recv_json()
                except Exception:
                    pass
            if rx_cmd_sent:
                try:
                    self._rx_req.setsockopt(zmq.RCVTIMEO, 1000)
                    self._rx_req.recv_json()
                except Exception:
                    pass
            # If either drain failed the socket is still dirty — nuke it.
            self._reset_sockets()
            raise

        finally:
            for p in (tx_file, rx_file):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def close(self):
        import zmq
        for sock in (self._tx_req, self._rx_req):
            if sock:
                try:
                    sock.setsockopt(zmq.LINGER, 0)
                    sock.close()
                except Exception:
                    pass
        for ctx in (self._tx_context, self._rx_context):
            if ctx:
                try:
                    ctx.term()
                except Exception:
                    pass


_channel = None


def send_and_receive(signal):
    global _channel
    if _channel is None:
        _channel = USRPChannel()
    return _channel.send_and_receive(signal)
