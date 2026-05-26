"""Streaming HTTP endpoint for the HYBRID workflow schema agent.

The graph lives in core/services/schema_graph_hybrid.py. This view drives it
with .stream(), translates LangGraph events into the SAME SSE shape the
frontend already understands (thread_created / node_start / token / result /
done / title / error), persists artifacts onto SchemaProject, and runs
post-stream search-indexing + memory extraction.

Coexists with core/schema_agent.py (agentic) — neither modifies the other.
Swap the URL route to switch which design serves /api/schema-agent/.
"""

import json
import logging
from uuid import uuid4

from django.http import HttpResponse, StreamingHttpResponse
from langchain_core.messages import AIMessage, HumanMessage
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import classify_error
from core.models import ConversationMessage, SchemaProject
from core.services import memory as ltm
from core.services import run_registry
from core.services.schema_graph_hybrid import (
    DEFAULT_MODEL,
    SUPPORTED_MODELS,
    SchemaContext,
    pg_checkpointer,
    schema_agent_hybrid,
)
from core.tasks import persist_schema_project
from core.utils import generate_chat_title

logger = logging.getLogger(__name__)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# Frontend `currentNode` labels — one per graph node entering.
_NODE_LABELS = {
    "summarize": "Compacting earlier turns",
    "recall": "Recalling what you've told me",
    "route": "Understanding your request",
    "gen_schema": "Designing the schema",
    "gen_sql": "Writing SQL + seed data",
    "respond": "Composing reply",
}


