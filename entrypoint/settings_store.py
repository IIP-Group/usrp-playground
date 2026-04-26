"""
Zentrale Config-Auflösung für USRP-relevante Parameter.

Reihenfolge: DB-Override > .env > Default.

Beim Schreiben über die Admin-UI landen Änderungen in der DB (Tabelle
setting_overrides). Der Worker pollt diese Tabelle periodisch bzw. beim
nächsten Task — dadurch ist "reload" praktisch sofort ohne Restart.
"""
import os
from typing import Any
from sqlalchemy.orm import Session
from models import SettingOverride


# Welche Keys dürfen per Admin-UI überschrieben werden und welchen Typ haben sie?
EDITABLE_KEYS: dict[str, dict] = {
    # Radio
    "CARRIER_FREQUENCY_HZ": {"type": "int", "group": "radio", "label": "Carrier Frequency (Hz)"},
    "SAMPLE_RATE_HZ":       {"type": "int", "group": "radio", "label": "Sample Rate (Hz)"},
    "BANDWIDTH_HZ":         {"type": "int", "group": "radio", "label": "Bandwidth (Hz)"},
    "TX_GAIN_DB":           {"type": "float", "group": "radio", "label": "TX Gain (dB)"},
    "RX_GAIN_DB":           {"type": "float", "group": "radio", "label": "RX Gain (dB)"},
    "CHANNEL_SNR_DB":       {"type": "float", "group": "radio", "label": "Sim. SNR (dB)"},
    "ANTENNA_TX":           {"type": "str",   "group": "radio", "label": "TX Antenna"},
    "ANTENNA_RX":           {"type": "str",   "group": "radio", "label": "RX Antenna"},
    "MASTER_CLOCK_RATE":    {"type": "int",   "group": "radio", "label": "Master Clock Rate (Hz)"},

    # Guard (neu — random uniform)
    "BEGIN_GUARD_MIN_SEC":  {"type": "float", "group": "guard", "label": "Begin Guard Min (s)"},
    "BEGIN_GUARD_MAX_SEC":  {"type": "float", "group": "guard", "label": "Begin Guard Max (s)"},
    "END_GUARD_MIN_SEC":    {"type": "float", "group": "guard", "label": "End Guard Min (s)"},
    "END_GUARD_MAX_SEC":    {"type": "float", "group": "guard", "label": "End Guard Max (s)"},
    "INITIAL_DELAY":        {"type": "float", "group": "guard", "label": "Initial Delay (s)"},

    # Duty cycle
    "DUTY_CYCLE_MAX_PERCENT": {"type": "float", "group": "safety", "label": "Max Duty Cycle (%)"},
    "DUTY_CYCLE_WINDOW_SEC":  {"type": "float", "group": "safety", "label": "Duty Cycle Window (s)"},

    # LBT
    "LBT_ENABLED":        {"type": "bool",  "group": "safety", "label": "Listen Before Talk"},
    "LBT_THRESHOLD_DBFS": {"type": "float", "group": "safety", "label": "LBT Threshold (dBFS)"},
    "LBT_SENSE_SAMPLES":  {"type": "int",   "group": "safety", "label": "LBT Sense Samples"},
    "LBT_MAX_RETRIES":    {"type": "int",   "group": "safety", "label": "LBT Max Retries"},
    "LBT_BACKOFF_SEC":    {"type": "float", "group": "safety", "label": "LBT Backoff (s)"},

    # Limits
    "MAX_UPLOAD_MB":      {"type": "int", "group": "limits", "label": "Max Upload (MB)"},
    "MAX_SAMPLES":        {"type": "int", "group": "limits", "label": "Max Samples"},
    "TASK_TTL_HOURS":     {"type": "int", "group": "limits", "label": "Task TTL (h)"},
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
