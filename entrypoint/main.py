import os
import uuid
import json
import asyncio
from pathlib import Path

from fastapi import FastAPI, Query, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from database import get_db, engine
from models import Token, Task, Log, User
import server_state
import settings_store
from admin_router import router as admin_router

app = FastAPI(title="USRP Benchmark System")
app.include_router(admin_router)

DATA_DIR = Path("/data")
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
DEFAULT_TOKEN = os.getenv("DEFAULT_AUTH_TOKEN", "default-bench-token-2024")
MAX_WS = int(os.getenv("MAX_WS_CONNECTIONS", "100"))


ws_count = 0


def _current_max_upload(db: Session) -> int:
    return int(settings_store.get(db, "MAX_UPLOAD_MB", 200)) * 1024 * 1024


def _current_max_pending(db: Session) -> int:
    return int(settings_store.get(db, "MAX_PENDING_TASKS", 200))


def _radio_info(db: Session) -> dict:
    """Compute the current RADIO_INFO dict using effective settings (DB > env)."""
    def g(key, default):
        return settings_store.get(db, key, default)

    return {
        "carrier_frequency_hz": int(g("CARRIER_FREQUENCY_HZ", 2_400_000_000)),
        "sample_rate_hz":       int(g("SAMPLE_RATE_HZ", 25_000_000)),
        "bandwidth_hz":         int(g("BANDWIDTH_HZ", 25_000_000)),
        "tx_gain_db":           float(g("TX_GAIN_DB", 30)),
        "rx_gain_db":           float(g("RX_GAIN_DB", 30)),
        "channel_snr_db":       float(g("CHANNEL_SNR_DB", 20)),
        "antenna_tx":           g("ANTENNA_TX", "TX/RX0"),
        "antenna_rx":           g("ANTENNA_RX", "RX1"),
        "max_upload_mb":        int(g("MAX_UPLOAD_MB", 200)),
        "max_samples":          int(g("MAX_SAMPLES", 2_500_000)),
        "use_real_usrp":        os.getenv("USE_REAL_USRP", "false").lower() == "true",
        "duty_cycle_max_percent": float(g("DUTY_CYCLE_MAX_PERCENT", 10)),
        "duty_cycle_window_sec":  float(g("DUTY_CYCLE_WINDOW_SEC", 60)),
        "lbt_enabled":          bool(g("LBT_ENABLED", True)),
        "lbt_threshold_dbfs":   float(g("LBT_THRESHOLD_DBFS", -50)),
        "begin_guard_min_sec":  float(g("BEGIN_GUARD_MIN_SEC", 0.1)),
        "begin_guard_max_sec":  float(g("BEGIN_GUARD_MAX_SEC", 0.1)),
        "end_guard_min_sec":    float(g("END_GUARD_MIN_SEC", 0.1)),
        "end_guard_max_sec":    float(g("END_GUARD_MAX_SEC", 0.1)),
    }


def _log(db, action, token_id=None, eth_id=None, n_samples=None, detail=None, ip=None):
    db.add(Log(token_id=token_id, eth_id=eth_id, action=action,
               n_samples=n_samples, detail=detail, ip=ip))
    db.commit()


@app.on_event("startup")
def startup():
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    from sqlalchemy.orm import Session as S
    with S(bind=engine) as db:
        if not db.query(Token).filter(Token.token == DEFAULT_TOKEN).first():
            db.add(Token(token=DEFAULT_TOKEN, label="default", is_default=True))
            db.commit()
        # Make sure server_state row exists
        server_state.get_state(db)


async def _ws_send(ws, **kwargs):
    await ws.send_text(json.dumps(kwargs))


def _eth_id_for_token(db: Session, token: Token) -> str | None:
    if token.is_default:
        return "[default]"
    if token.user_id:
        u = db.query(User).filter(User.id == token.user_id).first()
        if u:
            return u.eth_id
    return None


