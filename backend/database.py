"""
Database setup — PostgreSQL via asyncpg (Neon-compatible).
"""
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import Column, String, DateTime, ForeignKey, Text, Boolean, JSON, text
import uuid
from datetime import datetime, timezone

# ─── Database URL ─────────────────────────────────────────────────────────────
# SQLite fallback for local dev if DATABASE_URL is not set
_RAW_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./backlog_assistant.db")

# Neon (and most Postgres PaaS) deliver the URL as postgres:// or postgresql://
# SQLAlchemy async requires postgresql+asyncpg://
if _RAW_URL.startswith("postgres://"):
    _RAW_URL = _RAW_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif _RAW_URL.startswith("postgresql://") and "+asyncpg" not in _RAW_URL:
    _RAW_URL = _RAW_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

DATABASE_URL = _RAW_URL

_IS_POSTGRES = DATABASE_URL.startswith("postgresql")

# ─── Engine ───────────────────────────────────────────────────────────────────
# Neon uses serverless pooling — keep pool small to avoid exhausting connections
# on the free tier (max 10 concurrent connections).
if _IS_POSTGRES:
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_size=5,           # base pool size — safe for Neon free tier
        max_overflow=5,        # allow 5 extra connections under burst load
        pool_timeout=30,       # seconds to wait for a connection
        pool_recycle=300,      # recycle connections every 5 min (avoids Neon idle timeouts)
        pool_pre_ping=True,    # validate connections before use
        connect_args={
            "statement_cache_size": 0,  # required for PgBouncer / Neon pooler
        },
    )
else:
    # SQLite — simple config for local dev
    engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ─── ORM Base ─────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Models ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email           = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), default=_now)

    conversations  = relationship("Conversation",     back_populates="user",  cascade="all, delete-orphan", lazy="noload")
    refresh_tokens = relationship("RefreshToken",     back_populates="user",  cascade="all, delete-orphan", lazy="noload")
    reset_tokens   = relationship("PasswordResetToken", back_populates="user", cascade="all, delete-orphan", lazy="noload")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked    = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_now)

    user = relationship("User", back_populates="refresh_tokens", lazy="noload")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used       = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_now)

    user = relationship("User", back_populates="reset_tokens", lazy="noload")


class Conversation(Base):
    __tablename__ = "conversations"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title      = Column(String, default="New Session")
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now)

    user          = relationship("User",           back_populates="conversations", lazy="noload")
    messages      = relationship("Message",        back_populates="conversation",  cascade="all, delete-orphan",
                                 lazy="noload", order_by="Message.created_at")
    backlog_state = relationship("BacklogSession", back_populates="conversation",  cascade="all, delete-orphan",
                                 lazy="noload", uselist=False)


class Message(Base):
    __tablename__ = "messages"

    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role            = Column(String, nullable=False)   # user | assistant | system
    content         = Column(Text,   nullable=False)
    msg_type        = Column(String, default="text")   # text | file | review | result | meeting_notes | …
    created_at      = Column(DateTime(timezone=True), default=_now)

    conversation = relationship("Conversation", back_populates="messages", lazy="noload")


class BacklogSession(Base):
    """Persists the active backlog-agent state for one conversation."""
    __tablename__ = "backlog_sessions"

    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"),
                             nullable=False, unique=True, index=True)

    # Pipeline state
    pipeline_stage   = Column(String, default="idle")   # idle | uploaded | processing | review | done | general_query
    proposed_changes = Column(JSON,   nullable=True)    # serialised ProposedChange list
    review_index     = Column(String, default="0")      # index of the change being reviewed
    final_backlog    = Column(JSON,   nullable=True)    # full backlog after review
    changelog        = Column(JSON,   nullable=True)    # list of applied change summaries
    langsmith_run_id = Column(String, nullable=True)    # root LangSmith run ID for telemetry

    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now)

    conversation = relationship("Conversation", back_populates="backlog_state", lazy="noload")


# ─── Table creation & health check ───────────────────────────────────────────

async def create_tables() -> None:
    """Create all tables (idempotent — uses CREATE TABLE IF NOT EXISTS via checkfirst)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def check_db_connection() -> bool:
    """
    Verify the database is reachable.
    Returns True on success, False on failure.
    Used in the /health endpoint so ops can see DB status.
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ─── Session dependency ───────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()