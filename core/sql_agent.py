# ── stdlib ─────────────────────────────────────────────────────────
import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Optional
from uuid import uuid4

# ── Django / DRF ───────────────────────────────────────────────────
from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

# ── LangChain / LangGraph ──────────────────────────────────────────
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.tools import InjectedToolArg, tool
from langchain.tools import ToolRuntime
from langgraph.runtime import Runtime

# ── Other third-party ──────────────────────────────────────────────
from psycopg_pool import ConnectionPool
from sqlalchemy import text
from typing_extensions import TypedDict

# ── Local ──────────────────────────────────────────────────────────
from core.models import ChatSession, Connection, Result, TokenUsage
from core.services.connection import ConnectionService
from core.services.sql_prompt import build_system_prompt
from core.utils import generate_chat_title

logger = logging.getLogger(__name__)

DB_URI = settings.DB_URI
GROQ_API_KEY = settings.GROQ_API_KEY


# ── State & Context ────────────────────────────────────────────────

class SQLAgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@dataclass
class UserContext:
    user_id: str
    db: SQLDatabase
    connection: Connection
    secure_data: bool = False
    # Model name (Groq model id) — picked by the user per-request.
    # Pre-initialised LLMs are looked up by this key in LLMS_WITH_TOOLS.
    model: str = ""
    # Most recent chart-bound query result, populated by run_sql_query when
    # for_chart=True and consumed by generate_chart to fill the Chart.js JSON.
    last_chart_data: Optional[dict] = None


# ── Chart helpers ──────────────────────────────────────────────────

class ChartType(StrEnum):
    bar = "bar"
    doughnut = "doughnut"
    line = "line"
    scatter = "scatter"


def _build_chart_config(chart_type: ChartType, title: str) -> dict:
    base = {
        "type": chart_type.value,
        "data": {"labels": [], "datasets": [{"data": []}]},
        "options": {
            "plugins": {
                "legend": {"display": chart_type == ChartType.doughnut},
                "title": {"display": True, "text": title},
            },
        },
    }
    if chart_type == ChartType.bar:
        base["data"]["datasets"][0]["backgroundColor"] = [
            "rgba(255, 99, 132, 0.5)", "rgba(255, 159, 64, 0.5)",
            "rgba(255, 205, 86, 0.5)", "rgba(75, 192, 192, 0.5)",
            "rgba(54, 162, 235, 0.5)", "rgba(153, 102, 255, 0.5)",
            "rgba(201, 203, 207, 0.5)",
        ]
    elif chart_type == ChartType.line:
        base["data"]["datasets"][0]["fill"] = False
        base["data"]["datasets"][0]["tension"] = 0.1
    elif chart_type == ChartType.doughnut:
        base["data"]["datasets"][0]["backgroundColor"] = [
            "rgb(255, 99, 132)", "rgb(54, 162, 235)",
            "rgb(255, 205, 86)", "rgb(75, 192, 192)",
            "rgb(153, 102, 255)",
        ]
        base["data"]["datasets"][0]["hoverOffset"] = 4
    elif chart_type == ChartType.scatter:
        base["options"]["scales"] = {
            "x": {"type": "linear", "position": "bottom"},
            "y": {"type": "linear", "position": "left"},
        }
    return base


def fill_chart_with_data(chart_json: str, columns: list, rows: list, chart_type: str) -> str:
    config = json.loads(chart_json)
    ct = ChartType[chart_type]
    if ct in (ChartType.bar, ChartType.line, ChartType.doughnut):
        config["data"]["labels"] = [row[0] for row in rows]
        config["data"]["datasets"][0]["data"] = [row[1] for row in rows]
    elif ct == ChartType.scatter:
        config["data"]["datasets"][0]["data"] = [{"x": row[0], "y": row[1]} for row in rows]
    return json.dumps(config)


# ── SQL helpers ────────────────────────────────────────────────────

FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "REPLACE", "MERGE", "GRANT", "REVOKE",
}


def validate_read_only(query: str) -> None:
    first_keyword = query.strip().split()[0].upper() if query.strip() else ""
    if first_keyword in FORBIDDEN_KEYWORDS:
        raise ValueError(
            f"{first_keyword} statements are not allowed. Only SELECT queries are permitted."
        )


