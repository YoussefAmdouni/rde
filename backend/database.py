"""
Database setup — SQLite via aiosqlite for local dev.
"""
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import Column, String, DateTime, ForeignKey, Text, Boolean, JSON
import uuid
from datetime import datetime, timezone

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./backlog_assistant.db")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email           = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), default=_now)

    conversations  = relationship("Conversation", back_populates="user", cascade="all, delete-orphan", lazy="noload")
    refresh_tokens = relationship("RefreshToken",  back_populates="user", cascade="all, delete-orphan", lazy="noload")
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
    user     = relationship("User", back_populates="conversations", lazy="noload")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan",
                            lazy="noload", order_by="Message.created_at")
    # Store the backlog state for this session
    backlog_state = relationship("BacklogSession", back_populates="conversation",
                                 cascade="all, delete-orphan", lazy="noload", uselist=False)


class Message(Base):
    __tablename__ = "messages"
    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role            = Column(String, nullable=False)  # user | assistant | system
    content         = Column(Text, nullable=False)
    msg_type        = Column(String, default="text")  # text | file | review | result
    created_at      = Column(DateTime(timezone=True), default=_now)
    conversation = relationship("Conversation", back_populates="messages", lazy="noload")


class BacklogSession(Base):
    """Stores the active backlog agent state for a conversation."""
    __tablename__ = "backlog_sessions"
    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"),
                             nullable=False, unique=True, index=True)
    # No backlog_data — backlog lives in Pinecone, fetched at pipeline time
    pipeline_stage   = Column(String, default="idle")  # idle | uploaded | review | done
    proposed_changes = Column(JSON, nullable=True)      # serialized ProposedChange list
    review_index     = Column(String, default="0")      # which change is currently being reviewed
    final_backlog    = Column(JSON, nullable=True)      # full rebuilt backlog after review
    changelog        = Column(JSON, nullable=True)      # list of approved change summaries
    langsmith_run_id = Column(String, nullable=True)   # root LangSmith run ID for telemetry
    created_at       = Column(DateTime(timezone=True), default=_now)
    updated_at       = Column(DateTime(timezone=True), default=_now)
    conversation = relationship("Conversation", back_populates="backlog_state", lazy="noload")


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
