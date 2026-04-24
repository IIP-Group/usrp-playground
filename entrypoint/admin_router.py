"""
Admin API routes.

Alle Endpoints unter /admin/api/* sind per Session-Cookie geschützt (ausser /login).
Antworten sind JSON. Das Frontend ist statisch (nginx) und konsumiert diese API.
"""
import csv
import io
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, Request, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from database import get_db
from models import User, Token, Task, Log
import auth
import token_gen
import email_sender
import server_state
import settings_store


router = APIRouter(prefix="/admin/api", tags=["admin"])


ETH_ID_RE = re.compile(r"^[a-z][a-z0-9]{2,31}$", re.IGNORECASE)


# ---------------- LOGIN ----------------

@router.post("/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    if not auth.check_credentials(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    auth.issue_session(response, username)
    return {"ok": True, "username": username}


@router.post("/logout")
def logout(response: Response):
    auth.clear_session(response)
    return {"ok": True}


@router.get("/me")
def me(session: dict = Depends(auth.require_admin)):
    return {"username": session["username"], "exp": session["exp"]}


# ---------------- DASHBOARD ----------------

@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), _: dict = Depends(auth.require_admin)):
    now = datetime.utcnow()
    h24 = now - timedelta(hours=24)
    h1 = now - timedelta(hours=1)

    # Queue state
    pending = db.query(Task).filter(Task.state == "PD").count()
    running = db.query(Task).filter(Task.state == "R").count()

    # Submits
    submits_24h = db.query(Log).filter(Log.action == "submit", Log.created_at >= h24).count()
    submits_1h = db.query(Log).filter(Log.action == "submit", Log.created_at >= h1).count()

    # Users
    users_total = db.query(User).count()

    # Server state
    usrp_state = server_state.get_state(db)

    # Errors last 24h
    errors_24h = db.query(Log).filter(Log.action.in_(("task_error", "auth_failed")),
                                      Log.created_at >= h24).count()

    return {
        "pending": pending,
        "running": running,
        "submits_24h": submits_24h,
        "submits_1h": submits_1h,
        "users_total": users_total,
        "usrp_state": usrp_state,
        "errors_24h": errors_24h,
        "now": now.isoformat(),
    }


