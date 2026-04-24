import uuid
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    eth_id = Column(String(64), unique=True, nullable=False)
    email = Column(String(255), nullable=False)
    first_name = Column(String(128))
    last_name = Column(String(128))
    created_at = Column(DateTime, default=datetime.utcnow)


class Token(Base):
    __tablename__ = "tokens"
    id = Column(Integer, primary_key=True)
    token = Column(String(255), unique=True, nullable=False)
    label = Column(String(255))
    is_default = Column(Boolean, default=False)
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
    __tablename__ = "logs"
    id = Column(Integer, primary_key=True)
    token_id = Column(Integer, ForeignKey("tokens.id"), nullable=True)
    eth_id = Column(String(64), nullable=True)
    action = Column(String(50), nullable=False)
    n_samples = Column(Integer, nullable=True)
    detail = Column(Text, nullable=True)
    ip = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ServerState(Base):
    __tablename__ = "server_state"
    id = Column(Integer, primary_key=True)
    state = Column(String(16), nullable=False, default="running")
    updated_at = Column(DateTime, default=datetime.utcnow)


class SettingOverride(Base):
    __tablename__ = "setting_overrides"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)