def truncate_value(content: Any, length: int = 200) -> Any:
    if not isinstance(content, str) or length <= 0:
        return content
    if len(content) <= length:
        return content
    return content[: length - 3] + "..."


def execute_sql_query(
    db: SQLDatabase,
    query: str,
    for_chart: bool = False,
    chart_type: Optional[ChartType] = None,
) -> dict:
    """Execute SQL and return {"columns": [...], "rows": [...]}."""
    validate_read_only(query)

    with db._engine.connect() as conn:
        result = conn.execute(text(query))
        columns = list(result.keys())
        rows = [list(row) for row in result.fetchall()]

    truncated_rows = [[truncate_value(cell) for cell in row] for row in rows]

    if for_chart and chart_type in (ChartType.bar, ChartType.line, ChartType.doughnut, ChartType.scatter):
        if not truncated_rows:
            raise ValueError("No data returned from the query.")
        if len(truncated_rows[0]) != 2:
            raise ValueError(
                f"Chart requires exactly 2 columns (labels, values), but got "
                f"{len(truncated_rows[0])}. Columns: {columns}. Please rewrite the SQL "
                f"to SELECT only 2 columns."
            )

    return {"columns": columns, "rows": truncated_rows}


# ── Tools ──────────────────────────────────────────────────────────

# Marks the runtime param as caller-injected so LLM schema generation skips it.
# ToolNode still auto-injects it because it dispatches off the underlying Runtime type.
# InjectedRuntime = Annotated[Runtime[UserContext], InjectedToolArg]


@tool
def list_tables(runtime: ToolRuntime[UserContext]) -> str:
# def list_tables(runtime: InjectedRuntime) -> str:
    """List all available tables in the database. Call this first to see what tables exist."""
    return ", ".join(runtime.context.db.get_usable_table_names())


@tool
def get_table_schema(table_names: str, runtime: ToolRuntime[UserContext]) -> str:
# def get_table_schema(table_names: str, runtime: InjectedRuntime) -> str:
    """Get the schema and sample rows for the specified tables.
    Input is a comma-separated list of table names.
    Example: 'customers, orders, products'
    ALWAYS call list_tables first to verify the tables exist!"""
    db = runtime.context.db
    names = [t.strip() for t in table_names.split(",")]
    available = db.get_usable_table_names()
    invalid = [n for n in names if n not in available]
    if invalid:
        return f"ERROR: Tables {invalid} not found. Available tables: {', '.join(available)}"
    return db.get_table_info(names)


@tool
def run_sql_query(
    query: str,
    runtime: ToolRuntime[UserContext],
    # runtime: InjectedRuntime,
    for_chart: bool = False,
    chart_type: Optional[str] = None,
) -> str:
    """Execute a SQL query against the database and return results.
    If the query is for charting, set for_chart=True and specify chart_type
    (bar, line, doughnut, scatter). Chart queries MUST return exactly 2 columns.
    NEVER run this without calling get_table_schema first!"""
    db = runtime.context.db
    secure_data = runtime.context.secure_data

    try:
        validate_read_only(query)
    except ValueError as e:
        return f"ERROR: {e}"

    try:
        ct = ChartType[chart_type] if chart_type else None
        result = execute_sql_query(db, query, for_chart, ct)
        columns = result["columns"]
        rows = result["rows"]

        # Stash full data so a follow-up generate_chart call can fill the
        # Chart.js JSON without re-querying. Validation in execute_sql_query
        # has already enforced 2-column shape for chart queries.
        if for_chart and chart_type:
            runtime.context.last_chart_data = {
                "columns": columns,
                "rows": rows,
                "chart_type": chart_type,
            }

        if secure_data:
            if rows:
                data_types = [type(cell).__name__ for cell in rows[0]]
                return (
                    f"Query executed successfully.\n"
                    f"Columns: {columns}\n"
                    f"Column types: {data_types}\n"
                    f"Number of rows: {len(rows)}\n"
                    f"(Data hidden for security — user can see the results)"
                )
            return "Query executed successfully. No rows returned."

        preview_rows = rows[:10]
        return (
            f"Columns: {columns}\n"
            f"Rows (first {min(10, len(rows))} of {len(rows)}):\n{preview_rows}"
        )
    except ValueError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR executing query: {e}"


