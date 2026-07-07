"""
Central config resolution for USRP-relevant parameters.

Lookup order: host .env file → os.environ (container startup) → default.

The host .env file is mounted at /app/host.env (read-write). The Settings
UI writes changes directly to that file. os.environ reflects the values
from container startup and serves as a read-only fallback for keys not yet
written by the UI.
"""
import os
import threading
import time
from typing import Any
from sqlalchemy.orm import Session
from models import SettingOverride

_ENV_FILE = "/app/.env"
_file_cache: dict = {}
_file_cache_ts: float = 0.0
_file_lock = threading.Lock()
_CACHE_TTL = 2.0  # seconds


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
                # strip inline comment (only when preceded by whitespace)
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


def _write_env_key(key: str, value: str) -> None:
    with _file_lock:
        try:
            with open(_ENV_FILE) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        prefix = f"{key}="
        found = False
        new_lines = []
        for line in lines:
            if line.lstrip().startswith(prefix) or line.lstrip().startswith(f"{key} ="):
                new_lines.append(f"{key}={value}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{key}={value}\n")

        with open(_ENV_FILE, "w") as f:
            f.writelines(new_lines)

        # invalidate cache and update os.environ immediately
        _file_cache[key] = value
        _file_cache_ts = time.monotonic()
        os.environ[key] = value


