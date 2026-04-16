# SQL Agent API Reference

All endpoints require JWT authentication (`Authorization: Bearer <token>`) unless noted otherwise.

Base URL: `/api/`

---

## Connection APIs

### POST `/api/connect/`

Create a new database connection from a DSN string. Validates by actually connecting to the database, introspects schema (discovers all tables), and saves.

**Request:**

```json
{
  "dsn": "postgresql://user:password@localhost:5432/mydb",
  "name": "My Production DB"
}
```

**Response (201):**

```json
{
  "data": {
    "id": "a7b3c2d1-e5f6-4a8b-9c0d-1e2f3a4b5c6d",
    "name": "My Production DB",
    "dsn": "postgresql://user:password@localhost:5432/mydb",
    "database": "mydb",
    "type": "postgresql",
    "dialect": "postgresql",
    "is_sample": false,
    "options": {
      "schemas": [
        {
          "name": "public",
          "enabled": true,
          "tables": [
            { "name": "customers", "enabled": true },
            { "name": "orders", "enabled": true }
          ]
        }
      ]
    },
    "created_at": "2026-04-14T10:30:00Z"
  }
}
```

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "Failed to connect to database. Please check your DSN."}` |
| 400 | `{"error": "Invalid DSN. Database name is missing — append '/DBNAME'."}` |
| 400 | `{"error": "A connection with this DSN already exists."}` |
| 400 | `{"error": "Database type not supported: ..."}` |

---

### POST `/api/connect/file/`

Create a connection from an uploaded file. SQLite files are copied directly. CSV, Excel, and SAS files are converted to SQLite first.

**Request (multipart/form-data):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| file | File | Yes | The database/data file |
| type | string | Yes | One of: `sqlite`, `csv`, `excel`, `sas7bdat` |
| name | string | Yes | Display name for the connection |

**Response (201):** Same structure as `POST /api/connect/`.

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "file, type, and name are all required."}` |
| 400 | `{"error": "File must be a valid SQLite database."}` |
| 400 | `{"error": "Unsupported file type: pdf. Use sqlite, csv, excel, or sas7bdat."}` |

---

### GET `/api/connections/`

List all connections for the authenticated user.

**Request:** No body.

**Response (200):**

```json
{
  "data": [
    {
      "id": "a7b3c2d1-...",
      "name": "My Production DB",
      "dsn": "postgresql://user:password@localhost:5432/mydb",
      "database": "mydb",
      "type": "postgresql",
      "dialect": "postgresql",
      "is_sample": false,
      "options": { "schemas": [] },
      "created_at": "2026-04-14T10:30:00Z"
    },
    {
      "id": "b8c4d3e2-...",
      "name": "Q4 Sales",
      "dsn": "sqlite:///path/to/file.sqlite",
      "database": "file.sqlite",
      "type": "csv",
      "dialect": "sqlite",
      "is_sample": false,
      "options": { "schemas": [] },
      "created_at": "2026-04-14T11:00:00Z"
    }
  ]
}
```

---

### GET `/api/connection/<uuid:connection_id>/`

Get a single connection by UUID.

**Request:** No body.

**Response (200):**

```json
{
  "data": {
    "id": "a7b3c2d1-...",
    "name": "My Production DB",
    "dsn": "postgresql://user:password@localhost:5432/mydb",
    "database": "mydb",
    "type": "postgresql",
    "dialect": "postgresql",
    "is_sample": false,
    "options": {
      "schemas": [
        {
          "name": "public",
          "enabled": true,
          "tables": [
            { "name": "customers", "enabled": true },
            { "name": "orders", "enabled": true }
          ]
        }
      ]
    },
    "created_at": "2026-04-14T10:30:00Z"
  }
}
```

**Errors:**

| Status | Body |
|--------|------|
| 404 | `{"error": "Connection not found."}` |

---

### PATCH `/api/connection/<uuid:connection_id>/`

Update a connection's name, DSN, or table options. All fields are optional.

If DSN changes: re-validates the connection, re-introspects schema, merges with existing table preferences (new tables default to disabled, removed tables dropped).

If only options change: saves directly (used when toggling tables on/off in the UI).

**Request (all fields optional):**

