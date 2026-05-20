"""Schema agent graph — LTM, summarization, validated schema / SQL generation.

Promoted from the `core/ltm_schema.py` prototype into the production schema
agent. A single conversational agent drives validated tools:

    START -> summarize -> recall -> agent <-> tools -> END

Tools: generate_schema, validate_schema_json, generate_sql, validate_sql.

Long-term memory is automatic — the `recall` node injects relevant memories,
and the streaming view runs post-stream extraction. The agent has NO memory
tools, matching the SQL agent (one mental model, one core/services/memory.py).
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from django.conf import settings
from langchain_core.messages import AnyMessage, HumanMessage, RemoveMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.runtime import Runtime, get_runtime
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from typing_extensions import Annotated, TypedDict

from core.services import memory as ltm

try:
    import sqlglot
except ImportError:  # pragma: no cover — sqlglot is a hard dependency in prod.
    sqlglot = None

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
# Models the schema agent exposes — mirrors the frontend model picker.
SUPPORTED_MODELS = (
    "openai/gpt-oss-120b",
    "groq/compound",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
)
DEFAULT_MODEL = "openai/gpt-oss-120b"

MAX_TOKENS_BEFORE_SUMMARY = 3000
KEEP_RECENT_MESSAGES = 10

SUPPORTED_DIALECTS = {"postgresql", "mysql", "sqlite", "tsql", "standard"}
DEFAULT_DIALECT = "postgresql"

DB_URI = settings.DB_URI


def _groq(model: str, *, max_tokens: int, temperature: float = 0.1) -> ChatGroq:
    return ChatGroq(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=60,
        api_key=settings.GROQ_API_KEY,
        max_retries=3,
    )


# ── Runtime context + graph state ───────────────────────────────────────────
@dataclass
class SchemaContext:
    user_id: str
    model: str


class SchemaGraphState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    summary: str
    recalled: str


# ── Structured artifact IR ──────────────────────────────────────────────────
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


# ── Multi-model bundles ─────────────────────────────────────────────────────
# Pre-built per supported model. The agent picks its tool-bound LLM and the
# tools pick their structured generators by the request's chosen model.
def _build_bundle(model: str) -> dict:
    return {
        "schema": _groq(model, max_tokens=5000).with_structured_output(DatabaseSchema),
        "sql": _groq(model, max_tokens=7000).with_structured_output(SQLGeneration),
    }


SCHEMA_GENERATORS: dict[str, dict] = {m: _build_bundle(m) for m in SUPPORTED_MODELS}
_summarizer_llm = _groq(DEFAULT_MODEL, max_tokens=700, temperature=0.0)


def _generators_for(model: str) -> dict:
    return SCHEMA_GENERATORS.get(model) or SCHEMA_GENERATORS[DEFAULT_MODEL]


# ── Validation helpers ──────────────────────────────────────────────────────
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_UNSAFE_SQL_RE = re.compile(
    r"\b(drop|truncate|alter|grant|revoke|merge|replace|execute|exec)\b"
    r"|\bdelete\s+from\b"
    r"|\bupdate\s+[a-z_][a-z0-9_]*\s+set\b",
    re.IGNORECASE,
)


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


def _load_json_object(value: str | dict) -> tuple[dict | None, str | None]:
    if isinstance(value, dict):
        return value, None
    try:
        parsed = json.loads(value)
    except Exception as exc:
        return None, f"Invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "Expected a JSON object."
    return parsed, None


def validate_schema_payload(schema: dict) -> list[str]:
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
                    issues.append(
                        f"Index {index.get('name')} references missing column {name}.{col}."
                    )

    return issues


def _first_sql_keyword(statement: str) -> str:
    stripped = statement.strip().lstrip("(")
    return stripped.split(None, 1)[0].upper() if stripped else ""


def validate_sql_payload(sql: str, seed_data: str = "", dialect: str = DEFAULT_DIALECT) -> list[str]:
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


# ── Tools ───────────────────────────────────────────────────────────────────
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
    model = get_runtime(SchemaContext).context.model
    generator = _generators_for(model)["schema"]

    prompt = f"Dialect: {dialect}\nRequirements:\n{requirements.strip()}\n\n"
    if existing_schema_json.strip():
        prompt += f"Existing schema to refine:\n{existing_schema_json.strip()}\n"

    result = generator.invoke(
        [SystemMessage(content=SCHEMA_TOOL_PROMPT), HumanMessage(content=prompt)]
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

    model = get_runtime(SchemaContext).context.model
    generator = _generators_for(model)["sql"]
    prompt = (
        f"Dialect: {dialect}\n"
        f"Seed rows per table: {seed_rows}\n"
        f"Schema IR JSON:\n{json.dumps(schema_payload, ensure_ascii=True)}"
    )
    result = generator.invoke(
        [SystemMessage(content=SQL_TOOL_PROMPT), HumanMessage(content=prompt)]
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


TOOLS = [generate_schema, validate_schema_json, generate_sql, validate_sql]
AGENT_LLMS: dict[str, Any] = {
    model: _groq(model, max_tokens=2500).bind_tools(TOOLS) for model in SUPPORTED_MODELS
}


# ── Graph nodes ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a production-grade database architect with long-term memory.

You help users design, refine, validate, and explain database schemas and SQL.

Operating model:
- Use tools for schema generation, SQL generation, and validation.
- For schema creation or refinement, call generate_schema.
- For SQL creation, call generate_sql with a schema JSON artifact.
- If a tool reports validation issues, explain them and either repair by calling
  the appropriate generation tool again or ask for clarification.
- Do not claim SQL is production-ready unless validate_sql or generate_sql
  returned ok=true.
- Ask concise clarification questions when requirements are too vague or unsafe.
"""


