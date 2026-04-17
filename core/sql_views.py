"""Views for SQL agent conversations — streaming queries, manual SQL execution, and CSV export."""

import csv
import json
import logging
from io import StringIO
from uuid import uuid4

from django.http import StreamingHttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from core.models import ChatSession, Connection, Result
from core.serializers import ResultOutSerializer
from core.services.connection import ConnectionService, ConnectionError
from core.services.sql_graph import run_sql_agent_sync
from core.services.sql_toolkit import execute_sql_query, fill_chart_with_data, ChartType
from core.utils import generate_and_save_title

logger = logging.getLogger(__name__)


class SQLQueryView(APIView):
    """POST /api/conversation/<thread_id>/query/ — Stream an LLM SQL query via SSE."""
    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id):
        query = request.data.get("query")
        secure_data = request.data.get("secure_data", True)

        if not query:
            return Response({"error": "query is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Get or create the chat session
        try:
            chat = ChatSession.objects.get(thread_id=thread_id, user=request.user)
        except ChatSession.DoesNotExist:
            return Response({"error": "Conversation not found"}, status=status.HTTP_404_NOT_FOUND)

        if not chat.connection:
            return Response({"error": "This conversation has no database connection"}, status=status.HTTP_400_BAD_REQUEST)

        connection = chat.connection

        def stream_generator():
            try:
                for event_type, data in run_sql_agent_sync(
                    connection=connection,
                    query=query,
                    thread_id=thread_id,
                    secure_data=secure_data,
                ):
                    if event_type == "token":
                        payload = json.dumps({"type": "token", "text": data})
                        yield f"data: {payload}\n\n"

                    elif event_type == "tool_start":
                        payload = json.dumps({"type": "tool_start", "name": data["name"], "args": _safe_serialize(data["args"])})
                        yield f"data: {payload}\n\n"

                    elif event_type == "tool_result":
                        payload = json.dumps({"type": "tool_result", "name": data["name"], "content": data["content"][:500]})
                        yield f"data: {payload}\n\n"

                    elif event_type == "result":
                        # Store the result in the database
                        result = Result.objects.create(
                            thread_id=thread_id,
                            content=json.dumps(data["content"]),
                            type=data["type"],
                        )

                        payload = json.dumps({
                            "type": "result",
                            "result_type": data["type"],
                            "result_id": str(result.id),
                            "content": data["content"],
                        })
                        yield f"data: {payload}\n\n"

                    elif event_type == "done":
                        payload = json.dumps({"type": "done", "text": data})
                        yield f"data: {payload}\n\n"

                # Auto-generate title on first message
                if chat.title in ("New Chat", None, ""):
                    title = generate_and_save_title(thread_id, query)
                    payload = json.dumps({"type": "title", "thread_id": thread_id, "title": title})
                    yield f"data: {payload}\n\n"

            except Exception as e:
                logger.exception("Error in SQL query stream")
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

        response = StreamingHttpResponse(stream_generator(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


class RunSQLView(APIView):
    """POST /api/conversation/<thread_id>/run-sql/ — Execute raw SQL against the conversation's database."""
    permission_classes = [IsAuthenticated]

    def post(self, request, thread_id):
        sql = request.data.get("sql")
        linked_id = request.data.get("linked_id")

        if not sql:
            return Response({"error": "sql is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            chat = ChatSession.objects.get(thread_id=thread_id, user=request.user)
        except ChatSession.DoesNotExist:
            return Response({"error": "Conversation not found"}, status=status.HTTP_404_NOT_FOUND)

        if not chat.connection:
            return Response({"error": "This conversation has no database connection"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            db = ConnectionService.get_sql_database(chat.connection)
            result = execute_sql_query(db, sql)
            return Response({
                "data": {
                    "columns": result["columns"],
                    "rows": result["rows"],
                    "linked_id": linked_id,
                }
            })
        except ConnectionError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": f"SQL execution failed: {e}"}, status=status.HTTP_400_BAD_REQUEST)


class SQLConversationCreateView(APIView):
    """POST /api/sql-conversation/ — Create a new SQL conversation linked to a database connection."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        connection_id = request.data.get("connection_id")
        name = request.data.get("name", "New Chat")

        if not connection_id:
            return Response({"error": "connection_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            connection = Connection.objects.get(id=connection_id, user=request.user)
        except Connection.DoesNotExist:
            return Response({"error": "Connection not found"}, status=status.HTTP_404_NOT_FOUND)

        thread_id = uuid4().hex[:12]
        chat = ChatSession.objects.create(
            user=request.user,
            thread_id=thread_id,
            connection=connection,
            title=name,
        )

        return Response({
            "data": {
                "thread_id": chat.thread_id,
                "connection_id": str(connection.id),
                "title": chat.title,
                "created_at": chat.created_at.isoformat(),
            }
        }, status=status.HTTP_201_CREATED)


class SQLResultUpdateView(APIView):
    """PATCH /api/result/sql/<id>/ — Update a stored SQL query string."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, result_id):
        sql = request.data.get("sql")
        for_chart = request.data.get("for_chart", False)

        if not sql:
            return Response({"error": "sql is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = Result.objects.get(id=result_id, type=Result.ResultType.SQL_QUERY_STRING)
        except Result.DoesNotExist:
            return Response({"error": "SQL result not found"}, status=status.HTTP_404_NOT_FOUND)

        # Update the stored SQL
        content = json.loads(result.content)
        content["sql"] = sql
        result.content = json.dumps(content)
        result.save()

        response_data = {"data": ResultOutSerializer(result).data}

        # If linked to a chart, refresh the chart too
        if for_chart:
            chart_data = self._refresh_linked_chart(result)
            if chart_data:
                response_data["chart"] = chart_data

        return Response(response_data)

    def _refresh_linked_chart(self, sql_result):
        """Find and refresh the chart linked to this SQL result."""
        # Find the run result linked to this SQL
        run_result = Result.objects.filter(
            type=Result.ResultType.SQL_QUERY_RUN_RESULT,
            linked_id=sql_result.id,
        ).first()

        if not run_result:
            return None

        # Find the chart linked to the run result
        chart_result = Result.objects.filter(
            type=Result.ResultType.CHART_GENERATION_RESULT,
            linked_id=run_result.id,
        ).first()

        if not chart_result:
            return None

        # Re-run the SQL and update the chart
        try:
            chat = ChatSession.objects.filter(thread_id=sql_result.thread_id).first()
            if not chat or not chat.connection:
                return None

            db = ConnectionService.get_sql_database(chat.connection)
            content = json.loads(sql_result.content)
            chart_content = json.loads(chart_result.content)

            query_data = execute_sql_query(db, content["sql"], for_chart=True, chart_type=ChartType[chart_content.get("chart_type", "bar")])
            updated_json = fill_chart_with_data(
                chart_content["chartjs_json"],
                query_data["columns"],
                query_data["rows"],
                chart_content.get("chart_type", "bar"),
            )

            chart_content["chartjs_json"] = updated_json
            chart_result.content = json.dumps(chart_content)
            chart_result.save()

            return {"chartjs_json": updated_json, "result_id": str(chart_result.id)}
        except Exception as e:
            logger.error(f"Chart refresh failed: {e}")
            return None


class ChartRefreshView(APIView):
    """PATCH /api/result/chart/<id>/refresh/ — Re-run SQL and refresh chart data."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, result_id):
        try:
            chart_result = Result.objects.get(id=result_id, type=Result.ResultType.CHART_GENERATION_RESULT)
        except Result.DoesNotExist:
            return Response({"error": "Chart result not found"}, status=status.HTTP_404_NOT_FOUND)

        chart_content = json.loads(chart_result.content)

        if not chart_result.linked_id:
            return Response({"error": "Chart has no linked SQL result"}, status=status.HTTP_400_BAD_REQUEST)

        # Follow the chain: chart → run result → SQL string
        try:
            sql_result = Result.objects.get(id=chart_result.linked_id, type=Result.ResultType.SQL_QUERY_STRING)
        except Result.DoesNotExist:
            return Response({"error": "Linked SQL result not found"}, status=status.HTTP_404_NOT_FOUND)

        sql_content = json.loads(sql_result.content)

        # Get the database connection
        chat = ChatSession.objects.filter(thread_id=chart_result.thread_id).first()
        if not chat or not chat.connection:
            return Response({"error": "No database connection found"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            db = ConnectionService.get_sql_database(chat.connection)
            chart_type = chart_content.get("chart_type", "bar")
            query_data = execute_sql_query(db, sql_content["sql"], for_chart=True, chart_type=ChartType[chart_type])
            updated_json = fill_chart_with_data(
                chart_content["chartjs_json"],
                query_data["columns"],
                query_data["rows"],
                chart_type,
            )

            chart_content["chartjs_json"] = updated_json
            chart_result.content = json.dumps(chart_content)
            chart_result.save()

            return Response({
                "data": {
                    "chartjs_json": updated_json,
                    "created_at": chart_result.created_at.isoformat(),
                }
            })
        except Exception as e:
            return Response({"error": f"Chart refresh failed: {e}"}, status=status.HTTP_400_BAD_REQUEST)


class ExportCSVView(APIView):
    """GET /api/result/<id>/export-csv/ — Re-run SQL and stream as CSV download."""
    permission_classes = [IsAuthenticated]

    def get(self, request, result_id):
        try:
            result = Result.objects.get(id=result_id, type=Result.ResultType.SQL_QUERY_STRING)
        except Result.DoesNotExist:
            return Response({"error": "SQL result not found"}, status=status.HTTP_404_NOT_FOUND)

        content = json.loads(result.content)
        sql = content["sql"]

        chat = ChatSession.objects.filter(thread_id=result.thread_id).first()
        if not chat or not chat.connection:
            return Response({"error": "No database connection found"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            db = ConnectionService.get_sql_database(chat.connection)
            query_data = execute_sql_query(db, sql)

            def csv_generator():
                buffer = StringIO()
                writer = csv.writer(buffer)

                # Header row
                writer.writerow(query_data["columns"])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

                # Data rows
                for row in query_data["rows"]:
                    writer.writerow(row)
                    if buffer.tell() > 1024 * 1024:  # Flush every ~1MB
                        yield buffer.getvalue()
                        buffer.seek(0)
                        buffer.truncate(0)

                yield buffer.getvalue()

            response = StreamingHttpResponse(csv_generator(), content_type="text/csv")
            response["Content-Disposition"] = f"attachment; filename=export_{str(result_id)[:8]}.csv"
            return response

        except Exception as e:
            return Response({"error": f"CSV export failed: {e}"}, status=status.HTTP_400_BAD_REQUEST)


def _safe_serialize(obj):
    """Safely serialize tool args for JSON output."""
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)
