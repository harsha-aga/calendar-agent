"""Returns a valid access token for an account, refreshing it when needed."""

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import google_api
from .crypto import decrypt, encrypt
from .db import Account

_refresh_lock = threading.Lock()


def get_access_token(session: Session, account: Account) -> str:
    if account.needs_reauth:
        raise google_api.RefreshTokenRevoked(f"Account {account.email} must log in again")

    with _refresh_lock:
        session.refresh(account)  # another thread may have just refreshed it
        soon = datetime.now(timezone.utc) + timedelta(seconds=60)
        if (
            account.access_token_enc
            and account.access_token_expires_at
            and account.access_token_expires_at > soon
        ):
            return decrypt(account.access_token_enc)

        try:
            tokens = google_api.refresh_access_token(decrypt(account.refresh_token_enc))
        except google_api.RefreshTokenRevoked:
            account.needs_reauth = True
            session.commit()
            raise

        account.access_token_enc = encrypt(tokens["access_token"])
        account.access_token_expires_at = google_api.expiry_from(tokens)
        # Google occasionally rotates the refresh token; keep the newest one.
        if tokens.get("refresh_token"):
            account.refresh_token_enc = encrypt(tokens["refresh_token"])
        session.commit()
        return tokens["access_token"]
