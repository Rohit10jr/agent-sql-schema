"""Long-term-memory schema agent.

This module combines the useful parts of `ltm_agent.py` and `schema_agent.py`
into a smaller production-oriented graph:

    START -> summarize -> recall -> primary_agent <-> tools -> END

The primary agent decides what to do. Durable memory, schema generation, SQL
generation, and validation are tools. This keeps routing flexible while still
putting deterministic checks around LLM-generated artifacts.

Use from `python manage.py shell`:

    from core.ltm_schema import setup_ltm_schema, chat, latest_artifacts

    setup_ltm_schema()
    chat("Build a schema for a marketplace with buyers, sellers, products")
    latest_artifacts("ltm-schema-test-thread")
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from django.conf import settings
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import tool
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_groq import ChatGroq
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.config import get_store
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.runtime import Runtime, get_runtime
from langgraph.store.postgres import PostgresStore
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from typing_extensions import Annotated, TypedDict

try:
    import sqlglot
except ImportError:  # pragma: no cover - production requirements include this.
    sqlglot = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAIN_MODEL = "openai/gpt-oss-120b"
SCHEMA_MODEL = "openai/gpt-oss-120b"
SQL_MODEL = "openai/gpt-oss-120b"
SUMMARY_MODEL = "openai/gpt-oss-120b"

EMBED_DIMS = 1536
MAX_TOKENS_BEFORE_SUMMARY = 3000
KEEP_RECENT_MESSAGES = 10
RECALL_LIMIT = 4

SUPPORTED_DIALECTS = {"postgresql", "mysql", "sqlite", "tsql", "standard"}
DEFAULT_DIALECT = "postgresql"


# ---------------------------------------------------------------------------
# Persistence and model clients
# ---------------------------------------------------------------------------

pool = ConnectionPool(
    settings.DB_URI,
    max_size=10,
    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
)

checkpointer = PostgresSaver(pool)

embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-001",
    google_api_key=settings.GEMINI_API_KEY,
    output_dimensionality=EMBED_DIMS,
)

store = PostgresStore(
    pool,
    index={
        "embed": embeddings,
        "dims": EMBED_DIMS,
        "fields": ["content"],
    },
)


def _groq(model: str, *, max_tokens: int, temperature: float = 0.1) -> ChatGroq:
    return ChatGroq(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=60,
        api_key=settings.GROQ_API_KEY,
        max_retries=3,
    )


main_llm = _groq(MAIN_MODEL, max_tokens=2500)
schema_llm = _groq(SCHEMA_MODEL, max_tokens=5000)
sql_llm = _groq(SQL_MODEL, max_tokens=7000)
summarizer_llm = _groq(SUMMARY_MODEL, max_tokens=700, temperature=0.0)


def setup_ltm_schema() -> None:
    """Create checkpointer and long-term-memory store tables."""
    checkpointer.setup()
    store.setup()
    logger.info("LTM schema agent: checkpointer + store tables ready.")


# ---------------------------------------------------------------------------
# Runtime context and graph state
# ---------------------------------------------------------------------------

@dataclass
class Context:
    user_id: str


class LTMSchemaState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    summary: str
    recalled: str


def _memory_namespace(user_id: str) -> tuple[str, ...]:
    return ("memories", str(user_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Structured artifact models
# ---------------------------------------------------------------------------

class Column(BaseModel):
    name: str = Field(description="snake_case column name")
    type: str = Field(description="SQL type for the selected dialect")
    nullable: bool = Field(default=False)
    primary_key: bool = Field(default=False)
    unique: bool = Field(default=False)
    default: str | None = Field(default=None)
    check: str | None = Field(default=None)
    description: str = Field(default="")


class ForeignKey(BaseModel):
    column: str
    references_table: str
    references_column: str = "id"
    on_delete: Literal["CASCADE", "SET NULL", "RESTRICT", "NO ACTION"] = "NO ACTION"


class IndexDefinition(BaseModel):
    name: str
    columns: list[str]
    unique: bool = False


class Table(BaseModel):
    name: str
    purpose: str
    columns: list[Column]
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    indexes: list[IndexDefinition] = Field(default_factory=list)


class DatabaseSchema(BaseModel):
    dialect: Literal["postgresql", "mysql", "sqlite", "tsql", "standard"]
    tables: list[Table]
    assumptions: list[str] = Field(default_factory=list)
    answer: str = Field(description="Concise explanation of the schema design")


class SQLGeneration(BaseModel):
    dialect: Literal["postgresql", "mysql", "sqlite", "tsql", "standard"]
    sql: str = Field(description="CREATE TABLE statements only")
    seed_data: str = Field(description="INSERT statements only")
    answer: str = Field(description="Concise explanation of generated SQL")


schema_generator = schema_llm.with_structured_output(DatabaseSchema)
sql_generator = sql_llm.with_structured_output(SQLGeneration)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SECRET_PATTERNS = (
    re.compile(
        r"\b(pass(word|wd)?|secret|api[_-]?key|access[_-]?key|token|bearer|credential)s?\b\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)
_UNSAFE_SQL_RE = re.compile(
    r"\b(drop|truncate|alter|grant|revoke|merge|replace|execute|exec)\b"
    r"|\bdelete\s+from\b"
    r"|\bupdate\s+[a-z_][a-z0-9_]*\s+set\b",
    re.IGNORECASE,
)


def _looks_like_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_PATTERNS)


def _normalize_dialect(dialect: str | None) -> str:
    value = (dialect or DEFAULT_DIALECT).lower().strip()
    return value if value in SUPPORTED_DIALECTS else DEFAULT_DIALECT


def _sqlglot_read_dialect(dialect: str) -> str | None:
    return {
        "postgresql": "postgres",
        "mysql": "mysql",
        "sqlite": "sqlite",
        "tsql": "tsql",
        "standard": None,
    }.get(_normalize_dialect(dialect))


def _json_response(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, default=str)


def _load_json_object(value: str | dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(value, dict):
        return value, None
    try:
        parsed = json.loads(value)
    except Exception as exc:
        return None, f"Invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "Expected a JSON object."
    return parsed, None


def validate_schema_payload(schema: dict[str, Any]) -> list[str]:
    """Return validation issues for the schema IR. Empty list means valid."""
    issues: list[str] = []
    tables = schema.get("tables")
    if not isinstance(tables, list) or not tables:
        return ["Schema must contain at least one table."]

    table_names: set[str] = set()
    table_columns: dict[str, set[str]] = {}

    for table in tables:
        name = str(table.get("name", ""))
        if not _IDENT_RE.match(name):
            issues.append(f"Invalid table name: {name!r}.")
        if name in table_names:
            issues.append(f"Duplicate table name: {name}.")
        table_names.add(name)

        columns = table.get("columns")
        if not isinstance(columns, list) or not columns:
            issues.append(f"Table {name or '<unknown>'} has no columns.")
            table_columns[name] = set()
            continue

        seen_columns: set[str] = set()
        has_pk = False
        for column in columns:
            col_name = str(column.get("name", ""))
            if not _IDENT_RE.match(col_name):
                issues.append(f"Invalid column name {name}.{col_name!r}.")
            if col_name in seen_columns:
                issues.append(f"Duplicate column {name}.{col_name}.")
            seen_columns.add(col_name)
            if bool(column.get("primary_key")):
                has_pk = True
                if bool(column.get("nullable")):
                    issues.append(f"Primary key {name}.{col_name} cannot be nullable.")
            if not str(column.get("type", "")).strip():
                issues.append(f"Column {name}.{col_name} is missing a type.")

        if not has_pk:
            issues.append(f"Table {name} should define an explicit primary key.")
        table_columns[name] = seen_columns

    for table in tables:
        name = str(table.get("name", ""))
        for fk in table.get("foreign_keys") or []:
            col = str(fk.get("column", ""))
            ref_table = str(fk.get("references_table", ""))
            ref_col = str(fk.get("references_column", "id"))
            if col not in table_columns.get(name, set()):
                issues.append(f"Foreign key {name}.{col} has no matching local column.")
            if ref_table not in table_columns:
                issues.append(f"Foreign key {name}.{col} references missing table {ref_table}.")
            elif ref_col not in table_columns[ref_table]:
                issues.append(
                    f"Foreign key {name}.{col} references missing column "
                    f"{ref_table}.{ref_col}."
                )

        for index in table.get("indexes") or []:
            for col in index.get("columns") or []:
                if col not in table_columns.get(name, set()):
                    issues.append(f"Index {index.get('name')} references missing column {name}.{col}.")

    return issues


def _first_sql_keyword(statement: str) -> str:
    stripped = statement.strip().lstrip("(")
    return stripped.split(None, 1)[0].upper() if stripped else ""


def validate_sql_payload(
    sql: str,
    seed_data: str = "",
    dialect: str = DEFAULT_DIALECT,
) -> list[str]:
    """Parse generated SQL and return issues. Empty list means valid enough."""
    issues: list[str] = []
    read_dialect = _sqlglot_read_dialect(dialect)

    if not sql.strip():
        return ["SQL is empty."]
    if sqlglot is None:
        return ["SQL validation dependency sqlglot is not installed."]
    if _UNSAFE_SQL_RE.search(sql):
        issues.append("DDL contains unsafe or out-of-scope SQL keywords.")

    try:
        kwargs = {"read": read_dialect} if read_dialect else {}
        ddl_statements = sqlglot.parse(sql, **kwargs)
    except Exception as exc:
        issues.append(f"Could not parse DDL: {exc}")
        ddl_statements = []

    if not ddl_statements:
        issues.append("DDL contains no parseable statements.")

    for statement in [s for s in sql.split(";") if s.strip()]:
        if _first_sql_keyword(statement) != "CREATE":
            issues.append("DDL must contain CREATE statements only.")
            break

    if seed_data.strip():
        if _UNSAFE_SQL_RE.search(seed_data):
            issues.append("Seed data contains unsafe or out-of-scope SQL keywords.")
        try:
            kwargs = {"read": read_dialect} if read_dialect else {}
            seed_statements = sqlglot.parse(seed_data, **kwargs)
        except Exception as exc:
            issues.append(f"Could not parse seed data: {exc}")
            seed_statements = []
        if not seed_statements:
            issues.append("Seed data contains no parseable statements.")
        for statement in [s for s in seed_data.split(";") if s.strip()]:
            if _first_sql_keyword(statement) != "INSERT":
                issues.append("Seed data must contain INSERT statements only.")
                break

    return issues


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

@tool
def create_memory(content: str, category: str = "general") -> str:
    """Save a durable non-secret fact about the user or project preferences."""
    if _looks_like_secret(content):
        return "Refused: this looks like a credential or secret, so it was not saved."

    user_id = get_runtime(Context).context.user_id
    namespace = _memory_namespace(user_id)
    existing_hits = get_store().search(namespace, query=content, limit=3)
    normalized = " ".join(content.lower().split())
    for hit in existing_hits:
        old = " ".join(str(hit.value.get("content", "")).lower().split())
        if old == normalized:
            return f"Memory already exists [{hit.key}]."

    memory_id = uuid4().hex
    get_store().put(
        namespace,
        memory_id,
        {"content": content, "category": category, "created_at": _now()},
    )
    return f"Saved memory [{memory_id}]."


@tool
def search_memory(query: str) -> str:
    """Search the user's long-term memories. Returns ids for update/delete."""
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
    """Update an existing memory by id."""
    if _looks_like_secret(content):
        return "Refused: this looks like a credential or secret, so it was not saved."

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
    """Delete a memory by id when it is wrong or obsolete."""
    user_id = get_runtime(Context).context.user_id
    namespace = _memory_namespace(user_id)
    if get_store().get(namespace, memory_id) is None:
        return f"No memory with id [{memory_id}], nothing to delete."
    get_store().delete(namespace, memory_id)
    return f"Deleted memory [{memory_id}]."


