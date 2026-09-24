"""Settings, read from environment variables (and a local .env file)."""

import os
from datetime import time as dt_time

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing setting {name}. Copy .env.example to .env and fill it in."
        )
    return value


GOOGLE_CLIENT_ID = _required("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _required("GOOGLE_CLIENT_SECRET")
DATABASE_URL = _required("DATABASE_URL")
ENCRYPTION_KEY = _required("ENCRYPTION_KEY")

# Where this app runs locally. The OAuth redirect URI is derived from it and must
# match the one registered in Google Cloud exactly.
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:3000").rstrip("/")
OAUTH_REDIRECT_URI = f"{APP_BASE_URL}/oauth/callback"

# Public HTTPS address (e.g. your ngrok URL) that Google can reach for webhooks.
# Leave empty to run in polling-only mode.
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")

# How often to run a catch-up sync for every calendar, as a safety net for
# missed webhooks (or as the only sync mechanism in polling-only mode).
RECONCILE_INTERVAL_SECONDS = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "300"))

# Renew webhook channels that expire within this many hours.
CHANNEL_RENEW_WITHIN_HOURS = int(os.getenv("CHANNEL_RENEW_WITHIN_HOURS", "24"))

# Scopes: identify the user (openid, email) and read/write their events.
GOOGLE_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
]

# Only the primary calendar for now. Add more calendar IDs later.
DEFAULT_CALENDAR_ID = "primary"


# ---------- Step 2: scheduling engine ----------

def _parse_hhmm(value: str) -> dt_time:
    hours, minutes = value.strip().split(":")
    return dt_time(int(hours), int(minutes))


# Your time zone (IANA name). All answers are given in this zone.
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "America/New_York").strip()

# Working hours, used when looking for free time.
WORK_START = _parse_hhmm(os.getenv("WORK_START", "09:00"))
WORK_END = _parse_hhmm(os.getenv("WORK_END", "18:00"))
# Days you work, 1 = Monday ... 7 = Sunday.
WORK_DAYS = {int(d) for d in os.getenv("WORK_DAYS", "1,2,3,4,5").split(",") if d.strip()}


# ---------- Step 3: the agent ----------

# Which AI powers the chat: "anthropic" (Claude) or "openai".
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()

# Only the key for the provider you picked is needed. The sync service runs
# without either; only the chat page uses them.
# Anthropic: console.anthropic.com > API Keys
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5").strip()

# OpenAI: platform.openai.com > API keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5").strip()