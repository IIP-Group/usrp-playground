import os
import uuid
import json
import struct
import asyncio
import traceback
from pathlib import Path

from fastapi import FastAPI, Query, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from database import get_db, engine, SessionLocal
from models import Token, Task, Log, User
import server_state
import settings_store
import inventory
from admin_router import router as admin_router

app = FastAPI(title="USRP Sandbox System")
app.include_router(admin_router)

DATA_DIR = Path("/data")
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
DEFAULT_TOKEN = os.getenv("DEFAULT_AUTH_TOKEN", "default-bench-token-2024")
# In-memory cache for limits. Populated from settings_store (DB > env >
# default) and refreshed after every settings save.
_LIMITS = {
    "MAX_QUEUE": int(os.getenv("MAX_QUEUE", os.getenv("MAX_WS_CONNECTIONS", "100"))),
    "MAX_QUEUE_PER_IP": int(os.getenv("MAX_QUEUE_PER_IP", os.getenv("MAX_WS_PER_IP", "5"))),
    "UPLOAD_TIMEOUT_SEC": float(os.getenv("UPLOAD_TIMEOUT_SEC", "30")),
    "POLL_INTERVAL_SEC": float(os.getenv("POLL_INTERVAL_SEC", "2")),
}


def refresh_limits():
    """Re-read limit settings from DB. Called on startup and after admin saves."""
    try:
        with SessionLocal() as db:
            _LIMITS["MAX_QUEUE"] = int(settings_store.get(db, "MAX_QUEUE", _LIMITS["MAX_QUEUE"]))
            _LIMITS["MAX_QUEUE_PER_IP"] = int(settings_store.get(db, "MAX_QUEUE_PER_IP", _LIMITS["MAX_QUEUE_PER_IP"]))
            _LIMITS["UPLOAD_TIMEOUT_SEC"] = float(settings_store.get(db, "UPLOAD_TIMEOUT_SEC", _LIMITS["UPLOAD_TIMEOUT_SEC"]))
            _LIMITS["POLL_INTERVAL_SEC"] = float(settings_store.get(db, "POLL_INTERVAL_SEC", _LIMITS["POLL_INTERVAL_SEC"]))
    except Exception:
        pass  # keep last-known values if DB is unreachable


ws_count = 0
ws_per_ip: dict[str, int] = {}


def _current_max_upload(db: Session) -> int:
    return int(settings_store.get(db, "MAX_UPLOAD_MB", 200)) * 1024 * 1024


