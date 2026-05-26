"""Liveness + readiness probe.

GET /healthz/ — unauthenticated, returns 200 if the app process is alive AND
the primary database is reachable. Used by Docker / k8s healthchecks and any
external uptime monitor. Lightweight on purpose — no auth, no agent calls.
"""

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def healthz(request):
    db_ok = True
    db_error: str | None = None
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:
        db_ok = False
        db_error = type(e).__name__

    status = 200 if db_ok else 503
    return JsonResponse(
        {"status": "ok" if db_ok else "degraded", "db": db_ok, "db_error": db_error},
        status=status,
    )
