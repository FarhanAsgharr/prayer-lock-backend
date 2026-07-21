"""Vercel serverless entrypoint.

Vercel's Python runtime imports this module and serves the ASGI `app` it
exposes. All routes are rewritten to this single function by vercel.json, so
the whole FastAPI application runs behind one serverless handler.

Note on architecture: this backend was designed as a persistent server
(connection pooling, a Redis rate limiter, background-friendly request
handling). Serverless imposes real constraints — cold starts, per-invocation
connections, and function timeouts — so a persistent host (Railway, Render,
Fly.io) is the better fit. This entrypoint exists because Vercel was requested;
it works, but read backend/VERCEL.md for the caveats and the required managed
Postgres.
"""

from app.main import app

# Vercel detects and serves this ASGI application.
__all__ = ["app"]