@tool
def generate_chart(
    chart_type: str,
    title: str,
    runtime: ToolRuntime[UserContext],
    # runtime: InjectedRuntime,
) -> str:
    """Generate a Chart.js JSON config for visualizing the last query results.
    Only call this AFTER running a SQL query with for_chart=True.
    chart_type must be one of: bar, line, doughnut, scatter."""
    try:
        ct = ChartType[chart_type]
    except KeyError:
        return f"ERROR: Invalid chart type '{chart_type}'. Use: bar, line, doughnut, scatter."

    skeleton = json.dumps(_build_chart_config(ct, title))
    cached = runtime.context.last_chart_data
    if cached and cached.get("rows"):
        try:
            filled = fill_chart_with_data(
                skeleton,
                cached["columns"],
                cached["rows"],
                chart_type,
            )
            return f"CHART_JSON:{filled}"
        except Exception as e:
            logger.warning("fill_chart_with_data failed: %s", e)
    return f"CHART_JSON:{skeleton}"


# ── LLM & Graph (compiled once, reused per request) ────────────────

# Models exposed to users — pre-initialised at import time so requests don't
# pay the cost of constructing ChatGroq + bind_tools every call. Add a model
# here to expose it; remove and it disappears from the picker.
SUPPORTED_MODELS = (
    "openai/gpt-oss-120b",
    "groq/compound",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
)
DEFAULT_MODEL = "openai/gpt-oss-120b"


def _build_llm(model_name: str) -> ChatGroq:
    return ChatGroq(
        model=model_name,
        temperature=0.1,
        max_tokens=4000,
        timeout=60,
        api_key=GROQ_API_KEY,
        max_retries=3,
    )


tools = [list_tables, get_table_schema, run_sql_query, generate_chart]


# ── Guardrails ─────────────────────────────────────────────────────
#
# Catches abusive / unsafe / out-of-scope user input *before* it reaches the
# LLM. Destructive SQL is NOT checked here — sql_toolkit.validate_read_only
# owns that and inspects the actual query, which is more accurate than
# pattern-matching natural language.
#
# Each category has its own pattern list + refusal message. Patterns are
# matched as whole words (\b boundaries, case-insensitive) to avoid the
# Scunthorpe problem (e.g. "class" wouldn't trip an "ass" entry).
# Categories are checked in priority order: threats → hate → sexual →
# prompt-injection. Threats first so users in crisis see the helpline.

# THREAT_PATTERNS = (
#     "kill yourself", "kys", "kms", "kill myself",
#     "i'll kill", "i will kill", "gonna kill",
#     "shoot up", "shoot you", "shoot them",
#     "bomb you", "bomb the",
#     "hope you die", "want you dead",
#     "want to die", "i should die", "going to die",
#     "end my life", "end it all",
#     "harm myself", "harm yourself", "hurt myself", "hurt yourself",
#     "commit suicide", "thinking of suicide",
# )

HATE_PATTERNS = (
    "nigger", "nigga",
    "faggot",
    "tranny",
    "retard", "retarded",
    "kike",
    "chink", "ching chong",
    "spic",
    "gook",
    "wetback",
    "towelhead", "raghead",
    "gypped",
)

SEXUAL_PATTERNS = (
    "blowjob", "handjob", "rimjob", "footjob",
    "cunnilingus", "fellatio",
    "cumshot", "jizz",
    "masturbate", "masturbation",
    "jack off", "jerk off",
    "pornhub", "xvideos", "xnxx", "redtube", "onlyfans",
    "suck my", "fuck me", "want to fuck you",
)

INJECTION_PATTERNS = (
    "ignore previous instructions", "ignore all previous", "ignore the above",
    "forget previous instructions", "forget your instructions",
    "forget all instructions", "forget what i told you",
    "system prompt", "your prompt",
    "you are now", "pretend you are", "pretend to be",
    "act as if", "act as a",
    "developer mode", "jailbreak", "dan mode",
    "override your", "bypass your",
)