```json
{
  "name": "Renamed DB",
  "dsn": "postgresql://user:newpass@newhost:5432/mydb",
  "options": {
    "schemas": [
      {
        "name": "public",
        "enabled": true,
        "tables": [
          { "name": "customers", "enabled": true },
          { "name": "internal_logs", "enabled": false }
        ]
      }
    ]
  }
}
```

**Response (200):** Same structure as GET single connection (with updated values).

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "Another connection with this DSN already exists."}` |
| 400 | `{"error": "Failed to connect to database. Please check your DSN."}` |
| 404 | `{"error": "Connection not found."}` |

---

### DELETE `/api/connection/<uuid:connection_id>/`

Delete a connection. Chat sessions linked to this connection will have their `connection` set to `null` (SET_NULL).

**Request:** No body.

**Response (204):** No body.

**Errors:**

| Status | Body |
|--------|------|
| 404 | `{"error": "Connection not found."}` |

---

### POST `/api/connection/<uuid:connection_id>/refresh/`

Re-read the live database schema and merge with stored table preferences. Existing tables keep their enabled/disabled state. New tables default to disabled. Removed tables are dropped.

**Request:** No body.

**Response (200):** Same structure as GET single connection (with updated options).

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "Failed to connect to database. Please check your DSN."}` |
| 404 | `{"error": "Connection not found."}` |

---

## SQL Conversation APIs

### POST `/api/sql-conversation/`

Create a new chat session linked to a database connection. Auto-generates a thread_id.

**Request:**

```json
{
  "connection_id": "a7b3c2d1-e5f6-4a8b-9c0d-1e2f3a4b5c6d",
  "name": "New Chat"
}
```

**Response (201):**

