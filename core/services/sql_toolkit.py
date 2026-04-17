"""SQL tools for the LangGraph agent to interact with user databases.

This module provides the tools that the LLM agent can call during a conversation.
Tools are built as closures via build_sql_tools() so each tool captures the specific
database connection (SQLDatabase instance) it should operate on.

Tools:
    list_tables() - List all available table names in the database.
    get_table_schema(table_names) - Get CREATE TABLE statements and sample rows.
    run_sql_query(query, for_chart, chart_type) - Execute SQL and return results.
    generate_chart(chart_type, title) - Generate a Chart.js JSON config template.

Helpers:
    execute_sql_query(db, query) - Standalone SQL execution (used by views too).
    fill_chart_with_data(chart_json, columns, rows, chart_type) - Inject data into chart config.
    truncate_value(content, length) - Shorten long string values to save LLM tokens.
"""

import json
import logging
from enum import StrEnum
from typing import Any, Optional

from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.tools import tool
from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)


class ChartType(StrEnum):
    bar = "bar"
    doughnut = "doughnut"
    line = "line"
    scatter = "scatter"


def truncate_value(content: Any, length: int = 300) -> Any:
    """Truncate a string value to a max length."""
    if not isinstance(content, str) or length <= 0:
        return content
    if len(content) <= length:
        return content
    return content[:length - 3] + "..."


FORBIDDEN_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "REPLACE", "MERGE", "GRANT", "REVOKE"}


def validate_read_only(query: str) -> None:
    """Raise ValueError if the query is not a read-only SELECT statement."""
    first_keyword = query.strip().split()[0].upper() if query.strip() else ""
    if first_keyword in FORBIDDEN_KEYWORDS:
        raise ValueError(f"{first_keyword} statements are not allowed. Only SELECT queries are permitted.")


def execute_sql_query(db: SQLDatabase, query: str, for_chart: bool = False, chart_type: Optional[ChartType] = None) -> dict:
    """Execute SQL and return {"columns": [...], "rows": [...]}."""
    validate_read_only(query)

    with db._engine.connect() as conn:
        result = conn.execute(text(query))
        columns = list(result.keys())
        rows = [list(row) for row in result.fetchall()]

    # Truncate long string values
    truncated_rows = []
    for row in rows:
        truncated_rows.append([truncate_value(cell) for cell in row])

    if for_chart and chart_type in (ChartType.bar, ChartType.line, ChartType.doughnut, ChartType.scatter):
        if not truncated_rows:
            raise ValueError("No data returned from the query.")
        if len(truncated_rows[0]) != 2:
            raise ValueError(
                f"Chart requires exactly 2 columns (labels, values), but got {len(truncated_rows[0])}. "
                f"Columns: {columns}. Please rewrite the SQL to SELECT only 2 columns."
            )

    return {"columns": columns, "rows": truncated_rows}