def _radio_info(db: Session) -> dict:
    """Compute the current RADIO_INFO dict using effective settings (DB > env)."""
    def g(key, default):
        return settings_store.get(db, key, default)

    # Default carrier = centre of the SRD band, so that
    # carrier ± bandwidth/2 always sits inside the legal range.
    band = settings_store.srd_band_info()
    default_carrier = (int(band["f_min_hz"]) + int(band["f_max_hz"])) // 2

    # Gains live per channel in the Hardware-Inventory - there is no global
    # gain setting any more. The top-level tx_gain_db / rx_gain_db keys stay
    # for client compatibility and report channel 0's values (None when no
    # inventory is configured).
    inv_channels = inventory.read_inventory().get("channels", [])
    ch0 = inv_channels[0] if inv_channels else {}

    return {
        "carrier_frequency_hz": int(g("CARRIER_FREQUENCY_HZ", default_carrier)),
        "sample_rate_hz":       int(g("SAMPLE_RATE_HZ", 25_000_000)),
        "bandwidth_hz":         int(g("BANDWIDTH_HZ", 25_000_000)),
        "tx_gain_db":           (ch0.get("tx") or {}).get("gain_db"),
        "tx_power_dbm":         (ch0.get("tx") or {}).get("power_dbm",
                                                          g("TX_POWER_DBM", None)),
        "rx_gain_db":           (ch0.get("rx") or {}).get("gain_db"),
        "antenna_tx":           g("ANTENNA_TX", "TX/RX0"),
        "antenna_rx":           g("ANTENNA_RX", "RX1"),
        "max_upload_mb":        int(g("MAX_UPLOAD_MB", 200)),
        "max_samples":          int(g("MAX_SAMPLES", 2_500_000)),
        "duty_cycle_max_percent": float(g("DUTY_CYCLE_MAX_PERCENT", 10)),
        "duty_cycle_window_sec":  float(g("DUTY_CYCLE_WINDOW_SEC", 60)),
        "lbt_enabled":          bool(g("LBT_ENABLED", True)),
        "lbt_threshold_dbfs":   float(g("LBT_THRESHOLD_DBFS", -50)),
        "begin_guard_min_sec":  float(g("BEGIN_GUARD_MIN_SEC", 0.1)),
        "begin_guard_max_sec":  float(g("BEGIN_GUARD_MAX_SEC", 0.1)),
        "end_guard_min_sec":    float(g("END_GUARD_MIN_SEC", 0.1)),
        "end_guard_max_sec":    float(g("END_GUARD_MAX_SEC", 0.1)),
        "mimo_enabled":         bool(g("MIMO_ENABLED", False)),
        "mimo_max_channels":    int(g("MIMO_MAX_CHANNELS", 2)),
        # Channel list from the Hardware-Inventory. The position in this list
        # IS the physical channel index the worker drives, so the benchmark
        # page can offer a "test over channel X" picker.
        "channels":             [
            {"index": i, "id": c.get("id", str(i)),
             "label": c.get("label") or c.get("id") or str(i),
             "tx_gain_db": (c.get("tx") or {}).get("gain_db"),
             "tx_power_dbm": (c.get("tx") or {}).get("power_dbm"),
             "rx_gain_db": (c.get("rx") or {}).get("gain_db")}
            for i, c in enumerate(inv_channels)
        ],
    }


def _log(db, action, token_id=None, eth_id=None, n_samples=None, detail=None, ip=None):
    db.add(Log(token_id=token_id, eth_id=eth_id, action=action,
               n_samples=n_samples, detail=detail, ip=ip))
    db.commit()


@app.on_event("startup")
def startup():
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Idempotent schema migrations for columns added after initial deploy.
    # init.sql only runs on a fresh DB; existing volumes need ALTER.
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS tags TEXT NOT NULL DEFAULT ''"
        ))
    from sqlalchemy.orm import Session as S
    with S(bind=engine) as db:
        if not db.query(Token).filter(Token.token == DEFAULT_TOKEN).first():
            db.add(Token(token=DEFAULT_TOKEN, label="default", is_default=True))
            db.commit()
        # Make sure server_state row exists
        server_state.get_state(db)
        # Locked fields (CARRIER, BANDWIDTH, TX_POWER, …) must always come
        # from .env / built-ins. Drop any lingering DB overrides from
        # earlier runs so /info, the worker and the Settings UI all agree.
        n = settings_store.purge_locked_overrides(db)
        if n:
            print(f"[startup] purged {n} stale DB override(s) for locked fields")
    refresh_limits()


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


def _cancel_pending_task(task_uid, token_id, eth_id, ip):
    """The client is gone - if their task hasn't started yet, drop it so
    the worker doesn't waste USRP airtime on a result nobody will read."""
    if task_uid is None:
        return
    try:
        with SessionLocal() as db:
            fresh = db.query(Task).filter(Task.uid == task_uid).first()
            if fresh is None:
                return
            if fresh.state == "PD":
                tdir = INPUT_DIR / str(task_uid)
                if tdir.exists():
                    import shutil
                    shutil.rmtree(tdir, ignore_errors=True)
                db.delete(fresh)
                db.commit()
                _log(db, "cancelled", token_id=token_id, eth_id=eth_id,
                     ip=ip, detail=f"uid={task_uid} (while PD)")
            elif fresh.state == "R":
                _log(db, "cancelled", token_id=token_id, eth_id=eth_id,
                     ip=ip, detail=f"uid={task_uid} (while R, completing anyway)")
    except Exception:
        pass


