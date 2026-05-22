import json
import logging
import operator
import os
import uuid
from typing import Any, List, Literal
from uuid import uuid4

from django.conf import settings
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from dotenv import load_dotenv
from groq import BadRequestError
from langchain.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.exceptions import OutputParserException
from langchain_groq import ChatGroq
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, ValidationError
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from typing_extensions import Annotated, TypedDict

from .models import SchemaProject
from .prompt import (
    TEST_DECISION_SYSTEM_PROMPT,
    TEST_MESSAGE_SYSTEM_PROMPT,
    TEST_SQL_GENERATION_SYSTEM_PROMPT,
    TEST_TABLE_SCHEMA_SYSTEM_PROMPT,
)
from .tasks import persist_schema_project, save_schema_project
from .utils import generate_chat_title

logger = logging.getLogger(__name__)

api_key = settings.GROQ_API_KEY

# State Definition
class SqlState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    prompt: str
    model: str
    valid_intent: bool
    generate: bool
    explain: bool
    schema_table: str
    sql_table: str
    sql_seed_data: str
    # final_json: str

# Structured Output Models
class RouterDecision(BaseModel):
    """Decision for routing to next node with system architecture understanding."""
    valid_intent: bool = Field(
        description="True if the user wants to create / update / build / analyze / learn about any product, app, website, or database system."
    )
    generate: bool = Field(
        description="True if the intent involves creating or designing a product backend, website schema, or SQL."
    )
    explain: bool = Field(
        description=(
            "True if the user wants to learn, understand, debug, optimize, or analyze database structures, SQL queries, or architectural relationships of a website or app's backend."
        )
    )
    answer: str = Field(
        description=(
            "If valid_intent is True: A concise summary of the project intent (e.g., 'User wants to design a app / website / schema / sql'). "
            "If valid_intent is False: Mention that you are a Database Agent and request the user for more specific details about the app, features, or data requirements they need help with."
        )
    )

class TableColumn(BaseModel):
    """Column definition for a table."""
    name: str = Field(description="Column name")
    type: str = Field(description="Column data type")
    constraints: str = Field(description="Column constraints (e.g., PRIMARY KEY, NOT NULL)", default="")

class Table(BaseModel):
    """Table schema definition."""
    name: str = Field(description="Table name")
    columns: List[TableColumn] = Field(description="List of columns in the table")

class DatabaseSchema(BaseModel):
    """DataBase Table Schema."""
    tables: List[Table]
    answer: str = Field(description="A brief explanation of the present schema design and changes made.")

class SQLGeneration(BaseModel):
    """Generate SQL for table and seed data."""
    sql: str = Field(description="Complete CREATE TABLE statements for all tables")
    seed_data: str = Field(description="INSERT statements with sample data for all tables")
    answer: str = Field(description="A brief explanation of the present schema design and changes made.")

class FinalMessage(BaseModel):
    message: str = Field(description="your reply to user prompt")

# ── Models ─────────────────────────────────────────────────────────
# Models the schema agent exposes. Mirrors sql_agent.SUPPORTED_MODELS so the
# shared frontend model-picker works for both agents. Each model is pre-bound
# at import time into the 4 variants the graph nodes need: structured-output
# models for the router / schema / sql nodes, plus a plain model for the
# message node.
SUPPORTED_MODELS = (
    "openai/gpt-oss-120b",
    "groq/compound",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
)
DEFAULT_MODEL = "openai/gpt-oss-120b"


def _build_schema_model(model_name: str) -> ChatGroq:
    return ChatGroq(
        model=model_name,
        temperature=0.1,
        max_tokens=5000,
        timeout=30,
        api_key=api_key,
    )


def _build_model_bundle(model_name: str) -> dict:
    base = _build_schema_model(model_name)
    return {
        "router": base.with_structured_output(RouterDecision),
        "schema": base.with_structured_output(DatabaseSchema),
        "sql": base.with_structured_output(SQLGeneration),
        "plain": base,
    }


SCHEMA_LLMS: dict[str, dict] = {
    name: _build_model_bundle(name) for name in SUPPORTED_MODELS
}


def _models_for(state: SqlState) -> dict:
    """Pick the model bundle for the request's chosen model, defaulting if unknown."""
    name = state.get("model") or DEFAULT_MODEL
    return SCHEMA_LLMS.get(name) or SCHEMA_LLMS[DEFAULT_MODEL]


# ============
# Router Node
# ============

