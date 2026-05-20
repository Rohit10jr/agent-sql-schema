"""Standalone Long-Term Memory (LTM) agent — a testbed.

This module is intentionally self-contained so it can be exercised in isolation
before the LTM + summarization patterns are folded into the real SQL / schema
agents. Nothing else in the app imports it.

────────────────────────────────────────────────────────────────────────────
ARCHITECTURE — two persistence layers + summarization
────────────────────────────────────────────────────────────────────────────

1. Checkpointer  (PostgresSaver)   — SHORT-TERM, per-thread.
   Saves the full graph state every step. Lets a single conversation
   (`thread_id`) resume exactly where it left off.

2. Store         (PostgresStore)   — LONG-TERM, cross-thread.
   Namespaced key-value memories with pgvector semantic search. Survives
   across *different* threads — this is what lets the agent remember a user
   in a brand-new conversation. The checkpointer cannot do this.

3. Summarization (in-graph node)   — keeps the thread from growing forever.
   When the running message list exceeds a token budget, the oldest complete
   turns are condensed into a rolling text summary and removed from state.

Graph:  START → summarize → recall → agent ⇄ tools → END

  summarize  compacts old messages into `summary` if over the token budget
  recall     semantic-searches the store, injects relevant memories
  agent      the LLM, armed with four memory-management tools
  tools      executes create / search / update / delete memory tools

The agent both *receives* memories automatically (recall node) and can
*manage* them deliberately (tools) — create, search, update, delete.

────────────────────────────────────────────────────────────────────────────
USAGE  (from `python manage.py shell`)
────────────────────────────────────────────────────────────────────────────

    from core.ltm_agent import setup_ltm, chat, list_memories

    setup_ltm()                       # once — creates DB tables (idempotent)

    chat("Hi, I'm Alex and I only ever use PostgreSQL.")
    chat("What database do I use?")    # same thread — short-term memory
    chat("What database do I use?", thread_id="fresh-thread")  # LTM recall

    list_memories()                   # inspect what got stored
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, TypedDict
from uuid import uuid4

from django.conf import settings
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.config import get_store
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.runtime import Runtime, get_runtime
from langgraph.store.postgres import PostgresStore
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
MAIN_MODEL = "openai/gpt-oss-120b"      # tool-calling LLM (Groq)
SUMMARY_MODEL = "openai/gpt-oss-120b"   # cheaper/smaller is fine here

# Embedding dimension. MUST equal what the embedding model actually returns AND
# the width of the pgvector column. gemini-embedding-001 defaults to 3072 dims,
# so we explicitly pin output_dimensionality=1536 below to match this value.
# Change one → change both, and re-create the store table.
EMBED_DIMS = 1536

# Summarization fires when the live message list exceeds this many tokens.
# Lower it to make summarization easy to trigger while testing.
MAX_TOKENS_BEFORE_SUMMARY = 100
KEEP_RECENT_MESSAGES = 6                # never summarize away the freshest N
RECALL_LIMIT = 3                        # memories auto-injected per turn


# ── Connection pool — ONE pool shared by checkpointer + store ───────────────
# autocommit + prepare_threshold=0 + dict_row are what PostgresStore /
# PostgresSaver expect (mirrors their own from_conn_string defaults).
pool = ConnectionPool(
    settings.DB_URI,
    max_size=10,
    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
)

# ── Layer 1: checkpointer (short-term) ──────────────────────────────────────
checkpointer = PostgresSaver(pool)

# ── Layer 2: store (long-term, semantic) ────────────────────────────────────
embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-001",
    google_api_key=settings.GEMINI_API_KEY,
    # Pin the output width so it matches EMBED_DIMS / the vector column.
    output_dimensionality=EMBED_DIMS,
)
store = PostgresStore(
    pool,
    index={
        "embed": embeddings,
        "dims": EMBED_DIMS,
        "fields": ["content"],   # embed the `content` field of each memory
    },
)

# ── LLMs ────────────────────────────────────────────────────────────────────
main_llm = ChatGroq(
    model=MAIN_MODEL,
    temperature=0.1,
    max_tokens=2000,
    api_key=settings.GROQ_API_KEY,
    max_retries=3,
)
summarizer_llm = ChatGroq(
    model=SUMMARY_MODEL,
    temperature=0.0,
    max_tokens=512,
    api_key=settings.GROQ_API_KEY,
    max_retries=2,
)


def setup_ltm() -> None:
    """Create the checkpointer + store tables. Idempotent — run once.

    `store.setup()` also enables the pgvector extension and builds the ANN
    index. Safe to call repeatedly (everything is IF NOT EXISTS).
    """
    checkpointer.setup()
    store.setup()
    logger.info("LTM agent: checkpointer + store tables ready.")


# ── Runtime context (passed per-invocation, NOT stored in graph state) ──────
@dataclass
class Context:
    user_id: str


def _memory_namespace(user_id: str) -> tuple[str, ...]:
    """Hierarchical namespace for a user's memories.

    ("memories", "<user_id>") scopes every memory to one user and is the
    boundary that prevents memory leaking between users. To split further
    (profile / preferences / facts) you would extend the tuple, e.g.
    ("memories", user_id, "preferences"). Here we keep one flat namespace per
    user and carry `category` inside the value, so update/delete only need a
    key — simpler and far more reliable for an LLM to drive.
    """
    return ("memories", str(user_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Graph state ─────────────────────────────────────────────────────────────
class LTMState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    summary: str      # rolling summary of turns compacted out of `messages`
    recalled: str     # memories the recall node injected this turn


# ════════════════════════════════════════════════════════════════════════════
# MEMORY TOOLS — the agent calls these to manage long-term memory.
# Inside a tool, the store is reached with get_store() and the user with
# get_runtime(Context); both are set by LangGraph for the duration of the run.
# ════════════════════════════════════════════════════════════════════════════
@tool
def create_memory(content: str, category: str = "general") -> str:
    """Save a NEW long-term fact about the user.

    Use for durable things worth recalling in future conversations: identity,
    preferences, recurring goals, constraints. Do NOT save transient chatter.
    category is a free label, e.g. "profile", "preferences", "facts".
    """
    user_id = get_runtime(Context).context.user_id
    memory_id = uuid4().hex
    get_store().put(
        _memory_namespace(user_id),
        memory_id,
        {"content": content, "category": category, "created_at": _now()},
    )
    return f"Saved memory [{memory_id}]."


@tool
def search_memory(query: str) -> str:
    """Semantically search the user's long-term memories.

    Use when you need context that might have been saved in an earlier
    conversation. Returns each match with its id (needed to update/delete).
    """
    user_id = get_runtime(Context).context.user_id
    hits = get_store().search(_memory_namespace(user_id), query=query, limit=5)
    if not hits:
        return "No matching memories."
    return "\n".join(
        f"[{h.key}] ({h.value.get('category', 'general')}) {h.value.get('content', '')}"
        for h in hits
    )


@tool
def update_memory(memory_id: str, content: str) -> str:
    """Update an existing memory's content by its id.

    Use when a previously stored fact has changed (e.g. the user switched
    their preferred database). Get the id from search_memory first.
    """
    user_id = get_runtime(Context).context.user_id
    namespace = _memory_namespace(user_id)
    existing = get_store().get(namespace, memory_id)
    if existing is None:
        return f"No memory with id [{memory_id}]."
    value = dict(existing.value)
    value["content"] = content
    value["updated_at"] = _now()
    get_store().put(namespace, memory_id, value)
    return f"Updated memory [{memory_id}]."


@tool
def delete_memory(memory_id: str) -> str:
    """Delete a memory by its id when it is wrong or no longer relevant.

    Get the id from search_memory first.
    """
    user_id = get_runtime(Context).context.user_id
    get_store().delete(_memory_namespace(user_id), memory_id)
    return f"Deleted memory [{memory_id}]."


MEMORY_TOOLS = [create_memory, search_memory, update_memory, delete_memory]
llm_with_tools = main_llm.bind_tools(MEMORY_TOOLS)


# ════════════════════════════════════════════════════════════════════════════
# GRAPH NODES
# ════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """You are a helpful assistant with long-term memory.

