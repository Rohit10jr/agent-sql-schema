# SQL & Schema Agent — Backend

A Django + DRF backend that exposes two streaming LLM agents:

- **SQL Agent** — answers natural-language questions about a user's database. Lists tables, inspects schemas, runs read-only SQL, and renders charts.
- **Schema Agent (hybrid)** — designs a database schema (tables, columns, FKs) and the SQL + seed data to create it, from a plain-English description.

Both agents stream tokens, tool calls, and results to the frontend over SSE. They share long-term memory (per-user facts), a run registry for cooperative cancel + concurrency control, and a typed error contract.

---

## Stack

| Layer | Tech |
|---|---|
| Framework | Django 6.0, Django REST Framework |
| Agents | LangGraph 1.0.9, LangChain Core 1.2, langchain-groq, langchain-google-genai |
| LLM providers | Groq (primary), Google Gemini (embeddings for memory) |
| Database | PostgreSQL 16+ with `pgvector` extension |
| State persistence | LangGraph `PostgresSaver` (checkpointer) + `PostgresStore` (long-term memory) |
| Auth | JWT via `djangorestframework_simplejwt`, custom email-based user model |
| Async tasks (optional) | Celery + Redis |
| Static files | WhiteNoise (for admin + DRF browsable API) |
| Streaming | Server-Sent Events (`StreamingHttpResponse`) |
| Web server (prod) | gunicorn |

---

## Quick start (local)

### Prerequisites

- Python 3.12
- PostgreSQL 16+ with `pgvector` available
- A Groq API key — get one at https://console.groq.com
- A Google AI Studio key for Gemini embeddings — https://aistudio.google.com

### 1. Clone and create a venv

```powershell
git clone https://github.com/Rohit10jr/agent-sql-schema.git
cd agent-sql-schema/backend
python -m venv .venv
.\.venv\Scripts\activate
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Create the database

```sql
CREATE DATABASE agent_sql;
CREATE EXTENSION IF NOT EXISTS vector;
```

### 4. Configure `.env`

Create `backend/.env` with at minimum:

```
DEBUG=True
SECRET_KEY=<any-string-locally>

# Local Postgres
POSTGRES_DB=agent_sql
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# Same DB exposed as URL for LangGraph checkpointer
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agent_sql

# API keys
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=...
```

### 5. Run migrations + LangGraph setup

```powershell
python manage.py migrate
python manage.py setup_pgmemory   # creates LangGraph checkpoint + memory tables
python manage.py createsuperuser  # interactive — or use create_admin with env vars
```

### 6. Start the dev server

```powershell
python manage.py runserver
```

API is now at `http://127.0.0.1:8000/api/`. Admin at `http://127.0.0.1:8000/admin/`.

---

## Environment variables

### Required everywhere

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django secret. Generate with `python -c "import secrets; print(secrets.token_urlsafe(60))"` |
| `DEBUG` | `True` locally, `False` in production |
| `DATABASE_URL` | Postgres URL — used by Django ORM (in prod) and LangGraph checkpointer (always) |
| `GROQ_API_KEY` | LLM provider |
| `GEMINI_API_KEY` | Embeddings for long-term memory |

### Local-only (when `DEBUG=True`)

| Variable | Purpose |
|---|---|
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | Explicit DB config — bypasses `DATABASE_URL` parsing in dev |

### Production-only (when `DEBUG=False`)

| Variable | Purpose |
|---|---|
| `ALLOWED_HOSTS` | Comma-separated hostnames (e.g. `api.example.com,backend.example.com`). No scheme. |
| `CORS_ALLOWED_ORIGINS` | Comma-separated origins of frontends allowed to call this backend. With scheme (`https://...`). |
| `CSRF_TRUSTED_ORIGINS` | Same as CORS_ALLOWED_ORIGINS for CSRF protection. |
| `SECURE_SSL_REDIRECT` | `True` to force HTTPS (default `True`). |
| `SECURE_HSTS_SECONDS` | HSTS lifetime in seconds (default 1 year). |

### Optional

