"""Schema agent — HYBRID workflow design (alternative to schema_graph.py).

Graph:

    START → summarize → recall → route ─┬─ "schema"  → gen_schema → gen_sql → respond → END
                                         ├─ "sql"     →            gen_sql → respond → END
                                         └─ "explain" →                       respond → END

Key difference from the agentic `schema_graph.py`: artifacts (the schema IR
and the SQL strings) live in **state fields**, NOT in `messages`. There is no
agent⇄tools loop — a thin LLM router classifies intent, then a fixed pipeline
runs. Result: ~3× fewer tokens per turn, no single request blows the per-
minute TPM budget, predictable execution, and validation is a bounded in-node
retry instead of a free agent loop.

This file COEXISTS with `schema_graph.py` (the agentic design); neither
modifies the other. The pure-data IR (DatabaseSchema, SQLGeneration) and
validators are imported from schema_graph.py so the produced artifacts are
byte-identical between the two designs → fair side-by-side comparison.
"""

import json
import logging
from dataclasses import dataclass
from typing import Literal

from django.conf import settings
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_groq import ChatGroq
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from typing_extensions import Annotated, TypedDict

from core.services import memory as ltm
# Reuse the IR + validators from the agentic graph so the produced artifact
# shape is identical → fair comparison. Importing pure data, no agent code.
from core.services.schema_graph import (
    DEFAULT_DIALECT,
    DatabaseSchema,
    SQLGeneration,
    validate_schema_payload,
    validate_sql_payload,
)

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
SUPPORTED_MODELS = (
    "openai/gpt-oss-120b",
    "groq/compound",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
)
DEFAULT_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
SUMMARIZE_MODEL = "openai/gpt-oss-120b"

MAX_TOKENS_BEFORE_SUMMARY = 2500
KEEP_RECENT_MESSAGES = 8

DB_URI = settings.DB_URI


def _groq(
    model: str,
    *,
    max_tokens: int,
    temperature: float = 0.1,
    disable_streaming: bool = False,
) -> ChatGroq:
    return ChatGroq(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=60,
        api_key=settings.GROQ_API_KEY,
        max_retries=3,
        disable_streaming=disable_streaming,
    )


# ── Runtime context + graph state ───────────────────────────────────────────
@dataclass
class SchemaContext:
    user_id: str
    model: str


class SchemaState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    summary: str
    recalled: str
    intent: str
    # Artifacts live HERE — not in messages. The whole point of the hybrid.
    schema: dict | None
    sql: str | None
    seed_data: str | None
    dialect: str
    validation_issues: list[str]


# ── Router schema ───────────────────────────────────────────────────────────
class RouterDecision(BaseModel):
    intent: Literal["schema", "sql", "explain"] = Field(
        description=(
            'Pick ONE: '
            '"schema" — create or refine the database schema (will also (re)generate SQL); '
            '"sql" — regenerate SQL only from the EXISTING schema (no schema change); '
            '"explain" — a question or clarification with no artifact change.'
        ),
    )


# ── Multi-model bundles ─────────────────────────────────────────────────────
def _build_bundle(model: str) -> dict:
    # Tool-calling / structured-output paths disable streaming to dodge Groq's
    # streaming-tool-call JSON-parse bug. The `respond` LLM has no tools, so it
    # can stream text tokens normally.
    return {
        "router":  _groq(model, max_tokens=200,  disable_streaming=True).with_structured_output(RouterDecision),
        "schema":  _groq(model, max_tokens=2500, disable_streaming=True).with_structured_output(DatabaseSchema),
        "sql":     _groq(model, max_tokens=2000, disable_streaming=True).with_structured_output(SQLGeneration),
        "respond": _groq(model, max_tokens=600),  # plain text, streamable
    }


BUNDLES: dict[str, dict] = {m: _build_bundle(m) for m in SUPPORTED_MODELS}
_summarizer_llm = _groq(SUMMARIZE_MODEL, max_tokens=700, temperature=0.0)


def _bundle_for(model: str) -> dict:
    return BUNDLES.get(model) or BUNDLES[DEFAULT_MODEL]


# ── Prompts ─────────────────────────────────────────────────────────────────
ROUTER_PROMPT = """You classify a user's request for a database design assistant.

Output ONE intent:
- "schema": create or refine the schema (new requirements, add/remove/change tables or columns). Schema changes always trigger SQL regeneration downstream.
- "sql": regenerate SQL only when the schema is unchanged (e.g. more seed rows, re-emit from existing IR).
- "explain": a question, clarification, or off-topic message — no schema or SQL change.

If unsure, prefer "explain"."""


SCHEMA_TOOL_PROMPT = """You are a senior database architect.

Produce a normalized database schema IR by returning a DatabaseSchema object.
Rules:
- Use snake_case names.
- Every table must have an explicit primary key.
- Put relationships in foreign_keys, not only in column descriptions.
- Prefer normalized 3NF design unless the user asks for denormalization.
- Include practical indexes for common lookup and join columns.
- Do not invent sensitive user data, credentials, or secrets.
- If requirements are vague, make conservative assumptions and list them.
"""


