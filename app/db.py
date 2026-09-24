"""Database connection and the three tables: accounts, sync_state, events."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# none_as_null: store Python None as SQL NULL rather than a JSON null.
JsonType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class Base(DeclarativeBase):
    pass


class Account(Base):
    """One connected Google account and its (encrypted) OAuth tokens."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when Google rejects the refresh token; the user must log in again.
    needs_reauth: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SyncState(Base):
    """Per-calendar sync bookkeeping: the sync token and the webhook channel."""

    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("account_id", "calendar_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    calendar_id: Mapped[str] = mapped_column(String(255))
    sync_token: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    channel_resource_id: Mapped[str | None] = mapped_column(String(255))
    channel_token: Mapped[str | None] = mapped_column(String(128))
    channel_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    """Our normalized copy of a Google Calendar event.

    Recurring events are stored the way Google returns them: one row for the
    series (with `recurrence` rules) plus one row per exception (a moved or
    cancelled occurrence), linked by `recurring_event_id`. Expanding a series
    into individual occurrences is the scheduling engine's job (Step 2).
    """

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("account_id", "calendar_id", "google_event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    calendar_id: Mapped[str] = mapped_column(String(255))
    google_event_id: Mapped[str] = mapped_column(String(1024))

    status: Mapped[str] = mapped_column(String(32))  # confirmed | tentative | cancelled
    summary: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    # Always stored in UTC. All-day events start/end at midnight UTC of their date.
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    time_zone: Mapped[str | None] = mapped_column(String(64))  # the event's own IANA zone

    recurrence: Mapped[list | None] = mapped_column(JsonType)  # e.g. ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
    recurring_event_id: Mapped[str | None] = mapped_column(String(1024), index=True)
    original_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organizer_email: Mapped[str | None] = mapped_column(String(320))
    attendees: Mapped[list | None] = mapped_column(JsonType)
    html_link: Mapped[str | None] = mapped_column(Text)
    google_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict | None] = mapped_column(JsonType)  # the full event exactly as Google sent it
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def init_db() -> None:
    """Create any missing tables. (Use Alembic migrations once the schema settles.)"""
    Base.metadata.create_all(engine)