| Variable | Purpose |
|---|---|
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_FIRST_NAME`, `ADMIN_LAST_NAME` | Read by `create_admin` management command to provision the first superuser on deploy. |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | SMTP config for password reset + verification emails. Set `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` to preview emails in the dev console. |
| `FRONTEND_URL` | Used in transactional emails (password reset links). |
| `DJANGO_ADMIN_EMAIL` | Comma-separated emails — receive unhandled-error notifications. |

---

## Project structure

```
backend/
├── agent/                      # Django project (settings, urls, wsgi, asgi)
│   ├── settings.py            # DEBUG-aware, production-ready
│   ├── urls.py
│   └── wsgi.py
├── core/                       # Django app — all business logic
│   ├── models.py              # CustomUser, ChatSession, SchemaProject, Connection, Result, TokenUsage, ...
│   ├── sql_agent.py           # SQL agent: streaming view + LangGraph graph
│   ├── schema_agent_hybrid.py # Schema agent: streaming view (graph in services/)
│   ├── run_views.py           # POST /runs/<run_id>/cancel/ — stop button endpoint
│   ├── errors.py              # classify_error() — exception → structured SSE error
│   ├── views.py               # Auth, chat history, search, profile, etc.
│   ├── connection_views.py    # User DB connection CRUD
│   ├── memory_views.py        # LTM CRUD
│   ├── sql_views.py           # Result / chart / export endpoints
│   ├── schema_views.py        # Schema project CRUD
│   ├── services/
│   │   ├── run_registry.py        # In-process run handle dict + orphan-tool-call repair
│   │   ├── sql_prompt.py
│   │   ├── schema_graph_hybrid.py # Schema agent's LangGraph workflow
│   │   ├── memory.py              # Long-term memory store (PostgresStore + pgvector)
│   │   ├── connection.py
│   │   └── search_index.py
│   └── management/commands/
│       ├── create_admin.py        # Provision admin from env vars
│       ├── setup_pgmemory.py      # One-time LangGraph table setup
│       ├── setup_memory.py        # LTM-only variant
│       └── reindex_search.py
├── samples/
│   └── netflix.sqlite3       # Sample DB attached to every new user
├── requirements.txt
├── render.yaml               # Render Blueprint
└── .python-version           # 3.12
```

---

## Key features

### Streaming + cancel

Agent responses stream over SSE as a sequence of events:

- `run_started` — carries the `run_id` (used by the cancel endpoint)
- `thread_created` — for new chats
- `token` — incremental text tokens
- `tool_start` / `tool_result` — SQL agent
- `node_start` / `result` — Schema agent (with SCHEMA / SQL payloads)
- `done` — final assistant message
- `cancelled` — user clicked stop
- `error` — typed error (see below)

Users can stop a run mid-stream by `POST /api/runs/<run_id>/cancel/`. A cooperative `threading.Event` signals the streaming loop to break at the next super-step boundary (typically within a single token). The orphan-tool-call repair writes stub `ToolMessage(status="error")` rows so the next turn doesn't fail with `INVALID_CHAT_HISTORY`.

### Concurrency control

A second POST to the same agent on the same `thread_id` while a run is in flight returns **HTTP 409** with the existing `run_id`. The frontend can then choose to cancel and retry, or wait. State corruption from concurrent checkpointer writes is impossible.

### Typed errors

Every exception inside an agent stream is classified by `core/errors.py:classify_error` into one of:

`RATE_LIMIT`, `CONTEXT_OVERFLOW`, `BAD_REQUEST`, `AUTH`, `PROVIDER_TIMEOUT`, `PROVIDER_NETWORK`, `PROVIDER_DOWN`, `DB_DOWN`, `SCHEMA`, `RECURSION_LIMIT`, `INTERNAL`

Each carries a `retryable` flag and (for rate limits) a `retry_after_seconds` countdown. The frontend renders a banner with the appropriate UI (retry button, countdown, "start new chat" CTA).

### Long-term memory

Per-user durable facts are stored in a `PostgresStore` with pgvector embeddings. Each agent recalls relevant memories at the start of every turn and extracts new facts after each turn (best-effort, post-stream).

---

## API overview

All endpoints under `/api/`. JWT bearer auth required unless noted.

### Auth
- `POST /token/` — login (email + password) → access + refresh tokens
- `POST /token/refresh/`
- `POST /signup/`
- `POST /password/reset/`

### Agents
- `POST /sql-agent/` → SSE stream
- `POST /schema-agent/` → SSE stream
- `POST /runs/<run_id>/cancel/` → cancel an in-flight run

### Conversations
- `GET /threads/`
- `GET /threads/<thread_id>/history/`
- `DELETE /threads/<thread_id>/`

### Schema projects
- `GET /schema-projects/`
- `GET /schema-project/<slug>/`

### Connections
- `GET /connections/`
- `POST /connect/`
- `POST /connect/file/`

### Memory
- `GET /memories/`
- `DELETE /memories/<memory_id>/`

### Search
- `GET /search/?q=<query>` — full-text across both agents' chats

---

## Run with Docker (recommended for collaborators)

Frontend devs collaborating on this project don't need to install Python, Postgres, or pgvector locally. Just Docker.

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Compose plugin on Linux)
- A Groq API key + a Gemini API key

### Steps

```powershell
# 1. Clone
git clone https://github.com/Rohit10jr/agent-sql-schema.git
cd agent-sql-schema/backend

# 2. Copy the env template and paste in your API keys
cp .env.docker.example .env.docker
# → open .env.docker and fill in GROQ_API_KEY and GEMINI_API_KEY

