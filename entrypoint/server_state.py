"""
USRP server "sleep/wake" gate.

Wenn state == 'sleeping':
- Neue WebSocket-Verbindungen bekommen "Server is currently Sleeping zzZZ...."
- Worker pollt ebenfalls diesen State und hört auf zu arbeiten

State wird in DB gespeichert (single-row server_state table).
"""
from sqlalchemy.orm import Session
from datetime import datetime
from models import ServerState


def get_state(db: Session) -> str:
    row = db.query(ServerState).filter(ServerState.id == 1).first()
    if not row:
        row = ServerState(id=1, state="running")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row.state


def set_state(db: Session, state: str) -> str:
    if state not in ("running", "sleeping"):
        raise ValueError(f"Invalid state '{state}'")
    row = db.query(ServerState).filter(ServerState.id == 1).first()
    if not row:
        row = ServerState(id=1, state=state)
        db.add(row)
    else:
        row.state = state
        row.updated_at = datetime.utcnow()
    db.commit()
    return state


def is_running(db: Session) -> bool:
    return get_state(db) == "running"
