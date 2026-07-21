# Prayer Lock — Backend

FastAPI service for Prayer Lock: authentication, prayer-time computation, AI
vision verification, and prayer-tracking / analytics sync.

The mobile apps ([Android](https://github.com/FarhanAsgharr/prayer-lock-android),
[iOS](https://github.com/FarhanAsgharr/prayer-lock-ios)) are **offline-first** —
this backend is optional and adds cross-device sync, server-side statistics and
the admin surface.

## Stack

- **FastAPI** + **Python 3.11+**
- **PostgreSQL** (SQLAlchemy 2 + Alembic migrations)
- **Redis** for rate limiting (fails open if absent)
- **Firebase Admin** for identity, issuing our own short-lived JWTs
- Pluggable **vision provider** (OpenAI / Gemini / stub)

## Run locally

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"

# Postgres + Redis (or use Docker / a managed service)
createdb prayerlock
./.venv/bin/alembic upgrade head

./.venv/bin/uvicorn app.main:app --reload
# http://127.0.0.1:8000/docs
```

Configuration is via environment variables — see `app/core/config.py` for the
full list. Nothing secret is committed; set values in your environment or a
local `.env` (git-ignored).

## Tests

```bash
./.venv/bin/pytest        # 125 tests, runs against a local Postgres
./.venv/bin/ruff check app tests
```

## Deploy

- **Vercel** (serverless): see [`VERCEL.md`](VERCEL.md) — works, but needs a
  hosted Postgres and has serverless caveats.
- **Railway / Render / Fly.io** (recommended): run the app directly with
  Postgres and Redis add-ons; no serverless workarounds needed.

## API

Interactive docs at `/docs` (disabled in production). Key groups:

- `auth` — Firebase token exchange, refresh
- `prayer-times` — server-side schedule computation
- `verifications` — AI prayer-mat verification
- `tracking` — prayer history, lock sessions, emergency unlocks, device
  registration, dashboard statistics