# Which keys can be overridden via the admin UI? Each spec lists type and description.
#
# Special keys:
#   "options"  - fixed list of allowed values; UI renders a <select>, not free text.
#   "hidden"   - managed automatically (e.g. derived from another setting); not shown.
#
# MASTER_CLOCK_RATE is intentionally NOT exposed: it is hardware-bound (X410
# requires a specific value) and changing it would break the radio chain.
# It is still resolvable via settings_store.get() from .env / defaults, just not
# editable from the UI.
EDITABLE_KEYS: dict[str, dict] = {
    # Radio
    # Carrier is the centre of the SRD band by default. Visible in the UI
    # but locked - change via .env CARRIER_FREQUENCY_HZ if a non-centred
    # carrier is required.
    "CARRIER_FREQUENCY_HZ": {"type": "int", "group": "radio",
        "label": "Carrier Frequency",
        "locked": True,
        "desc": "Centre of the transmitted band - defaults to the middle "
                "of the SRD band (2441.75 MHz). The signal occupies "
                "carrier ± bandwidth/2."},
    "BANDWIDTH_HZ":         {"type": "int", "group": "radio",
        "label": "Bandwidth",
        "locked": True,
        "desc": "Analog anti-aliasing filter at the USRP front-end. "
                "Set in .env so the configured carrier ± bandwidth/2 "
                "always fits inside the SRD band. The actual sample rate "
                "is bandwidth × ratio (see button below)."},
    "SAMPLE_RATE_BANDWIDTH_RATIO": {"type": "int", "group": "radio",
        "label": "Sample / Bandwidth Ratio",
        "min": 1, "max": 5,
        "desc": "Multiplier from bandwidth to sample rate. Click the button "
                "to cycle 1× → 2× → … → 5× → 1×. Higher ratios cost "
                "proportionally more network throughput."},
    # SAMPLE_RATE_HZ is computed from BANDWIDTH_HZ × ratio on save.
    "SAMPLE_RATE_HZ":       {"type": "int", "group": "radio",
        "label": "Sample Rate",
        "desc": "Derived: bandwidth × ratio.",
        "hidden": True},
    "TX_GAIN_DB":           {"type": "float", "group": "radio",
        "label": "TX Gain (dB)",
        "desc": "Sendeverstärkung. B210: Range 0-89.75 dB. "
                "Höher = mehr Ausgangsleistung. Bei B210 ist das die einzige "
                "Möglichkeit, die Leistung zu steuern (keine kalibrierte Power-API)."},
    "RX_GAIN_DB":           {"type": "float", "group": "radio",
        "label": "RX Gain (dB)",
        "desc": "Receive gain. Too high → saturation, too low → noise."},
    # Antennas and per-channel gains live in the Hardware-Inventory page now.
    # The legacy ANTENNA_TX / ANTENNA_RX env vars only act as a fallback when
    # no inventory is configured at all.

    # MIMO
    "MIMO_ENABLED":         {"type": "bool",  "group": "radio",
        "label": "MIMO Enabled",
        "desc": "Allow students to send multi-channel (MIMO) signals. When "
                "off, the server rejects any upload that announces mode=mimo "
                "and only accepts single-channel SISO."},
    "MIMO_MAX_CHANNELS":    {"type": "int",   "group": "radio",
        "label": "MIMO Max Channels",
        "min": 1, "max": 4,
        "disabled_when": {"key": "MIMO_ENABLED", "equals": False},
        "desc": "Maximum number of channels accepted in a MIMO upload. "
                "B210 has 2 TX/RX paths, so 2 is the natural cap."},

    # Guard (random uniform)
    "BEGIN_GUARD_MIN_SEC":  {"type": "float", "group": "guard",
        "label": "Begin Guard Min",
        "desc": "Minimum pause before transmission starts (random ∈ [min, max]). Avoids click artefacts."},
    "BEGIN_GUARD_MAX_SEC":  {"type": "float", "group": "guard",
        "label": "Begin Guard Max",
        "desc": "Maximum pause before transmission starts."},
    "END_GUARD_MIN_SEC":    {"type": "float", "group": "guard",
        "label": "End Guard Min",
        "desc": "Minimum pause after transmission ends (reverb / receiver latency)."},
    "END_GUARD_MAX_SEC":    {"type": "float", "group": "guard",
        "label": "End Guard Max",
        "desc": "Maximum pause after transmission ends."},
    "INITIAL_DELAY":        {"type": "float", "group": "guard",
        "label": "Initial Delay",
        "desc": "Delay between task start and the first sample."},

    # Duty cycle
    "DUTY_CYCLE_ENABLED":     {"type": "bool",  "group": "safety",
        "label": "Duty Cycle Enabled",
        "desc": "Master switch for duty-cycle limiting. When off, the worker "
                "transmits without a duty quota - only enable for lab "
                "experiments where regulation does not apply."},
    "DUTY_CYCLE_MAX_PERCENT": {"type": "float", "group": "safety",
        "label": "Max Duty Cycle",
        "disabled_when": {"key": "DUTY_CYCLE_ENABLED", "equals": False},
        "desc": "Maximum fraction of active transmit time within the window. ETSI-compliant value is typically 10%."},
    "DUTY_CYCLE_WINDOW_SEC":  {"type": "float", "group": "safety",
        "label": "Duty Cycle Window",
        "disabled_when": {"key": "DUTY_CYCLE_ENABLED", "equals": False},
        "desc": "Time window over which duty cycle is measured (typically 60 s)."},

    # LBT
    "LBT_ENABLED":        {"type": "bool",  "group": "safety",
        "label": "Listen Before Talk",
        "desc": "Sense the channel before transmitting. Recommended for ISM-band compliance."},
    "LBT_THRESHOLD_DBFS": {"type": "float", "group": "safety",
        "label": "LBT Threshold",
        "disabled_when": {"key": "LBT_ENABLED", "equals": False},
        "desc": "Power threshold above which the channel is considered busy."},
    "LBT_SENSE_SAMPLES":  {"type": "int",   "group": "safety",
        "label": "LBT Sense Samples",
        "disabled_when": {"key": "LBT_ENABLED", "equals": False},
        "desc": "Number of samples used for the power average."},
    "LBT_MAX_RETRIES":    {"type": "int",   "group": "safety",
        "label": "LBT Max Retries",
        "disabled_when": {"key": "LBT_ENABLED", "equals": False},
        "desc": "How often to retry if the channel is busy - after that, error out."},
    "LBT_BACKOFF_SEC":    {"type": "float", "group": "safety",
        "label": "LBT Backoff",
        "disabled_when": {"key": "LBT_ENABLED", "equals": False},
        "desc": "Wait time between LBT attempts (random uniform)."},

    # Limits
    "MAX_UPLOAD_MB":      {"type": "int", "group": "limits",
        "label": "Max Upload",
        "desc": "Maximum signal size per upload. Larger uploads are rejected by the server."},
    "MAX_SAMPLES":        {"type": "int", "group": "limits",
        "label": "Max Samples",
        "desc": "Maximum number of IQ samples per task. Protects against excessively long captures."},
    "TASK_TTL_HOURS":     {"type": "int", "group": "limits",
        "label": "Task TTL",
        "desc": "How long task files (input/output) stay on disk before they get cleaned up."},
    "MAX_QUEUE":          {"type": "int", "group": "limits",
        "label": "Max Queue Size",
        "desc": "Total cap of running + waiting tasks. Additional clients get 'queue full' (HTTP 403) immediately."},
    "MAX_QUEUE_PER_IP":   {"type": "int", "group": "limits",
        "label": "Max Queue per IP",
        "desc": "Maximum concurrent tasks per client IP. Stops one spammer from blocking other clients."},
    "UPLOAD_TIMEOUT_SEC": {"type": "float", "group": "limits",
        "label": "Upload Timeout",
        "desc": "How long the server waits for signal data after the WebSocket connect before releasing the slot (slow-loris guard)."},
    "POLL_INTERVAL_SEC":  {"type": "float", "group": "limits",
        "label": "Status-Poll Interval",
        "desc": "How often the server pushes queue-status updates while the task is waiting."},
}


def _coerce(value: str, type_: str) -> Any:
    if type_ == "int":
        return int(value)
    if type_ == "float":
        return float(value)
    if type_ == "bool":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return value


