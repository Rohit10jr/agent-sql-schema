"""Database connection management service.

Handle creation, updating, deletion, and schema management of database
connections. Support multiple connection types including PostgreSQL, MySQL,
SQLite, CSV, Excel, and SAS7BDAT files.

Key methods:
    ConnectionService.validate_and_connect(dsn) - Test a DSN by making a real connection.
    ConnectionService.create_connection(user, dsn, name) - Validate, introspect, and save.
    ConnectionService.create_csv_connection(user, file_obj, name) - Convert CSV to SQLite.
    ConnectionService.update_connection(connection, data) - Update name/DSN/options.
    ConnectionService.refresh_schema(connection) - Re-read live DB, merge preferences.
    ConnectionService.get_sql_database(connection) - Create a live SQLDatabase for querying.
"""

import io
import logging
import sqlite3
import tempfile
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pyreadstat
from django.conf import settings
from langchain_community.utilities.sql_database import SQLDatabase
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError, NoSuchModuleError, ProgrammingError

from core.models import Connection

logger = logging.getLogger(__name__)

# Directory where uploaded/converted SQLite files are stored
DATA_DIR = Path(settings.BASE_DIR) / "data"
DATA_DIR.mkdir(exist_ok=True)

# DB system schemas we never want to expose to the SQL agent.
SYSTEM_SCHEMAS = {
    # Postgres
    "information_schema",
    "pg_catalog",
    "pg_toast",
    # MySQL / MariaDB
    "mysql",
    "sys",
    "performance_schema",
    # MSSQL
    "INFORMATION_SCHEMA",
    "sys",
}


def _is_user_schema(name: str) -> bool:
    """True for application schemas; False for DB-internal system schemas."""
    if not name:
        return False
    if name in SYSTEM_SCHEMAS:
        return False
    if name.startswith("pg_"):  # pg_temp_1, pg_toast_temp_1, etc.
        return False
    return True


class ConnectionError(Exception):
    """Raised when a database connection cannot be established."""
    pass


