"""Creating, renewing and stopping Google push-notification channels (webhooks)."""

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import config, google_api
from .db import Account, SessionLocal, SyncState
from .tokens import get_access_token

log = logging.getLogger(__name__)


def webhooks_enabled() -> bool:
    return config.WEBHOOK_BASE_URL.startswith("https://")


def start_channel(state_id: int) -> None:
    """Open a new webhook channel for a calendar, then close the old one (if any)."""
    if not webhooks_enabled():
        log.info("WEBHOOK_BASE_URL is not an https URL; polling only, no webhook channel")
        return

    with SessionLocal() as session:
        state = session.get(SyncState, state_id)
        account = session.get(Account, state.account_id)
        access_token = get_access_token(session, account)

        old_channel_id, old_resource_id = state.channel_id, state.channel_resource_id
        channel_id = str(uuid.uuid4())
        channel_token = secrets.token_urlsafe(32)

        # Save the new channel before registering it, so the first notification
        # (which can arrive immediately) is recognized.
        state.channel_id = channel_id
        state.channel_token = channel_token
        state.channel_resource_id = None
        session.commit()

        try:
            resp = google_api.watch_events(
                access_token,
                state.calendar_id,
                channel_id,
                channel_token,
                f"{config.WEBHOOK_BASE_URL}/webhooks/google",
            )
        except Exception:
            state.channel_id, state.channel_resource_id = old_channel_id, old_resource_id
            session.commit()
            raise

        state.channel_resource_id = resp["resourceId"]
        state.channel_expires_at = datetime.fromtimestamp(
            int(resp["expiration"]) / 1000, tz=timezone.utc
        )
        session.commit()
        log.info("Webhook channel open for %s until %s", account.email, state.channel_expires_at)

        if old_channel_id and old_resource_id:
            try:
                google_api.stop_channel(access_token, old_channel_id, old_resource_id)
            except Exception:
                log.warning("Could not stop old channel %s (it will expire on its own)", old_channel_id)


def renew_expiring_channels() -> None:
    if not webhooks_enabled():
        return
    cutoff = datetime.now(timezone.utc) + timedelta(hours=config.CHANNEL_RENEW_WITHIN_HOURS)
    with SessionLocal() as session:
        ids = session.scalars(
            select(SyncState.id)
            .join(Account)
            .where(Account.needs_reauth.is_(False))
            .where((SyncState.channel_expires_at.is_(None)) | (SyncState.channel_expires_at < cutoff))
        ).all()
    for state_id in ids:
        try:
            start_channel(state_id)
        except Exception:
            log.exception("Could not renew webhook channel for sync_state %s", state_id)
