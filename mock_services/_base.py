"""Error injection mixin for mock services.

Adds configurable random errors (429, 500) and slow responses to mock
endpoints, so robustness scoring reflects actual error-recovery ability.

Usage in a mock service server.py:
    import sys; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mock_services._base import add_error_injection

    app = FastAPI(title="Mock Gmail API")
    add_error_injection(app)

Control via env vars:
    CLAW_INJECT_ERRORS=0.25  # probability of injecting an error (default 0 = disabled)
    CLAW_INJECT_ERRORS=0     # explicitly disable (default); per-task override possible
                             # via services[].env.CLAW_INJECT_ERRORS in task.yaml.
                             # The legacy `ERROR_RATE` env var is intentionally NOT
                             # honored — its generic name kept getting set by
                             # unrelated tooling, silently poisoning runs.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


def frozen_now() -> datetime:
    """Frozen "now" derived from the EXECUTION_DATE env var (YYYY-MM-DD).

    Used for fixture-relative time logic (cutoffs, defaults, "today" labels)
    so eval results are reproducible regardless of the wall clock.
    Audit-log timestamps should keep using datetime.now() for real ordering.
    """
    s = os.environ.get("EXECUTION_DATE")
    if not s:
        raise RuntimeError(
            "EXECUTION_DATE env var must be set to YYYY-MM-DD; "
            "refusing to compute frozen_now() without a frozen clock."
        )
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError as e:
        raise RuntimeError(f"EXECUTION_DATE='{s}' must be YYYY-MM-DD: {e}")
    return d.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

# Endpoints that should never have errors injected (grader + health)
_EXEMPT_SUFFIXES = ("/audit", "/reset", "/health", "/docs", "/openapi.json")

# Error injection is OFF by default. The previous knob was `ERROR_RATE`, a name
# so generic that any stray env var (CI, shell rc, parent process) silently
# turned on random 429/500 responses across every mock service — which is what
# poisoned the claw_slack tool calls in the 26-05-13 run. We now require the
# explicit `CLAW_INJECT_ERRORS` variable; it is unique to claw-anything and
# cannot collide with unrelated tooling.
_ERROR_RATE_ENV = "CLAW_INJECT_ERRORS"
_ERROR_RATE = float(os.environ.get(_ERROR_RATE_ENV, "0"))

# One-line startup signal so operators can confirm injection state without
# having to grep env vars. Printed once per mock service process import.
if _ERROR_RATE > 0:
    print(f"[errinj] {_ERROR_RATE_ENV}={_ERROR_RATE} — error injection ENABLED", flush=True)


def _should_inject() -> bool:
    """Roll the dice for error injection."""
    rate = float(os.environ.get(_ERROR_RATE_ENV, str(_ERROR_RATE)))
    return random.random() < rate


class ErrorInjectionMiddleware(BaseHTTPMiddleware):
    """Randomly returns 429 or 500 errors, or adds latency."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Never inject errors on audit/reset/docs endpoints
        if any(path.endswith(suffix) for suffix in _EXEMPT_SUFFIXES):
            return await call_next(request)

        # Health-check probes from ServiceManager send this header — skip injection.
        if request.headers.get("X-Health-Check") == "1":
            return await call_next(request)

        # Only inject on POST endpoints (the actual tool calls)
        if request.method != "POST":
            return await call_next(request)

        if _should_inject():
            error_type = random.choices(
                ["rate_limit", "server_error", "slow"],
                weights=[0.35, 0.35, 0.30],
                k=1,
            )[0]

            if error_type == "rate_limit":
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "rate_limit_exceeded",
                        "message": "Too many requests. Please retry after a short delay.",
                        "retry_after_seconds": 2,
                    },
                    headers={"Retry-After": "2"},
                )
            elif error_type == "server_error":
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "internal_server_error",
                        "message": "An unexpected error occurred. Please try again.",
                    },
                )
            else:
                # Slow response — add 2-4s latency but still return real data
                delay = random.uniform(2.0, 4.0)
                time.sleep(delay)
                return await call_next(request)

        return await call_next(request)


def add_error_injection(app):
    """Add error injection middleware to a FastAPI app."""
    app.add_middleware(ErrorInjectionMiddleware)
