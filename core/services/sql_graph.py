"""LangGraph SQL agent that chats with user databases.

This module builds a ReAct agent graph that can:
1. Inspect database schemas (list tables, get column info)
2. Generate and execute SQL queries
3. Generate Chart.js visualizations from query results
4. Maintain conversation history via PostgreSQL checkpointer

Architecture:
    build_sql_agent(connection) -> Compiled LangGraph
        Nodes: "agent" (LLM with tools) <-> "tools" (executes tool calls)
        Checkpointer: PostgresSaver (persists messages per thread_id)

    run_sql_agent(connection, query, thread_id) -> AsyncGenerator
        Yields typed events for SSE streaming:
        - ("token", str)        : Text chunk from AI response
        - ("tool_start", dict)  : Tool call initiated {name, args}
        - ("tool_result", dict) : Tool finished {name, content}
        - ("result", dict)      : Structured result {type, content, id}
        - ("done", str)         : Final AI message text
"""

import json
import logging
import os
from typing import Annotated, Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.prebuilt import ToolNode
from psycopg_pool import ConnectionPool
from typing_extensions import TypedDict

from core.models import Connection
from core.services.connection import ConnectionService
from core.services.sql_toolkit import build_sql_tools
from core.services.sql_prompt import build_system_prompt

logger = logging.getLogger(__name__)

DB_URI = os.getenv("DATABASE_URL", "postgresql://postgres:1234@localhost:5432/agent")


# ── State ───────────────────────────────────────────────────────────

class SQLAgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


# ── Graph Builder ───────────────────────────────────────────────────

def build_sql_agent(connection: Connection, secure_data: bool = False):
    """Build a compiled LangGraph SQL agent for a given database connection."""

    # Create live DB connection and tools
    db = ConnectionService.get_sql_database(connection)
    tools = build_sql_tools(db, secure_data=secure_data)

    # LLM with tools bound
    llm = ChatGroq(
        model=os.getenv("SQL_AGENT_MODEL", "llama-3.3-70b-versatile"),
        api_key=os.getenv("GROQ_API_KEY"),
        temperature=0,
        max_retries=3,
    )
    llm_with_tools = llm.bind_tools(tools)

    # System prompt
    dialect = connection.dialect or connection.type
    system_prompt = build_system_prompt(dialect=dialect)

    # ── Nodes ───────────────────────────────────────────────────

    def call_model(state: SQLAgentState) -> dict:
        messages = state["messages"]

        # Prepend system prompt if not already there
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=system_prompt)] + messages

        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    def should_continue(state: SQLAgentState) -> str:
        """Route to tools if the LLM made tool calls, otherwise end."""
        last_message = state["messages"][-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"
        return END

    # ── Build Graph ─────────────────────────────────────────────

    tool_node = ToolNode(tools)

    graph = StateGraph(SQLAgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", tool_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    # Compile with PostgreSQL checkpointer for message persistence
    pool = ConnectionPool(conninfo=DB_URI, max_size=3)
    checkpointer = PostgresSaver(pool)

    return graph.compile(checkpointer=checkpointer)


# ── Query Helper ────────────────────────────────────��───────────────

async def run_sql_agent(
    connection: Connection,
    query: str,
    thread_id: str,
    secure_data: bool = False,
    history: list[BaseMessage] | None = None,
):
    """Run the SQL agent and yield (event_type, data) tuples for streaming.

    Event types:
        - "token": A text chunk from the AI response
        - "tool_start": Tool call initiated {"name": ..., "args": ...}
        - "tool_result": Tool finished {"name": ..., "content": ...}
        - "result": A structured result (SQL, chart, etc.)
        - "done": Final AI message text
    """
    app = build_sql_agent(connection, secure_data=secure_data)

    config = {"configurable": {"thread_id": thread_id}}

    # Build input messages
    input_messages = []
    if history:
        input_messages.extend(history)
    input_messages.append(HumanMessage(content=query))

    results = []  # Collect structured results (SQL, charts)

    async for event in app.astream_events(
        {"messages": input_messages},
        config=config,
        version="v2",
    ):
        kind = event["event"]

        if kind == "on_chat_model_stream":
            # Token-level streaming from the LLM
            chunk = event["data"]["chunk"]
            if hasattr(chunk, "content") and chunk.content:
                yield ("token", chunk.content)

        elif kind == "on_tool_start":
            tool_name = event.get("name", "")
            tool_input = event["data"].get("input", {})
            yield ("tool_start", {"name": tool_name, "args": tool_input})

        elif kind == "on_tool_end":
            tool_name = event.get("name", "")
            output = event["data"].get("output", "")
            content = str(output) if not isinstance(output, str) else output

            # Detect structured results from tool output
            if tool_name == "run_sql_query":
                # Extract SQL from the tool input
                tool_input = event["data"].get("input", {})
                sql = tool_input.get("query", "") if isinstance(tool_input, dict) else ""
                for_chart = tool_input.get("for_chart", False) if isinstance(tool_input, dict) else False
                chart_type = tool_input.get("chart_type") if isinstance(tool_input, dict) else None

                if sql:
                    yield ("result", {
                        "type": "SQL_QUERY_STRING",
                        "content": {"sql": sql, "for_chart": for_chart},
                        "id": uuid4().hex,
                    })

                # Try to parse the run result
                if not content.startswith("ERROR"):
                    yield ("result", {
                        "type": "SQL_QUERY_RUN_RESULT",
                        "content": {"raw": content, "for_chart": for_chart},
                        "id": uuid4().hex,
                    })

            elif tool_name == "generate_chart" and "CHART_JSON:" in content:
                chart_json = content.split("CHART_JSON:", 1)[1]
                yield ("result", {
                    "type": "CHART_GENERATION_RESULT",
                    "content": {"chartjs_json": chart_json},
                    "id": uuid4().hex,
                })

            yield ("tool_result", {"name": tool_name, "content": content})

    # Get the final AI message from the checkpointer state
    final_state = app.get_state(config)
    if final_state and final_state.values.get("messages"):
        last_msg = final_state.values["messages"][-1]
        if isinstance(last_msg, AIMessage) and last_msg.content:
            yield ("done", last_msg.content)