def _latest_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    return ""


def _safe_cut_index(messages: list[AnyMessage], min_recent: int) -> int:
    """Index to summarize up to — always a HumanMessage (turn) boundary, so a
    tool call is never split from its result. 0 → nothing safe to summarize."""
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


def summarize_conversation(state: SchemaGraphState) -> dict:
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
        "changes, generated artifacts, and validation issues.\n\n"
        f"EXISTING SUMMARY:\n{previous or '(none)'}\n\n"
        f"NEW MESSAGES:\n{_render_messages(older)}"
    )
    try:
        new_summary = str(_summarizer_llm.invoke(prompt).content)
    except Exception:
        logger.exception("Schema agent summarization failed")
        return {}

    removals = [RemoveMessage(id=m.id) for m in older if m.id]
    if not removals:
        return {"summary": new_summary}
    return {"summary": new_summary, "messages": removals}


def recall_memories(state: SchemaGraphState, runtime: Runtime[SchemaContext]) -> dict:
    """Pull long-term memories relevant to the latest user message."""
    query = _latest_user_text(state["messages"])
    memories = ltm.recall(runtime.context.user_id, query)
    return {"recalled": ltm.format_for_prompt(memories)}


def call_agent(state: SchemaGraphState, runtime: Runtime[SchemaContext]) -> dict:
    """The conversational agent — picks its tool-bound LLM by the chosen model."""
    llm = AGENT_LLMS.get(runtime.context.model) or AGENT_LLMS[DEFAULT_MODEL]

    system = SYSTEM_PROMPT
    if state.get("summary"):
        system += f"\n\n## Summary of earlier conversation\n{state['summary']}"
    if state.get("recalled"):
        system += f"\n\n## What you remember about this user\n{state['recalled']}"

    response = llm.invoke([SystemMessage(content=system), *state["messages"]])
    return {"messages": [response]}


# ── Graph ───────────────────────────────────────────────────────────────────
_builder = StateGraph(SchemaGraphState, context_schema=SchemaContext)
_builder.add_node("summarize", summarize_conversation)
_builder.add_node("recall", recall_memories)
_builder.add_node("agent", call_agent)
_builder.add_node("tools", ToolNode(TOOLS))

_builder.add_edge(START, "summarize")
_builder.add_edge("summarize", "recall")
_builder.add_edge("recall", "agent")
_builder.add_conditional_edges("agent", tools_condition)
_builder.add_edge("tools", "agent")

_pool = ConnectionPool(DB_URI)
pg_checkpointer = PostgresSaver(_pool)
schema_agent = _builder.compile(checkpointer=pg_checkpointer, store=ltm.store)
