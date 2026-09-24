"""The web server: Google login, the webhook receiver, and a small status page."""

import html
import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from . import agent, config, google_api
from .chat_page import CHAT_HTML
from .channels import renew_expiring_channels, start_channel, webhooks_enabled
from .crypto import encrypt
from .db import Account, Event, SessionLocal, SyncState, init_db
from .sync import sync_all, sync_calendar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("calendar-agent")


# ---------- background jobs ----------

_stop = threading.Event()


def _background_loop() -> None:
    """Every few minutes: renew webhook channels about to expire and run a
    catch-up sync, in case a webhook was missed (or webhooks are off)."""
    while not _stop.wait(config.RECONCILE_INTERVAL_SECONDS):
        try:
            renew_expiring_channels()
            sync_all()
        except Exception:
            log.exception("Background job failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not webhooks_enabled():
        log.warning(
            "No https WEBHOOK_BASE_URL set: running in polling-only mode "
            "(changes appear within %ss)", config.RECONCILE_INTERVAL_SECONDS
        )
    worker = threading.Thread(target=_background_loop, daemon=True)
    worker.start()
    yield
    _stop.set()


app = FastAPI(title="Calendar Agent: sync service", lifespan=lifespan)


# ---------- OAuth login ----------

# state value -> time issued. Protects the login flow against CSRF.
# (In memory is fine for local development with one server process.)
_pending_states: dict[str, float] = {}


@app.get("/oauth/login")
def oauth_login():
    now = time.time()
    for s, issued in list(_pending_states.items()):
        if now - issued > 600:
            del _pending_states[s]
    state = secrets.token_urlsafe(24)
    _pending_states[state] = now
    return RedirectResponse(google_api.build_auth_url(state))


def _connect_calendar(state_id: int) -> None:
    """After login: first full sync, then open the webhook channel."""
    try:
        sync_calendar(state_id, force_full=True)
        start_channel(state_id)
    except Exception:
        log.exception("Initial setup failed for sync_state %s", state_id)


@app.get("/oauth/callback")
def oauth_callback(background: BackgroundTasks, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"Google sign-in was not completed: {error}")
    if not state or _pending_states.pop(state, None) is None:
        raise HTTPException(400, "Login link expired or invalid. Start again at /oauth/login.")
    if not code:
        raise HTTPException(400, "Missing authorization code.")

    tokens = google_api.exchange_code(code)
    granted = tokens.get("scope", "").split()
    if "https://www.googleapis.com/auth/calendar.events" not in granted:
        raise HTTPException(
            400,
            "Calendar access wasn't granted. Start again at /oauth/login and tick the calendar checkbox.",
        )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(400, "Google didn't return a refresh token. Start again at /oauth/login.")

    user = google_api.get_userinfo(tokens["access_token"])

    with SessionLocal() as session:
        account = session.scalar(select(Account).where(Account.google_sub == user["sub"]))
        if account is None:
            account = Account(google_sub=user["sub"], email=user.get("email", ""))
            session.add(account)
        account.email = user.get("email", account.email)
        account.refresh_token_enc = encrypt(refresh_token)
        account.access_token_enc = encrypt(tokens["access_token"])
        account.access_token_expires_at = google_api.expiry_from(tokens)
        account.needs_reauth = False
        session.flush()

        sync_state = session.scalar(
            select(SyncState).where(
                SyncState.account_id == account.id,
                SyncState.calendar_id == config.DEFAULT_CALENDAR_ID,
            )
        )
        if sync_state is None:
            sync_state = SyncState(account_id=account.id, calendar_id=config.DEFAULT_CALENDAR_ID)
            session.add(sync_state)
        session.commit()
        state_id = sync_state.id

    background.add_task(_connect_calendar, state_id)
    return RedirectResponse("/", status_code=303)


# ---------- webhook receiver ----------

@app.post("/webhooks/google")
def google_webhook(request: Request, background: BackgroundTasks):
    """Google calls this when something changed. The notification carries no
    event data, only "something changed", so we answer fast and sync in the background."""
    channel_id = request.headers.get("X-Goog-Channel-ID")
    channel_token = request.headers.get("X-Goog-Channel-Token")
    resource_id = request.headers.get("X-Goog-Resource-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")

    with SessionLocal() as session:
        state = session.scalar(select(SyncState).where(SyncState.channel_id == channel_id))
        if state is None:
            # An old or unknown channel. 200 so Google stops retrying.
            return {"ok": True, "ignored": "unknown channel"}
        if not state.channel_token or not secrets.compare_digest(state.channel_token, channel_token or ""):
            raise HTTPException(403, "Bad channel token")
        if state.channel_resource_id and resource_id != state.channel_resource_id:
            raise HTTPException(403, "Unexpected resource")
        state_id = state.id

    if resource_state == "sync":
        return {"ok": True}  # just confirms the channel was created

    background.add_task(_sync_quietly, state_id)
    return {"ok": True}


def _sync_quietly(state_id: int) -> None:
    try:
        sync_calendar(state_id)
    except Exception:
        pass  # logged and stored in sync_state.last_error


# ---------- manual controls & inspection ----------

@app.post("/sync/{state_id}")
def trigger_sync(state_id: int, full: bool = False):
    n = sync_calendar(state_id, force_full=full)
    return {"ok": True, "events_processed": n}


@app.get("/events")
def list_events(account_id: int | None = None, include_cancelled: bool = False, limit: int = 50):
    """Most recently changed events first, handy for watching sync work."""
    with SessionLocal() as session:
        q = select(Event).order_by(Event.synced_at.desc(), Event.google_updated_at.desc().nulls_last())
        if account_id is not None:
            q = q.where(Event.account_id == account_id)
        if not include_cancelled:
            q = q.where(Event.status != "cancelled")
        rows = session.scalars(q.limit(min(limit, 500))).all()
        return [
            {
                "id": e.google_event_id,
                "summary": e.summary,
                "status": e.status,
                "start": e.start_at,
                "end": e.end_at,
                "all_day": e.all_day,
                "time_zone": e.time_zone,
                "recurrence": e.recurrence,
                "recurring_event_id": e.recurring_event_id,
                "original_start": e.original_start_at,
                "attendees": e.attendees,
                "updated_in_google": e.google_updated_at,
                "synced_at": e.synced_at,
            }
            for e in rows
        ]


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def status_page():
    with SessionLocal() as session:
        rows = session.execute(
            select(Account, SyncState).join(SyncState, SyncState.account_id == Account.id)
        ).all()
        counts = dict(
            session.execute(
                select(Event.account_id, func.count())
                .where(Event.status != "cancelled")
                .group_by(Event.account_id)
            ).all()
        )

    def fmt(dt: datetime | None) -> str:
        if not dt:
            return "never"
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    mode = (
        f"Webhooks on ({html.escape(config.WEBHOOK_BASE_URL)})"
        if webhooks_enabled()
        else f"Polling only, every {config.RECONCILE_INTERVAL_SECONDS}s (set WEBHOOK_BASE_URL for instant updates)"
    )
    body = "".join(
        f"<tr><td>{html.escape(a.email)}{' <b>(log in again)</b>' if a.needs_reauth else ''}</td>"
        f"<td>{html.escape(s.calendar_id)}</td><td>{counts.get(a.id, 0)}</td>"
        f"<td>{fmt(s.last_synced_at)}</td><td>{fmt(s.channel_expires_at) if s.channel_id else 'none'}</td>"
        f"<td>{html.escape(s.last_error or '')}</td>"
        f"<td><form method=post action=/sync/{s.id}><button>Sync now</button></form></td></tr>"
        for a, s in rows
    ) or "<tr><td colspan=7>No accounts yet.</td></tr>"

    return f"""<!doctype html><html><head><meta charset=utf-8><title>Calendar sync</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:8px;text-align:left;font-size:14px}}
a.btn{{display:inline-block;padding:8px 14px;background:#1a73e8;color:#fff;border-radius:6px;text-decoration:none}}</style>
</head><body><h1>Calendar sync</h1><p>{mode}</p>
<p><a class=btn href=/chat>Open the chat</a> &nbsp; <a class=btn href=/oauth/login>Connect a Google account</a> &nbsp; <a href=/events>View synced events (JSON)</a></p>
<table><tr><th>Account</th><th>Calendar</th><th>Events</th><th>Last synced</th><th>Webhook expires</th><th>Last error</th><th></th></tr>
{body}</table></body></html>"""


# ---------- Step 3: chat with the agent ----------

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    account_id: int | None = None


@app.get("/chat", response_class=HTMLResponse)
def chat_page():
    return CHAT_HTML


@app.post("/api/chat")
def api_chat(body: ChatRequest):
    try:
        reply = agent.chat([m.model_dump() for m in body.messages], body.account_id)
        return {"reply": reply}
    except agent.AgentError as exc:
        return {"error": str(exc)}
    except Exception:
        log.exception("Chat failed")
        return {"error": "Something went wrong. The terminal running the app has the details."}


@app.get("/api/free")
def api_free(start: str, end: str, min_minutes: int = 30, working_hours_only: bool = True, account_id: int | None = None):
    """The scheduling engine without the AI, e.g. /api/free?start=2026-10-01T12:00&end=2026-10-01T17:00"""
    try:
        acct, _ = agent.pick_account(account_id)
    except agent.AgentError as exc:
        raise HTTPException(400, str(exc))
    return json.loads(
        agent.run_tool(
            acct,
            "find_free_time",
            {"start": start, "end": end, "min_duration_minutes": min_minutes, "working_hours_only": working_hours_only},
        )
    )