# ---------------------------------------------------------------------------
# Schema and SQL tools
# ---------------------------------------------------------------------------

SCHEMA_TOOL_PROMPT = """You are a senior database architect.

Produce a normalized database schema IR by calling the DatabaseSchema tool.
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

Convert the provided schema IR into executable SQL by calling the SQLGeneration
tool. Rules:
- `sql` contains CREATE TABLE statements only.
- `seed_data` contains INSERT statements only.
- Create parent tables before child tables.
- Include primary keys, foreign keys, unique constraints, NOT NULL, defaults,
  and useful indexes when represented by the schema IR.
- Generate the requested number of seed rows per table.
- Keep values simple and properly escaped.
"""


@tool
def generate_schema(
    requirements: str,
    dialect: str = DEFAULT_DIALECT,
    existing_schema_json: str = "",
) -> str:
    """Generate or refine a validated schema JSON artifact from requirements."""
    dialect = _normalize_dialect(dialect)
    prompt = (
        f"Dialect: {dialect}\n"
        f"Requirements:\n{requirements.strip()}\n\n"
    )
    if existing_schema_json.strip():
        prompt += f"Existing schema to refine:\n{existing_schema_json.strip()}\n"

    result = schema_generator.invoke(
        [
            SystemMessage(content=SCHEMA_TOOL_PROMPT),
            HumanMessage(content=prompt),
        ]
    )
    payload = result.model_dump()
    payload["dialect"] = dialect
    issues = validate_schema_payload(payload)
    return _json_response(
        artifact="schema",
        ok=not issues,
        validation_issues=issues,
        schema=payload,
        answer=result.answer,
    )


