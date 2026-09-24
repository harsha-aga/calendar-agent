"""Full and incremental sync from Google Calendar into the events table."""

import logging
import threading
from collections import defaultdict
from datetime import date, datetime, time, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from . import google_api
from .db import Account, Event, SessionLocal, SyncState
from .tokens import get_access_token

log = logging.getLogger(__name__)

# One lock per calendar so a burst of webhooks can't run overlapping syncs.
# (Fine for a single server process; use a Postgres advisory lock when you scale out.)
_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


# ---------- turning Google's JSON into a row ----------

def _parse_datetime(value: str) -> datetime:
    # Python < 3.11 doesn't accept a trailing "Z".
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_when(when: dict | None) -> tuple[datetime | None, bool, str | None]:
    """Google gives either {"dateTime": ..., "timeZone": ...} or {"date": "YYYY-MM-DD"}."""
    if not when:
        return None, False, None
    if "dateTime" in when:
        return _parse_datetime(when["dateTime"]), False, when.get("timeZone")
    if "date" in when:
        d = date.fromisoformat(when["date"])
        return datetime.combine(d, time.min, tzinfo=timezone.utc), True, when.get("timeZone")
    return None, False, None


def normalize_event(raw: dict) -> dict:
    start_at, all_day, tz = _parse_when(raw.get("start"))
    end_at, _, _ = _parse_when(raw.get("end"))
    original_start_at, _, _ = _parse_when(raw.get("originalStartTime"))
    updated = raw.get("updated")
    return {
        "google_event_id": raw["id"],
        "status": raw.get("status", "confirmed"),
        "summary": raw.get("summary"),
        "description": raw.get("description"),
        "location": raw.get("location"),
        "start_at": start_at,
        "end_at": end_at,
        "all_day": all_day,
        "time_zone": tz,
        "recurrence": raw.get("recurrence"),
        "recurring_event_id": raw.get("recurringEventId"),
        "original_start_at": original_start_at,
        "organizer_email": (raw.get("organizer") or {}).get("email"),
        "attendees": [
            {
                "email": a.get("email"),
                "name": a.get("displayName"),
                "response": a.get("responseStatus"),
                "optional": a.get("optional", False),
                "organizer": a.get("organizer", False),
                "self": a.get("self", False),
            }
            for a in raw.get("attendees", [])
        ]
        or None,
        "html_link": raw.get("htmlLink"),
        "google_updated_at": _parse_datetime(updated) if updated else None,
        "raw": raw,
    }


def _is_bare_deletion(raw: dict) -> bool:
    """Deleted events often arrive as just {"id", "status": "cancelled"} with no details."""
    return raw.get("status") == "cancelled" and "start" not in raw


def _upsert_events(session: Session, state: SyncState, items: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    for raw in items:
        key = {
            "account_id": state.account_id,
            "calendar_id": state.calendar_id,
            "google_event_id": raw["id"],
        }
        if _is_bare_deletion(raw):
            # Mark it cancelled but keep the details we already had.
            row = {**key, "status": "cancelled", "raw": raw, "synced_at": now}
            update_cols = {"status": "cancelled", "synced_at": now}
            # A cancelled single occurrence of a recurring series says which one it was.
            if raw.get("recurringEventId"):
                row["recurring_event_id"] = raw["recurringEventId"]
                original_start_at, _, _ = _parse_when(raw.get("originalStartTime"))
                row["original_start_at"] = update_cols["original_start_at"] = original_start_at
        else:
            row = {**key, **normalize_event(raw), "synced_at": now}
            update_cols = {k: v for k, v in row.items() if k not in key}

        stmt = insert(Event).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "calendar_id", "google_event_id"],
            set_=update_cols,
        )
        session.execute(stmt)


# ---------- the sync itself ----------

def _run_listing(session: Session, account: Account, state: SyncState, sync_token: str | None) -> int:
    """Page through events.list, saving each page. Returns the number of events processed."""
    count = 0
    page_token = None
    while True:
        access_token = get_access_token(session, account)
        page = google_api.list_events_page(
            access_token, state.calendar_id, sync_token=sync_token, page_token=page_token
        )
        items = page.get("items", [])
        _upsert_events(session, state, items)
        session.commit()
        count += len(items)

        page_token = page.get("nextPageToken")
        if page_token:
            continue
        # The last page carries the token for the next incremental sync.
        state.sync_token = page.get("nextSyncToken")
        state.last_synced_at = datetime.now(timezone.utc)
        state.last_error = None
        session.commit()
        return count


def full_sync(session: Session, account: Account, state: SyncState) -> int:
    log.info("Full sync: %s / %s", account.email, state.calendar_id)
    session.execute(
        delete(Event).where(
            Event.account_id == state.account_id, Event.calendar_id == state.calendar_id
        )
    )
    state.sync_token = None
    session.commit()
    return _run_listing(session, account, state, sync_token=None)


def incremental_sync(session: Session, account: Account, state: SyncState) -> int:
    try:
        n = _run_listing(session, account, state, sync_token=state.sync_token)
        if n:
            log.info("Incremental sync: %s / %s, %d changed", account.email, state.calendar_id, n)
        return n
    except google_api.SyncTokenExpired:
        session.rollback()
        log.warning("Sync token expired for %s / %s; running full sync", account.email, state.calendar_id)
        return full_sync(session, account, state)


def sync_calendar(state_id: int, force_full: bool = False) -> int:
    """Bring one calendar up to date. Safe to call from any thread, any number of times."""
    with _locks[state_id]:
        with SessionLocal() as session:
            state = session.get(SyncState, state_id)
            if state is None:
                return 0
            account = session.get(Account, state.account_id)
            try:
                if force_full or not state.sync_token:
                    return full_sync(session, account, state)
                return incremental_sync(session, account, state)
            except Exception as exc:
                session.rollback()
                state = session.get(SyncState, state_id)
                state.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                session.commit()
                log.exception("Sync failed for sync_state %s", state_id)
                raise


def sync_all() -> None:
    """Catch-up sync for every calendar we know about (runs on a timer)."""
    with SessionLocal() as session:
        ids = session.scalars(
            select(SyncState.id).join(Account).where(Account.needs_reauth.is_(False))
        ).all()
    for state_id in ids:
        try:
            sync_calendar(state_id)
        except Exception:
            pass  # already logged and recorded in sync_state.last_error