# REFUSAL_THREAT = (
#     "I can't engage with messages about harming yourself or others. "
#     "If you're in distress, please contact a local crisis line."
# )
REFUSAL_HATE = (
    "Your message contains language I can't respond to. "
    "Please rephrase respectfully and try again."
)
REFUSAL_SEXUAL = (
    "That's outside the scope of this assistant. "
    "I'm here to help with questions about your database."
)
REFUSAL_INJECTION = (
    "I can't change my role or override my instructions. "
    "Ask a question about your data and I'll do my best to help."
)


def _compile_patterns(patterns: tuple[str, ...]) -> re.Pattern:
    """Build a single \\b-bounded, case-insensitive regex from a list of phrases."""
    body = "|".join(re.escape(p) for p in patterns)
    return re.compile(rf"\b(?:{body})\b", re.IGNORECASE)


# Order matters: first match wins. Threats first (helpline response),
# then hate, sexual, injection.
GUARDRAILS: tuple[tuple[str, re.Pattern, str], ...] = (
    # ("threat",    _compile_patterns(THREAT_PATTERNS),    REFUSAL_THREAT),
    ("hate",      _compile_patterns(HATE_PATTERNS),      REFUSAL_HATE),
    ("sexual",    _compile_patterns(SEXUAL_PATTERNS),    REFUSAL_SEXUAL),
    ("injection", _compile_patterns(INJECTION_PATTERNS), REFUSAL_INJECTION),
)


def _guardrail_refusal(messages: list[BaseMessage]) -> Optional[str]:
    """Return a category-specific refusal if the latest user message trips a guardrail."""
    latest_user = next(
        (m for m in reversed(messages) if isinstance(m, HumanMessage)),
        None,
    )
    if not latest_user:
        return None
    text = str(latest_user.content or "")
    for category, regex, refusal in GUARDRAILS:
        match = regex.search(text)
        if match:
            logger.info("Guardrail tripped: category=%s match=%r", category, match.group(0))
            return refusal
    return None

LLMS_WITH_TOOLS: dict[str, Any] = {
    name: _build_llm(name).bind_tools(tools) for name in SUPPORTED_MODELS
}


def call_model(state: SQLAgentState, runtime: Runtime[UserContext]) -> dict:
    messages = state["messages"]

    # Guardrail: bail out with a prebuilt refusal if the user's input hits a
    # blocked pattern. The returned AIMessage carries no tool_calls, so
    # should_continue routes the graph straight to END and the user sees the
    # refusal via the same `done` SSE event as any normal answer.
    refusal = _guardrail_refusal(messages)
    if refusal:
        return {"messages": [AIMessage(content=refusal)]}

    connection = runtime.context.connection
    dialect = connection.dialect or connection.type
    model_name = runtime.context.model or DEFAULT_MODEL
    print(f"=== model_name : {model_name} ===")

    # Fall back to default if the user passed an unknown / unsupported model.
    llm_with_tools = LLMS_WITH_TOOLS.get(model_name) or LLMS_WITH_TOOLS[DEFAULT_MODEL]

    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=build_system_prompt(dialect=dialect))] + messages

    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


def should_continue(state: SQLAgentState) -> str:
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


graph = StateGraph(SQLAgentState, context_schema=UserContext)
graph.add_node("agent", call_model)
graph.add_node("tools", ToolNode(tools))
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
graph.add_edge("tools", "agent")

_pool = ConnectionPool(conninfo=DB_URI, min_size=1, max_size=3)
_checkpointer = PostgresSaver(_pool)
# _checkpointer.setup()  # run once on first deploy to create checkpoint tables
sql_agent = graph.compile(checkpointer=_checkpointer)


