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


# Welche Keys dürfen per Admin-UI überschrieben werden, welcher Typ und welche Beschreibung?
EDITABLE_KEYS: dict[str, dict] = {
    # Radio
    "CARRIER_FREQUENCY_HZ": {"type": "int", "group": "radio",
        "label": "Carrier Frequency (Hz)",
        "desc": "Trägerfrequenz, auf der gesendet/empfangen wird. Typisch 2.4 GHz für ISM-Band."},
    "SAMPLE_RATE_HZ":       {"type": "int", "group": "radio",
        "label": "Sample Rate (Hz)",
        "desc": "Abtastrate des USRP. Bestimmt die Basisband-Bandbreite (typisch 25 MSps)."},
    "BANDWIDTH_HZ":         {"type": "int", "group": "radio",
        "label": "Bandwidth (Hz)",
        "desc": "Analoger Anti-Aliasing-Filter im USRP-Frontend. Sollte ≤ Sample Rate sein."},
    "TX_GAIN_DB":           {"type": "float", "group": "radio",
        "label": "TX Gain (dB)",
        "desc": "Sendeverstärkung des USRP. 0 = aus, 30 = mittel, ~70 = max."},
    "RX_GAIN_DB":           {"type": "float", "group": "radio",
        "label": "RX Gain (dB)",
        "desc": "Empfangsverstärkung. Zu hoch → Sättigung, zu niedrig → Rauschen."},
    "CHANNEL_SNR_DB":       {"type": "float", "group": "radio",
        "label": "Sim. SNR (dB)",
        "desc": "Künstliches SNR im AWGN-Simulator-Modus (nur ohne echten USRP)."},
    "ANTENNA_TX":           {"type": "str",   "group": "radio",
        "label": "TX Antenna",
        "desc": "Antennen-Port am USRP für Senden (z.B. TX/RX0)."},
    "ANTENNA_RX":           {"type": "str",   "group": "radio",
        "label": "RX Antenna",
        "desc": "Antennen-Port am USRP für Empfangen (z.B. RX1)."},
    "MASTER_CLOCK_RATE":    {"type": "int",   "group": "radio",
        "label": "Master Clock Rate (Hz)",
        "desc": "Interner USRP-Takt. Sample Rate muss durch diesen Wert teilbar sein."},

    # Guard (random uniform)
    "BEGIN_GUARD_MIN_SEC":  {"type": "float", "group": "guard",
        "label": "Begin Guard Min (s)",
        "desc": "Minimale Pause vor dem Sendebeginn (random ∈ [Min, Max]). Schützt vor Klick-Artefakten."},
    "BEGIN_GUARD_MAX_SEC":  {"type": "float", "group": "guard",
        "label": "Begin Guard Max (s)",
        "desc": "Maximale Pause vor dem Sendebeginn."},
    "END_GUARD_MIN_SEC":    {"type": "float", "group": "guard",
        "label": "End Guard Min (s)",
        "desc": "Minimale Pause nach dem Sendeende (Reverb/Empfänger-Latenz)."},
    "END_GUARD_MAX_SEC":    {"type": "float", "group": "guard",
        "label": "End Guard Max (s)",
        "desc": "Maximale Pause nach dem Sendeende."},
    "INITIAL_DELAY":        {"type": "float", "group": "guard",
        "label": "Initial Delay (s)",
        "desc": "Verzögerung zwischen Task-Start und erstem Sample."},

    # Duty cycle
    "DUTY_CYCLE_MAX_PERCENT": {"type": "float", "group": "safety",
        "label": "Max Duty Cycle (%)",
        "desc": "Maximaler Anteil aktiver Sendezeit innerhalb des Fensters. ETSI-konform meist 10%."},
    "DUTY_CYCLE_WINDOW_SEC":  {"type": "float", "group": "safety",
        "label": "Duty Cycle Window (s)",
        "desc": "Zeitfenster, über das der Duty Cycle gemessen wird (typisch 60s)."},

    # LBT
    "LBT_ENABLED":        {"type": "bool",  "group": "safety",
        "label": "Listen Before Talk",
        "desc": "Vor dem Senden prüfen ob der Kanal frei ist. Empfohlen für ISM-Band-Compliance."},
    "LBT_THRESHOLD_DBFS": {"type": "float", "group": "safety",
        "label": "LBT Threshold (dBFS)",
        "desc": "Pegelschwelle: über diesem Wert gilt der Kanal als belegt."},
    "LBT_SENSE_SAMPLES":  {"type": "int",   "group": "safety",
        "label": "LBT Sense Samples",
        "desc": "Wieviele Samples zum Pegel-Mitteln genutzt werden."},
    "LBT_MAX_RETRIES":    {"type": "int",   "group": "safety",
        "label": "LBT Max Retries",
        "desc": "Wie oft erneut versucht wird, falls Kanal belegt — danach Fehler."},
    "LBT_BACKOFF_SEC":    {"type": "float", "group": "safety",
        "label": "LBT Backoff (s)",
        "desc": "Wartezeit zwischen LBT-Versuchen (random uniform)."},

    # Limits
    "MAX_UPLOAD_MB":      {"type": "int", "group": "limits",
        "label": "Max Upload (MB)",
        "desc": "Maximale Signalgröße pro Upload. Größere Uploads werden vom Server abgelehnt."},
    "MAX_SAMPLES":        {"type": "int", "group": "limits",
        "label": "Max Samples",
        "desc": "Maximale Anzahl IQ-Samples pro Task. Schutz vor zu langen Aufnahmen."},
    "TASK_TTL_HOURS":     {"type": "int", "group": "limits",
        "label": "Task TTL (h)",
        "desc": "Wie lange Task-Dateien (input/output) auf der Disk bleiben, bevor sie aufgeräumt werden."},
    "MAX_QUEUE":          {"type": "int", "group": "limits",
        "label": "Max Queue Size",
        "desc": "Gesamt-Obergrenze gleichzeitig laufender + wartender Tasks. Mehr Clients werden mit 'queue full' (HTTP 403) sofort abgewiesen."},
    "MAX_QUEUE_PER_IP":   {"type": "int", "group": "limits",
        "label": "Max Queue per IP",
        "desc": "Maximale Tasks pro Client-IP gleichzeitig. Schützt vor einzelnen Spammern, ohne andere Clients zu behindern."},
    "UPLOAD_TIMEOUT_SEC": {"type": "float", "group": "limits",
        "label": "Upload Timeout (s)",
        "desc": "Wie lange der Server nach dem WebSocket-Connect auf die Signal-Daten wartet, bevor er den Slot wieder freigibt (Slow-Loris-Schutz)."},
    "POLL_INTERVAL_SEC":  {"type": "float", "group": "limits",
        "label": "Status-Poll Interval (s)",
        "desc": "Wie oft der Server dem Client seinen Queue-Status mitteilt, während die Task wartet."},
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