def get(db: Session, key: str, default: Any = None) -> Any:
    """Get value: host .env file → os.environ → default."""
    spec = EDITABLE_KEYS.get(key)
    type_ = spec["type"] if spec else "str"

    if spec and spec.get("locked"):
        return _locked_value(key)

    # 1. host .env file (live, written by the Settings UI)
    file_val = _file_get(key)
    if file_val is not None:
        return _coerce(file_val, type_)

    # 2. os.environ (container startup values - read-only fallback)
    env_val = os.getenv(key)
    if env_val is not None:
        return _coerce(env_val, type_)

    return default


# Display defaults - used by the Settings UI when no DB override / .env value
# is set, so that fields show a real number instead of an empty string (which
# also confuses the dirty-tracking comparison).
_DISPLAY_DEFAULTS = {
    "SAMPLE_RATE_BANDWIDTH_RATIO": 2,
}


def all_current(db: Session) -> dict:
    """Snapshot of all editable settings, with their current effective values.

    Locked fields bypass any DB override:
    - CARRIER_FREQUENCY_HZ is always computed as the centre of the SRD band
      (so carrier ± bandwidth/2 stays inside the legal range).
    - All other locked fields read from .env / built-in defaults only.
    """
    out = {}
    for key, spec in EDITABLE_KEYS.items():
        if spec.get("locked"):
            value = _locked_value(key)
            source = "computed" if key == "CARRIER_FREQUENCY_HZ" else "env"
        else:
            value = get(db, key, default=_DISPLAY_DEFAULTS.get(key, ""))
            source = _source(db, key)
        out[key] = {**spec, "value": value, "source": source}
    return out


def _locked_value(key: str):
    """Resolve a locked field's value live from .env (file → os.environ)."""
    if key == "CARRIER_FREQUENCY_HZ":
        band = srd_band_info()
        return (int(band["f_min_hz"]) + int(band["f_max_hz"])) // 2
    spec = EDITABLE_KEYS.get(key, {})
    type_ = spec.get("type", "str")
    raw = _file_get(key)
    if raw is None:
        raw = os.getenv(key, "")
    if raw == "" or raw is None:
        defaults = {
            "BANDWIDTH_HZ": 15_625_000,
            "TX_POWER_DBM": 7.5,
        }
        return defaults.get(key, "")
    return _coerce(raw, type_)


def _source(db: Session, key: str) -> str:
    if _file_get(key) is not None:
        return "env"
    if os.getenv(key) is not None:
        return "env"
    return "default"


def set_override(db: Session, key: str, value: str) -> None:
    """Write setting directly to the host .env file."""
    if key not in EDITABLE_KEYS:
        raise ValueError(f"Setting '{key}' is not editable")
    _coerce(value, EDITABLE_KEYS[key]["type"])
    _write_env_key(key, value)


def clear_override(db: Session, key: str) -> None:
    """Reset a setting by removing it from the host .env file."""
    with _file_lock:
        try:
            with open(_ENV_FILE) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return
        prefix = f"{key}="
        new_lines = [l for l in lines
                     if not l.lstrip().startswith(prefix)
                     and not l.lstrip().startswith(f"{key} =")]
        with open(_ENV_FILE, "w") as f:
            f.writelines(new_lines)
        _file_cache.pop(key, None)


def purge_locked_overrides(db: Session) -> int:
    """Drop every DB override for fields that are flagged `locked: True`.

    Locked fields must come from .env / built-ins (or the band-centre
    formula for the carrier). Stale DB overrides from before the lock
    was added would otherwise still leak through `get()` and cause a
    mismatch between the Settings UI and the live /info / worker.

    Called once on entrypoint startup.
    """
    locked = [k for k, spec in EDITABLE_KEYS.items() if spec.get("locked")]
    if not locked:
        return 0
    n = (db.query(SettingOverride)
           .filter(SettingOverride.key.in_(locked))
           .delete(synchronize_session=False))
    db.commit()
    return int(n)


# ---------------- 2.4 GHz SRD band info (RIR1008-11 / EN 300 440) -----------
# License-exempt non-specific SRD: 2400-2483.5 MHz, 10 mW EIRP, no duty cycle,
# no LBT. The numbers below feed the read-only info banner on the Settings
# page; values are sourced from the .env with sensible fallbacks.

def _live_get(name: str) -> str:
    """Read a value live from the .env file, falling back to os.environ."""
    val = _file_get(name)
    if val is None:
        val = os.getenv(name)
    return (val or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = _live_get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _live_get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def srd_band_info() -> dict:
    return {
        "label":      _live_get("SRD_BAND_LABEL")
                      or "2.4 GHz SRD - RIR1008-11 (Non-specific SRD)",
        "f_min_hz":   _env_int("SRD_BAND_F_MIN_HZ", 2_400_000_000),
        "f_max_hz":   _env_int("SRD_BAND_F_MAX_HZ", 2_483_500_000),
        "max_eirp_dbm": _env_float("SRD_MAX_EIRP_DBM", 10.0),
        "duty_cycle_pct": None,        # not regulated on 2.4 GHz SRD
        "lbt_required":   False,
        "note":       _live_get("SRD_NOTE"),
    }
