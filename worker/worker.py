import os
import sys
import json
import time
import shutil
import traceback
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Task, ServerState
from channel import send_and_receive, receive_only

DATA_DIR = Path("/data")
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"

POLL_INTERVAL = float(os.getenv("WORKER_POLL_INTERVAL_SEC", "0.2"))
TASK_TTL_HOURS = int(os.getenv("TASK_TTL_HOURS", "24"))
MAX_SAMPLES = int(os.getenv("MAX_SAMPLES", "2500000"))


def _with_rf_retry(fn, attempts=3, backoff_s=0.5):
    """Retry transient RF-stream errors (USB overflow / sequence glitches
    on stream start, mostly B210 multi-channel). Failed captures are
    discarded entirely, so a retry can never mix corrupted data in.
    Config errors etc. are NOT retried - they would fail identically."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RuntimeError as e:
            msg = str(e)
            transient = ("OVERFLOW" in msg or "Out of sequence" in msg
                         or "ERROR_CODE_LATE" in msg
                         or "RX stream timeout" in msg)
            if not transient or attempt == attempts:
                raise
            print(f"[rf-retry] transient RF error "
                  f"(attempt {attempt}/{attempts}): {msg.strip()[-160:]}",
                  flush=True)
            time.sleep(backoff_s)


def process_f32(task_uid):
    from usrp_testbed_library.mimo_format import (
        is_mimo_blob, decode_mimo, decode_siso, encode_siso, encode_mimo,
    )

    out_dir = OUTPUT_DIR / task_uid
    out_dir.mkdir(parents=True, exist_ok=True)

    in_dir = INPUT_DIR / task_uid

    # Optional sidecar: SISO channel picker + listen (RX-only) config.
    meta = {}
    meta_path = in_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            meta = {}
    try:
        channel = int(meta.get("channel", 0) or 0)
    except Exception:
        channel = 0

    listen_cfg = meta.get("listen")
    if listen_cfg:
        # Listen task: no TX signal, just capture n_samples.
        n_samples = int(listen_cfg.get("n_samples", 0) or 0)
        if n_samples <= 0 or n_samples > MAX_SAMPLES:
            raise ValueError(
                f"Invalid listen request: {n_samples} samples (max {MAX_SAMPLES})"
            )
        n_channels = int(listen_cfg.get("channels", 1) or 1)
        if n_channels > 1:
            received = _with_rf_retry(
                lambda: receive_only(n_samples, n_channels=n_channels))
        else:
            received = _with_rf_retry(
                lambda: receive_only(n_samples, channel=channel))
    else:
        blob = (in_dir / "input.f32").read_bytes()

        if is_mimo_blob(blob):
            signal = decode_mimo(blob)           # shape (n_samples, n_channels)
        else:
            signal = decode_siso(blob)           # shape (n_samples,)
        n_samples = signal.shape[0]

        if n_samples > MAX_SAMPLES:
            raise ValueError(f"Signal too large: {n_samples} samples (max {MAX_SAMPLES})")

        if signal.ndim == 2:
            # MIMO already spans channels 0..N-1 - the picker doesn't apply.
            received = _with_rf_retry(lambda: send_and_receive(signal))
        else:
            received = _with_rf_retry(
                lambda: send_and_receive(signal, channel=channel))

    out_path = out_dir / "output.f32"
    if received.ndim == 2:
        out_path.write_bytes(encode_mimo(received))
    else:
        out_path.write_bytes(encode_siso(received))


def _server_running(db):
    row = db.query(ServerState).filter(ServerState.id == 1).first()
    return (row is None) or (row.state == "running")


def poll_and_process():
    db = SessionLocal()
    try:
        if not _server_running(db):
            return False

        task = db.query(Task).filter(Task.state == "PD").order_by(Task.created_at.asc()).first()
        if not task:
            return False

        task_uid = str(task.uid)
        task.state = "R"
        db.commit()

        try:
            process_f32(task_uid)
            task.state = "D"
            task.done_at = datetime.utcnow()
            db.commit()
        except Exception:
            task.state = "D"
            task.done_at = datetime.utcnow()
            task.error_message = traceback.format_exc()
            db.commit()

        return True
    finally:
        db.close()


def recover_stale_tasks():
    """Mark tasks left in 'R' by a crashed/restarted worker as failed.

    Without this, their clients would poll forever and the tasks would
    look active even though nobody is processing them."""
    db = SessionLocal()
    try:
        stale = db.query(Task).filter(Task.state == "R").all()
        for task in stale:
            task.state = "D"
            task.done_at = datetime.utcnow()
            task.error_message = (
                "The worker was restarted while this task was running. "
                "Please resubmit."
            )
        if stale:
            db.commit()
            print(f"[startup] recovered {len(stale)} stale running task(s)")
    except Exception:
        db.rollback()
    finally:
        db.close()


def cleanup_old_tasks():
    cutoff = datetime.utcnow() - timedelta(hours=TASK_TTL_HOURS)
    db = SessionLocal()
    try:
        old = db.query(Task).filter(Task.created_at < cutoff).all()
        for task in old:
            uid = str(task.uid)
            for d in (INPUT_DIR / uid, OUTPUT_DIR / uid):
                if d.exists():
                    shutil.rmtree(d)
            db.delete(task)
        if old:
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def main():
    try:
        recover_stale_tasks()
    except Exception:
        pass   # DB may not be up yet; the poll loop retries anyway
    counter = 0
    while True:
        try:
            had_work = poll_and_process()
        except Exception:
            had_work = False
        if not had_work:
            time.sleep(POLL_INTERVAL)
        counter += 1
        if counter >= 30:
            cleanup_old_tasks()
            counter = 0


if __name__ == "__main__":
    main()