@app.websocket("/ws/run")
async def ws_run(ws: WebSocket):
    global ws_count

    ip = ws.client.host if ws.client else "unknown"

    # -------- Single queue: each accepted WS occupies one queue slot --------
    # Cheap rejects BEFORE touching the DB.
    if ws_count >= _LIMITS["MAX_QUEUE"]:
        await ws.close(code=1013, reason="queue full")
        return
    # Per-IP cap: stop a single client/loop from monopolizing all slots.
    if ws_per_ip.get(ip, 0) >= _LIMITS["MAX_QUEUE_PER_IP"]:
        await ws.close(code=1013, reason="too many connections from your IP")
        return

    # Reserve the slot BEFORE the first await: between check and increment
    # no other coroutine may run, otherwise N parallel handshakes could all
    # pass the check and overshoot the limit. Released in the finally below.
    ws_count += 1
    ws_per_ip[ip] = ws_per_ip.get(ip, 0) + 1

    task = None
    task_uid = None
    eth_id = None
    token_id = None

    try:
        await ws.accept()
        # -------- One short DB session for setup (auth + radio info + queue check) --------
        with SessionLocal() as db:
            if not server_state.is_running(db):
                await _ws_send(ws, error="server_sleeping", message="SLEEPING ZZZZ")
                await ws.close()
                return

            auth_token = ws.query_params.get("auth_token", "")
            token = db.query(Token).filter(Token.token == auth_token).first()
            if not token:
                _log(db, "auth_failed", ip=ip, detail=f"token={auth_token[:20]}")
                await _ws_send(ws, error="auth_failed", message="Invalid auth token")
                await ws.close()
                return

            token_id = token.id
            eth_id = _eth_id_for_token(db, token)
            radio_info = _radio_info(db)
            max_upload = _current_max_upload(db)

        await _ws_send(ws, message="info", **radio_info)

        # -------- Receive payload (with optional mode-handshake) -----------
        # Protocol:
        #   * Client MAY send a TEXT frame first:
        #       {"mode": "siso"}                       (same as no handshake)
        #       {"mode": "mimo", "channels": <int>}    (multi-channel upload)
        #       {"mode": "listen", "n_samples": <int>} (RX only, no payload)
        #   * Then exactly ONE BINARY frame containing the payload
        #     (except mode=listen, which has no payload).
        #     SISO  payload  = raw float32 interleaved I/Q
        #     MIMO  payload  = mimo_format header + channel-sequential IQ
        # Backward compat: if the first frame is already binary, it's SISO.
        mimo_enabled = bool(radio_info.get("mimo_enabled", False))
        mimo_max_ch = int(radio_info.get("mimo_max_channels", 2))
        mode = "siso"
        announced_channels = 1
        listen_samples = 0
        # Which inventory channel a SISO test runs over (index into the
        # inventory channel list). 0 = first channel = legacy behaviour.
        selected_channel = 0
        n_inv_channels = len(radio_info.get("channels", []))
        try:
            first = await asyncio.wait_for(
                ws.receive(), timeout=_LIMITS["UPLOAD_TIMEOUT_SEC"]
            )
        except asyncio.TimeoutError:
            with SessionLocal() as db:
                _log(db, "upload_timeout", token_id=token_id, eth_id=eth_id, ip=ip)
            await _ws_send(ws, error="upload_timeout",
                           message=f"No upload within {_LIMITS['UPLOAD_TIMEOUT_SEC']:.0f}s")
            await ws.close()
            return

        if first.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(first.get("code", 1006))

        data = None
        if "text" in first and first["text"] is not None:
            if len(first["text"]) > 4096:
                await _ws_send(ws, error="bad_handshake",
                               message="Handshake frame too large")
                await ws.close()
                return
            try:
                handshake = json.loads(first["text"])
                if not isinstance(handshake, dict):
                    raise ValueError("handshake must be a JSON object")
            except Exception:
                await _ws_send(ws, error="bad_handshake",
                               message="First text frame must be a JSON object")
                await ws.close()
                return
            mode = str(handshake.get("mode", "siso")).lower()
            if mode not in ("siso", "mimo", "listen"):
                await _ws_send(ws, error="bad_handshake",
                               message=f"Unknown mode '{mode}'")
                await ws.close()
                return
            # Optional channel picker (SISO only). Reject out-of-range so a
            # stale UI can't silently fall back to the wrong antenna.
            try:
                selected_channel = int(handshake.get("channel", 0) or 0)
            except (TypeError, ValueError):
                selected_channel = 0
            if selected_channel < 0 or (n_inv_channels and selected_channel >= n_inv_channels):
                await _ws_send(ws, error="bad_handshake",
                               message=f"channel must be 0..{max(0, n_inv_channels - 1)}")
                await ws.close()
                return
            if mode == "mimo":
                if not mimo_enabled:
                    await _ws_send(ws, error="mimo_disabled",
                                   message="MIMO is disabled on the server")
                    await ws.close()
                    return
                try:
                    announced_channels = int(handshake.get("channels", 1) or 1)
                except (TypeError, ValueError):
                    announced_channels = 0    # forces the range error below
                if announced_channels < 1 or announced_channels > mimo_max_ch:
                    await _ws_send(ws, error="bad_handshake",
                                   message=f"channels must be 1..{mimo_max_ch}")
                    await ws.close()
                    return
            elif mode == "listen":
                try:
                    listen_samples = int(handshake.get("n_samples", 0) or 0)
                except (TypeError, ValueError):
                    listen_samples = 0
                max_samples = int(radio_info.get("max_samples", 0) or 0)
                if listen_samples <= 0 or (max_samples and listen_samples > max_samples):
                    await _ws_send(ws, error="bad_handshake",
                                   message=f"n_samples must be 1..{max_samples}")
                    await ws.close()
                    return
                try:
                    announced_channels = int(handshake.get("channels", 1) or 1)
                except (TypeError, ValueError):
                    announced_channels = 1
                if announced_channels < 1 or announced_channels > mimo_max_ch:
                    await _ws_send(ws, error="bad_handshake",
                                   message=f"channels must be 1..{mimo_max_ch}")
                    await ws.close()
                    return
                if announced_channels > 1:
                    if not mimo_enabled:
                        await _ws_send(ws, error="mimo_disabled",
                                       message="MIMO is disabled on the server")
                        await ws.close()
                        return
                    # MIMO listen always captures channels 0..N-1.
                    selected_channel = 0
            await _ws_send(ws, message="ack", mode=mode, channels=announced_channels)
            if mode == "listen":
                data = b""     # RX only - no payload follows
            else:
                try:
                    data = await asyncio.wait_for(
                        ws.receive_bytes(), timeout=_LIMITS["UPLOAD_TIMEOUT_SEC"]
                    )
                except asyncio.TimeoutError:
                    with SessionLocal() as db:
                        _log(db, "upload_timeout", token_id=token_id, eth_id=eth_id, ip=ip)
                    await _ws_send(ws, error="upload_timeout",
                                   message=f"No payload within {_LIMITS['UPLOAD_TIMEOUT_SEC']:.0f}s")
                    await ws.close()
                    return
        elif "bytes" in first and first["bytes"] is not None:
            data = first["bytes"]
        else:
            await _ws_send(ws, error="bad_frame",
                           message="Expected text handshake or binary payload")
            await ws.close()
            return

        if len(data) > max_upload:
            with SessionLocal() as db:
                _log(db, "file_too_large", token_id=token_id, eth_id=eth_id,
                     ip=ip, detail=f"{len(data)} bytes")
            await _ws_send(ws, error="file_too_large",
                           message=f"Max {max_upload} bytes")
            await ws.close()
            return

        # Quick payload sanity-check against the announced mode. Catches
        # the obvious foot-gun where someone says mode=mimo but forgets the
        # 16-byte header.
        is_mimo_blob = len(data) >= 8 and data[:8] == b"MIMO\x00\x00\x00\x00"
        if mode == "mimo" and not is_mimo_blob:
            await _ws_send(ws, error="bad_payload",
                           message="mode=mimo but payload has no MIMO header")
            await ws.close()
            return
        mimo_explicit = (mode == "mimo")
        if mode == "siso" and is_mimo_blob:
            # MIMO blob without handshake - reject so we never silently
            # accept multi-channel data while MIMO is off.
            if not mimo_enabled:
                await _ws_send(ws, error="mimo_disabled",
                               message="MIMO is disabled on the server")
                await ws.close()
                return
            mode = "mimo"

        # -------- Validate the payload BEFORE it occupies a queue slot -----
        # Everything rejected here would otherwise waste queue time and only
        # fail later in the worker with a less direct error.
        max_samples = int(radio_info.get("max_samples", 0) or 0)
        if mode == "mimo":
            if len(data) < 16:
                await _ws_send(ws, error="bad_payload",
                               message="MIMO payload shorter than its 16-byte header")
                await ws.close()
                return
            hdr_ch, hdr_n = struct.unpack("<II", data[8:16])
            if hdr_ch < 1 or hdr_ch > mimo_max_ch:
                await _ws_send(ws, error="bad_payload",
                               message=f"MIMO header: channels must be 1..{mimo_max_ch}")
                await ws.close()
                return
            if mimo_explicit and hdr_ch != announced_channels:
                await _ws_send(ws, error="bad_payload",
                               message=f"MIMO header says {hdr_ch} channel(s) but "
                                       f"handshake announced {announced_channels}")
                await ws.close()
                return
            if len(data) - 16 < hdr_ch * hdr_n * 8:
                await _ws_send(ws, error="bad_payload",
                               message="MIMO payload truncated: fewer bytes than the "
                                       "header promises")
                await ws.close()
                return
            announced_channels = hdr_ch
            payload_samples = hdr_n
        elif mode == "siso":
            payload_samples = len(data) // 8
        else:   # listen - validated in the handshake already
            payload_samples = listen_samples
        if mode != "listen":
            if payload_samples <= 0:
                await _ws_send(ws, error="bad_payload",
                               message="Empty signal")
                await ws.close()
                return
            if max_samples and payload_samples > max_samples:
                await _ws_send(ws, error="too_many_samples",
                               message=f"Signal has {payload_samples} samples per "
                                       f"channel, max is {max_samples}")
                await ws.close()
                return

        # -------- Create task --------
        task_uid = uuid.uuid4()
        task_dir = INPUT_DIR / str(task_uid)
        task_dir.mkdir(parents=True)
        (task_dir / "input.f32").write_bytes(data)
        # Sidecar metadata the worker reads alongside the signal. Channel
        # selection only applies to SISO paths (MIMO drives channels 0..N-1).
        meta = {"channel": selected_channel if mode in ("siso", "listen") else 0}
        if mode == "listen":
            meta["listen"] = {"n_samples": listen_samples,
                              "channels": announced_channels}
        (task_dir / "meta.json").write_text(json.dumps(meta))
        n_samples = payload_samples
        # Release the upload buffer NOW - it is on disk. Otherwise every
        # queued connection pins its full payload (up to max_upload) in RAM
        # for the whole queue wait.
        data = b""

        with SessionLocal() as db:
            task = Task(uid=task_uid, token_id=token_id, n_samples=n_samples)
            db.add(task)
            db.commit()
            db.refresh(task)
            created_at = task.created_at
            _log(db, "submit", token_id=token_id, eth_id=eth_id,
                 n_samples=n_samples, ip=ip, detail=f"uid={task_uid}")
            pos = db.query(Task).filter(
                Task.state == "PD", Task.created_at < created_at
            ).count()

        await _ws_send(ws, message="queued", uid=str(task_uid),
                       state="PD", queue_position=pos)

        # -------- Poll status (short DB session per poll, NOT held across sleep) --------
        # Adaptive interval: poll fast while the task is running or next in
        # line (keeps the round-trip snappy), relax to the configured
        # interval while it sits deep in the queue (keeps DB load low with
        # many waiting clients).
        db_failures = 0
        poll_fast = min(0.25, _LIMITS["POLL_INTERVAL_SEC"])
        interval = poll_fast
        while True:
            await asyncio.sleep(interval)
            try:
                with SessionLocal() as db:
                    running = server_state.is_running(db)
                    fresh = None
                    state = error_message = None
                    pos = 0
                    if running:
                        fresh = db.query(Task).filter(Task.uid == task_uid).first()
                        if fresh is not None:
                            state = fresh.state
                            error_message = fresh.error_message
                            pos = db.query(Task).filter(
                                Task.state == "PD", Task.created_at < created_at
                            ).count()
                db_failures = 0
            except Exception:
                # A transient DB hiccup must not kill every waiting client.
                # Skip this status round; give up only if it persists.
                db_failures += 1
                if db_failures >= 10:
                    await _ws_send(ws, error="server_error",
                                   message="Temporary server problem, please resubmit")
                    await ws.close()
                    return
                continue
            if not running:
                await _ws_send(ws, error="server_sleeping",
                               message="Server went to sleep zzZZ....")
                await ws.close()
                return
            if fresh is None:
                await _ws_send(ws, error="task_lost", message="Task disappeared")
                await ws.close()
                return
            await _ws_send(ws, message="status", uid=str(task_uid),
                           state=state, queue_position=pos)
            if state == "D":
                break
            interval = poll_fast if (state == "R" or pos == 0) \
                else _LIMITS["POLL_INTERVAL_SEC"]

        if error_message:
            with SessionLocal() as db:
                _log(db, "task_error", token_id=token_id, eth_id=eth_id,
                     ip=ip, detail=f"uid={task_uid}")
            await _ws_send(ws, error="processing_failed", message=error_message)
        else:
            f32_path = OUTPUT_DIR / str(task_uid) / "output.f32"
            if f32_path.exists():
                with SessionLocal() as db:
                    _log(db, "download", token_id=token_id, eth_id=eth_id, ip=ip,
                         detail=f"uid={task_uid}")
                out_bytes = f32_path.read_bytes()
                out_is_mimo = len(out_bytes) >= 8 and out_bytes[:8] == b"MIMO\x00\x00\x00\x00"
                await _ws_send(ws, message="done", uid=str(task_uid),
                               mode="mimo" if out_is_mimo else "siso")
                await ws.send_bytes(out_bytes)
            else:
                await _ws_send(ws, error="no_output", message="Output file not found")

        await ws.close()

    except WebSocketDisconnect:
        # Client hung up - if their task is still pending, cancel it.
        _cancel_pending_task(task_uid, token_id, eth_id, ip)
    except Exception:
        # Don't let one bad client take down the server - log, clean up the
        # pending task (a send to a vanished client raises here, not
        # WebSocketDisconnect), and close.
        traceback.print_exc()
        _cancel_pending_task(task_uid, token_id, eth_id, ip)
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        ws_count -= 1
        c = ws_per_ip.get(ip, 0) - 1
        if c <= 0:
            ws_per_ip.pop(ip, None)
        else:
            ws_per_ip[ip] = c


@app.get("/api/status")
def public_status(db: Session = Depends(get_db)):
    """Anonymous, public health probe used by the /status landing page.
    Reports only whether the server is running - no queue / IP details."""
    state = server_state.get_state(db)
    return {"state": state, "ok": state == "running"}


@app.get("/health")
def health(auth_token: str = Query(...), db: Session = Depends(get_db)):
    token = db.query(Token).filter(Token.token == auth_token).first()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    pending = db.query(Task).filter(Task.state.in_(("PD", "R"))).count()
    return {
        "status": "ok",
        "pending_tasks": pending,
        "queue_size": ws_count,
        "queue_max": _LIMITS["MAX_QUEUE"],
        "queue_per_ip_max": _LIMITS["MAX_QUEUE_PER_IP"],
        "queue_per_ip": dict(ws_per_ip),
        "usrp_state": server_state.get_state(db),
    }


@app.get("/info")
def info(auth_token: str = Query(...), db: Session = Depends(get_db)):
    token = db.query(Token).filter(Token.token == auth_token).first()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return _radio_info(db)