# 3. Build + start the stack
docker compose up --build
```

First boot takes ~2 min (downloads Postgres + Python base images, installs deps). Subsequent boots are ~10 seconds.

After the logs settle, the backend is at:

- API — `http://localhost:8000/api/`
- Admin — `http://localhost:8000/admin/` (login with the `ADMIN_EMAIL` / `ADMIN_PASSWORD` from `.env.docker`)
- Healthcheck — `http://localhost:8000/api/healthz/`
- Postgres — `localhost:5433` (5433 to avoid clashing with any local Postgres install; useful for connecting TablePlus / DBeaver / pgAdmin)

### What runs inside

| Service | Image | Port (host) | Purpose |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg16` | `5433` | Postgres 16 with pgvector preinstalled |
| `backend` | Built from `./Dockerfile` | `8000` | Django + LangGraph agents |

### Code changes auto-reload

The `backend` service mounts the local code directory inside the container, so editing a `.py` file on your host triggers Django's `runserver` auto-reload — no rebuild needed.

### Common Docker commands

```powershell
docker compose up                    # start (foreground)
docker compose up -d                 # start (detached / background)
docker compose down                  # stop containers (keep data volume)
docker compose down -v               # stop containers AND delete the DB volume
docker compose logs -f backend       # tail backend logs
docker compose exec backend bash     # shell into the backend container
docker compose exec db psql -U postgres agent_sql   # psql into the DB
docker compose build --no-cache backend             # force-rebuild the backend image
```

### Switching DEBUG modes inside Docker

The same image runs `runserver` in dev and `gunicorn` in prod — chosen at startup by the `DEBUG` env var.

```
# In .env.docker
DEBUG=True     # → runserver (hot reload)
DEBUG=False    # → gunicorn (production server, no reload)
```

Restart the container after changing: `docker compose up -d --force-recreate backend`.

---

## Deployment (Render)

The repo includes a `render.yaml` Blueprint. To deploy:

1. **Provision Postgres**: create a new Render PostgreSQL instance.
2. **Enable pgvector** — in the database's web shell:
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
3. **Create the Web Service**: point it at the repo. If using Blueprint, Render reads `render.yaml` and wires the DB automatically.
4. **Set env vars** on the web service (see the table above — at minimum: `DEBUG=False`, `SECRET_KEY`, `ALLOWED_HOSTS`, `GROQ_API_KEY`, `GEMINI_API_KEY`, optional admin + CORS).
5. **Build command**:
   ```
   pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate --noinput && python manage.py setup_pgmemory && python manage.py create_admin
   ```
6. **Start command**:
   ```
   gunicorn agent.wsgi:application --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --graceful-timeout 120
   ```

> **Note:** `--workers 1` is required. The run registry (cancel + 409 concurrency) is in-process memory and breaks across multiple workers. For horizontal scale, the registry needs to move to Redis.

---

## Logging

Configured in `agent/settings.py`. Loggers:

- `core` — all app code (`logger = logging.getLogger(__name__)` in any file under `core/`)
- `django`, `django.server`, `django.request` — Django internals
- `langgraph`, `langchain` — at `WARNING` by default; flip to `INFO` for retry / fault-tolerance investigations

Each agent's `logger.exception` calls include structured context: `run_id`, `user_id`, `thread_id`, `agent`, `model`, `error_code`, `retryable`. Grep production logs by `run_id` to trace a single request end-to-end.

In dev (`DEBUG=True`), logs also write to `backend/logs/app.log` (rotating, 5 MB × 5 backups). In prod, only stdout — capture via systemd / docker / Render's log viewer.

---

## Local testing tips

### Trigger errors deliberately

| Goal | How |
|---|---|
| `RATE_LIMIT` | Spam queries on Groq free tier |
| `CONTEXT_OVERFLOW` | Send a 50K-token message |
| `DB_DOWN` | Stop Postgres briefly during a request |
| `cancelled` event | Click the Stop button mid-stream |
| Concurrent 409 | Open two tabs, hit Send simultaneously on the same chat |

### Run a single management command

```powershell
python manage.py setup_pgmemory
python manage.py reindex_search
python manage.py create_admin    # reads ADMIN_* env vars
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ImproperlyConfigured: SECRET_KEY must not be empty` | `.env` file has a malformed line or wrong encoding — python-dotenv silently skipped it. Re-save `.env` as UTF-8 (no BOM). |
| `extension "vector" does not exist` | pgvector not enabled on the DB. Run `CREATE EXTENSION IF NOT EXISTS vector;`. |
| `Cannot resolve keyword 'username' into field` | Old `create_admin.py` from a project with the default User model. This project uses email — the current command handles it. |
| Streaming hangs after ~60s | Reverse proxy idle timeout. Agents emit tokens regularly, so this only happens if the LLM stalls for >60s of silence. |
| CORS error in browser | Frontend origin is not in `CORS_ALLOWED_ORIGINS` env var on the backend. |
| Admin page has no CSS | `collectstatic` didn't run or WhiteNoise middleware is missing. |
| `INVALID_CHAT_HISTORY` on next message after a cancel | Orphan tool-call repair didn't fire — check `core.services.run_registry` logs. |

---

## License

Licensed under the [Apache License 2.0](./LICENSE).