SQL_TOOL_PROMPT = """You are a senior SQL database engineer.

Convert the provided schema IR into executable SQL by returning a SQLGeneration object.
Rules:
- `sql` contains CREATE TABLE statements only.
- `seed_data` contains INSERT statements only.
- Create parent tables before child tables.
- Include primary keys, foreign keys, unique constraints, NOT NULL, defaults, and useful indexes when represented by the schema IR.
- Generate ~3 seed rows per table. Keep values simple and properly escaped.
"""


RESPOND_SYSTEM_PROMPT = """You are a production-grade database architect with long-term memory.

The schema, ER diagram, and SQL have ALREADY been generated and are shown to the user in a separate artifact panel.

Response style (important):
- Do NOT paste schema JSON, CREATE TABLE / INSERT statements, full column lists, or table-by-table definitions into your message — the user sees them in the panel.
- Your reply is short and conversational: what you built or changed, key design decisions, any assumptions, and any validation issues.
- A few sentences is ideal. Mention a table or column name inline when explaining a decision; do not enumerate the whole schema.
- If validation issues remain unresolved, surface them plainly and ask for clarification.
"""


# ── Helpers ─────────────────────────────────────────────────────────────────
def _latest_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    return ""


def _safe_cut_index(messages: list[AnyMessage], min_recent: int) -> int:
    target = max(0, len(messages) - min_recent)
    for index in range(target, len(messages)):
        if isinstance(messages[index], HumanMessage):
            return index
    return 0


def _render_messages(messages: list[AnyMessage]) -> str:
    lines = []
    for message in messages:
        role = message.__class__.__name__.replace("Message", "")
        content = str(getattr(message, "content", "") or "")
        if len(content) > 4000:
            content = content[:4000] + "... [truncated]"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ── Nodes ───────────────────────────────────────────────────────────────────
def summarize_conversation(state: SchemaState) -> dict:
    """Compact old turns into a rolling summary once the thread gets long."""
    messages = state["messages"]
    if count_tokens_approximately(messages) <= MAX_TOKENS_BEFORE_SUMMARY:
        return {}
    cut = _safe_cut_index(messages, KEEP_RECENT_MESSAGES)
    if cut <= 0:
        return {}
    older = messages[:cut]
    previous = state.get("summary", "")
    prompt = (
        "Maintain a compact running summary for a database design session. "
        "Preserve user requirements, design decisions, open questions, schema "
        "changes, and any validation issues.\n\n"
        f"EXISTING SUMMARY:\n{previous or '(none)'}\n\n"
        f"NEW MESSAGES:\n{_render_messages(older)}"
    )
    try:
        new_summary = str(_summarizer_llm.invoke(prompt).content)
    except Exception:
        logger.exception("Hybrid schema agent summarization failed")
        return {}
    removals = [RemoveMessage(id=m.id) for m in older if m.id]
    if not removals:
        return {"summary": new_summary}
    return {"summary": new_summary, "messages": removals}


def recall_memories(state: SchemaState, runtime: Runtime[SchemaContext]) -> dict:
    """Pull long-term memories relevant to the latest user message."""
    query = _latest_user_text(state["messages"])
    memories = ltm.recall(runtime.context.user_id, query)
    return {"recalled": ltm.format_for_prompt(memories)}


def route_intent(state: SchemaState, runtime: Runtime[SchemaContext]) -> dict:
    """One cheap classify call → intent ∈ {schema, sql, explain}."""
    bundle = _bundle_for(runtime.context.model)
    router = bundle["router"]
    query = _latest_user_text(state["messages"])
    has_schema = bool(state.get("schema"))
    user_msg = (
        f"User message:\n{query}\n\n"
        f"Does a schema already exist? {'yes' if has_schema else 'no'}\n"
        "Pick the intent: schema | sql | explain."
    )
    try:
        result = router.invoke([
            SystemMessage(content=ROUTER_PROMPT),
            HumanMessage(content=user_msg),
        ])
        return {"intent": result.intent}
    except Exception:
        logger.exception("Hybrid router failed; defaulting to explain")
        return {"intent": "explain"}


def generate_schema_node(state: SchemaState, runtime: Runtime[SchemaContext]) -> dict:
    """Generate or refine the DatabaseSchema. One validation retry, bounded."""
    bundle = _bundle_for(runtime.context.model)
    generator = bundle["schema"]
    query = _latest_user_text(state["messages"])
    dialect = state.get("dialect") or DEFAULT_DIALECT
    existing = state.get("schema")

    base_prompt = f"Dialect: {dialect}\nRequirements:\n{query.strip()}\n"
    if existing:
        base_prompt += (
            "\nExisting schema to refine (apply the requested changes):\n"
            f"{json.dumps(existing, ensure_ascii=False)}\n"
        )

    def _generate(extra: str = "") -> tuple[dict, list[str]]:
        result = generator.invoke([
            SystemMessage(content=SCHEMA_TOOL_PROMPT),
            HumanMessage(content=base_prompt + extra),
        ])
        payload = result.model_dump()
        # Honor the generator's chosen dialect; fall back to requested.
        payload["dialect"] = payload.get("dialect") or dialect
        return payload, validate_schema_payload(payload)

    payload, issues = _generate()
    if issues:
        retry_extra = (
            "\n\nPrevious attempt had validation issues — FIX them:\n"
            + "\n".join(f"- {i}" for i in issues)
        )
        payload, issues = _generate(retry_extra)

    return {
        "schema": payload,
        "dialect": payload.get("dialect") or dialect,
        "validation_issues": issues,
    }