class ConnectionService:

    # ── DSN Validation ──────────────────────────────────────────────

    @staticmethod
    def validate_and_connect(dsn: str) -> SQLDatabase:
        """Try connecting to the database via DSN. Returns a live SQLDatabase instance.
        Falls back to host.docker.internal if localhost fails."""
        try:
            db = SQLDatabase.from_uri(dsn)
            database = db._engine.url.database
            if not database:
                raise ConnectionError("Invalid DSN. Database name is missing — append '/DBNAME'.")
            return db

        except OperationalError:
            if "localhost" in dsn:
                docker_dsn = dsn.replace("localhost", "host.docker.internal")
                try:
                    db = SQLDatabase.from_uri(docker_dsn)
                    if not db._engine.url.database:
                        raise ConnectionError("Invalid DSN. Database name is missing — append '/DBNAME'.")
                    return db
                except OperationalError:
                    raise ConnectionError("Failed to connect to database. Please check your DSN.")
            raise ConnectionError("Failed to connect to database. Please check your DSN.")

        except NoSuchModuleError as e:
            raise ConnectionError(f"Database type not supported: {e}")

        except ProgrammingError as e:
            if "Must specify the full search path" in str(e):
                raise ConnectionError(
                    "Invalid DSN. Specify the full search path starting from database "
                    "(e.g. 'SNOWFLAKE_SAMPLE_DATA/TPCH_SF1')."
                )
            raise ConnectionError(f"Database error: {e}")

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            raise ConnectionError("Failed to connect to database. Please check your DSN.")

    # ── Schema Introspection ────────────────────────────────────────

    @staticmethod
    def introspect_schema(db: SQLDatabase) -> dict:
        """Read user (non-system) schemas and tables from a live database connection.
        Returns the options dict: {"schemas": [{"name": ..., "enabled": True, "tables": [...]}]}"""
        engine = db._engine
        inspector = inspect(engine)
        schema_names = [n for n in inspector.get_schema_names() if _is_user_schema(n)]

        schemas = []
        for schema_name in schema_names:
            tables = inspector.get_table_names(schema=schema_name)
            views = inspector.get_view_names(schema=schema_name)
            all_tables = sorted(set(tables + views))

            schemas.append({
                "name": schema_name,
                "enabled": True,
                "tables": [{"name": t, "enabled": True} for t in all_tables],
            })

        schemas.sort(key=lambda s: s["name"])
        return {"schemas": schemas}

    @staticmethod
    def merge_options(old_options: dict | None, db: SQLDatabase) -> dict:
        """Merge old enabled/disabled preferences with the current live schema.
        - Existing tables keep their enabled state
        - New tables default to disabled
        - Removed tables are dropped"""
        engine = db._engine
        inspector = inspect(engine)
        schema_names = [n for n in inspector.get_schema_names() if _is_user_schema(n)]

        if not old_options or not old_options.get("schemas"):
            return ConnectionService.introspect_schema(db)

        # Build lookup maps from old options
        old_schema_enabled = {s["name"]: s["enabled"] for s in old_options["schemas"]}
        old_table_enabled = {}
        for schema in old_options["schemas"]:
            for table in schema.get("tables", []):
                old_table_enabled[(schema["name"], table["name"])] = table["enabled"]

        schemas = []
        for schema_name in schema_names:
            tables = inspector.get_table_names(schema=schema_name)
            views = inspector.get_view_names(schema=schema_name)
            all_tables = sorted(set(tables + views))

            schemas.append({
                "name": schema_name,
                "enabled": old_schema_enabled.get(schema_name, False),
                "tables": [
                    {
                        "name": t,
                        "enabled": old_table_enabled.get((schema_name, t), False),
                    }
                    for t in all_tables
                ],
            })

        schemas.sort(key=lambda s: s["name"])
        return {"schemas": schemas}

    # ── Connection CRUD ─────────────────────────────────────────────

    @staticmethod
    def create_connection(user, dsn: str, name: str, connection_type: str = None, is_sample: bool = False) -> Connection:
        """Validate DSN, introspect schema, and save a new Connection."""
        db = ConnectionService.validate_and_connect(dsn)

        # Use the potentially modified DSN (e.g. docker fallback)
        final_dsn = str(db._engine.url.render_as_string(hide_password=False))
        dialect = db.dialect
        database = db._engine.url.database

        if not connection_type:
            connection_type = dialect

        # Check for duplicate
        if Connection.objects.filter(user=user, dsn=final_dsn).exists():
            raise ConnectionError("A connection with this DSN already exists.")

        options = ConnectionService.introspect_schema(db)

        connection = Connection.objects.create(
            user=user,
            dsn=final_dsn,
            database=database,
            name=name,
            type=connection_type,
            dialect=dialect,
            is_sample=is_sample,
            options=options,
        )
        return connection

    # ── File-based Connections ──────────────────────────────────────

    @staticmethod
    def _generate_sqlite_path() -> Path:
        """Generate a unique file path for a new SQLite database."""
        filename = uuid4().hex[:12] + ".sqlite"
        return DATA_DIR / filename

    @staticmethod
    def create_sqlite_connection(user, file_bytes: bytes, name: str, is_sample: bool = False) -> Connection:
        """Create a connection from raw SQLite file bytes."""
        file_path = ConnectionService._generate_sqlite_path()
        file_path.write_bytes(file_bytes)

        dsn = f"sqlite:///{file_path.absolute()}"
        return ConnectionService.create_connection(user, dsn=dsn, name=name, connection_type="sqlite", is_sample=is_sample)

    # Encoding fallback chain for CSV. Order matters:
    #   utf-8       — modern default; most exports from databases / web tools.
    #   utf-8-sig   — UTF-8 with BOM; common from Excel "Save as CSV UTF-8".
    #   cp1252      — Windows Western Europe; legacy Excel exports (é = 0xe9).
    #   latin-1     — Last resort; never raises UnicodeDecodeError on any bytes,
    #                 so we're guaranteed to read something even for unknown
    #                 encodings (characters may render imperfectly but the data
    #                 lands in SQLite intact).
    CSV_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")

    @staticmethod
    def _read_csv_robust(file_obj) -> pd.DataFrame:
        """Read a CSV file trying common encodings in order."""
        # Buffer once — pd.read_csv consumes the stream and we may need to retry.
        raw = file_obj.read()
        last_error: Exception | None = None
        for encoding in ConnectionService.CSV_ENCODINGS:
            try:
                return pd.read_csv(io.BytesIO(raw), encoding=encoding)
            except UnicodeDecodeError as e:
                last_error = e
                continue
        # Unreachable in practice — latin-1 accepts any byte sequence.
        raise ConnectionError(
            f"Could not decode CSV in any supported encoding "
            f"({', '.join(ConnectionService.CSV_ENCODINGS)}): {last_error}"
        )

    @staticmethod
    def _read_excel_all_sheets(file_obj, filename: str) -> dict[str, pd.DataFrame]:
        """Read every sheet of an Excel file, picking the engine by extension."""
        ext = Path(filename or "").suffix.lower()
        # Buffer so we can retry with a different engine if needed.
        raw = file_obj.read()

        if ext in (".xlsx", ".xlsm"):
            engine = "openpyxl"
        elif ext == ".xls":
            engine = "xlrd"
        elif ext == ".xlsb":
            engine = "pyxlsb"
        else:
            # Unknown extension — let pandas guess.
            engine = None

        try:
            return pd.read_excel(io.BytesIO(raw), sheet_name=None, engine=engine)
        except ImportError as e:
            # Engine package isn't installed in this environment.
            installs = {"xlrd": "xlrd", "pyxlsb": "pyxlsb"}
            pkg = installs.get(engine or "")
            hint = f" Run: pip install {pkg}" if pkg else ""
            raise ConnectionError(
                f"This Excel format ({ext}) requires an extra package that "
                f"isn't installed.{hint}"
            ) from e
        except Exception as e:
            # openpyxl on .xls gives a confusing zip error; rewrite to be useful.
            raise ConnectionError(
                f"Could not read Excel file ({ext or 'unknown extension'}): {e}"
            ) from e

    @staticmethod
    def create_csv_connection(user, file_obj, name: str) -> Connection:
        """Convert a CSV file to SQLite and create a connection."""
        file_path = ConnectionService._generate_sqlite_path()

        df = ConnectionService._read_csv_robust(file_obj)

        conn = sqlite3.connect(file_path)
        table_name = name.lower().replace(" ", "_")
        df.to_sql(table_name, conn, if_exists="replace", index=False)
        conn.commit()
        conn.close()

        dsn = f"sqlite:///{file_path.absolute()}"
        return ConnectionService.create_connection(user, dsn=dsn, name=name, connection_type="csv")

    @staticmethod
    def create_excel_connection(user, file_obj, name: str) -> Connection:
        """Convert an Excel file to SQLite and create a connection.
        Each sheet becomes a separate table."""
        file_path = ConnectionService._generate_sqlite_path()

        # Django's UploadedFile exposes the original filename via `.name`.
        filename = getattr(file_obj, "name", "") or ""
        sheets = ConnectionService._read_excel_all_sheets(file_obj, filename)

        conn = sqlite3.connect(file_path)
        for sheet_name, df in sheets.items():
            table_name = str(sheet_name).lower().replace(" ", "_")
            df.to_sql(table_name, conn, if_exists="replace", index=False)
        conn.commit()
        conn.close()

        dsn = f"sqlite:///{file_path.absolute()}"
        return ConnectionService.create_connection(user, dsn=dsn, name=name, connection_type="excel")

    @staticmethod
    def create_sas_connection(user, file_obj, name: str) -> Connection:
        """Convert a SAS7BDAT file to SQLite and create a connection."""
        file_path = ConnectionService._generate_sqlite_path()

        # pyreadstat needs a file path, not a file object
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sas7bdat") as tmp:
            tmp.write(file_obj.read())
            tmp_path = tmp.name

        try:
            conn = sqlite3.connect(file_path)
            df, meta = pyreadstat.read_sas7bdat(tmp_path)

            # Use SAS labels as column names when available
            renames = {col: label or col for col, label in meta.column_names_to_labels.items()}
            df.rename(columns=renames, inplace=True)

            table_name = name.lower().replace(" ", "_")
            df.to_sql(table_name, conn, if_exists="replace", index=False)
            conn.commit()
            conn.close()

            dsn = f"sqlite:///{file_path.absolute()}"
            return ConnectionService.create_connection(user, dsn=dsn, name=name, connection_type="sas")
        finally:
            os.unlink(tmp_path)

    # ── Update & Refresh ────────────────────────────────────────────

    @staticmethod
    def update_connection(connection: Connection, data: dict) -> Connection:
        """Update a connection's name, DSN, or options."""
        if "dsn" in data and data["dsn"]:
            new_dsn = data["dsn"]

            # Check duplicate (allow same connection to keep its own DSN)
            existing = Connection.objects.filter(user=connection.user, dsn=new_dsn).exclude(id=connection.id).first()
            if existing:
                raise ConnectionError("Another connection with this DSN already exists.")

            db = ConnectionService.validate_and_connect(new_dsn)
            connection.dsn = str(db._engine.url.render_as_string(hide_password=False))
            connection.database = db._engine.url.database
            connection.dialect = db.dialect
            connection.options = ConnectionService.merge_options(connection.options, db)

        elif "options" in data and data["options"]:
            connection.options = data["options"]

        if "name" in data and data["name"]:
            connection.name = data["name"]

        connection.save()
        return connection

    @staticmethod
    def refresh_schema(connection: Connection) -> Connection:
        """Re-read the live database schema and merge with stored preferences."""
        db = ConnectionService.validate_and_connect(connection.dsn)
        connection.options = ConnectionService.merge_options(connection.options, db)
        connection.save()
        return connection

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def is_valid_sqlite_file(file_obj) -> bool:
        """Check the first 16 bytes for the SQLite magic header."""
        header = file_obj.read(16)
        file_obj.seek(0)
        return header == b"SQLite format 3\000"

    @staticmethod
    def get_sql_database(connection: Connection) -> SQLDatabase:
        """Create a live SQLDatabase instance from a stored connection,
        respecting the user's enabled/disabled schema and table preferences.

        Defensively skips DB system schemas so connections introspected before
        the system-schema filter was added still work.
        """
        options = connection.options
        enabled_schemas: list[dict] = []
        if options and options.get("schemas"):
            enabled_schemas = [
                s for s in options["schemas"]
                if s.get("enabled") and _is_user_schema(s.get("name", ""))
            ]

        if not enabled_schemas:
            # Nothing user-configured — let SQLDatabase do default discovery.
            engine = create_engine(connection.dsn)
            return SQLDatabase(engine)

        # Single schema → scope SQLDatabase to it; pass bare table names.
        if len(enabled_schemas) == 1:
            schema = enabled_schemas[0]
            include_tables = [
                t["name"]
                for t in schema.get("tables", [])
                if t.get("enabled")
            ]
            engine = create_engine(connection.dsn)
            return SQLDatabase(
                engine,
                schema=schema["name"],
                include_tables=include_tables or None,
            )

        # Multiple schemas → no scoping; SQLDatabase looks at the default schema.
        # Only include enabled tables from that schema (best-effort fallback).
        include_tables = [
            t["name"]
            for s in enabled_schemas
            for t in s.get("tables", [])
            if t.get("enabled")
        ]
        engine = create_engine(connection.dsn)
        return SQLDatabase(
            engine,
            include_tables=include_tables or None,
        )
