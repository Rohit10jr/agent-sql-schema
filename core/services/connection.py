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
        """Read all schemas and tables from a live database connection.
        Returns the options dict: {"schemas": [{"name": ..., "enabled": True, "tables": [...]}]}"""
        engine = db._engine
        inspector = inspect(engine)
        schema_names = inspector.get_schema_names()

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
        schema_names = inspector.get_schema_names()

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

    @staticmethod
    def create_csv_connection(user, file_obj, name: str) -> Connection:
        """Convert a CSV file to SQLite and create a connection."""
        file_path = ConnectionService._generate_sqlite_path()

        conn = sqlite3.connect(file_path)
        df = pd.read_csv(file_obj)
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

        conn = sqlite3.connect(file_path)
        sheets = pd.read_excel(file_obj, sheet_name=None, engine="openpyxl")
        for sheet_name, df in sheets.items():
            table_name = sheet_name.lower().replace(" ", "_")
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
        respecting the user's enabled/disabled schema and table preferences."""
        options = connection.options
        if options and options.get("schemas"):
            enabled_schemas = [s for s in options["schemas"] if s["enabled"]]
            schemas = [s["name"] for s in enabled_schemas]
            include_tables = [
                f"{s['name']}.{t['name']}"
                for s in enabled_schemas
                for t in s.get("tables", [])
                if t["enabled"]
            ]
        else:
            schemas = None
            include_tables = None

        engine = create_engine(connection.dsn)
        return SQLDatabase(
            engine,
            schema=schemas[0] if schemas and len(schemas) == 1 else None,
            include_tables=[t.split(".")[-1] for t in include_tables] if include_tables else None,
        )