def descision_node(state: SqlState):

    structured_router_model = _models_for(state)["router"]
    user_prompt = state["prompt"]
    all_messages = state.get("messages", [])
    # recent_history = all_messages[-10:] if all_messages else []
    
    # input_messages = [{"role": "system", "content": TEST_DECISION_SYSTEM_PROMPT}]
    # input_messages.extend(recent_history)
    # input_messages.append({"role": "user", "content": user_prompt})

    # result = structured_router_model.invoke(input_messages)
    result = structured_router_model.invoke(
        [{"role": "system", "content": TEST_DECISION_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt}]
    )

    new_messages = [HumanMessage(content=user_prompt), AIMessage(content=result.answer)]
    print("--- Decision Node ---")
    # print( "valid_intent", result.valid_intent)
    # print( "generate", result.generate)
    # print( "explain", result.explain)
    return {
        "messages": new_messages,
        "valid_intent": result.valid_intent,
        "generate": result.generate,
        "explain": result.explain,
        # "answer": result.answer,
    }

def route_decision(state: SqlState) -> Literal["create_table_schema", "generate_sql_node", "message_node"]:
    """Decides whether to create schema, generate SQL, or end."""
    
    print("--- Route Decision ---")

    if not state["valid_intent"]:
        return END
    if state["generate"]:
        return "create_table_schema"
    return "message_node"

# =============
# Schema Nodes
# =============

def create_table_schema(state: SqlState):
    """
    Generates or refines a table schema using structured output.
    """
    structured_schema_model = _models_for(state)["schema"]
    prompt = state["prompt"]
    generate = state.get("generate")
    previous_schema = state.get("schema_table", "")
    all_messages = state.get("messages", [])
    recent_history = all_messages[-15:] if all_messages else []    

    instruction = f"""
        Generate a database table schema for User Requirement: {prompt}
    """
    if generate and previous_schema:
        instruction += f"""
        You previously generated the following table schema:
        {previous_schema} """
        
    result = structured_schema_model.invoke(
        [
            {"role": "system", "content": TEST_TABLE_SCHEMA_SYSTEM_PROMPT},
            *recent_history,
            {"role": "user", "content": instruction},
        ]
    )

    print("--- Create Table Schema ---")
    ai_message = [AIMessage(content=result.answer)]
    tables_data = [table.model_dump() for table in result.tables]
    tables_dump_data = result.model_dump_json(exclude={"answer"}),

    # print("tables_data", tables_data)
    # print("tables_dump_data", tables_dump_data)

    return {
        "messages": ai_message,
        "schema_table": json.dumps({"tables": tables_data})
        # "schema_table": tables_dump_data,
    }
        
# ===========
# Sql Nodes
# ===========

