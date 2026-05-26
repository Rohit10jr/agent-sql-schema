#!/usr/bin/env bash
# Container boot sequence. Idempotent — safe to re-run on every container start.
#
#   1. Wait until Postgres accepts connections (compose's healthcheck already
#      gates this, but we keep our own wait-loop so the image is usable
#      outside compose too).
#   2. Apply Django migrations.
#   3. Create the LangGraph checkpoint + memory tables (idempotent).
#   4. Provision the admin superuser from ADMIN_* env vars (idempotent).
#   5. In prod (DEBUG=False) → collectstatic + gunicorn.
#      In dev  (DEBUG=True)  → runserver (auto-reload).

set -euo pipefail

echo "──── Waiting for Postgres ──────────────────────────────────"
python <<'PY'
import os, time, sys
import psycopg

url = os.environ.get("DATABASE_URL")
if not url:
    print("DATABASE_URL is not set — cannot connect to Postgres.")
    sys.exit(1)

for attempt in range(1, 31):
    try:
        with psycopg.connect(url, connect_timeout=2):
            print(f"Postgres ready after {attempt} attempt(s).")
            sys.exit(0)
    except Exception as e:
        print(f"  [{attempt}/30] not ready yet: {type(e).__name__}")
        time.sleep(1)

print("Postgres never came up — giving up.")
sys.exit(1)
PY

echo "──── Applying Django migrations ───────────────────────────"
python manage.py migrate --noinput

echo "──── Setting up LangGraph checkpoint + memory tables ──────"
python manage.py setup_pgmemory

echo "──── Provisioning admin (if env vars set, idempotent) ─────"
python manage.py create_admin

# DEBUG defaults to "True" so a bare `docker run` is dev-friendly.
DEBUG_LOWER="$(echo "${DEBUG:-True}" | tr '[:upper:]' '[:lower:]')"

if [ "$DEBUG_LOWER" = "false" ]; then
    echo "──── Production mode: collectstatic + gunicorn ────────────"
    python manage.py collectstatic --noinput
    exec gunicorn agent.wsgi:application \
        --bind 0.0.0.0:8000 \
        --workers 1 \
        --threads 4 \
        --timeout 120 \
        --graceful-timeout 120 \
        --access-logfile - \
        --error-logfile -
else
    echo "──── Development mode: runserver (hot reload) ─────────────"
    exec python manage.py runserver 0.0.0.0:8000
fi