@app.websocket("/ws/run")
async def ws_run(ws: WebSocket):
    global ws_count

    await ws.accept()
    ws_count += 1

    db = next(get_db())
    ip = ws.client.host if ws.client else None

    try:
        # -------- Server sleeping? --------
        if not server_state.is_running(db):
            await _ws_send(ws, error="server_sleeping", message="SLEEPING ZZZZ")
            await ws.close()
            return

        if ws_count > MAX_WS:
            await _ws_send(ws, error="too_many_connections",
                           message="Server full, try again later")
            await ws.close()
            return

        # -------- Auth --------
        auth_token = ws.query_params.get("auth_token", "")
        token = db.query(Token).filter(Token.token == auth_token).first()
        if not token:
            _log(db, "auth_failed", ip=ip, detail=f"token={auth_token[:20]}")
            await _ws_send(ws, error="auth_failed", message="Invalid auth token")
            await ws.close()
            return

        eth_id = _eth_id_for_token(db, token)

        # -------- Send radio info --------
        radio_info = _radio_info(db)
        await _ws_send(ws, message="info", **radio_info)

        # -------- Queue check --------
        max_pending = _current_max_pending(db)
        if db.query(Task).filter(Task.state.in_(("PD", "R"))).count() >= max_pending:
            _log(db, "queue_full", token_id=token.id, eth_id=eth_id, ip=ip)
            await _ws_send(ws, error="queue_full",
                           message="Too many pending tasks, try again later")
            await ws.close()
            return

        # -------- Receive bytes --------
        data = await ws.receive_bytes()
        max_upload = _current_max_upload(db)
        if len(data) > max_upload:
            _log(db, "file_too_large", token_id=token.id, eth_id=eth_id,
                 ip=ip, detail=f"{len(data)} bytes")
            await _ws_send(ws, error="file_too_large",
                           message=f"Max {max_upload} bytes")
            await ws.close()
            return

        # -------- Create task --------
        task_uid = uuid.uuid4()
        task_dir = INPUT_DIR / str(task_uid)
        task_dir.mkdir(parents=True)
        (task_dir / "input.f32").write_bytes(data)

        n_samples = len(data) // 8  # complex64 = 2 * float32 = 8 bytes per sample
        task = Task(uid=task_uid, token_id=token.id, n_samples=n_samples)
        db.add(task)
        db.commit()

        _log(db, "submit", token_id=token.id, eth_id=eth_id,
             n_samples=n_samples, ip=ip, detail=f"uid={task_uid}")

        pos = db.query(Task).filter(
            Task.state == "PD", Task.created_at < task.created_at
        ).count()
        await _ws_send(ws, message="queued", uid=str(task_uid),
                       state="PD", queue_position=pos)

        # -------- Poll status --------
        while True:
            await asyncio.sleep(2)
            # Check if server went to sleep mid-task
            if not server_state.is_running(db):
                await _ws_send(ws, error="server_sleeping",
                               message="Server went to sleep zzZZ....")
                await ws.close()
                return
            db.refresh(task)
            pos = db.query(Task).filter(
                Task.state == "PD", Task.created_at < task.created_at
            ).count()
            await _ws_send(ws, message="status", uid=str(task_uid),
                           state=task.state, queue_position=pos)
            if task.state == "D":
                break

        if task.error_message:
            _log(db, "task_error", token_id=token.id, eth_id=eth_id,
                 ip=ip, detail=f"uid={task_uid}")
            await _ws_send(ws, error="processing_failed", message=task.error_message)
        else:
            f32_path = OUTPUT_DIR / str(task_uid) / "output.f32"
            if f32_path.exists():
                _log(db, "download", token_id=token.id, eth_id=eth_id, ip=ip,
                     detail=f"uid={task_uid}")
                await _ws_send(ws, message="done", uid=str(task_uid))
                await ws.send_bytes(f32_path.read_bytes())
            else:
                await _ws_send(ws, error="no_output", message="Output file not found")

        await ws.close()

    except WebSocketDisconnect:
        pass
    finally:
        ws_count -= 1
        db.close()


@app.get("/health")
def health(auth_token: str = Query(...), db: Session = Depends(get_db)):
    token = db.query(Token).filter(Token.token == auth_token).first()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    pending = db.query(Task).filter(Task.state.in_(("PD", "R"))).count()
    return {
        "status": "ok",
        "pending_tasks": pending,
        "ws_connections": ws_count,
        "usrp_state": server_state.get_state(db),
    }


@app.get("/info")
def info(auth_token: str = Query(...), db: Session = Depends(get_db)):
    token = db.query(Token).filter(Token.token == auth_token).first()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return _radio_info(db)