def generate_sql_node(state: SchemaState, runtime: Runtime[SchemaContext]) -> dict:
    """Generate validated SQL + seed data from the schema IR in state."""
    schema_payload = state.get("schema")
    if not schema_payload:
        return {
            "validation_issues": (state.get("validation_issues") or [])
            + ["No schema exists yet — describe one first."]
        }

    bundle = _bundle_for(runtime.context.model)
    generator = bundle["sql"]
    dialect = (
        state.get("dialect") or schema_payload.get("dialect") or DEFAULT_DIALECT
    )

    base_prompt = (
        f"Dialect: {dialect}\n"
        f"Seed rows per table: 3\n"
        f"Schema IR JSON:\n{json.dumps(schema_payload, ensure_ascii=False)}"
    )

    def _generate(extra: str = ""):
        result = generator.invoke([
            SystemMessage(content=SQL_TOOL_PROMPT),
            HumanMessage(content=base_prompt + extra),
        ])
        return result, validate_sql_payload(result.sql, result.seed_data, dialect)

    result, issues = _generate()
    if issues:
        retry_extra = (
            "\n\nPrevious attempt had issues — FIX them:\n"
            + "\n".join(f"- {i}" for i in issues)
        )
        result, issues = _generate(retry_extra)

    return {
        "sql": result.sql,
        "seed_data": result.seed_data,
        # Keep any earlier issues (e.g. from schema retry) alongside new ones.
        "validation_issues": (state.get("validation_issues") or []) + issues,
    }


def respond_node(state: SchemaState, runtime: Runtime[SchemaContext]) -> dict:
    """Write the user-facing reply. Streams tokens; never dumps artifacts."""
    bundle = _bundle_for(runtime.context.model)
    llm = bundle["respond"]

    system = RESPOND_SYSTEM_PROMPT
    if state.get("summary"):
        system += f"\n\n## Summary of earlier conversation\n{state['summary']}"
    if state.get("recalled"):
        system += f"\n\n## What you remember about this user\n{state['recalled']}"

    # Compact "what just happened" note — never the full artifacts.
    notes: list[str] = []
    intent = state.get("intent", "explain")
    schema = state.get("schema")
    if intent == "schema" and schema:
        n_tables = len(schema.get("tables", []))
        notes.append(
            f"You just designed/refined a schema with {n_tables} tables; it is shown in the artifact panel."
        )
    if intent == "sql" and state.get("sql"):
        notes.append("You just (re)generated SQL DDL + seed data; shown in the artifact panel.")
    if state.get("validation_issues"):
        notes.append(
            "Outstanding validation issues: " + "; ".join(state["validation_issues"])
        )

    query = _latest_user_text(state["messages"])

    prompt_messages: list[AnyMessage] = [SystemMessage(content=system)]
    if notes:
        prompt_messages.append(SystemMessage(content="\n".join(notes)))
    prompt_messages.append(HumanMessage(content=query))

    response = llm.invoke(prompt_messages)
    return {"messages": [response]}


# ── Conditional routing ─────────────────────────────────────────────────────
def _branch_after_route(state: SchemaState) -> str:
    return state.get("intent", "explain")


# ── Graph ───────────────────────────────────────────────────────────────────
_builder = StateGraph(SchemaState, context_schema=SchemaContext)
_builder.add_node("summarize", summarize_conversation)
_builder.add_node("recall", recall_memories)
_builder.add_node("route", route_intent)
_builder.add_node("gen_schema", generate_schema_node)
_builder.add_node("gen_sql", generate_sql_node)
_builder.add_node("respond", respond_node)

_builder.add_edge(START, "summarize")
_builder.add_edge("summarize", "recall")
_builder.add_edge("recall", "route")

_builder.add_conditional_edges(
    "route",
    _branch_after_route,
    {"schema": "gen_schema", "sql": "gen_sql", "explain": "respond"},
)
# "schema" intent: schema → sql → respond (a changed schema needs matching SQL).
_builder.add_edge("gen_schema", "gen_sql")
_builder.add_edge("gen_sql", "respond")
_builder.add_edge("respond", END)

# Own pool + checkpointer so the hybrid is fully isolated from the agentic
# design. PostgresSaver tables are shared with other LangGraph instances; rows
# are keyed by thread_id so there's no collision.
_pool = ConnectionPool(DB_URI)
pg_checkpointer = PostgresSaver(_pool)
schema_agent_hybrid = _builder.compile(checkpointer=pg_checkpointer, store=ltm.store)