@router.get("/logs")
def logs(
    limit: int = 100,
    action: Optional[str] = None,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    q = db.query(Log).order_by(desc(Log.created_at))
    if action:
        q = q.filter(Log.action == action)
    rows = q.limit(min(limit, 500)).all()
    return [{
        "id": r.id,
        "eth_id": r.eth_id,
        "token_id": r.token_id,
        "action": r.action,
        "n_samples": r.n_samples,
        "detail": r.detail,
        "ip": r.ip,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.post("/logs/bulk")
def logs_bulk(
    payload: dict,
    db: Session = Depends(get_db),
    session: dict = Depends(auth.require_admin),
):
    """
    Destructive operations on logs. Require the admin password to be re-entered.
    payload: {action: "delete"|"delete_all", ids?: [int,...], password: str}
    """
    password = payload.get("password", "")
    if not auth.check_credentials(session["username"], password):
        raise HTTPException(status_code=401, detail="Passwort falsch")

    action = payload.get("action")
    if action == "delete_all":
        n = db.query(Log).delete()
        db.commit()
        return {"ok": True, "action": action, "count": n}

    ids = payload.get("ids") or []
    if action == "delete":
        if not ids:
            raise HTTPException(status_code=400, detail="Keine Logs ausgewählt")
        n = db.query(Log).filter(Log.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return {"ok": True, "action": action, "count": n}

    raise HTTPException(status_code=400, detail=f"Unbekannte Aktion '{action}'")


@router.get("/tasks")
def tasks(
    limit: int = 50,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    rows = db.query(Task).order_by(desc(Task.created_at)).limit(min(limit, 500)).all()
    # Attach eth_id via token -> user
    out = []
    for t in rows:
        eth_id = None
        tok = db.query(Token).filter(Token.id == t.token_id).first()
        if tok and tok.user_id:
            u = db.query(User).filter(User.id == tok.user_id).first()
            eth_id = u.eth_id if u else None
        elif tok and tok.is_default:
            eth_id = "[default]"
        out.append({
            "uid": str(t.uid),
            "state": t.state,
            "n_samples": t.n_samples,
            "eth_id": eth_id,
            "error_message": t.error_message,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "done_at": t.done_at.isoformat() if t.done_at else None,
        })
    return out


# ---------------- USERS ----------------

def _user_row(db: Session, u: User) -> dict:
    tok = db.query(Token).filter(Token.user_id == u.id).first()
    submits_count = 0
    last_submit = None
    if tok:
        submits_count = db.query(Log).filter(
            Log.token_id == tok.id, Log.action == "submit"
        ).count()
        last = db.query(Log).filter(
            Log.token_id == tok.id, Log.action == "submit"
        ).order_by(desc(Log.created_at)).first()
        if last:
            last_submit = last.created_at.isoformat()
    return {
        "id": u.id,
        "eth_id": u.eth_id,
        "email": u.email,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "token": tok.token if tok else None,
        "submits": submits_count,
        "last_submit": last_submit,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/users")
def users_list(
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    query = db.query(User)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            func.lower(User.eth_id).like(like)
            | func.lower(User.email).like(like)
            | func.lower(User.first_name).like(like)
            | func.lower(User.last_name).like(like)
        )
    users = query.order_by(User.eth_id).all()
    return [_user_row(db, u) for u in users]


@router.post("/users")
def users_create(
    payload: dict,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    eth_id = (payload.get("eth_id") or "").strip().lower()
    if not ETH_ID_RE.match(eth_id):
        raise HTTPException(status_code=400, detail="Invalid ETH ID")
    email = (payload.get("email") or f"{eth_id}@ethz.ch").strip()
    first = (payload.get("first_name") or "").strip()
    last = (payload.get("last_name") or "").strip()

    existing = db.query(User).filter(User.eth_id == eth_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")

    u = User(eth_id=eth_id, email=email, first_name=first, last_name=last)
    db.add(u)
    db.flush()

    tok = Token(token=token_gen.generate_token(eth_id),
                label=f"user:{eth_id}", user_id=u.id)
    db.add(tok)
    db.commit()
    db.refresh(u)
    return _user_row(db, u)


def _delete_users_cascade(db: Session, user_ids: list[int]) -> int:
    """Delete users along with their dependent rows (tokens, tasks, logs).
    Returns the number of users removed."""
    if not user_ids:
        return 0
    token_ids = [t.id for t in db.query(Token).filter(Token.user_id.in_(user_ids)).all()]
    if token_ids:
        db.query(Log).filter(Log.token_id.in_(token_ids)).delete(synchronize_session=False)
        db.query(Task).filter(Task.token_id.in_(token_ids)).delete(synchronize_session=False)
    # Tokens get removed via ON DELETE CASCADE on users.id
    n = db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.commit()
    return n


@router.delete("/users/{user_id}")
def users_delete(
    user_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    _delete_users_cascade(db, [u.id])
    return {"ok": True}


@router.post("/users/bulk")
def users_bulk(
    payload: dict,
    db: Session = Depends(get_db),
    session: dict = Depends(auth.require_admin),
):
    """action: delete | regenerate.  Delete requires password re-entry."""
    action = payload.get("action")
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=400, detail="Keine Users ausgewählt")

    if action == "delete":
        password = payload.get("password", "")
        if not auth.check_credentials(session["username"], password):
            raise HTTPException(status_code=401, detail="Passwort falsch")
        n = _delete_users_cascade(db, ids)
        return {"ok": True, "action": action, "count": n}

    if action == "regenerate":
        users = db.query(User).filter(User.id.in_(ids)).all()
        updated = []
        for u in users:
            tok = db.query(Token).filter(Token.user_id == u.id).first()
            new_tok = token_gen.generate_token(u.eth_id)
            if tok:
                tok.token = new_tok
            else:
                db.add(Token(token=new_tok, label=f"user:{u.eth_id}", user_id=u.id))
            updated.append(u.eth_id)
        db.commit()
        return {"ok": True, "action": action, "count": len(updated), "users": updated}

    raise HTTPException(status_code=400, detail=f"Unbekannte Aktion '{action}'")


@router.post("/users/upload_csv")
async def users_upload_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    """
    CSV format (Moodle participants):
        "First name","Last name","ID number","Email address",Groups
    Wir ziehen First/Last/ID-number/Email. ID number = rsahleanu@ethz.ch → ETH-ID.
    """
    raw = (await file.read()).decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    header = next(reader, None)
    created = 0
    skipped = 0
    errors: list[str] = []

    for row in reader:
        if not row or all(not c.strip() for c in row):
            continue
        # Expected columns
        first = row[0].strip() if len(row) > 0 else ""
        last = row[1].strip() if len(row) > 1 else ""
        idnum = row[2].strip() if len(row) > 2 else ""
        email = row[3].strip() if len(row) > 3 else ""

        # Pull ETH-ID from whichever column has xxx@ethz.ch
        eth_id = None
        for cand in (idnum, email):
            if "@" in cand:
                eth_id = cand.split("@", 1)[0].strip().lower()
                break
        if not eth_id or not ETH_ID_RE.match(eth_id):
            skipped += 1
            errors.append(f"skip: {first} {last} / {idnum or email}")
            continue

        if db.query(User).filter(User.eth_id == eth_id).first():
            skipped += 1
            continue

        full_email = email if email else f"{eth_id}@ethz.ch"
        u = User(eth_id=eth_id, email=full_email, first_name=first, last_name=last)
        db.add(u)
        db.flush()
        db.add(Token(token=token_gen.generate_token(eth_id),
                     label=f"user:{eth_id}", user_id=u.id))
        created += 1

    db.commit()
    return {"ok": True, "created": created, "skipped": skipped, "errors": errors[:20]}


# ---------------- SMTP / EMAIL ----------------

@router.post("/email/test")
def email_test(payload: dict, _: dict = Depends(auth.require_admin)):
    creds = email_sender.SmtpCredentials(
        username=payload.get("username", ""),
        password=payload.get("password", ""),
        host=payload.get("host") or email_sender.DEFAULT_SMTP_HOST,
        port=int(payload.get("port") or email_sender.DEFAULT_SMTP_PORT),
        sender_email=payload.get("sender_email"),
    )
    ok, msg = email_sender.test_credentials(creds)
    return {"ok": ok, "message": msg}


@router.post("/email/send")
def email_send(
    payload: dict,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    creds = email_sender.SmtpCredentials(
        username=payload.get("username", ""),
        password=payload.get("password", ""),
        host=payload.get("host") or email_sender.DEFAULT_SMTP_HOST,
        port=int(payload.get("port") or email_sender.DEFAULT_SMTP_PORT),
        sender_email=payload.get("sender_email"),
    )
    subject = payload.get("subject", "Your USRP Benchmark Token")
    body = payload.get("body", "Hi [FIRST_NAME],\n\nYour token: [TOKEN]\n")
    user_ids = payload.get("user_ids") or []

    if not user_ids:
        raise HTTPException(status_code=400, detail="No users selected")

    users = db.query(User).filter(User.id.in_(user_ids)).all()
    results = []
    for u in users:
        tok = db.query(Token).filter(Token.user_id == u.id).first()
        if not tok:
            results.append({"eth_id": u.eth_id, "ok": False, "message": "no token"})
            continue
        ok, msg = email_sender.send_token_email(
            creds=creds,
            to_email=u.email,
            subject=subject,
            body_template=body,
            token=tok.token,
            first_name=u.first_name or "",
            last_name=u.last_name or "",
            eth_id=u.eth_id,
        )
        results.append({"eth_id": u.eth_id, "ok": ok, "message": msg})
    return {"ok": True, "results": results}


# ---------------- SERVER CONTROL ----------------

@router.get("/server")
def server_get(db: Session = Depends(get_db), _: dict = Depends(auth.require_admin)):
    return {"state": server_state.get_state(db)}


@router.post("/server")
def server_set(
    payload: dict,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    state = payload.get("state", "").lower()
    try:
        new_state = server_state.set_state(db, state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "state": new_state}


# ---------------- SETTINGS ----------------

@router.get("/settings")
def settings_get(db: Session = Depends(get_db), _: dict = Depends(auth.require_admin)):
    return settings_store.all_current(db)


@router.put("/settings")
def settings_put(
    payload: dict,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    """payload: {key: value, ...} — only EDITABLE_KEYS are accepted."""
    applied = []
    errors = {}
    for key, value in payload.items():
        try:
            settings_store.set_override(db, key, str(value))
            applied.append(key)
        except Exception as e:
            errors[key] = str(e)
    return {"ok": len(errors) == 0, "applied": applied, "errors": errors}


@router.delete("/settings/{key}")
def settings_reset_one(
    key: str,
    db: Session = Depends(get_db),
    _: dict = Depends(auth.require_admin),
):
    settings_store.clear_override(db, key)
    return {"ok": True}
