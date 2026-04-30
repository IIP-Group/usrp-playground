import uuid
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from database import Base


class User(Base):
    """
    Registered course participant. Owns one token.
    ETH-Kürzel (eth_id) is the part before @ethz.ch — used for display/logging.
    """
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    eth_id = Column(String(64), unique=True, nullable=False)   # z.B. "rsahleanu"
    email = Column(String(255), nullable=False)                # volle E-Mail
    first_name = Column(String(128))
    last_name = Column(String(128))
    tags = Column(Text, nullable=False, default="")            # comma-separated, lowercase
    created_at = Column(DateTime, default=datetime.utcnow)


class Token(Base):
    __tablename__ = "tokens"
    id = Column(Integer, primary_key=True)
    token = Column(String(255), unique=True, nullable=False)
    label = Column(String(255))
    is_default = Column(Boolean, default=False)
    # user_id optional — default token has no user
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Task(Base):
    __tablename__ = "tasks"
    uid = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_id = Column(Integer, ForeignKey("tokens.id"), nullable=False)
    state = Column(String(2), nullable=False, default="PD")
    n_samples = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    done_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)


class Log(Base):
    """
    Simple audit log. Only stores: who (token_id, eth_id shown in admin),
    when (created_at), and what (action, detail, n_samples).
    """
    __tablename__ = "logs"
    id = Column(Integer, primary_key=True)
    token_id = Column(Integer, ForeignKey("tokens.id"), nullable=True)
    eth_id = Column(String(64), nullable=True)     # denormalised for fast display
    action = Column(String(50), nullable=False)    # e.g. "submit", "download", "auth_failed"
    n_samples = Column(Integer, nullable=True)     # samples submitted (for submit action)
    detail = Column(Text, nullable=True)
    ip = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ServerState(Base):
    """
    Single-row table. Controls whether the USRP processing pipeline is active.
    When state='sleeping', WebSocket connections get a "Server is currently
    Sleeping zzZZ...." message and are closed.
    """
    __tablename__ = "server_state"
    id = Column(Integer, primary_key=True)     # always 1
    state = Column(String(16), nullable=False, default="running")  # running | sleeping
    updated_at = Column(DateTime, default=datetime.utcnow)


class SettingOverride(Base):
    """
    Runtime overrides for .env values that are editable from the admin panel.
    When present, takes precedence over .env for the USRP-relevant params
    (guard times, LBT, duty cycle, ...).
    """
    __tablename__ = "setting_overrides"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)
