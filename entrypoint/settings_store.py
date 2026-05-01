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
    # Carrier is the centre of the SRD band by default. Visible in the UI
    # but locked — change via .env CARRIER_FREQUENCY_HZ if a non-centred
    # carrier is required.
    "CARRIER_FREQUENCY_HZ": {"type": "int", "group": "radio",
        "label": "Carrier Frequency",
        "locked": True,
        "desc": "Centre of the transmitted band — defaults to the middle "
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
    "TX_POWER_DBM":         {"type": "float", "group": "radio",
        "label": "TX Power",
        "locked": True,
        "desc": "Calibrated absolute output power at the USRP TX/RX SMA "
                "(uses UHD's set_tx_power_reference). Locked at the .env "
                "default to keep EIRP under the 10 dBm SRD limit. "
                "Change via .env TX_POWER_DBM if you really need to."},
    "RX_GAIN_DB":           {"type": "float", "group": "radio",
        "label": "RX Gain",
        "desc": "Receive gain. Too high → saturation, too low → noise."},
    "ANTENNA_TX":           {"type": "str",   "group": "radio",
        "label": "TX Antenna",
        "desc": "USRP antenna port used for transmit (e.g. TX/RX0)."},
    "ANTENNA_RX":           {"type": "str",   "group": "radio",
        "label": "RX Antenna",
        "desc": "USRP antenna port used for receive (e.g. RX1)."},

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
    "DUTY_CYCLE_MAX_PERCENT": {"type": "float", "group": "safety",
        "label": "Max Duty Cycle",
        "desc": "Maximum fraction of active transmit time within the window. ETSI-compliant value is typically 10%."},
    "DUTY_CYCLE_WINDOW_SEC":  {"type": "float", "group": "safety",
        "label": "Duty Cycle Window",
        "desc": "Time window over which duty cycle is measured (typically 60 s)."},

    # LBT
    "LBT_ENABLED":        {"type": "bool",  "group": "safety",
        "label": "Listen Before Talk",
        "desc": "Sense the channel before transmitting. Recommended for ISM-band compliance."},
    "LBT_THRESHOLD_DBFS": {"type": "float", "group": "safety",
        "label": "LBT Threshold",
        "desc": "Power threshold above which the channel is considered busy."},
    "LBT_SENSE_SAMPLES":  {"type": "int",   "group": "safety",
        "label": "LBT Sense Samples",
        "desc": "Number of samples used for the power average."},
    "LBT_MAX_RETRIES":    {"type": "int",   "group": "safety",
        "label": "LBT Max Retries",
        "desc": "How often to retry if the channel is busy — after that, error out."},
    "LBT_BACKOFF_SEC":    {"type": "float", "group": "safety",
        "label": "LBT Backoff",
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


# ---------------- 2.4 GHz SRD band info (RIR1008-11 / EN 300 440) -----------
# License-exempt non-specific SRD: 2400–2483.5 MHz, 10 mW EIRP, no duty cycle,
# no LBT. The numbers below feed the read-only info banner on the Settings
# page; values are sourced from the .env with sensible fallbacks.

def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def srd_band_info() -> dict:
    return {
        "label":      os.getenv("SRD_BAND_LABEL",
                                "2.4 GHz SRD — RIR1008-11 (Non-specific SRD)"),
        "f_min_hz":   _env_int("SRD_BAND_F_MIN_HZ", 2_400_000_000),
        "f_max_hz":   _env_int("SRD_BAND_F_MAX_HZ", 2_483_500_000),
        "max_eirp_dbm": _env_float("SRD_MAX_EIRP_DBM", 10.0),
        "duty_cycle_pct": None,        # not regulated on 2.4 GHz SRD
        "lbt_required":   False,
        "note":       os.getenv("SRD_NOTE",
                                "License-exempt, NIB/NPB. Stay within "
                                "2400-2483.5 MHz, ≤10 mW EIRP. No duty cycle, "
                                "no LBT. EIRP = USRP_TX_Output - cable_loss "
                                "+ antenna_gain."),
    }
