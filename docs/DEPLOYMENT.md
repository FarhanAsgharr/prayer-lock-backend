# Backend deployment & operations

Copy-paste-ready commands for configuring, deploying, migrating, checking, and
rolling back the Prayer Lock backend. Host-specific Vercel walkthrough is in
[VERCEL.md](../VERCEL.md); this is the host-agnostic operations
reference.

> **The app works without this backend.** Prayer times fall back to on-device
> calculation and verification fails open. The backend adds cross-device sync,
> server-side AI verification, and accounts. Deploy it when you want those;
> until then the app is fully functional offline.

---

## What you must provide

Everything below needs infrastructure only you can create: a hosted Postgres
database, optionally Redis, and (optionally) a Firebase project and an AI vision
API key. None can be provisioned for you. The **code and configuration are
ready** — these are the values it reads.

---

## 1. Environment variables

The full annotated template is [`.env.example`](../.env.example).
Copy it and fill in real values:

```bash
cd backend
cp .env.example .env      # local dev; for prod, set these in your host's dashboard
```

**Required in production:**

| Variable | What it is | How to get it |
|---|---|---|
| `ENVIRONMENT` | `production` — enables the prod safety guards | set literally |
| `DATABASE_URL` | Pooled PostgreSQL DSN | Neon / Supabase / Railway / Render |
| `JWT_SECRET` | Signing key for the backend's own tokens | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |

**Optional:**

| Variable | Effect if absent |
|---|---|
| `REDIS_URL` | Rate limiting disabled (fails open — safe, unthrottled) |
| `FIREBASE_CREDENTIALS_PATH` | Identity verification stubbed |
| `VISION_PROVIDER` + key | Photo verification approves without an API call (`stub`) |

The app **refuses to start in production with the default `JWT_SECRET`**
(`app/core/config.py` validator), so a misconfigured deploy fails loudly instead
of shipping an insecure key.

Generate a production secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## 2. Database migrations

Alembic reads `DATABASE_URL` from the app config automatically (`alembic/env.py`),
so every command just needs that env var set.

```bash
cd backend
export DATABASE_URL='postgresql+psycopg://user:password@host:5432/prayerlock'

# See the current migration state of the database
alembic current

# See the full migration history (should be one linear chain)
alembic history

# Confirm there is exactly ONE head (no divergent branches)
alembic heads

# Apply all pending migrations — run this on every deploy that adds a migration
alembic upgrade head

# Preview the SQL without executing it (review before running on prod)
alembic upgrade head --sql
```

**Current migration state** (verified in the repo, offline):

```
7c1a3b8f42d9 (head)  add jafari calculation method and islamic section
ecd79cc13f67         add qaza status and prayer window columns
4e9e5cd9e6e1         add ahle_hadith and jafari madhab
092d748bc326         initial schema  (base)
```

One root, one head, linear, no branches. All migrations are **additive** (new
tables/columns/enum values), so `alembic upgrade head` is safe to run on a live
database.

> **Important for the Ja'fari option.** The head migration adds the
> `CalculationMethod.JAFARI` Postgres enum value. If a user selects Ja'fari and
> syncs **before** this migration has run on production, the write fails. Run
> `alembic upgrade head` on production **before** announcing the feature.

Creating a new migration (when you change a model):

```bash
alembic revision --autogenerate -m "describe the change"
# Review the generated file, then:
alembic upgrade head
```

---

## 3. Deploy

The app is a standard ASGI FastAPI application (`app.main:app`), served on
Vercel through `api/index.py`.

### Vercel (configured)

```bash
cd backend
vercel               # preview deploy
vercel --prod        # production deploy
```

Set the environment variables first (once):

```bash
vercel env add DATABASE_URL production      # paste the DSN when prompted
vercel env add JWT_SECRET production
vercel env add ENVIRONMENT production       # value: production
# repeat for REDIS_URL / vision keys as needed
vercel env ls production                     # verify they are set
```

See [VERCEL.md](../VERCEL.md) for the serverless caveats
(no bundled Postgres/Redis, ~10s function cap) and why Railway/Render are a
better fit for a persistent server.

### Railway / Render (recommended for a persistent server)

Both run the app as-is. Set the same env vars in the dashboard, point the start
command at the ASGI app, and run migrations as a release step:

```bash
# Start command
uvicorn app.main:app --host 0.0.0.0 --port $PORT

# Release/pre-deploy command (runs migrations before traffic shifts)
alembic upgrade head
```

---

## 4. Health checks

The app exposes `/health`, which checks the **database**, not just the process —
so a load balancer sees "degraded" when Postgres is unreachable.

```bash
# Replace with your deployed URL
curl -s https://your-backend.example.com/health | python -m json.tool
```

Healthy response:

```json
{ "status": "ok", "database": "ok", "environment": "production", "version": "..." }
```

Degraded (process up, database unreachable):

```json
{ "status": "degraded", "database": "unavailable", ... }
```

Use `/health` as your platform's health-check path and uptime monitor target.
API docs are at `/docs`.

---

## 5. Rollback

### Roll back a deploy

```bash
# Vercel: promote the previous deployment
vercel rollback                 # interactive; pick the last-good deployment
# or: vercel ls   then   vercel promote <deployment-url>

# Railway / Render: use the dashboard's "Rollback" / "Redeploy" on the prior
# successful deploy.
```

### Roll back a migration

Only if a migration caused a problem. Because migrations here are additive, a
rollback is rarely needed — but the command is:

```bash
export DATABASE_URL='...'

alembic downgrade -1            # undo the most recent migration
alembic downgrade <revision>    # go back to a specific revision
alembic downgrade base          # undo everything (empties the schema — careful)
```

> Postgres **enum value additions cannot be removed** by a normal downgrade
> (Postgres has no `DROP VALUE`). The head migration's `downgrade()` reflects
> this. In practice you roll *forward* with a fix rather than dropping an enum
> value. Never `downgrade base` on a database with real user data.

### Full recovery order after an outage

1. `curl /health` — is it the app or the database?
2. If database: check the provider's status, connection limits, and that
   `DATABASE_URL` is still valid.
3. If app: `vercel rollback` (or platform equivalent) to the last-good deploy.
4. Re-run `alembic upgrade head` if the rollback reverted a needed migration.
5. Confirm `/health` returns `"status": "ok"`.