def generate_sql_node(state: SqlState):
    """
    Final node that converts the approved JSON schema into raw SQL and seed data.
    """
    structured_sql_model = _models_for(state)["sql"]
    prompt = state["prompt"]
    generate = state.get("generate")
    schema_table = state.get("schema_table", "")
    sql_table = state.get("sql_table", "")
    sql_seed_data = state.get("sql_seed_data", "")

    instruction = f"""
        Generate appropriate SQL and seed data for this user prompt {prompt}.
        """
    
    if generate and schema_table: 
        instruction += f"""
        Existing Schema: {schema_table}
        """

    if generate and sql_table:
        instruction += f"""
        Existing SQL: {sql_table}
        Existing Seed Data: {sql_seed_data}
        Update or refine the SQL and seed data based on the user request.
        """

    # full_prompt = SQL_GENERATION_SYSTEM_PROMPT + "\n" + instruction
    # result = structured_sql_model.invoke(full_prompt)
    result = structured_sql_model.invoke(
            [
            {"role": "system", "content": TEST_SQL_GENERATION_SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]
    )

    print("--- SQL Generated ---")
    ai_message = [AIMessage(content=result.answer)]

    return {
        "messages": ai_message,
        "sql_table": result.sql,
        "sql_seed_data": result.seed_data,
    }

# ===============
# Message Nodes
# ===============

# sql_reply = schema_model.with_structured_output(FinalMessage)

def message_node(state: SqlState):

    plain_model = _models_for(state)["plain"]
    user_prompt = state["prompt"]
    explain = state.get("explain", False)
    generate = state.get("generate", False)

    schema_table = state.get("schema_table")
    sql_table = state.get("sql_table")
    sql_seed_data = state.get("sql_seed_data")

    all_messages = state.get("messages", [])
    recent_history = all_messages[-15:] if all_messages else []    

    # Base system instruction
    # system_content = MESSAGE_SYSTEM_PROMPT
    system_content = f"user prompt: {user_prompt}"

    if generate and explain:
        system_content += f"""
        --- TECHNICAL CONTEXT ---
        Current Schema: {schema_table}
        """
        # system_content += f"""
        # --- TECHNICAL CONTEXT ---
        # Current Schema: {schema_table}
        # Current SQL: {sql_table}
        # Current Seed Data: {sql_seed_data}
        # -------------------------
        # """

    # messages_for_llm = [{"role": "system", "content": system_content}]
    # messages_for_llm.extend(recent_history)
    # messages_for_llm.append({"role": "user", "content": user_prompt})

    # messages_for_llm = [
    #     SystemMessage(content=system_content),
    #     *recent_history,
    #     HumanMessage(content=user_prompt)
    #     ]

    # result = sql_reply.invoke(messages_for_llm)
    result = plain_model.invoke(
        [
            {"role": "system", "content": TEST_MESSAGE_SYSTEM_PROMPT},
            *recent_history,
            {"role": "user", "content": system_content},
        ]
    )

    print("--- Message Node ---")
    # print("final response:", result.content)
    ai_msg = AIMessage(content=result.content)

    return {
        "messages": [ai_msg],
    }


# 5. Graph Construction
schema_graph = StateGraph(SqlState)
schema_graph.add_node("descision_node", descision_node)
schema_graph.add_node("create_table_schema", create_table_schema,
    retry_policy=RetryPolicy(
            max_attempts=2,
            initial_interval=1.0,
            backoff_factor=2.0,
            max_interval= 30,
            retry_on=Exception
        )
    )
schema_graph.add_node("generate_sql_node", generate_sql_node,
    retry_policy=RetryPolicy(
            max_attempts=2,
            initial_interval=1.0,
            backoff_factor=2.0,
            max_interval= 30,
            retry_on=Exception
        )
    )
schema_graph.add_node("message_node", message_node)

schema_graph.add_edge(START, "descision_node")
schema_graph.add_conditional_edges(
    "descision_node",
    route_decision,
    {
        END: END,
        "create_table_schema": "create_table_schema",
        "message_node": "message_node"
    }
)
schema_graph.add_edge("create_table_schema", "generate_sql_node")
schema_graph.add_edge("generate_sql_node", "message_node")
schema_graph.add_edge("message_node", END)

DB_URI= settings.DB_URI

pool = ConnectionPool(DB_URI)
pg_checkpointer = PostgresSaver(pool)

schema_agent = schema_graph.compile(checkpointer=pg_checkpointer)

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# Node labels surfaced to the UI. Keep keys aligned with graph node names.
_NODE_LABELS = {
    "descision_node": "Understanding your request",
    "create_table_schema": "Designing the schema",
    "generate_sql_node": "Writing SQL + seed data",
    "message_node": "Composing reply",
}


class SchemaAgent(APIView):
    """Streaming endpoint for the schema agent.

    Mirrors the SSE shape used by `SqlAgent` so the frontend can share its
    streaming utilities. The underlying graph nodes are unchanged — this view
    just consumes the graph's stream events and translates them into SSE.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return HttpResponse("Hello, this is your SQL Schema AI, JARVIS.")

    def post(self, request):
        # Read the JSON body directly (same pattern as SqlAgent.post) — the
        # old MessageSerializer had no `model` field.
        query = request.data.get("query")
        thread_id = request.data.get("thread_id") or uuid4().hex
        thread_id = str(thread_id)

        if not query:
            return Response(
                {"error": "query is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Model defaults to DEFAULT_MODEL when omitted or unsupported.
        requested_model = request.data.get("model") or DEFAULT_MODEL
        model = requested_model if requested_model in SCHEMA_LLMS else DEFAULT_MODEL

        # Get or create the project up-front so we have a stable slug for the
        # checkpointer and a row for history listing. Empty projects (failed
        # first turn) get cleaned up in finally below.
        project, new_project = SchemaProject.objects.get_or_create(
            slug=thread_id,
            defaults={"user": request.user},
        )
        if not new_project and project.user != request.user:
            return Response(
                {"error": "Unauthorized project access"},
                status=status.HTTP_403_FORBIDDEN,
            )

        config = {"configurable": {"thread_id": thread_id}}

        def stream_generator():
            produced_response = False
            final_text = ""
            emitted_nodes: set[str] = set()

            try:
                if new_project:
                    yield _sse({
                        "type": "thread_created",
                        "slug": thread_id,
                    })

                for mode, data in schema_agent.stream(
                    {"prompt": query, "model": model},
                    stream_mode=["messages", "updates"],
                    config=config,
                ):
                    # ─── 1. MESSAGES MODE — token streaming (message_node only) ──
                    if mode == "messages":
                        token, metadata = data
                        node = metadata.get("langgraph_node", "")
                        # Structured-output nodes don't produce streamable tokens;
                        # only message_node's free-form text is worth streaming.
                        if node != "message_node":
                            continue
                        content = getattr(token, "content", None)
                        if content:
                            text = str(content)
                            final_text += text
                            yield _sse({
                                "type": "token",
                                "kind": "text",
                                "node": node,
                                "text": text,
                            })

                    # ─── 2. UPDATES MODE — node-level progress + structured results ──
                    elif mode == "updates":
                        for node_name, state_update in data.items():
                            if node_name not in emitted_nodes:
                                emitted_nodes.add(node_name)
                                yield _sse({
                                    "type": "node_start",
                                    "node": node_name,
                                    "label": _NODE_LABELS.get(node_name, node_name),
                                })

                            if not isinstance(state_update, dict):
                                continue

                            # Schema generation → ship the structured tables JSON.
                            schema_table = state_update.get("schema_table")
                            if schema_table:
                                yield _sse({
                                    "type": "result",
                                    "result_type": "SCHEMA",
                                    "content": {"schema_table": schema_table},
                                })

                            # SQL generation → ship CREATE + INSERT strings.
                            sql_table = state_update.get("sql_table")
                            sql_seed = state_update.get("sql_seed_data")
                            if sql_table or sql_seed:
                                yield _sse({
                                    "type": "result",
                                    "result_type": "SQL",
                                    "content": {
                                        "sql_table": sql_table or "",
                                        "sql_seed_data": sql_seed or "",
                                    },
                                })

                # ─── 3. FINAL — pull canonical state, persist, emit done + title ──
                final_state = schema_agent.get_state(config)
                values = final_state.values if final_state else {}
                messages = values.get("messages", [])
                last = messages[-1] if messages else None

                # Prefer the streamed text; fall back to last AIMessage content.
                if not final_text and isinstance(last, AIMessage):
                    final_text = str(last.content or "")

                generate_intent = values.get("generate", False)
                schema_json = values.get("schema_table")
                sql_table_json = values.get("sql_table")
                sql_seed_json = values.get("sql_seed_data")

                # Emit the canonical schema + SQL directly from final state, in the
                # exact shape the frontend expects. This is the reliable delivery
                # path — the mid-stream `updates` parsing is best-effort and will be
                # tightened later.
                if schema_json:
                    yield _sse({
                        "type": "result",
                        "result_type": "SCHEMA",
                        "content": {"schema_table": schema_json},
                    })
                if sql_table_json or sql_seed_json:
                    yield _sse({
                        "type": "result",
                        "result_type": "SQL",
                        "content": {
                            "sql_table": sql_table_json or "",
                            "sql_seed_data": sql_seed_json or "",
                        },
                    })

                if final_text:
                    produced_response = True
                    yield _sse({"type": "done", "text": final_text})

                # Persist structured outputs. Done synchronously — it's a single
                # fast DB write and avoids requiring a running Celery worker +
                # result backend (django_celery_results isn't migrated here).
                if produced_response and (schema_json or sql_table_json or sql_seed_json):
                    persist_schema_project(
                        generate_intent,
                        project.id,
                        schema_json,
                        sql_table_json,
                        sql_seed_json,
                    )

                # Title generation on the first successful turn of a new project.
                if new_project and produced_response:
                    title_input = f"User: {query}\nAssistant: {final_text}"
                    try:
                        new_title = generate_chat_title(title_input)
                        project.name = new_title
                        project.save(update_fields=["name"])
                        yield _sse({"type": "title", "slug": thread_id, "title": new_title})
                    except Exception:
                        logger.exception("Schema-agent title generation failed for %s", thread_id)

                # Mirror the conversation text into the search index. Best-effort
                # — a failure here must never break the response stream.
                if produced_response:
                    try:
                        from core.services.search_index import reindex_thread
                        reindex_thread(request.user, "schema", thread_id, messages)
                    except Exception:
                        logger.exception(
                            "Failed to index schema thread %s for search", thread_id
                        )

            except Exception as e:
                logger.exception("Schema-agent stream failed")
                yield _sse({"type": "error", "error": str(e)})

            finally:
                # Clean up empty new projects (parallels the SQL-agent cleanup).
                if new_project and not produced_response:
                    try:
                        SchemaProject.objects.filter(
                            slug=thread_id, user=request.user
                        ).delete()
                        pg_checkpointer.delete_thread(thread_id)
                        logger.info(
                            "Deleted empty SchemaProject %s after failed first turn",
                            thread_id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to clean up empty SchemaProject %s", thread_id
                        )

        response = StreamingHttpResponse(stream_generator(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response