# Calendar Agent

Ask questions about your Google Calendar in plain English ("Am I free Thursday
afternoon?") and get answers from your real schedule.

It's built in three layers:

1. **Sync service**: keeps a copy of your Google Calendar in your own Postgres database, up to date automatically.
2. **Scheduling engine** (`app/schedule.py`): works out when you're busy and free: repeating meetings, moved or cancelled occurrences, time zones and daylight saving, all-day events, "show as free" and declined events, working hours. Plain code, no AI.
3. **Agent** (`app/agent.py` + the `/chat` page): Claude reads your question, asks the scheduling engine, and answers. It can only *read* your calendar for now.

Sections 1–7 below set up the sync service. Section 8 turns on the chat.

What it does:

- **Google login** (`/oauth/login`) with the refresh token stored encrypted
- **Full sync** of your primary calendar the first time you connect
- **Incremental sync**: after that, it only fetches what changed (using Google's sync token)
- **Webhooks**: Google tells the app the moment something changes
- **Self-healing**: renews webhook channels before they expire, runs a catch-up
  sync every 5 minutes in case a webhook was missed, and re-syncs from scratch if
  Google says the sync token is too old (HTTP 410)
- **Status page** at `http://localhost:3000` and your synced events at `/events`

```
app/
  config.py      settings from .env
  db.py          the three tables: accounts, sync_state, events
  crypto.py      encrypts tokens before they hit the database
  google_api.py  the Google OAuth + Calendar calls
  tokens.py      refreshes access tokens when they expire
  sync.py        full + incremental sync, turning Google's JSON into rows
  channels.py    opens, renews and closes webhook channels
  schedule.py    the scheduling engine: occurrences, busy/free, free slots
  agent.py       Claude + the calendar tools it can call
  chat_page.py   the chat page at /chat
  main.py        the web server: login, webhook receiver, status page, chat API
```

---

## 1. Finish the Google Cloud setup (5 minutes)

You've already created the project, enabled the Calendar API, and set up the
consent screen. Two more things:

**a) Add the scopes.** In Google Auth Platform → **Data Access** → *Add or remove scopes*, make sure these three are added, then Save:

- `openid`
- `.../auth/userinfo.email`
- `.../auth/calendar.events`

**b) Create the OAuth client.** In Google Auth Platform → **Clients** → *Create client*:

- Application type: **Web application**
- Name: `calendar-agent-local`
- Authorized redirect URIs: `http://localhost:3000/oauth/callback` (exactly this, no trailing slash)

Click Create and copy the **Client ID** and **Client secret**. (Download the JSON too, and keep it somewhere safe. Never commit it to Git.)

## 2. Start Postgres

Pick one:

