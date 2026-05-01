"""
Convert LangGraph's raw checkpoint message stream into a UI-friendly
chat-history shape.

Raw input — `chat_agent.get_state(config).values["messages"]` — is a list of
LangChain `BaseMessage` objects (HumanMessage, AIMessage, ToolMessage). These
are produced as tuples like `[("content", ...), ("type", "ai"), ("tool_calls", [...]), ...]`
when serialised, but in-memory they're real objects.

Output shape: nao-style "messages with parts":

    {
      "thread_id": "...",
      "messages": [
        {
          "id": "...",
          "role": "user" | "assistant",
          "parts": [
            { "type": "text",        "text": "..."},
            { "type": "reasoning",   "text": "..."},
            { "type": "tool-call",   "tool_call_id": "...", "tool_name": "...", "args": {...}},
            { "type": "tool-result", "tool_call_id": "...", "tool_name": "...", "content": "..."},
          ],
          "usage": { "input_tokens": ..., "output_tokens": ..., "total_tokens": ... } | None,
          "created_at": None,
        },
        ...
      ]
    }

Conventions:
- Each USER turn becomes one message with role="user".
- Each ASSISTANT turn = a *contiguous run* of AIMessage(s) + ToolMessage(s)
  between two user turns. All those AI/tool messages are flattened into one
  assistant message with N parts (multiple reasoning blocks, multiple tool calls
  + their paired results, and a final text).
- Tool messages are matched to the tool call they answer via `tool_call_id`.
"""

from typing import Any
from uuid import uuid4


def _msg_attr(message: Any, attr: str, default: Any = None) -> Any:
    """Tolerant getter — works with both LangChain message objects and dicts."""
    if isinstance(message, dict):
        return message.get(attr, default)
    return getattr(message, attr, default)


def _msg_type(message: Any) -> str:
    """Return 'human', 'ai', 'tool', 'system', or 'unknown'."""
    t = _msg_attr(message, "type")
    if t:
        return t
    # Fallback for serialised tuples: inspect class name
    cls = type(message).__name__.lower()
    if "human" in cls:
        return "human"
    if "ai" in cls:
        return "ai"
    if "tool" in cls:
        return "tool"
    if "system" in cls:
        return "system"
    return "unknown"


def _user_message(raw: Any) -> dict:
    return {
        "id": str(_msg_attr(raw, "id") or uuid4().hex),
        "role": "user",
        "parts": [{"type": "text", "text": str(_msg_attr(raw, "content", "") or "")}],
        "usage": None,
        "created_at": None,
    }


def _ai_parts(raw: Any) -> list[dict]:
    """Extract all parts contributed by a single AI message: reasoning,
    tool-calls, and text (any combination, in order)."""
    parts: list[dict] = []

    additional_kwargs = _msg_attr(raw, "additional_kwargs", {}) or {}

    # Reasoning block (if the model emitted one — Groq / OpenAI o-series do)
    reasoning = additional_kwargs.get("reasoning_content")
    if reasoning:
        parts.append({"type": "reasoning", "text": str(reasoning)})

    # Tool calls
    tool_calls = _msg_attr(raw, "tool_calls", None) or []
    for tc in tool_calls:
        # `tc` could be a dict or a LangChain ToolCall object.
        if isinstance(tc, dict):
            tc_name = tc.get("name", "unknown")
            tc_id = tc.get("id", "")
            tc_args = tc.get("args", {})
        else:
            tc_name = getattr(tc, "name", "unknown")
            tc_id = getattr(tc, "id", "")
            tc_args = getattr(tc, "args", {})
        parts.append({
            "type": "tool-call",
            "tool_call_id": str(tc_id),
            "tool_name": str(tc_name),
            "args": tc_args,
        })

    # Final text content (if non-empty)
    content = _msg_attr(raw, "content", "") or ""
    if content and isinstance(content, str) and content.strip():
        parts.append({"type": "text", "text": content})

    return parts


def _tool_result_part(raw: Any) -> dict:
    return {
        "type": "tool-result",
        "tool_call_id": str(_msg_attr(raw, "tool_call_id", "") or ""),
        "tool_name": str(_msg_attr(raw, "name", "") or ""),
        "content": str(_msg_attr(raw, "content", "") or ""),
    }


def _new_assistant_message(message_id: str | None = None) -> dict:
    return {
        "id": message_id or f"msg_asst_{uuid4().hex[:12]}",
        "role": "assistant",
        "parts": [],
        "usage": None,
        "created_at": None,
    }


def format_chat_history(raw_messages: list[Any], thread_id: str | None = None) -> dict:
    """
    Convert LangGraph's raw message list into a UI-friendly history.

    Returns: {"thread_id": <id>, "messages": [...]}
    """
    messages: list[dict] = []
    current_assistant: dict | None = None

    def close_assistant() -> None:
        """Push the in-progress assistant message into the result list."""
        nonlocal current_assistant
        if current_assistant and current_assistant["parts"]:
            messages.append(current_assistant)
        current_assistant = None

    for raw in raw_messages or []:
        role = _msg_type(raw)

        if role == "human":
            close_assistant()
            messages.append(_user_message(raw))

        elif role == "ai":
            # Open or continue the current assistant turn.
            if current_assistant is None:
                current_assistant = _new_assistant_message(
                    message_id=str(_msg_attr(raw, "id") or "") or None
                )
            current_assistant["parts"].extend(_ai_parts(raw))

            # Track usage on the LAST AI message of the turn (we'll keep
            # overwriting; whichever is last wins, which is the one with the
            # final text).
            usage = _msg_attr(raw, "usage_metadata", None)
            if usage:
                # Convert pydantic / dict-like → plain dict
                if hasattr(usage, "model_dump"):
                    usage = usage.model_dump()
                elif hasattr(usage, "_asdict"):
                    usage = usage._asdict()
                current_assistant["usage"] = dict(usage) if not isinstance(usage, dict) else usage

        elif role == "tool":
            # Tool result attaches to the in-progress assistant turn.
            if current_assistant is None:
                # Defensive: tool message before any AI message — start a new
                # assistant message anyway so we don't drop data.
                current_assistant = _new_assistant_message()
            current_assistant["parts"].append(_tool_result_part(raw))

        # 'system' and 'unknown' are skipped — they're not user-visible.

    close_assistant()

    return {
        "thread_id": thread_id,
        "messages": messages,
    }
