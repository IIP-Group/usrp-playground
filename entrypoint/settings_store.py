"""
Central config resolution for USRP-relevant parameters.

Lookup order: DB override > .env > default.

Saving from the admin UI writes to the `setting_overrides` table. The
worker polls this table periodically / on the next task, so "reload" is
effectively immediate without a restart.
"""
import os
from typing import Any
from sqlalchemy.orm import Session
from models import SettingOverride


# Which keys can be overridden via the admin UI? Each spec lists type and description.
#
# Special keys:
#   "options"  — fixed list of allowed values; UI renders a <select>, not free text.
#   "hidden"   — managed automatically (e.g. derived from another setting); not shown.
#
# MASTER_CLOCK_RATE is intentionally NOT exposed: it is hardware-bound (X410
# requires a specific value) and changing it would break the radio chain.
# It is still resolvable via settings_store.get() from .env / defaults, just not
# editable from the UI.
EDITABLE_KEYS: dict[str, dict] = {
    # Radio
    "CARRIER_FREQUENCY_HZ": {"type": "int", "group": "radio",
        "label": "Carrier Frequency",
        "desc": "Carrier frequency used for transmit/receive. Pick from supported ISM/test bands.",
        "options": [
            {"value": 433_920_000,   "label": "433.92 MHz — ISM"},
            {"value": 868_000_000,   "label": "868 MHz — ISM"},
            {"value": 2_400_000_000, "label": "2.4 GHz — Wi-Fi / Bluetooth (default)"},
            {"value": 5_800_000_000, "label": "5.8 GHz — ISM"},
        ]},
    "SAMPLE_RATE_HZ":       {"type": "int", "group": "radio",
        "label": "Sample Rate (Hz)",
        "desc": "USRP sample rate. Bandwidth is locked to this value automatically."},
    # BANDWIDTH_HZ is auto-mirrored to SAMPLE_RATE_HZ on save (see admin_router).
    "BANDWIDTH_HZ":         {"type": "int", "group": "radio",
        "label": "Bandwidth (Hz)",
        "desc": "Analog anti-aliasing filter — locked to Sample Rate.",
        "hidden": True},
    "TX_GAIN_DB":           {"type": "float", "group": "radio",
        "label": "TX Gain (dB)",
        "desc": "USRP transmit gain. 0 = off, 30 = mid, ~70 = max."},
    "RX_GAIN_DB":           {"type": "float", "group": "radio",
        "label": "RX Gain (dB)",
        "desc": "Receive gain. Too high → saturation, too low → noise."},
    "CHANNEL_SNR_DB":       {"type": "float", "group": "radio",
        "label": "Sim. SNR (dB)",
        "desc": "Synthetic SNR in AWGN simulator mode (only when no real USRP is attached)."},
    "ANTENNA_TX":           {"type": "str",   "group": "radio",
        "label": "TX Antenna",
        "desc": "USRP antenna port used for transmit (e.g. TX/RX0)."},
    "ANTENNA_RX":           {"type": "str",   "group": "radio",
        "label": "RX Antenna",
        "desc": "USRP antenna port used for receive (e.g. RX1)."},

    # Guard (random uniform)
    "BEGIN_GUARD_MIN_SEC":  {"type": "float", "group": "guard",
        "label": "Begin Guard Min (s)",
        "desc": "Minimum pause before transmission starts (random ∈ [min, max]). Avoids click artefacts."},
    "BEGIN_GUARD_MAX_SEC":  {"type": "float", "group": "guard",
        "label": "Begin Guard Max (s)",
        "desc": "Maximum pause before transmission starts."},
    "END_GUARD_MIN_SEC":    {"type": "float", "group": "guard",
        "label": "End Guard Min (s)",
        "desc": "Minimum pause after transmission ends (reverb / receiver latency)."},
    "END_GUARD_MAX_SEC":    {"type": "float", "group": "guard",
        "label": "End Guard Max (s)",
        "desc": "Maximum pause after transmission ends."},
    "INITIAL_DELAY":        {"type": "float", "group": "guard",
        "label": "Initial Delay (s)",
        "desc": "Delay between task start and the first sample."},

    # Duty cycle
    "DUTY_CYCLE_MAX_PERCENT": {"type": "float", "group": "safety",
        "label": "Max Duty Cycle (%)",
        "desc": "Maximum fraction of active transmit time within the window. ETSI-compliant value is typically 10%."},
    "DUTY_CYCLE_WINDOW_SEC":  {"type": "float", "group": "safety",
        "label": "Duty Cycle Window (s)",
        "desc": "Time window over which duty cycle is measured (typically 60 s)."},

    # LBT
    "LBT_ENABLED":        {"type": "bool",  "group": "safety",
        "label": "Listen Before Talk",
        "desc": "Sense the channel before transmitting. Recommended for ISM-band compliance."},
    "LBT_THRESHOLD_DBFS": {"type": "float", "group": "safety",
        "label": "LBT Threshold (dBFS)",
        "desc": "Power threshold above which the channel is considered busy."},
    "LBT_SENSE_SAMPLES":  {"type": "int",   "group": "safety",
        "label": "LBT Sense Samples",
        "desc": "Number of samples used for the power average."},
    "LBT_MAX_RETRIES":    {"type": "int",   "group": "safety",
        "label": "LBT Max Retries",
        "desc": "How often to retry if the channel is busy — after that, error out."},
    "LBT_BACKOFF_SEC":    {"type": "float", "group": "safety",
        "label": "LBT Backoff (s)",
        "desc": "Wait time between LBT attempts (random uniform)."},

    # Limits
    "MAX_UPLOAD_MB":      {"type": "int", "group": "limits",
        "label": "Max Upload (MB)",
        "desc": "Maximum signal size per upload. Larger uploads are rejected by the server."},
    "MAX_SAMPLES":        {"type": "int", "group": "limits",
        "label": "Max Samples",
        "desc": "Maximum number of IQ samples per task. Protects against excessively long captures."},
    "TASK_TTL_HOURS":     {"type": "int", "group": "limits",
        "label": "Task TTL (h)",
        "desc": "How long task files (input/output) stay on disk before they get cleaned up."},
    "MAX_QUEUE":          {"type": "int", "group": "limits",
        "label": "Max Queue Size",
        "desc": "Total cap of running + waiting tasks. Additional clients get 'queue full' (HTTP 403) immediately."},
    "MAX_QUEUE_PER_IP":   {"type": "int", "group": "limits",
        "label": "Max Queue per IP",
        "desc": "Maximum concurrent tasks per client IP. Stops one spammer from blocking other clients."},
    "UPLOAD_TIMEOUT_SEC": {"type": "float", "group": "limits",
        "label": "Upload Timeout (s)",
        "desc": "How long the server waits for signal data after the WebSocket connect before releasing the slot (slow-loris guard)."},
    "POLL_INTERVAL_SEC":  {"type": "float", "group": "limits",
        "label": "Status-Poll Interval (s)",
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
    """Get value with override > env > default resolution."""
    spec = EDITABLE_KEYS.get(key)
    type_ = spec["type"] if spec else "str"

    # 1. DB override
    row = db.query(SettingOverride).filter(SettingOverride.key == key).first()
    if row is not None:
        return _coerce(row.value, type_)

    # 2. .env
    env_val = os.getenv(key)
    if env_val is not None:
        return _coerce(env_val, type_)

    # 3. Default
    return default


def all_current(db: Session) -> dict:
    """Snapshot of all editable settings, with their current effective values."""
    out = {}
    for key, spec in EDITABLE_KEYS.items():
        out[key] = {
            **spec,
            "value": get(db, key, default=""),
            "source": _source(db, key),
        }
    return out


def _source(db: Session, key: str) -> str:
    if db.query(SettingOverride).filter(SettingOverride.key == key).first():
        return "db"
    if os.getenv(key) is not None:
        return "env"
    return "default"


def set_override(db: Session, key: str, value: str) -> None:
    if key not in EDITABLE_KEYS:
        raise ValueError(f"Setting '{key}' is not editable")
    # Validate coercion so we never store a broken value
    _coerce(value, EDITABLE_KEYS[key]["type"])

    row = db.query(SettingOverride).filter(SettingOverride.key == key).first()
    if row is None:
        row = SettingOverride(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


def clear_override(db: Session, key: str) -> None:
    db.query(SettingOverride).filter(SettingOverride.key == key).delete()
    db.commit()


# ---------------- Band presets (license-free SRD, REC 70-03) ----------------
# Each preset describes the regulatory envelope of one ISM/SRD band. Values
# come from the .env (BAND_<id>_*). An unregulated field is represented by
# `None` and rendered as "—" in the UI.

_BAND_IDS = ("433", "868", "2400", "5800")

# Built-in defaults so the feature works even if .env is missing the
# BAND_* keys. Anything in the .env (or "" for "not regulated") wins.
_BAND_FALLBACKS = {
    "433": {
        "label":    "433 MHz — 10 mW ERP, no duty cycle",
        "carrier":  433_920_000,
        "eirp_dbm": 10,
        "dc_pct":   None,
        "lbt":      False,
        "note":     "433.05–434.79 MHz, 10 mW ERP. Highest legal "
                    "continuous-TX option in this band without licence.",
    },
    "868": {
        "label":    "868 MHz — 500 mW ERP, 10 % duty cycle",
        "carrier":  869_525_000,
        "eirp_dbm": 27,
        "dc_pct":   10,
        "lbt":      False,
        "note":     "Use 869.4–869.65 MHz subband. Other 868 MHz subbands "
                    "are tighter (25 mW / 0.1–1 %).",
    },
    "2400": {
        "label":    "2.4 GHz — 100 mW EIRP, no duty cycle",
        "carrier":  2_400_000_000,
        "eirp_dbm": 20,
        "dc_pct":   None,
        "lbt":      False,
        "note":     "2400–2483.5 MHz wideband. Spread-spectrum or LBT "
                    "recommended for ETSI EN 300 328 compliance.",
    },
    "5800": {
        "label":    "5.8 GHz — 25 mW EIRP, no duty cycle",
        "carrier":  5_800_000_000,
        "eirp_dbm": 14,
        "dc_pct":   None,
        "lbt":      False,
        "note":     "5725–5875 MHz non-specific SRD. Power-limited but "
                    "no duty cycle.",
    },
}


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else v


def _opt_float(name: str):
    raw = _env(name).strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _opt_int(name: str):
    raw = _env(name).strip()
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _opt_bool(name: str):
    raw = _env(name).strip().lower()
    if raw in ("1", "true", "yes", "on"):  return True
    if raw in ("0", "false", "no", "off"): return False
    return None


def list_bands() -> list[dict]:
    """Return the configured band presets — .env wins, fallbacks fill gaps.

    Each setting honours an explicit empty value in the .env as "not
    regulated" (rendered as "—"). Only keys that are completely *missing*
    fall back to the built-in defaults.
    """
    def pick(envname: str, parser, fallback):
        if envname in os.environ:
            raw = os.environ[envname].strip()
            if raw == "":
                return None
            try:
                return parser(raw)
            except ValueError:
                return fallback
        return fallback

    out = []
    for bid in _BAND_IDS:
        prefix = f"BAND_{bid}_"
        fb = _BAND_FALLBACKS.get(bid, {})
        out.append({
            "id":              bid,
            "label":           pick(prefix + "LABEL", str, fb.get("label", f"{bid} MHz")),
            "carrier_hz":      pick(prefix + "CARRIER_HZ", int, fb.get("carrier")),
            "max_eirp_dbm":    pick(prefix + "MAX_EIRP_DBM", float, fb.get("eirp_dbm")),
            "duty_cycle_pct":  pick(prefix + "DUTY_CYCLE_PERCENT", float, fb.get("dc_pct")),
            "lbt_required":    _opt_bool(prefix + "LBT_REQUIRED")
                               if (prefix + "LBT_REQUIRED") in os.environ
                               else fb.get("lbt"),
            "note":            pick(prefix + "NOTE", str, fb.get("note", "")),
        })
    return out
