"""Long-term user memory — shared service.

Wraps a single LangGraph PostgresStore (pgvector semantic search) and exposes
everything the agents and the memory REST API need:

    recall(user_id, query)              — agent READ path  (semantic search)
    extract_and_store(user_id, ...)     — agent WRITE path  (post-stream)
    list / create / update / delete_memory — user-facing CRUD (REST API)

Memories are scoped by the namespace tuple ("memories", "<user_id>") — that
tuple is the per-user isolation boundary; nothing else keeps one user's
memories from another's.

A memory value is a JSON dict:
    {"content": str, "category": str, "source": "agent"|"user",
     "created_at": iso, "updated_at": iso (optional)}

Used by both the SQL agent and (later) the schema agent. The standalone
core/ltm_agent.py testbed is separate and unaffected.
"""

import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from django.conf import settings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_groq import ChatGroq
from langgraph.store.postgres import PostgresStore
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Embedding width. MUST match what the model returns AND the pgvector column.
# gemini-embedding-001 defaults to 3072 dims, so output_dimensionality is
# pinned to this value below. Change one → change both, recreate the table.
EMBED_DIMS = 1536

# A new fact is skipped as a duplicate when an existing memory scores above
# this cosine similarity against it.
DEDUP_SIMILARITY_THRESHOLD = 0.90

RECALL_LIMIT = 3   # memories injected into the agent prompt per turn


# ── Infrastructure (module-level singletons) ────────────────────────────────
_pool = ConnectionPool(
    settings.DB_URI,
    max_size=5,
    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
)

_embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-001",
    google_api_key=settings.GEMINI_API_KEY,
    output_dimensionality=EMBED_DIMS,
)

store = PostgresStore(
    _pool,
    index={"embed": _embeddings, "dims": EMBED_DIMS, "fields": ["content"]},
)

# Small, cheap LLM used only to extract durable facts from a finished turn.
_extractor_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.0,
    max_tokens=512,
    api_key=settings.GROQ_API_KEY,
    max_retries=2,
)


def setup_memory_store() -> None:
    """Create the store tables + pgvector index. Idempotent — run once."""
    store.setup()
    logger.info("Memory store tables ready.")


# ── Namespace + helpers ─────────────────────────────────────────────────────
def _namespace(user_id) -> tuple[str, ...]:
    """Per-user memory namespace — the isolation boundary."""
    return ("memories", str(user_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Credential-like patterns — memory is for durable, shareable facts, never
# secrets. Code-level backstop in addition to prompt instructions.
_SECRET_PATTERNS = (
    re.compile(
        r"\b(pass(word|wd)?|secret|api[_-]?key|access[_-]?key|token|bearer|credential)s?\b\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
)


def looks_like_secret(text: str) -> bool:
    """True if the text appears to contain a credential. Heuristic guard."""
    return any(pattern.search(text or "") for pattern in _SECRET_PATTERNS)


def _to_dict(item) -> dict:
    """Flatten a store Item into the dict shape the API / agent consume."""
    return {"id": item.key, **item.value}


# ── Agent READ path ─────────────────────────────────────────────────────────
def recall(user_id, query: str, limit: int = RECALL_LIMIT) -> list[dict]:
    """Return memories semantically relevant to `query`, best match first."""
    if not query:
        return []
    try:
        hits = store.search(_namespace(user_id), query=query, limit=limit)
    except Exception:
        logger.exception("Memory recall failed for user %s", user_id)
        return []
    return [_to_dict(h) for h in hits]


def format_for_prompt(memories: list[dict]) -> str:
    """Render recalled memories as a block for the agent's system prompt."""
    if not memories:
        return ""
    return "\n".join(f"- {m.get('content', '')}" for m in memories)


# ── Agent WRITE path ────────────────────────────────────────────────────────
class _ExtractedFacts(BaseModel):
    """Structured-output schema for the extraction LLM."""

    facts: list[str] = Field(
        default_factory=list,
        description=(
            "Durable facts the user EXPLICITLY stated about themselves or their "
            "preferences. Empty list if the turn contained nothing durable."
        ),
    )


_EXTRACTION_PROMPT = """You extract durable facts about a user that are worth \
remembering for future data / SQL assistant conversations.

INCLUDE only things the user explicitly stated about themselves: identity or \
role, business domain, preferred SQL dialect, default behaviours they want \
(e.g. "always show row counts", "never use SELECT *"), naming conventions, \
recurring goals or constraints.

EXCLUDE: one-off requests, transient questions, anything inferred or assumed, \
query results, secrets, and anything about a specific database's data values.

Return an empty list if nothing durable was stated.

USER MESSAGE:
{user_text}

ASSISTANT REPLY:
{assistant_text}"""

_extractor = _extractor_llm.with_structured_output(_ExtractedFacts)


def extract_and_store(user_id, user_text: str, assistant_text: str) -> int:
    """Extract durable facts from one finished turn and save the new ones.

    Designed to run AFTER the response is streamed (off the hot path). Returns
    the number of memories actually created. Skips secrets and near-duplicates.
    """
    if not user_text:
        return 0

    result = _extractor.invoke(
        _EXTRACTION_PROMPT.format(
            user_text=user_text,
            assistant_text=assistant_text or "",
        )
    )

    namespace = _namespace(user_id)
    created = 0
    for fact in result.facts:
        fact = (fact or "").strip()
        if not fact or looks_like_secret(fact):
            continue
        # Dedup: skip if a very similar memory already exists.
        existing = store.search(namespace, query=fact, limit=1)
        if existing and (existing[0].score or 0) >= DEDUP_SIMILARITY_THRESHOLD:
            continue
        store.put(
            namespace,
            uuid4().hex,
            {
                "content": fact,
                "category": "general",
                "source": "agent",
                "created_at": _now(),
            },
        )
        created += 1
    return created


# ── User-facing CRUD (REST API) ─────────────────────────────────────────────
def list_memories(user_id) -> list[dict]:
    """Every memory for a user, newest first."""
    hits = store.search(_namespace(user_id), limit=500)
    memories = [_to_dict(h) for h in hits]
    memories.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return memories


def create_memory(user_id, content: str, category: str = "general") -> dict:
    """Create a user-authored memory. Raises ValueError on secret-like input."""
    content = (content or "").strip()
    if not content:
        raise ValueError("Memory content cannot be empty.")
    if looks_like_secret(content):
        raise ValueError("That looks like a credential — refusing to store it.")

    namespace = _namespace(user_id)
    memory_id = uuid4().hex
    value = {
        "content": content,
        "category": category or "general",
        "source": "user",
        "created_at": _now(),
    }
    store.put(namespace, memory_id, value)
    return {"id": memory_id, **value}


def update_memory(user_id, memory_id: str, content: str) -> dict | None:
    """Update a memory's content. Returns None if it does not exist."""
    content = (content or "").strip()
    if not content:
        raise ValueError("Memory content cannot be empty.")
    if looks_like_secret(content):
        raise ValueError("That looks like a credential — refusing to store it.")

    namespace = _namespace(user_id)
    existing = store.get(namespace, memory_id)
    if existing is None:
        return None

    value = dict(existing.value)
    value["content"] = content
    value["updated_at"] = _now()
    store.put(namespace, memory_id, value)
    return {"id": memory_id, **value}


def delete_memory(user_id, memory_id: str) -> bool:
    """Delete a memory. Returns False if it did not exist."""
    namespace = _namespace(user_id)
    if store.get(namespace, memory_id) is None:
        return False
    store.delete(namespace, memory_id)
    return True
