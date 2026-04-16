import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser

from core.models import Connection
from core.serializers import (
    ConnectionCreateSerializer,
    ConnectionOutSerializer,
    ConnectionUpdateSerializer,
    FileConnectionCreateSerializer,
)
from core.services.connection import ConnectionError, ConnectionService

logger = logging.getLogger(__name__)


class ConnectView(APIView):
    """POST /api/connect/ — Create a new database connection from a DSN string."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ConnectionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            connection = ConnectionService.create_connection(
                user=request.user,
                dsn=serializer.validated_data["dsn"],
                name=serializer.validated_data["name"],
            )
        except ConnectionError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"data": ConnectionOutSerializer(connection).data},
            status=status.HTTP_201_CREATED,
        )


class FileConnectView(APIView):
    """POST /api/connect/file/ — Create a connection from an uploaded file (SQLite, CSV, Excel, SAS)."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file = request.FILES.get("file")
        file_type = request.data.get("type")
        name = request.data.get("name")

        if not file or not file_type or not name:
            return Response(
                {"error": "file, type, and name are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            if file_type == "sqlite":
                if not ConnectionService.is_valid_sqlite_file(file):
                    return Response(
                        {"error": "File must be a valid SQLite database."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                connection = ConnectionService.create_sqlite_connection(
                    user=request.user, file_bytes=file.read(), name=name,
                )

            elif file_type == "csv":
                connection = ConnectionService.create_csv_connection(
                    user=request.user, file_obj=file, name=name,
                )

            elif file_type == "excel":
                connection = ConnectionService.create_excel_connection(
                    user=request.user, file_obj=file, name=name,
                )

            elif file_type == "sas7bdat":
                connection = ConnectionService.create_sas_connection(
                    user=request.user, file_obj=file, name=name,
                )

            else:
                return Response(
                    {"error": f"Unsupported file type: {file_type}. Use sqlite, csv, excel, or sas7bdat."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except ConnectionError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"data": ConnectionOutSerializer(connection).data},
            status=status.HTTP_201_CREATED,
        )


class ConnectionListView(APIView):
    """GET /api/connections/ — List all connections for the authenticated user."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        connections = Connection.objects.filter(user=request.user)
        return Response(
            {"data": ConnectionOutSerializer(connections, many=True).data},
        )


class ConnectionDetailView(APIView):
    """GET/PATCH/DELETE /api/connection/<id>/ — Retrieve, update, or delete a single connection."""
    permission_classes = [IsAuthenticated]

    def _get_connection(self, request, connection_id):
        try:
            return Connection.objects.get(id=connection_id, user=request.user)
        except Connection.DoesNotExist:
            return None

    def get(self, request, connection_id):
        connection = self._get_connection(request, connection_id)
        if not connection:
            return Response({"error": "Connection not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response({"data": ConnectionOutSerializer(connection).data})

    def patch(self, request, connection_id):
        connection = self._get_connection(request, connection_id)
        if not connection:
            return Response({"error": "Connection not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ConnectionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            updated = ConnectionService.update_connection(connection, serializer.validated_data)
        except ConnectionError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"data": ConnectionOutSerializer(updated).data})

    def delete(self, request, connection_id):
        connection = self._get_connection(request, connection_id)
        if not connection:
            return Response({"error": "Connection not found."}, status=status.HTTP_404_NOT_FOUND)

        connection.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConnectionRefreshView(APIView):
    """POST /api/connection/<id>/refresh/ — Re-read the live database schema."""
    permission_classes = [IsAuthenticated]

    def post(self, request, connection_id):
        try:
            connection = Connection.objects.get(id=connection_id, user=request.user)
        except Connection.DoesNotExist:
            return Response({"error": "Connection not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            updated = ConnectionService.refresh_schema(connection)
        except ConnectionError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"data": ConnectionOutSerializer(updated).data})
