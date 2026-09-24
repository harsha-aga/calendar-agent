"""Thin wrapper around the Google OAuth and Calendar REST endpoints we use."""

from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# One shared HTTP client. Tests swap this for a fake transport.
http = httpx.Client(timeout=30)


class GoogleAPIError(Exception):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"Google API error {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


class SyncTokenExpired(Exception):
    """Google returned 410 Gone: the sync token is no longer valid; do a full sync."""


class RefreshTokenRevoked(Exception):
    """The user revoked access (or the token expired); they must log in again."""


def _check(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        raise GoogleAPIError(resp.status_code, resp.text)
    return resp.json() if resp.content else {}


# ---------- OAuth ----------

def build_auth_url(state: str) -> str:
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(config.GOOGLE_SCOPES),
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # always return a refresh token, even on re-login
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Swap the one-time code from the callback for access + refresh tokens."""
    resp = http.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": config.OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    return _check(resp)


def refresh_access_token(refresh_token: str) -> dict:
    resp = http.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
    )
    if resp.status_code == 400 and "invalid_grant" in resp.text:
        raise RefreshTokenRevoked(resp.text)
    return _check(resp)


def get_userinfo(access_token: str) -> dict:
    resp = http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    return _check(resp)


def expiry_from(token_response: dict) -> datetime:
    seconds = int(token_response.get("expires_in", 3600))
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ---------- Calendar ----------

def _events_url(calendar_id: str) -> str:
    return f"{CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events"


def list_events_page(
    access_token: str,
    calendar_id: str,
    *,
    sync_token: str | None = None,
    page_token: str | None = None,
) -> dict:
    """Fetch one page of events.

    Without a sync_token this is a full listing; with one, Google returns only
    what changed since that token was issued (including deletions).
    """
    params: dict[str, str | int] = {
        "maxResults": 250,
        # Keep recurring series as one event plus exceptions rather than
        # expanding every occurrence (which would be huge and endless).
        "singleEvents": "false",
    }
    if page_token:
        params["pageToken"] = page_token
    elif sync_token:
        params["syncToken"] = sync_token

    resp = http.get(
        _events_url(calendar_id),
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if resp.status_code == 410:
        raise SyncTokenExpired()
    return _check(resp)


def watch_events(
    access_token: str, calendar_id: str, channel_id: str, channel_token: str, address: str
) -> dict:
    """Ask Google to POST to `address` whenever events on this calendar change."""
    resp = http.post(
        f"{_events_url(calendar_id)}/watch",
        json={
            "id": channel_id,
            "type": "web_hook",
            "address": address,
            "token": channel_token,  # echoed back in every notification so we can verify it
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return _check(resp)


def stop_channel(access_token: str, channel_id: str, resource_id: str) -> None:
    resp = http.post(
        f"{CALENDAR_API}/channels/stop",
        json={"id": channel_id, "resourceId": resource_id},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if resp.status_code == 404:
        return  # already gone
    _check(resp)