class SchemaAgentHybrid(APIView):
    """Streaming endpoint for the hybrid (workflow) schema agent."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return HttpResponse("Hybrid schema agent is running.")

    def post(self, request):
        query = request.data.get("query")
        thread_id = str(request.data.get("thread_id") or uuid4().hex)

        if not query:
            return Response({"error": "query is required"}, status=status.HTTP_400_BAD_REQUEST)

        requested_model = request.data.get("model") or DEFAULT_MODEL
        model = requested_model if requested_model in SUPPORTED_MODELS else DEFAULT_MODEL

        # Reject a second POST on the same thread while a run is in flight.
        # Frontend reads the existing run_id and decides whether to cancel + retry.
        run_id = uuid4().hex
        try:
            handle = run_registry.register(
                run_id=run_id,
                user_id=str(request.user.id),
                agent="schema",
                thread_id=thread_id,
            )
        except run_registry.ConcurrentRunError as e:
            return Response(
                {
                    "error": "A run is already in flight on this thread",
                    "run_id": e.existing.run_id,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Get or create the project up-front so the checkpointer has a stable
        # slug. Empty projects (failed first turn) are cleaned up in `finally`.
        try:
            project, new_project = SchemaProject.objects.get_or_create(
                slug=thread_id,
                defaults={"user": request.user},
            )
            if not new_project and project.user != request.user:
                run_registry.unregister(run_id)
                return Response(
                    {"error": "Unauthorized project access"},
                    status=status.HTTP_403_FORBIDDEN,
                )
        except Exception:
            run_registry.unregister(run_id)
            raise

        config = {"configurable": {"thread_id": thread_id}}
        context = SchemaContext(user_id=str(request.user.id), model=model)

        # Seed state from the persisted project on the first turn so refinement
        # works even for projects created by a different design (agentic / old).
        initial: dict = {"messages": [HumanMessage(content=query)]}
        try:
            existing_state = schema_agent_hybrid.get_state(config)
            already_has_schema = bool(existing_state and existing_state.values.get("schema"))
        except Exception:
            already_has_schema = False
        if not already_has_schema and project.schema_json:
            try:
                parsed = (
                    json.loads(project.schema_json)
                    if isinstance(project.schema_json, str)
                    else project.schema_json
                )
                if isinstance(parsed, dict):
                    initial["schema"] = parsed
                    initial["sql"] = project.sql_json
                    initial["seed_data"] = project.seed_json
            except Exception:
                logger.exception("Could not seed hybrid state from project %s", thread_id)

        def stream_generator():
            produced_response = False
            was_cancelled = False
            final_text = ""
            schema_artifact: dict | None = None
            sql_artifact: str | None = None
            seed_artifact: str | None = None

            try:
                if new_project:
                    yield _sse({"type": "thread_created", "slug": thread_id})

                # Tell the client the run_id so it can target POST /runs/<id>/cancel/.
                yield _sse({"type": "run_started", "run_id": run_id})

                for mode, data in schema_agent_hybrid.stream(
                    initial,
                    stream_mode=["messages", "updates"],
                    config=config,
                    context=context,
                ):
                    # Cooperative cancellation: check between super-step events.
                    if handle.cancel_event.is_set():
                        was_cancelled = True
                        run_registry.repair_orphan_tool_calls(schema_agent_hybrid, config)
                        yield _sse({"type": "cancelled", "run_id": run_id})
                        break

                    # ── 1. MESSAGES — only stream the respond node's tokens ──
                    if mode == "messages":
                        token, metadata = data
                        if metadata.get("langgraph_node") != "respond":
                            continue
                        content = getattr(token, "content", None)
                        if content:
                            text = str(content)
                            final_text += text
                            yield _sse(
                                {
                                    "type": "token",
                                    "kind": "text",
                                    "node": "respond",
                                    "text": text,
                                }
                            )

                    # ── 2. UPDATES — node progress + artifact results ────────
                    elif mode == "updates":
                        for node_name, state_update in data.items():
                            label = _NODE_LABELS.get(node_name)
                            if label:
                                yield _sse(
                                    {
                                        "type": "node_start",
                                        "node": node_name,
                                        "label": label,
                                    }
                                )

                            if not isinstance(state_update, dict):
                                continue

                            # gen_schema completed → emit SCHEMA result event.
                            if node_name == "gen_schema" and state_update.get("schema"):
                                schema_artifact = state_update["schema"]
                                yield _sse(
                                    {
                                        "type": "result",
                                        "result_type": "SCHEMA",
                                        "content": {
                                            "schema_table": json.dumps(schema_artifact),
                                        },
                                    }
                                )

                            # gen_sql completed → emit SQL result event.
                            if node_name == "gen_sql" and (
                                state_update.get("sql") or state_update.get("seed_data")
                            ):
                                sql_artifact = state_update.get("sql") or sql_artifact
                                seed_artifact = state_update.get("seed_data") or seed_artifact
                                yield _sse(
                                    {
                                        "type": "result",
                                        "result_type": "SQL",
                                        "content": {
                                            "sql_table": sql_artifact or "",
                                            "sql_seed_data": seed_artifact or "",
                                        },
                                    }
                                )

                # ── 3. FINAL — done + persist + title + indexing ────────────
                # Skipped on cancel: the `cancelled` SSE event has already been
                # emitted and committed checkpointer state carries the partial
                # work to the next turn.
                if was_cancelled:
                    return

                final_state = schema_agent_hybrid.get_state(config)
                values = final_state.values if final_state else {}
                final_messages = values.get("messages", [])
                last = final_messages[-1] if final_messages else None
                if not final_text and isinstance(last, AIMessage):
                    final_text = str(last.content or "")

                if final_text:
                    produced_response = True
                    yield _sse({"type": "done", "text": final_text})

                # Persist artifacts to SchemaProject. Missing artifacts fall
                # back to existing project values so a schema-only or sql-only
                # turn never wipes the other.
                final_schema = values.get("schema") or schema_artifact
                final_sql = values.get("sql") or sql_artifact
                final_seed = values.get("seed_data") or seed_artifact

                if produced_response and (final_schema or final_sql):
                    schema_json = json.dumps(final_schema) if final_schema else project.schema_json
                    sql_json = final_sql if final_sql else project.sql_json
                    seed_json = final_seed if final_seed else project.seed_json
                    if schema_json:
                        persist_schema_project(
                            True,
                            project.id,
                            schema_json,
                            sql_json,
                            seed_json,
                        )

                # Title for the first successful turn of a new project.
                if new_project and produced_response:
                    try:
                        new_title = generate_chat_title(f"User: {query}\nAssistant: {final_text}")
                        project.name = new_title
                        project.save(update_fields=["name"])
                        yield _sse(
                            {
                                "type": "title",
                                "slug": thread_id,
                                "title": new_title,
                            }
                        )
                    except Exception:
                        logger.exception("Hybrid schema title generation failed for %s", thread_id)

                # Mirror the conversation into the chat-search index.
                if produced_response:
                    try:
                        from core.services.search_index import reindex_thread

                        reindex_thread(request.user, "schema", thread_id, final_messages)
                    except Exception:
                        logger.exception(
                            "Failed to index hybrid schema thread %s for search", thread_id
                        )

                # Post-stream long-term memory extraction. Best-effort.
                if produced_response:
                    try:
                        ltm.extract_and_store(request.user.id, query, final_text)
                    except Exception:
                        logger.exception(
                            "Memory extraction failed for hybrid schema thread %s", thread_id
                        )

            except Exception as e:
                info = classify_error(e)
                logger.exception(
                    "schema_agent_hybrid_stream_failed",
                    extra={
                        "run_id": run_id,
                        "user_id": str(request.user.id),
                        "thread_id": thread_id,
                        "agent": "schema",
                        "model": model,
                        "error_code": info.code,
                        "error_class": type(e).__name__,
                        "retryable": info.retryable,
                    },
                )
                yield _sse(info.to_sse(run_id=run_id))

            finally:
                run_registry.unregister(run_id)

                # Drop an empty new project so it doesn't clutter the sidebar.
                # Skip on cancel: the user may want to retry on the same thread.
                if new_project and not produced_response and not was_cancelled:
                    try:
                        SchemaProject.objects.filter(
                            slug=thread_id,
                            user=request.user,
                        ).delete()
                        ConversationMessage.objects.filter(
                            user=request.user,
                            agent="schema",
                            thread_id=thread_id,
                        ).delete()
                        pg_checkpointer.delete_thread(thread_id)
                        logger.info(
                            "Deleted empty hybrid SchemaProject %s after failed first turn",
                            thread_id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to clean up empty hybrid SchemaProject %s", thread_id
                        )

        response = StreamingHttpResponse(stream_generator(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