- **Docker** (from this folder): `docker compose up -d`
- **Mac without Docker**: install [Postgres.app](https://postgresapp.com), start it, then run `createdb calendar_agent`. Your `DATABASE_URL` becomes `postgresql+psycopg://YOUR_MAC_USERNAME@localhost:5432/calendar_agent`
- **Homebrew**: `brew install postgresql@16 && brew services start postgresql@16 && createdb calendar_agent`, with the same `DATABASE_URL` as Postgres.app

## 3. Install the app

You need Python 3.10 or newer (`python3 --version`).

```bash
cd calendar-agent
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Fill in `.env`

```bash
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Open `.env` and set:

- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` from step 1b
- `ENCRYPTION_KEY`: the value the command above printed
- `DATABASE_URL`: leave as-is if you used Docker; otherwise use the value from step 2
- Leave `WEBHOOK_BASE_URL` empty for now

## 5. Run it and connect your calendar

```bash
uvicorn app.main:app --port 3000 --reload
```

1. Open **http://localhost:3000** and click **Connect a Google account**.
2. Google will show *"Google hasn't verified this app."* That's expected for your own test app: click **Continue**.
3. Tick the calendar permission box and continue.
4. You land back on the status page. Refresh after a few seconds and you'll see your event count and "Last synced" time.
5. Open **http://localhost:3000/events** to see the events themselves.

At this point you're in **polling mode**: changes you make in Google Calendar show up within 5 minutes. Next, make them instant.

## 6. Turn on webhooks (instant updates)

Google can only send webhooks to a public HTTPS address, so you need a tunnel to your laptop.

1. Install ngrok (`brew install ngrok` on Mac, or download it from ngrok.com), create a free account, and run the `ngrok config add-authtoken ...` command it gives you.
2. In a **second terminal**: `ngrok http 3000`
3. Copy the `https://....ngrok-free.app` address it shows into `.env` as `WEBHOOK_BASE_URL`.
4. Restart the app (Ctrl+C, then run the uvicorn command again).
5. Go to http://localhost:3000 and click **Connect a Google account** again. This opens a webhook channel. The "Webhook expires" column should now show a date about a week out.

Now edit an event in Google Calendar and refresh `/events`: the change should be there within a few seconds, and your terminal logs `Incremental sync: ... 1 changed`.

> The free ngrok address can change each time you restart ngrok. When it does, update `WEBHOOK_BASE_URL`, restart the app, and reconnect. (A free ngrok static domain, or Cloudflare Tunnel, avoids this.)

## 7. Prove it works: the Step 1 checklist

Don't move on to Step 2 until all of these pass.

- [ ] **Create** an event in Google Calendar → it appears at `/events`
- [ ] **Edit** its time or title → the change appears
- [ ] **Delete** it → it disappears from `/events` (and shows as `cancelled` at `/events?include_cancelled=true`)
- [ ] **Recurring**: create a weekly event, then move *just one* occurrence. You should see the series (with `recurrence` rules) plus a separate row for the moved occurrence, whose `recurring_event_id` points at the series
- [ ] **Delete one occurrence** of that series → a `cancelled` row appears for that occurrence only
- [ ] **Catch-up**: stop the app, make a few changes in Google Calendar, start it again, then click **Sync now** → all changes arrive
- [ ] **Full re-sync**: run `psql calendar_agent -c "update sync_state set sync_token = null"` (with Docker: `docker compose exec postgres psql -U postgres calendar_agent -c "update sync_state set sync_token = null"`), then click **Sync now** → the log says `Full sync`, and the event count matches what you had before. That's the same path the app takes when Google reports an expired sync token (HTTP 410).

## 8. Turn on the chat (Steps 2 and 3)

**If you're upgrading from the Step 1 version:** unzip the new version over your existing `calendar-agent` folder (say yes to replacing files; your `.env` isn't in the zip, so it's kept). Then, in Terminal from the project folder:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

**Get an Anthropic API key:** go to console.anthropic.com, add a payment method under **Billing** (pay as you go; a calendar question costs around a cent), then **API Keys** → **Create Key**. Copy the key; it starts with `sk-ant-`.

**Add it to `.env`** (`open -e .env`) by adding these lines at the bottom, putting your key after `ANTHROPIC_API_KEY=`:

```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-5
USER_TIMEZONE=America/New_York
WORK_START=09:00
WORK_END=18:00
WORK_DAYS=1,2,3,4,5
```

Change the time zone and working hours if yours are different. `WORK_DAYS` uses 1 = Monday through 7 = Sunday.

**Restart the app** (Ctrl+C in its Terminal window, then `uvicorn app.main:app --port 3000 --reload`) and open **http://localhost:3000/chat**.

Try:
- Am I free Thursday afternoon?
- What does my week look like?
- Find me a free hour tomorrow
- Can I do 3 to 4pm on Friday?
- What's my first meeting on Monday?

Each time you ask, the app first pulls in any changes from Google Calendar, so answers reflect edits you just made, even without webhooks. The terminal logs each tool the agent called and what it got back, which is handy for seeing how an answer was reached.

**Check the engine without the AI:** http://localhost:3000/api/free?start=2026-10-01T12:00&end=2026-10-01T17:00 (use your own dates) returns the free slots directly.

**What the agent can't do yet:** create, move or cancel events. That's the next step, and it'll ask for your approval before changing anything.

## Troubleshooting

| What you see | What it means |
|---|---|
| `Error 400: redirect_uri_mismatch` | The redirect URI in Google Cloud isn't exactly `http://localhost:3000/oauth/callback` |
| `Error 403: access_denied` | Your Google account isn't in the **Test users** list (Google Auth Platform → Audience) |
| "Calendar access wasn't granted" | You didn't tick the calendar box on Google's consent screen. Connect again |
| Status page shows **(log in again)** | The refresh token stopped working. In Testing mode Google expires them after 7 days, so just reconnect. |
| `Missing setting ...` on startup | `.env` is missing a value, or you're not running from the project folder |
| Webhook column says "none" | `WEBHOOK_BASE_URL` is empty or not `https://`, or you didn't reconnect after setting it |
| `connection refused` on port 5432 | Postgres isn't running (step 2) |
| Chat says "API key was rejected" | `ANTHROPIC_API_KEY` in `.env` is wrong or has extra spaces; fix it and restart |
| Chat says the model "wasn't found" | Set `ANTHROPIC_MODEL` in `.env` to a model name shown in your Anthropic console, then restart |
| Chat says "no credit" | Add credit under Billing at console.anthropic.com |
| Answers are an hour or a few hours off | `USER_TIMEZONE` in `.env` doesn't match where you are |

## Notes for later

- **Multiple accounts** already work: each Google login gets its own rows.
- **One server process**: run a single uvicorn worker for now (the sync lock is in-memory).
- **Schema changes**: tables are created automatically on startup. Once the schema settles, switch to Alembic migrations.
- **Going live for other people** requires Google's app verification, because calendar scopes are "sensitive." It isn't needed while you're the only test user.
- **Recurring events** are stored as a series plus exceptions, exactly as Google sends them; `app/schedule.py` expands them into individual occurrences when answering questions.
- **Only your primary calendar** is synced. Other calendars (shared, holidays, birthdays) aren't counted as busy yet.
# calendar-agent
