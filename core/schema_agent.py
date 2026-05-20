"""Streaming HTTP endpoint for the schema agent.

The agent graph itself lives in core/services/schema_graph.py (LTM +
summarization + validated schema/SQL tools). This module is just the DRF view:
it drives the graph with .stream(), translates LangGraph events into the SSE
shape the frontend expects, persists generated artifacts onto the SchemaProject
row, and runs post-stream search indexing + memory extraction.

SSE event vocabulary (unchanged, so the frontend is untouched):
    thread_created · node_start · token · result (SCHEMA|SQL) · done · title · error
"""

import json
import logging
from uuid import uuid4

from django.http import HttpResponse, StreamingHttpResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import ConversationMessage, SchemaProject
from core.services import memory as ltm
from core.services.schema_graph import (
    DEFAULT_MODEL,
    SUPPORTED_MODELS,
    SchemaContext,
    pg_checkpointer,
    schema_agent,
)
from core.tasks import persist_schema_project
from core.utils import generate_chat_title

logger = logging.getLogger(__name__)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# Progress labels surfaced to the UI when the agent invokes a tool.
_TOOL_LABELS = {
    "generate_schema": "Designing the schema",
    "generate_sql": "Writing SQL + seed data",
    "validate_schema_json": "Validating the schema",
    "validate_sql": "Validating SQL",
}


def _parse_artifact(content) -> dict | None:
    """Parse a tool message's JSON content into an artifact dict, or None."""
    try:
        payload = json.loads(str(content or ""))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


class SchemaAgent(APIView):
    """Streaming endpoint for the schema agent. Shares the SqlAgent SSE shape."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return HttpResponse("Schema agent is running.")

    def post(self, request):
        query = request.data.get("query")
        thread_id = str(request.data.get("thread_id") or uuid4().hex)

        if not query:
            return Response(
                {"error": "query is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        requested_model = request.data.get("model") or DEFAULT_MODEL
        model = requested_model if requested_model in SUPPORTED_MODELS else DEFAULT_MODEL

        # Get or create the project up-front so the checkpointer has a stable
        # slug. Empty projects (failed first turn) are cleaned up in finally.
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
        context = SchemaContext(user_id=str(request.user.id), model=model)

        def stream_generator():
            produced_response = False
            final_text = ""
            latest_schema: dict | None = None   # last generate_schema `schema` dict
            latest_sql: dict | None = None      # last generate_sql artifact

            try:
                if new_project:
                    yield _sse({"type": "thread_created", "slug": thread_id})

                for mode, data in schema_agent.stream(
                    {"messages": [HumanMessage(content=query)]},
                    stream_mode=["messages", "updates"],
                    config=config,
                    context=context,
                ):
                    # ─── 1. MESSAGES — stream the agent node's text tokens ──
                    if mode == "messages":
                        token, metadata = data
                        if metadata.get("langgraph_node") != "agent":
                            continue
                        content = getattr(token, "content", None)
                        if content:
                            text = str(content)
                            final_text += text
                            yield _sse({
                                "type": "token",
                                "kind": "text",
                                "node": "agent",
                                "text": text,
                            })

                    # ─── 2. UPDATES — tool progress + artifact results ──────
                    elif mode == "updates":
                        for node_name, state_update in data.items():
                            if not isinstance(state_update, dict):
                                continue
                            messages = state_update.get("messages", []) or []

                            # Agent requested a tool → emit a progress label.
                            if node_name == "agent":
                                for msg in messages:
                                    for call in getattr(msg, "tool_calls", None) or []:
                                        label = _TOOL_LABELS.get(call["name"])
                                        if label:
                                            yield _sse({
                                                "type": "node_start",
                                                "node": call["name"],
                                                "label": label,
                                            })

                            # Tool finished → parse artifact JSON, emit result.
                            if node_name == "tools":
                                for msg in messages:
                                    if not isinstance(msg, ToolMessage):
                                        continue
                                    artifact = _parse_artifact(msg.content)
                                    if not artifact:
                                        continue
                                    kind = artifact.get("artifact")
                                    if kind == "schema" and artifact.get("schema"):
                                        latest_schema = artifact["schema"]
                                        yield _sse({
                                            "type": "result",
                                            "result_type": "SCHEMA",
                                            "content": {
                                                "schema_table": json.dumps(latest_schema),
                                            },
                                        })
                                    elif kind == "sql" and artifact.get("sql"):
                                        latest_sql = artifact
                                        yield _sse({
                                            "type": "result",
                                            "result_type": "SQL",
                                            "content": {
                                                "sql_table": artifact.get("sql", ""),
                                                "sql_seed_data": artifact.get("seed_data", ""),
                                            },
                                        })

                # ─── 3. FINAL — done + persist + title + indexing ──────────
                final_state = schema_agent.get_state(config)
                final_messages = final_state.values.get("messages", []) if final_state else []
                last = final_messages[-1] if final_messages else None
                if not final_text and isinstance(last, AIMessage):
                    final_text = str(last.content or "")

                if final_text:
                    produced_response = True
                    yield _sse({"type": "done", "text": final_text})

                # Persist the generated artifacts onto the SchemaProject. Missing
                # artifacts fall back to the project's current values so a
                # schema-only or SQL-only turn never wipes the other.
                if produced_response and (latest_schema or latest_sql):
                    schema_json = (
                        json.dumps(latest_schema) if latest_schema else project.schema_json
                    )
                    sql_json = latest_sql.get("sql") if latest_sql else project.sql_json
                    seed_json = (
                        latest_sql.get("seed_data") if latest_sql else project.seed_json
                    )
                    if schema_json:
                        persist_schema_project(
                            True, project.id, schema_json, sql_json, seed_json,
                        )

                # Title generation on the first successful turn of a new project.
                if new_project and produced_response:
                    try:
                        new_title = generate_chat_title(
                            f"User: {query}\nAssistant: {final_text}"
                        )
                        project.name = new_title
                        project.save(update_fields=["name"])
                        yield _sse({"type": "title", "slug": thread_id, "title": new_title})
                    except Exception:
                        logger.exception(
                            "Schema-agent title generation failed for %s", thread_id
                        )

                # Mirror the conversation into the search index. Best-effort.
                if produced_response:
                    try:
                        from core.services.search_index import reindex_thread
                        reindex_thread(request.user, "schema", thread_id, final_messages)
                    except Exception:
                        logger.exception(
                            "Failed to index schema thread %s for search", thread_id
                        )

                # Post-stream long-term memory extraction. Best-effort.
                if produced_response:
                    try:
                        ltm.extract_and_store(request.user.id, query, final_text)
                    except Exception:
                        logger.exception(
                            "Memory extraction failed for schema thread %s", thread_id
                        )

            except Exception as e:
                logger.exception("Schema-agent stream failed")
                yield _sse({"type": "error", "error": str(e)})

            finally:
                # Drop a brand-new project that never produced a response so it
                # doesn't clutter the sidebar. Runs even on client disconnect.
                if new_project and not produced_response:
                    try:
                        SchemaProject.objects.filter(
                            slug=thread_id, user=request.user,
                        ).delete()
                        ConversationMessage.objects.filter(
                            user=request.user, agent="schema", thread_id=thread_id,
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

        response = StreamingHttpResponse(
            stream_generator(), content_type="text/event-stream"
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