def build_sql_tools(db: SQLDatabase, secure_data: bool = False):
    """Build the LangChain tools the SQL agent can use."""

    @tool
    def list_tables() -> str:
        """List all available tables in the database. Call this first to see what tables exist."""
        tables = db.get_usable_table_names()
        return ", ".join(tables)

    @tool
    def get_table_schema(table_names: str) -> str:
        """Get the schema and sample rows for the specified tables.
        Input is a comma-separated list of table names.
        Example: 'customers, orders, products'
        ALWAYS call list_tables first to verify the tables exist!"""
        names = [t.strip() for t in table_names.split(",")]
        available = db.get_usable_table_names()

        # Validate table names
        invalid = [n for n in names if n not in available]
        if invalid:
            return f"ERROR: Tables {invalid} not found. Available tables: {', '.join(available)}"

        return db.get_table_info(names)

    @tool
    def run_sql_query(query: str, for_chart: bool = False, chart_type: Optional[str] = None) -> str:
        """Execute a SQL query against the database and return results.
        If the query is for charting, set for_chart=True and specify chart_type (bar, line, doughnut, scatter).
        Chart queries MUST return exactly 2 columns: labels and values.
        If you get an error, rewrite the query and try again.
        NEVER run this without calling get_table_schema first!"""
        # Block DML/DDL statements — only SELECT is allowed
        try:
            validate_read_only(query)
        except ValueError as e:
            return f"ERROR: {e}"

        try:
            ct = ChartType[chart_type] if chart_type else None
            result = execute_sql_query(db, query, for_chart, ct)
            columns = result["columns"]
            rows = result["rows"]

            if secure_data:
                # Hide actual data from LLM
                if rows:
                    data_types = [type(cell).__name__ for cell in rows[0]]
                    return (
                        f"Query executed successfully.\n"
                        f"Columns: {columns}\n"
                        f"Column types: {data_types}\n"
                        f"Number of rows: {len(rows)}\n"
                        f"(Data hidden for security — user can see the results)"
                    )
                return "Query executed successfully. No rows returned."
            else:
                # Show truncated results to LLM
                preview_rows = rows[:10]
                return (
                    f"Columns: {columns}\n"
                    f"Rows (first {min(10, len(rows))} of {len(rows)}):\n{preview_rows}"
                )
        except ValueError as e:
            return f"ERROR: {e}"
        except Exception as e:
            return f"ERROR executing query: {e}"

    @tool
    def generate_chart(chart_type: str, title: str) -> str:
        """Generate a Chart.js JSON config for visualizing the last query results.
        Only call this AFTER running a SQL query with for_chart=True.
        chart_type must be one of: bar, line, doughnut, scatter.
        title is a short description for the chart title."""
        try:
            ct = ChartType[chart_type]
        except KeyError:
            return f"ERROR: Invalid chart type '{chart_type}'. Use: bar, line, doughnut, scatter."

        chart_config = _build_chart_config(ct, title)
        return f"CHART_JSON:{json.dumps(chart_config)}"

    return [list_tables, get_table_schema, run_sql_query, generate_chart]


# ── Chart Config Builder ────────────────────────────────────────────

def _build_chart_config(chart_type: ChartType, title: str) -> dict:
    """Build a Chart.js config template. Data will be filled in by the frontend or result service."""
    base = {
        "type": chart_type.value,
        "data": {
            "labels": [],
            "datasets": [{
                "data": [],
            }],
        },
        "options": {
            "plugins": {
                "legend": {"display": chart_type == ChartType.doughnut},
                "title": {"display": True, "text": title},
            },
        },
    }

    if chart_type == ChartType.bar:
        base["data"]["datasets"][0]["backgroundColor"] = [
            "rgba(255, 99, 132, 0.5)",
            "rgba(255, 159, 64, 0.5)",
            "rgba(255, 205, 86, 0.5)",
            "rgba(75, 192, 192, 0.5)",
            "rgba(54, 162, 235, 0.5)",
            "rgba(153, 102, 255, 0.5)",
            "rgba(201, 203, 207, 0.5)",
        ]
    elif chart_type == ChartType.line:
        base["data"]["datasets"][0]["fill"] = False
        base["data"]["datasets"][0]["tension"] = 0.1
    elif chart_type == ChartType.doughnut:
        base["data"]["datasets"][0]["backgroundColor"] = [
            "rgb(255, 99, 132)",
            "rgb(54, 162, 235)",
            "rgb(255, 205, 86)",
            "rgb(75, 192, 192)",
            "rgb(153, 102, 255)",
        ]
        base["data"]["datasets"][0]["hoverOffset"] = 4
    elif chart_type == ChartType.scatter:
        base["options"]["scales"] = {
            "x": {"type": "linear", "position": "bottom"},
            "y": {"type": "linear", "position": "left"},
        }

    return base


def fill_chart_with_data(chart_json: str, columns: list, rows: list, chart_type: str) -> str:
    """Insert query result data into a Chart.js JSON config."""
    config = json.loads(chart_json)
    ct = ChartType[chart_type]

    if ct in (ChartType.bar, ChartType.line, ChartType.doughnut):
        config["data"]["labels"] = [row[0] for row in rows]
        config["data"]["datasets"][0]["data"] = [row[1] for row in rows]
    elif ct == ChartType.scatter:
        config["data"]["datasets"][0]["data"] = [{"x": row[0], "y": row[1]} for row in rows]

    return json.dumps(config)
