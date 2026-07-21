# Deploying the backend to Vercel

The configuration is ready (`vercel.json`, `api/index.py`, `requirements.txt`).
This guide is the manual part — it needs **your** Vercel login and a **hosted
database**, neither of which can be done for you.

---

## Read this first — Vercel is not the ideal host for this backend

This backend was built as a **persistent server**: it keeps a database
connection pool, uses Redis for rate limiting, and has request handlers (AI
vision verification) that can take several seconds. Vercel is **serverless**,
which fights all three:

| Need | On Vercel |
|---|---|
| PostgreSQL | Not provided — you must add a hosted one (Neon, Supabase, Vercel Postgres) |
| Redis | Not provided — rate limiting fails open without it (safe, but unprotected) |
| Long requests | Hobby plan caps functions at ~10s; a slow vision API call may time out |
| Connection pooling | Handled (NullPool + you supply a pooled DB URL), but it is a workaround |

**If you have any flexibility, use [Railway](https://railway.app) or
[Render](https://render.com) instead** — both run this backend as-is, with
Postgres and Redis as one-click add-ons and no serverless caveats. The included
`Dockerfile`-style setup works directly there.

If you still want Vercel, continue.

---

## 1. Get a hosted PostgreSQL database

The single hard requirement. Free options:

- **Vercel Postgres** — Storage tab in your Vercel project (simplest).
- **[Neon](https://neon.tech)** — free tier, gives a pooled connection string.
- **[Supabase](https://supabase.com)** — free tier; use the *connection pooler*
  URL (port 6543), not the direct one.

Copy the connection string. It must be in SQLAlchemy form:

```
postgresql+psycopg://USER:PASSWORD@HOST:PORT/DBNAME
```

(Add `+psycopg` after `postgresql` if the provider gives a plain
`postgresql://` URL.)

## 2. Run the database migrations once

Vercel will not run migrations for you. From your machine, against the hosted
database:

```bash
cd backend
DATABASE_URL="postgresql+psycopg://...your hosted db..." ./.venv/bin/alembic upgrade head
```

This creates all tables, including the `madhab` enum with all four schools.

## 3. Deploy

```bash
cd backend
vercel login          # opens a browser — only you can do this
vercel                # first deploy (preview)
vercel --prod         # production deploy
```

## 4. Set environment variables in Vercel

Project → Settings → Environment Variables. At minimum:

| Variable | Value |
|---|---|
| `DATABASE_URL` | your hosted Postgres URL from step 1 |
| `JWT_SECRET` | a long random string — `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ENVIRONMENT` | `production` |
| `VISION_PROVIDER` | `openai` or `gemini` |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | your key for the chosen provider |
| `FIREBASE_CREDENTIALS_PATH` | see note below |

Optional:

| Variable | Value |
|---|---|
| `REDIS_URL` | an [Upstash](https://upstash.com) Redis URL, to enable rate limiting |

> **Firebase on serverless:** `FIREBASE_CREDENTIALS_PATH` expects a file path,
> which serverless has no persistent place for. For Vercel, store the
> service-account JSON contents in a variable and adapt
> `app/services/auth_service.py` to read the JSON from the environment instead
> of a path. Until you do, token verification is unavailable — but every
> offline app feature still works, since the app is offline-first.

After setting variables, redeploy: `vercel --prod`.

## 5. Verify

```bash
curl https://your-project.vercel.app/health
```

Expect `{"status":"ok","database":"ok",...}`. If `database` is `unavailable`,
recheck `DATABASE_URL` and that step 2's migrations ran.

## 6. Point the apps at it

Rebuild the mobile apps against your deployed URL:

```bash
flutter build apk --release --dart-define=API_BASE_URL=https://your-project.vercel.app
flutter build ios --release --dart-define=API_BASE_URL=https://your-project.vercel.app
```

Until then the apps run fully offline; only cloud sync is inactive.
