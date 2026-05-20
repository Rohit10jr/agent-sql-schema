"""REST API for user-facing long-term memory management.

Lets a user see and curate what the agents have remembered about them — the
counterpart to the agents' automatic recall (read) and extraction (write).
All operations are scoped to request.user via the memory service.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.services import memory as ltm

logger = logging.getLogger(__name__)


class MemoryListCreateView(APIView):
    """GET  /api/memories/  — list the caller's long-term memories.
    POST /api/memories/  — add a user-authored memory.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"memories": ltm.list_memories(request.user.id)})

    def post(self, request):
        content = request.data.get("content", "")
        category = request.data.get("category", "general")
        try:
            memory = ltm.create_memory(request.user.id, content, category)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"memory": memory}, status=status.HTTP_201_CREATED)


class MemoryDetailView(APIView):
    """PATCH  /api/memories/<id>/ — edit a memory's content.
    DELETE /api/memories/<id>/ — remove a memory.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, memory_id):
        content = request.data.get("content", "")
        try:
            memory = ltm.update_memory(request.user.id, memory_id, content)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if memory is None:
            return Response(
                {"error": "Memory not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response({"memory": memory})

    def delete(self, request, memory_id):
        if not ltm.delete_memory(request.user.id, memory_id):
            return Response(
                {"error": "Memory not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