You can remember things about the user across conversations using your tools:
- create_memory: save a new durable fact (identity, preferences, goals).
- search_memory: look up something that may have been saved earlier.
- update_memory: correct a stored fact that has changed.
- delete_memory: remove a fact that is wrong or obsolete.

Be proactive: when the user tells you something durable about themselves,
save it. When a stored fact changes, update it. Keep memories concise.
Never save secrets, passwords, or transient small-talk."""


def _latest_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    return ""


def _safe_cut_index(messages: list[AnyMessage], min_recent: int) -> int:
    """Index to summarize up to — always a HumanMessage boundary.

    Cutting only at the start of a turn guarantees the summarized slice
    contains whole turns, so we never orphan a tool call from its result.
    Returns 0 when no safe boundary exists (→ skip summarization).
    """
    target = max(0, len(messages) - min_recent)
    for i in range(target, len(messages)):
        if isinstance(messages[i], HumanMessage):
            return i
    return 0


def _render(messages: list[AnyMessage]) -> str:
    lines = []
    for message in messages:
        role = message.__class__.__name__.replace("Message", "")
        lines.append(f"{role}: {message.content}")
    return "\n".join(lines)


def summarize_conversation(state: LTMState) -> dict:
    """Compact old turns into a rolling summary once the thread gets long."""
    messages = state["messages"]
    if count_tokens_approximately(messages) <= MAX_TOKENS_BEFORE_SUMMARY:
        return {}

    cut = _safe_cut_index(messages, KEEP_RECENT_MESSAGES)
    if cut <= 0:
        return {}  # nothing safely summarizable yet

    older = messages[:cut]
    previous = state.get("summary", "")
    prompt = (
        "Maintain a running summary of a conversation. Fold the new messages "
        "into the existing summary. Keep it concise but preserve facts, "
        "decisions, and context needed to continue.\n\n"
        f"EXISTING SUMMARY:\n{previous or '(none yet)'}\n\n"
        f"NEW MESSAGES TO FOLD IN:\n{_render(older)}"
    )
    new_summary = str(summarizer_llm.invoke(prompt).content)

    # RemoveMessage(id=...) tells the add_messages reducer to drop those
    # messages from state — the thread genuinely shrinks.
    return {
        "summary": new_summary,
        "messages": [RemoveMessage(id=m.id) for m in older],
    }


def recall_memories(state: LTMState, runtime: Runtime[Context]) -> dict:
    """Semantic-search the store for memories relevant to the latest message."""
    query = _latest_user_text(state["messages"])
    if not query:
        return {"recalled": ""}

    namespace = _memory_namespace(runtime.context.user_id)
    hits = runtime.store.search(namespace, query=query, limit=RECALL_LIMIT)
    if not hits:
        return {"recalled": ""}

    lines = []
    for hit in hits:
        content = hit.value.get("content", "")
        if hit.score is not None:
            lines.append(f"- {content}  (id={hit.key}, relevance={hit.score:.2f})")
        else:
            lines.append(f"- {content}  (id={hit.key})")
    return {"recalled": "\n".join(lines)}


def call_agent(state: LTMState) -> dict:
    """The LLM turn — sees the system prompt + summary + recalled memories."""
    system = SYSTEM_PROMPT
    if state.get("summary"):
        system += f"\n\n--- Summary of earlier conversation ---\n{state['summary']}"
    if state.get("recalled"):
        system += f"\n\n--- Relevant long-term memories ---\n{state['recalled']}"

    response = llm_with_tools.invoke([SystemMessage(system), *state["messages"]])
    return {"messages": [response]}


# ════════════════════════════════════════════════════════════════════════════
# GRAPH WIRING
# ════════════════════════════════════════════════════════════════════════════
_builder = StateGraph(LTMState, context_schema=Context)
_builder.add_node("summarize", summarize_conversation)
_builder.add_node("recall", recall_memories)
_builder.add_node("agent", call_agent)
_builder.add_node("tools", ToolNode(MEMORY_TOOLS))

_builder.add_edge(START, "summarize")
_builder.add_edge("summarize", "recall")
_builder.add_edge("recall", "agent")
# tools_condition → "tools" if the LLM asked for a tool, else END.
_builder.add_conditional_edges("agent", tools_condition)
_builder.add_edge("tools", "agent")

ltm_agent = _builder.compile(checkpointer=checkpointer, store=store)


# ════════════════════════════════════════════════════════════════════════════
# TEST HELPERS
# ════════════════════════════════════════════════════════════════════════════
def chat(
    message: str,
    *,
    user_id: str = "ltm-test-user",
    thread_id: str = "ltm-test-thread",
) -> str:
    """Send one message and return the assistant's reply.

    Same thread_id  → short-term memory (checkpointer) carries the history.
    Same user_id, new thread_id → only long-term memory (store) carries over.
    """
    result = ltm_agent.invoke(
        {"messages": [HumanMessage(message)]},
        config={"configurable": {"thread_id": thread_id}},
        context=Context(user_id=user_id),
    )
    return str(result["messages"][-1].content)


def list_memories(user_id: str = "ltm-test-user") -> list[dict]:
    """Dump every stored memory for a user — handy for inspecting test runs."""
    hits = store.search(_memory_namespace(user_id), limit=100)
    return [{"id": h.key, **h.value} for h in hits]


def clear_memories(user_id: str = "ltm-test-user") -> int:
    """Delete all of a user's memories. Returns how many were removed."""
    namespace = _memory_namespace(user_id)
    hits = store.search(namespace, limit=1000)
    for hit in hits:
        store.delete(namespace, hit.key)
    return len(hits)
