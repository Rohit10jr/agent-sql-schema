import logging

import sqlglot
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from langchain_core.messages import AIMessage, HumanMessage
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveDestroyAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import SchemaProject
from .serializers import (
    SchemaProjectDetailSerializer,
    SchemaProjectListSerializer,
    SchemaProjectUpdateSerializer,
)

logger = logging.getLogger(__name__)


def _format_schema_history(raw_messages) -> list[dict]:
    """Walk the LangGraph message list and return one turn per (user → assistant) pair.

    Each turn:
        {
            "id": index,
            "role": "user" | "assistant",
            "text": str,
        }

    Schema agent always emits one HumanMessage at the start of a turn (from
    decision_node) followed by 1-N AIMessages from downstream nodes. We
    concatenate the AIMessage texts for that turn so the UI sees one bubble.
    """
    turns: list[dict] = []
    pending_assistant: list[str] = []

    def flush_assistant():
        if pending_assistant:
            turns.append({
                "id": len(turns),
                "role": "assistant",
                "text": "\n\n".join(t for t in pending_assistant if t),
            })
            pending_assistant.clear()

    for msg in raw_messages:
        if isinstance(msg, HumanMessage):
            flush_assistant()
            turns.append({
                "id": len(turns),
                "role": "user",
                "text": str(msg.content or ""),
            })
        elif isinstance(msg, AIMessage):
            content = str(msg.content or "").strip()
            if content:
                pending_assistant.append(content)
    flush_assistant()
    return turns


class SchemaProjectListView(ListAPIView):
    serializer_class = SchemaProjectListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SchemaProject.objects.filter(user=self.request.user).order_by("-updated_at")


class SchemaProjectDetailView(RetrieveUpdateDestroyAPIView):
    serializer_class = SchemaProjectDetailSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "slug"

    def get_queryset(self):
        return SchemaProject.objects.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.request.method in ["PUT", "PATCH"]:
            return SchemaProjectUpdateSerializer
        return SchemaProjectDetailSerializer

    def retrieve(self, request, *args, **kwargs):
        """Return the project plus replayed message history from the checkpointer."""
        instance = self.get_object()
        data = self.get_serializer(instance).data
        data["messages"] = self._fetch_history(instance.slug)
        return Response(data)

    def perform_destroy(self, instance):
        """Delete the project AND clear its LangGraph checkpoint so the thread is fully gone."""
        slug = instance.slug
        user = instance.user
        instance.delete()

        from .models import ConversationMessage
        ConversationMessage.objects.filter(
            user=user, agent="schema", thread_id=slug,
        ).delete()

        try:
            # Imported lazily to avoid circular import at module load.
            from .schema_agent import pg_checkpointer
            pg_checkpointer.delete_thread(slug)
        except Exception:
            logger.exception("Failed to clear schema-agent checkpoint for slug %s", slug)

    @staticmethod
    def _fetch_history(slug: str) -> list[dict]:
        try:
            from .schema_agent import schema_agent
            config = {"configurable": {"thread_id": slug}}
            state = schema_agent.get_state(config)
            if not state or "messages" not in state.values:
                return []
            return _format_schema_history(state.values.get("messages", []))
        except Exception:
            logger.exception("Failed to load schema-agent history for slug %s", slug)
            return []


# ===========================
# ===== Schema variants =====
# ===========================

class GetSQLVariantView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        project_id = request.data.get('project_id')
        target_dialect = request.data.get('sql_type') # e.g., 'mysql' or 'postgres'

        if not project_id or not target_dialect:
            return Response({"error": "Missing project_id or sql_type"}, status=400)

        # 1. Ownership & Project Retrieval
        project = get_object_or_404(SchemaProject, id=project_id, user=request.user)

        # 2. Check if this dialect already exists in variants
        if target_dialect in project.variants:
            return Response(project.variants[target_dialect], status=status.HTTP_200_OK)

        # 3. Generate using sqlglot if not found
        try:
            # We transpile both the table structure and the seed data
            # Use sqlglot.transpile(...)[0] or join if there are multiple
            
            raw_table_sql = project.sql_json # Your source SQL string
            raw_seed_sql = project.seed_json # Your source Seed string
            
            # Conversion
            new_table_sql = "\n\n".join(sqlglot.transpile(raw_table_sql, write=target_dialect, pretty=True))
            new_seed_sql = "\n\n".join(sqlglot.transpile(raw_seed_sql, write=target_dialect, pretty=True))

            # 4. Save to nested JSON
            project.save_variant(target_dialect, new_table_sql, new_seed_sql)

            return Response(project.variants[target_dialect], status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({"error": f"Transpilation failed: {str(e)}"}, status=500)
        