@tool
def validate_schema_json(schema_json: str) -> str:
    """Validate a schema JSON artifact and return concrete issues."""
    payload, error = _load_json_object(schema_json)
    if error:
        return _json_response(artifact="schema_validation", ok=False, issues=[error])
    issues = validate_schema_payload(payload)
    return _json_response(artifact="schema_validation", ok=not issues, issues=issues)


@tool
def generate_sql(
    schema_json: str,
    dialect: str = DEFAULT_DIALECT,
    seed_rows_per_table: int = 3,
) -> str:
    """Generate validated CREATE TABLE SQL and INSERT seed data from schema JSON."""
    dialect = _normalize_dialect(dialect)
    seed_rows = max(0, min(int(seed_rows_per_table or 0), 5))
    schema_payload, error = _load_json_object(schema_json)
    if error:
        return _json_response(artifact="sql", ok=False, validation_issues=[error])

    schema_issues = validate_schema_payload(schema_payload)
    if schema_issues:
        return _json_response(
            artifact="sql",
            ok=False,
            validation_issues=schema_issues,
            message="Schema must be fixed before SQL generation.",
        )

    prompt = (
        f"Dialect: {dialect}\n"
        f"Seed rows per table: {seed_rows}\n"
        f"Schema IR JSON:\n{json.dumps(schema_payload, ensure_ascii=True)}"
    )
    result = sql_generator.invoke(
        [
            SystemMessage(content=SQL_TOOL_PROMPT),
            HumanMessage(content=prompt),
        ]
    )
    issues = validate_sql_payload(result.sql, result.seed_data, dialect)
    return _json_response(
        artifact="sql",
        ok=not issues,
        validation_issues=issues,
        dialect=dialect,
        sql=result.sql,
        seed_data=result.seed_data,
        answer=result.answer,
    )