```json
{
  "data": {
    "thread_id": "f8a3b2c1d4e5",
    "connection_id": "a7b3c2d1-e5f6-4a8b-9c0d-1e2f3a4b5c6d",
    "title": "New Chat",
    "created_at": "2026-04-14T10:30:00Z"
  }
}
```

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "connection_id is required"}` |
| 404 | `{"error": "Connection not found"}` |

---

### POST `/api/conversation/<thread_id>/query/`

The main endpoint. Sends the user's question to the LLM SQL agent, which generates SQL, executes it, and optionally creates charts. Streams results via Server-Sent Events (SSE) in real-time. Results are stored in the database as they arrive.

Auto-generates a conversation title on the first message.

**Request:**

```json
{
  "query": "Show me top 10 customers by revenue",
  "secure_data": true
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| query | string | required | The user's natural language question |
| secure_data | boolean | true | If true, hides actual row data from the LLM (privacy mode) |

**Response: SSE stream (`Content-Type: text/event-stream`).**

Events arrive in real-time as the agent works:

```
data: {"type": "token", "text": "Let me "}
data: {"type": "token", "text": "look at the "}
data: {"type": "token", "text": "available tables."}

data: {"type": "tool_start", "name": "list_tables", "args": {}}

data: {"type": "tool_result", "name": "list_tables", "content": "customers, orders, products"}

data: {"type": "tool_start", "name": "get_table_schema", "args": {"table_names": "customers"}}

data: {"type": "tool_result", "name": "get_table_schema", "content": "CREATE TABLE customers (id INT, name VARCHAR, revenue DECIMAL)..."}

data: {"type": "tool_start", "name": "run_sql_query", "args": {"query": "SELECT name, revenue FROM customers ORDER BY revenue DESC LIMIT 10", "for_chart": false}}

data: {"type": "result", "result_type": "SQL_QUERY_STRING", "result_id": "uuid-1", "content": {"sql": "SELECT name, revenue FROM customers ORDER BY revenue DESC LIMIT 10", "for_chart": false}}

data: {"type": "result", "result_type": "SQL_QUERY_RUN_RESULT", "result_id": "uuid-2", "content": {"raw": "Columns: ['name', 'revenue']\nRows (first 10 of 10):\n[['Alice', 50000], ['Bob', 42000]]", "for_chart": false}}

data: {"type": "tool_result", "name": "run_sql_query", "content": "Columns: ['name', 'revenue']..."}

data: {"type": "token", "text": "Here are your top 10 customers..."}

data: {"type": "done", "text": "Here are your top 10 customers by revenue. Alice leads with $50,000, followed by Bob at $42,000..."}

data: {"type": "title", "thread_id": "f8a3b2c1d4e5", "title": "Top Customers by Revenue"}
```

**SSE Event Types:**

| type | When | Data fields |
|------|------|-------------|
| `token` | AI text streaming (real-time) | `text` |
| `tool_start` | Agent calls a tool | `name`, `args` |
| `tool_result` | Tool execution finished | `name`, `content` (truncated to 500 chars) |
| `result` | Structured result stored in DB | `result_type`, `result_id`, `content` |
| `done` | Final AI answer | `text` |
| `title` | Auto-generated title (first message only) | `thread_id`, `title` |
| `error` | Something went wrong | `error` |

**Result types in `result` events:**

| result_type | content structure | Description |
|-------------|-------------------|-------------|
| `SQL_QUERY_STRING` | `{"sql": "SELECT ...", "for_chart": false}` | The generated SQL query |
| `SQL_QUERY_RUN_RESULT` | `{"raw": "Columns: [...] Rows: [...]", "for_chart": false}` | Query execution results |
| `CHART_GENERATION_RESULT` | `{"chartjs_json": "{...}"}` | Chart.js JSON config |

**Errors (within stream):**

```
data: {"type": "error", "error": "Failed to connect to database"}
```

**Errors (before stream starts):**

| Status | Body |
|--------|------|
| 400 | `{"error": "query is required"}` |
| 400 | `{"error": "This conversation has no database connection"}` |
| 404 | `{"error": "Conversation not found"}` |

---

### POST `/api/conversation/<thread_id>/run-sql/`

Manually execute a raw SQL query against the conversation's database. No LLM involved, result not stored. Used when the user edits and re-runs SQL in the UI.

**Request:**

```json
{
  "sql": "SELECT name, revenue FROM customers WHERE revenue > 1000 LIMIT 20",
  "linked_id": "uuid-1"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| sql | string | Yes | The SQL query to execute |
| linked_id | string | No | UUID of the SQL_QUERY_STRING result this execution relates to |

**Response (200):**

```json
{
  "data": {
    "columns": ["name", "revenue"],
    "rows": [
      ["Alice", 50000],
      ["Bob", 42000],
      ["Charlie", 38000]
    ],
    "linked_id": "uuid-1"
  }
}
```

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "sql is required"}` |
| 400 | `{"error": "This conversation has no database connection"}` |
| 400 | `{"error": "SQL execution failed: relation \"nonexistent\" does not exist"}` |
| 404 | `{"error": "Conversation not found"}` |

---

## Result APIs

### PATCH `/api/result/sql/<uuid:result_id>/`

Update a stored SQL query string. If `for_chart` is true and the SQL is linked to a chart, validates the new SQL is compatible and refreshes the chart.

**Request:**

```json
{
  "sql": "SELECT name, revenue FROM customers WHERE country = 'US' LIMIT 10",
  "for_chart": true
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| sql | string | required | The new SQL query string |
| for_chart | boolean | false | Whether this SQL feeds a chart |

**Response (200) without chart:**

```json
{
  "data": {
    "id": "uuid-1",
    "thread_id": "f8a3b2c1d4e5",
    "content": "{\"sql\": \"SELECT name, revenue FROM customers WHERE country = 'US' LIMIT 10\", \"for_chart\": true}",
    "type": "SQL_QUERY_STRING",
    "linked_id": null,
    "created_at": "2026-04-14T10:31:00Z"
  }
}
```

**Response (200) with chart refresh:**

```json
{
  "data": {
    "id": "uuid-1",
    "thread_id": "f8a3b2c1d4e5",
    "content": "{\"sql\": \"...\", \"for_chart\": true}",
    "type": "SQL_QUERY_STRING",
    "linked_id": null,
    "created_at": "2026-04-14T10:31:00Z"
  },
  "chart": {
    "chartjs_json": "{\"type\": \"bar\", \"data\": {\"labels\": [\"Alice\", \"Bob\"], \"datasets\": [{\"data\": [50000, 42000]}]}}",
    "result_id": "uuid-3"
  }
}
```

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "sql is required"}` |
| 404 | `{"error": "SQL result not found"}` |

---

### PATCH `/api/result/chart/<uuid:result_id>/refresh/`

Re-run the SQL query behind a chart and update the chart with fresh data. Follows the chain: chart -> linked SQL string -> execute -> update chart config.

**Request:** No body. Just the chart result UUID in the URL.

**Response (200):**

```json
{
  "data": {
    "chartjs_json": "{\"type\": \"bar\", \"data\": {\"labels\": [\"Alice\", \"Bob\", \"Charlie\"], \"datasets\": [{\"data\": [52000, 43000, 39000]}]}, \"options\": {\"plugins\": {\"title\": {\"display\": true, \"text\": \"Top Customers\"}}}}",
    "created_at": "2026-04-14T12:00:00Z"
  }
}
```

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "Chart has no linked SQL result"}` |
| 400 | `{"error": "No database connection found"}` |
| 400 | `{"error": "Chart refresh failed: connection refused"}` |
| 404 | `{"error": "Chart result not found"}` |
| 404 | `{"error": "Linked SQL result not found"}` |

---

### GET `/api/result/<uuid:result_id>/export-csv/`

Re-run the stored SQL query and stream the full results as a CSV file download. The UUID must belong to a `SQL_QUERY_STRING` result type.

**Request:** No body.

**Response (200):** Binary file download.

Headers:
```
Content-Type: text/csv
Content-Disposition: attachment; filename=export_a7b3c2d1.csv
```

Body:
```csv
name,revenue
Alice,50000
Bob,42000
Charlie,38000
```

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "The provided result_id does not belong to an SQL_QUERY_STRING_RESULT"}` |
| 400 | `{"error": "No database connection found"}` |
| 400 | `{"error": "CSV export failed: connection refused"}` |
| 404 | `{"error": "SQL result not found"}` |