# ── SSE helpers ────────────────────────────────────────────────────

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _safe_serialize(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def _extract_token_content(token: Any) -> tuple[str, str]:
    """Returns (kind, text). `kind` is 'reasoning' or 'text'.

    Groq / OpenAI o-series stream `content_blocks` of two shapes:
      { type: 'reasoning', reasoning: '...' }  → kind='reasoning'
      { type: 'text',      text: '...'      }  → kind='text'
    """
    if hasattr(token, "content_blocks") and token.content_blocks:
        block = token.content_blocks[0]
        if isinstance(block, dict):
            block_type = str(block.get("type") or "text")
            text = block.get("reasoning") if block_type == "reasoning" else block.get("text", "")
        else:
            block_type = str(getattr(block, "type", "") or "text")
            if block_type == "reasoning":
                text = getattr(block, "reasoning", "")
            else:
                text = getattr(block, "text", "")
        return ("reasoning" if block_type == "reasoning" else "text"), str(text or "")

    if hasattr(token, "content"):
        return "text", str(token.content or "")
    return "text", ""

# ── View ───────────────────────────────────────────────────────────

class SqlAgent(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_id = str(request.user.id)
        query = request.data.get("query")
        thread_id = request.data.get("thread_id")
        connection_id = request.data.get("connection_id")
        secure_data = request.data.get("secure_data", False)
        # Model defaults to DEFAULT_MODEL when omitted or unsupported.
        requested_model = request.data.get("model") or DEFAULT_MODEL
        model = requested_model if requested_model in LLMS_WITH_TOOLS else DEFAULT_MODEL

        if not query:
            return Response({"error": "query is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve thread + connection
        new_thread = False
        if thread_id:
            try:
                chat = ChatSession.objects.get(thread_id=thread_id, user=request.user)
            except ChatSession.DoesNotExist:
                return Response({"error": "Conversation not found"}, status=status.HTTP_404_NOT_FOUND)
            if not chat.connection:
                return Response(
                    {"error": "This conversation has no database connection"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            connection = chat.connection
        else:
            if not connection_id:
                return Response({"error": "connection_id is required"}, status=status.HTTP_400_BAD_REQUEST)
            try:
                connection = Connection.objects.get(id=connection_id, user=request.user)
            except Connection.DoesNotExist:
                return Response({"error": "Connection not found"}, status=status.HTTP_404_NOT_FOUND)
            new_thread = True
            thread_id = uuid4().hex
            chat = ChatSession.objects.create(
                user=request.user,
                thread_id=thread_id,
                connection=connection,
                title="New Chat",
            )

        thread_id = str(thread_id)
        config = {"configurable": {"thread_id": thread_id}}

        try:
            db = ConnectionService.get_sql_database(connection)
        except Exception as e:
            return Response({"error": f"Failed to connect: {e}"}, status=status.HTTP_400_BAD_REQUEST)

        context = UserContext(
            user_id=user_id,
            db=db,
            connection=connection,
            secure_data=secure_data,
            model=model,
        )

        def stream_generator():
            last_run_result_id: Optional[Any] = None  # link CHART_GENERATION_RESULT -> SQL_QUERY_RUN_RESULT
            produced_response = False  # any AIMessage content this turn? gates ChatSession cleanup on failure

            try:
                if new_thread:
                    yield _sse({
                        "type": "thread_created",
                        "thread_id": thread_id,
                        "connection_id": str(connection.id),
                    })

                for mode, data in sql_agent.stream(
                    {"messages": [HumanMessage(content=query)]},
                    stream_mode=["messages", "updates"],
                    config=config,
                    context=context,
                    version="v2",
                ):
                    # ─── 1. MESSAGES MODE — token + reasoning streaming ─────
                    if mode == "messages":
                        token, metadata = data
                        kind, content = _extract_token_content(token)
                        if content:
                            yield _sse({
                                "type": "token",
                                "kind": kind,                              # 'reasoning' | 'text'
                                "node": metadata.get("langgraph_node", "unknown"),
                                "text": content,
                            })
                    # ─── 2. UPDATES MODE — node completions ─────────────────
                    elif mode == "updates":
                        for node_name, state_update in data.items():
                            messages = (
                                state_update.get("messages", [])
                                if isinstance(state_update, dict)
                                else []
                            )

                            if node_name == "agent":
                                # Agent finished: persist token usage + surface tool calls + persist SQL strings
                                for msg in messages:
                                    # Persist token usage (one row per LLM round-trip)
                                    usage = getattr(msg, "usage_metadata", None) or {}
                                    if usage:
                                        details = usage.get("output_token_details") or {}
                                        TokenUsage.objects.create(
                                            user=request.user,
                                            thread_id=thread_id,
                                            model_name=model,
                                            provider="groq",
                                            input_tokens=int(usage.get("input_tokens") or 0),
                                            output_tokens=int(usage.get("output_tokens") or 0),
                                            reasoning_tokens=int(details.get("reasoning") or 0),
                                            total_tokens=int(usage.get("total_tokens") or 0),
                                        )

                                    for tc in getattr(msg, "tool_calls", None) or []:
                                        tc_name = tc["name"]
                                        tc_args = tc.get("args", {})

                                        yield _sse({
                                            "type": "tool_start",
                                            "name": tc_name,
                                            "args": _safe_serialize(tc_args),
                                        })

                                        if tc_name == "run_sql_query":
                                            sql = tc_args.get("query", "")
                                            if sql:
                                                rec = Result.objects.create(
                                                    thread_id=thread_id,
                                                    type=Result.ResultType.SQL_QUERY_STRING,
                                                    content=json.dumps({
                                                        "sql": sql,
                                                        "for_chart": tc_args.get("for_chart", False),
                                                    }),
                                                )
                                                yield _sse({
                                                    "type": "result",
                                                    "result_type": Result.ResultType.SQL_QUERY_STRING,
                                                    "result_id": str(rec.id),
                                                    "content": {
                                                        "sql": sql,
                                                        "for_chart": tc_args.get("for_chart", False),
                                                    },
                                                })

                            elif node_name == "tools":
                                # Tool node finished: surface tool results + persist run/chart results
                                for msg in messages:
                                    name = getattr(msg, "name", None)
                                    content = str(getattr(msg, "content", "") or "")
                                    if not name:
                                        continue

                                    # Charts ship a full Chart.js JSON in `content` and are
                                    # rendered by the frontend — never truncate those.
                                    sse_content = content if name == "generate_chart" else content[:500]
                                    yield _sse({
                                        "type": "tool_result",
                                        "name": name,
                                        "content": sse_content,
                                    })

                                    if name == "run_sql_query" and not content.startswith("ERROR"):
                                        rec = Result.objects.create(
                                            thread_id=thread_id,
                                            type=Result.ResultType.SQL_QUERY_RUN_RESULT,
                                            content=json.dumps({"raw": content}),
                                        )
                                        last_run_result_id = rec.id
                                        yield _sse({
                                            "type": "result",
                                            "result_type": Result.ResultType.SQL_QUERY_RUN_RESULT,
                                            "result_id": str(rec.id),
                                            "content": {"raw": content},
                                        })

                                    elif name == "generate_chart" and "CHART_JSON:" in content:
                                        chart_json = content.split("CHART_JSON:", 1)[1]
                                        rec = Result.objects.create(
                                            thread_id=thread_id,
                                            type=Result.ResultType.CHART_GENERATION_RESULT,
                                            content=json.dumps({"chartjs_json": chart_json}),
                                            linked_id=last_run_result_id,
                                        )
                                        yield _sse({
                                            "type": "result",
                                            "result_type": Result.ResultType.CHART_GENERATION_RESULT,
                                            "result_id": str(rec.id),
                                            "content": {"chartjs_json": chart_json},
                                        })

                # ─── 3. FINAL — done event + title for new threads ─────────
                final_state = sql_agent.get_state(config)
                final_messages = final_state.values.get("messages", []) if final_state else []
                last = final_messages[-1] if final_messages else None

                if isinstance(last, AIMessage) and last.content:
                    produced_response = True
                    yield _sse({"type": "done", "text": last.content})

                # Only title-generate on success — failed runs get cleaned up below.
                if new_thread and produced_response:
                    title_input = f"User: {query}\nAssistant: {last.content}"
                    title = generate_chat_title(title_input)
                    chat.title = title
                    chat.save(update_fields=["title"])
                    yield _sse({"type": "title", "thread_id": thread_id, "title": title})

            except Exception as e:
                logger.exception("SQL agent stream failed")
                yield _sse({"type": "error", "error": str(e)})

            finally:
                # If this was a brand-new chat and the agent never produced a real
                # response, drop the empty ChatSession so it doesn't clutter the
                # sidebar. Runs even on client disconnect.
                if new_thread and not produced_response:
                    try:
                        deleted, _ = ChatSession.objects.filter(
                            thread_id=thread_id, user=request.user
                        ).delete()
                        if deleted:
                            logger.info(
                                "Deleted empty ChatSession %s after failed first turn",
                                thread_id,
                            )
                    except Exception:
                        logger.exception(
                            "Failed to clean up empty ChatSession %s", thread_id
                        )

        response = StreamingHttpResponse(stream_generator(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
