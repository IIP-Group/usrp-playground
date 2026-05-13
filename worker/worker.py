import os
import sys
import time
import shutil
import traceback
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Task, ServerState
from channel import send_and_receive

DATA_DIR = Path("/data")
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"

POLL_INTERVAL = 2
TASK_TTL_HOURS = int(os.getenv("TASK_TTL_HOURS", "24"))
MAX_SAMPLES = int(os.getenv("MAX_SAMPLES", "2500000"))


def process_f32(task_uid):
    from usrp_testbed_library.mimo_format import (
        is_mimo_blob, decode_mimo, decode_siso, encode_siso, encode_mimo,
    )

    out_dir = OUTPUT_DIR / task_uid
    out_dir.mkdir(parents=True, exist_ok=True)

    blob = (INPUT_DIR / task_uid / "input.f32").read_bytes()

    if is_mimo_blob(blob):
        signal = decode_mimo(blob)           # shape (n_samples, n_channels)
        n_samples = signal.shape[0]
    else:
        signal = decode_siso(blob)           # shape (n_samples,)
        n_samples = signal.shape[0]

    if n_samples > MAX_SAMPLES:
        raise ValueError(f"Signal too large: {n_samples} samples (max {MAX_SAMPLES})")

    received = send_and_receive(signal)

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