@tool
def validate_sql(sql: str, seed_data: str = "", dialect: str = DEFAULT_DIALECT) -> str:
    """Validate generated DDL and seed SQL without executing it."""
    dialect = _normalize_dialect(dialect)
    issues = validate_sql_payload(sql, seed_data, dialect)
    return _json_response(artifact="sql_validation", ok=not issues, issues=issues)


TOOLS = [
    create_memory,
    search_memory,
    update_memory,
    delete_memory,
    generate_schema,
    validate_schema_json,
    generate_sql,
    validate_sql,
]
llm_with_tools = main_llm.bind_tools(TOOLS)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a production-grade database architect with long-term memory.

You help users design, refine, validate, and explain database schemas and SQL.

Operating model:
- You are the only conversational agent node.
- Use tools for durable memory, schema generation, SQL generation, and validation.
- For schema creation or refinement, call generate_schema.
- For SQL creation, call generate_sql with a schema JSON artifact.
- If a tool reports validation issues, explain them and either repair by calling
  the appropriate generation tool again or ask for clarification.
- Do not claim SQL is production-ready unless validate_sql or generate_sql
  returned ok=true.
- Save only durable, non-secret user preferences or project constraints.
- Never save passwords, API keys, tokens, DSNs, or private credentials.
- Ask concise clarification questions when requirements are too vague or unsafe.
"""


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


def _render(messages: list[AnyMessage]) -> str:
    lines = []
    for message in messages:
        role = message.__class__.__name__.replace("Message", "")
        content = str(getattr(message, "content", "") or "")
        if len(content) > 4000:
            content = content[:4000] + "... [truncated]"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def summarize_conversation(state: LTMSchemaState) -> dict:
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
        "changes, generated artifacts, validation issues, and durable user "
        "preferences. Do not include secrets.\n\n"
        f"EXISTING SUMMARY:\n{previous or '(none)'}\n\n"
        f"NEW MESSAGES:\n{_render(older)}"
    )
    summary = str(summarizer_llm.invoke(prompt).content)
    removals = [RemoveMessage(id=m.id) for m in older if m.id]
    if not removals:
        return {"summary": summary}
    return {"summary": summary, "messages": removals}


def recall_memories(state: LTMSchemaState, runtime: Runtime[Context]) -> dict:
    query = _latest_user_text(state["messages"])
    if not query:
        return {"recalled": ""}

    namespace = _memory_namespace(runtime.context.user_id)
    hits = runtime.store.search(namespace, query=query, limit=RECALL_LIMIT)
    if not hits:
        return {"recalled": ""}

    logger.debug(
        "ltm_schema recall hits: %s",
        [(h.key, round(h.score, 3) if h.score is not None else None) for h in hits],
    )
    lines = [f"- {h.value.get('content', '')} (id={h.key})" for h in hits]
    return {"recalled": "\n".join(lines)}


def call_agent(state: LTMSchemaState) -> dict:
    system = SYSTEM_PROMPT
    if state.get("summary"):
        system += f"\n\nEarlier conversation summary:\n{state['summary']}"
    if state.get("recalled"):
        system += f"\n\nRelevant long-term memories:\n{state['recalled']}"

    response = llm_with_tools.invoke([SystemMessage(content=system), *state["messages"]])
    return {"messages": [response]}


builder = StateGraph(LTMSchemaState, context_schema=Context)
builder.add_node("summarize", summarize_conversation)
builder.add_node("recall", recall_memories)
builder.add_node("agent", call_agent)
builder.add_node("tools", ToolNode(TOOLS))

builder.add_edge(START, "summarize")
builder.add_edge("summarize", "recall")
builder.add_edge("recall", "agent")
builder.add_conditional_edges("agent", tools_condition)
builder.add_edge("tools", "agent")

ltm_schema_agent = builder.compile(checkpointer=checkpointer, store=store)


# ---------------------------------------------------------------------------
# Test helpers / artifact extraction
# ---------------------------------------------------------------------------

def chat(
    message: str,
    *,
    user_id: str = "ltm-schema-test-user",
    thread_id: str = "ltm-schema-test-thread",
) -> str:
    """Send one message and return the assistant's final text response."""
    result = ltm_schema_agent.invoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": thread_id}},
        context=Context(user_id=user_id),
    )
    return str(result["messages"][-1].content)


