"""Cancel endpoint for in-flight LangGraph runs.

POST /api/runs/<run_id>/cancel/

Authorization: caller must own the run (request.user.id == handle.user_id).

The action is cooperative — setting the cancel_event asks the streaming
view's loop to break at the next super-step boundary. We return as soon
as the signal is set; the SSE stream's `cancelled` event is the hard
confirmation that the run has actually stopped.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.services import run_registry

logger = logging.getLogger(__name__)


class RunCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, run_id: str):
        handle = run_registry.get(run_id)
        if handle is None:
            return Response(
                {"error": "Run not found or already finished", "run_id": run_id},
                status=status.HTTP_404_NOT_FOUND,
            )
        if handle.user_id != str(request.user.id):
            return Response(
                {"error": "Unauthorized"},
                status=status.HTTP_403_FORBIDDEN,
            )

        run_registry.cancel(run_id)
        logger.info(
            "Cancel requested for run %s (user=%s agent=%s thread=%s)",
            run_id,
            handle.user_id,
            handle.agent,
            handle.thread_id,
        )
        return Response(
            {"status": "cancelling", "run_id": run_id},
            status=status.HTTP_200_OK,
        )