---

## URL Summary

| Method | URL | View | Description |
|--------|-----|------|-------------|
| POST | `/api/connect/` | ConnectView | Connect via DSN |
| POST | `/api/connect/file/` | FileConnectView | Connect via file upload |
| GET | `/api/connections/` | ConnectionListView | List all connections |
| GET | `/api/connection/<id>/` | ConnectionDetailView | Get connection |
| PATCH | `/api/connection/<id>/` | ConnectionDetailView | Update connection |
| DELETE | `/api/connection/<id>/` | ConnectionDetailView | Delete connection |
| POST | `/api/connection/<id>/refresh/` | ConnectionRefreshView | Refresh schema |
| POST | `/api/sql-conversation/` | SQLConversationCreateView | Create SQL chat |
| POST | `/api/conversation/<thread_id>/query/` | SQLQueryView | Stream LLM query (SSE) |
| POST | `/api/conversation/<thread_id>/run-sql/` | RunSQLView | Execute raw SQL |
| PATCH | `/api/result/sql/<id>/` | SQLResultUpdateView | Update stored SQL |
| PATCH | `/api/result/chart/<id>/refresh/` | ChartRefreshView | Refresh chart data |
| GET | `/api/result/<id>/export-csv/` | ExportCSVView | Download CSV |

---

## Options JSON Structure

The `options` field on Connection stores which schemas and tables are visible to the SQL agent:

```json
{
  "schemas": [
    {
      "name": "public",
      "enabled": true,
      "tables": [
        { "name": "customers", "enabled": true },
        { "name": "orders", "enabled": true },
        { "name": "internal_logs", "enabled": false }
      ]
    },
    {
      "name": "analytics",
      "enabled": false,
      "tables": [
        { "name": "events", "enabled": false }
      ]
    }
  ]
}
```

- `enabled: true` on a schema means the agent can see tables in it
- `enabled: true` on a table means the agent can query it
- When refreshing schema, new tables default to `enabled: false`
- Removed tables are dropped from options

---

## Result Linking

Results chain together via `linked_id`:

```
SQL_QUERY_STRING (id: uuid-1)
    ^
    |  linked_id
    |
SQL_QUERY_RUN_RESULT (id: uuid-2, linked_id: uuid-1)
    ^
    |  linked_id
    |
CHART_GENERATION_RESULT (id: uuid-3, linked_id: uuid-2)
```

This allows the frontend to:
- Show which SQL produced which data table
- Show which data produced which chart
- Refresh charts by following the chain back to the SQL