def get_state(thread_id: str = "ltm-schema-test-thread"):
    """Return the raw LangGraph checkpoint state for inspection."""
    return ltm_schema_agent.get_state({"configurable": {"thread_id": thread_id}})


def latest_artifacts(thread_id: str = "ltm-schema-test-thread") -> dict[str, Any]:
    """Extract latest schema and SQL artifacts from tool messages."""
    state = get_state(thread_id)
    values = state.values if state else {}
    artifacts: dict[str, Any] = {"schema": None, "sql": None}
    for message in values.get("messages", []) or []:
        if not isinstance(message, ToolMessage):
            continue
        content = str(message.content or "")
        try:
            payload = json.loads(content)
        except Exception:
            continue
        if payload.get("artifact") == "schema" and payload.get("schema"):
            artifacts["schema"] = payload
        elif payload.get("artifact") == "sql" and payload.get("sql"):
            artifacts["sql"] = payload
    return artifacts


def list_memories(user_id: str = "ltm-schema-test-user") -> list[dict[str, Any]]:
    hits = store.search(_memory_namespace(user_id), limit=100)
    return [{"id": hit.key, **hit.value} for hit in hits]


def clear_memories(user_id: str = "ltm-schema-test-user") -> int:
    namespace = _memory_namespace(user_id)
    hits = store.search(namespace, limit=1000)
    for hit in hits:
        store.delete(namespace, hit.key)
    return len(hits)
