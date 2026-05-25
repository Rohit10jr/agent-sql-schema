"""In-process registry of in-flight LangGraph runs.

Tracks one RunHandle per active run keyed by run_id (UUID hex). A secondary
index lets us detect concurrent POSTs on the same (user_id, agent, thread_id)
and reject them with HTTP 409.

Cancellation is cooperative: the streaming view checks `handle.cancel_event`
at each iteration of `graph.stream(...)`. Setting the event from the cancel
endpoint asks the view to break out of its loop at the next super-step
boundary (typically within one streamed token, sub-100ms).

Assumes a single Python process (e.g. `manage.py runserver` or
gunicorn --workers 1). For multi-worker deployments swap the in-process
dicts for a Redis-backed registry.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

Agent = Literal["schema", "sql"]


@dataclass
class RunHandle:
    run_id: str
    user_id: str
    agent: Agent
    thread_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)


_LOCK = threading.Lock()
_RUNS: dict[str, RunHandle] = {}
_BY_THREAD: dict[tuple[str, str, str], str] = {}


class ConcurrentRunError(Exception):
    """Another run is already in flight for the same (user, agent, thread)."""

    def __init__(self, existing: RunHandle):
        self.existing = existing
        super().__init__(
            f"Run {existing.run_id} already in flight for {existing.agent}/{existing.thread_id}"
        )


def register(run_id: str, user_id: str, agent: Agent, thread_id: str) -> RunHandle:
    """Create and store a RunHandle. Raises ConcurrentRunError if one already exists."""
    with _LOCK:
        key = (user_id, agent, thread_id)
        existing_id = _BY_THREAD.get(key)
        if existing_id and existing_id in _RUNS:
            raise ConcurrentRunError(_RUNS[existing_id])
        handle = RunHandle(run_id=run_id, user_id=user_id, agent=agent, thread_id=thread_id)
        _RUNS[run_id] = handle
        _BY_THREAD[key] = run_id
        return handle


def unregister(run_id: str) -> None:
    """Remove a RunHandle. Safe to call multiple times."""
    with _LOCK:
        handle = _RUNS.pop(run_id, None)
        if handle is not None:
            key = (handle.user_id, handle.agent, handle.thread_id)
            if _BY_THREAD.get(key) == run_id:
                _BY_THREAD.pop(key, None)


def cancel(run_id: str) -> RunHandle | None:
    """Signal cancellation. Returns the handle if found, else None."""
    with _LOCK:
        handle = _RUNS.get(run_id)
        if handle is None:
            return None
        handle.cancel_event.set()
        return handle


def find_by_thread(user_id: str, agent: Agent, thread_id: str) -> RunHandle | None:
    with _LOCK:
        run_id = _BY_THREAD.get((user_id, agent, thread_id))
        if not run_id:
            return None
        return _RUNS.get(run_id)


def get(run_id: str) -> RunHandle | None:
    with _LOCK:
        return _RUNS.get(run_id)


def repair_orphan_tool_calls(graph, config) -> bool:
    """If the thread's last AIMessage has unmatched tool_calls, write stub
    ToolMessages so the next turn doesn't fail with INVALID_CHAT_HISTORY.

    Cancellation between an `agent` super-step (which commits an AIMessage
    with tool_calls) and the matching `tools` super-step leaves orphans
    that poison the conversation. This repair makes the message log valid
    again. Returns True if a repair was performed.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    try:
        state = graph.get_state(config)
        messages = state.values.get("messages", []) if state else []
    except Exception:
        logger.exception("repair_orphan_tool_calls: could not fetch state")
        return False

    if not messages:
        return False

    last = messages[-1]
    if not isinstance(last, AIMessage):
        return False

    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        return False

    stubs = [
        ToolMessage(
            tool_call_id=tc["id"],
            name=tc.get("name", "unknown"),
            content="(cancelled by user)",
            status="error",
        )
        for tc in tool_calls
    ]
    try:
        graph.update_state(config, {"messages": stubs})
        logger.info(
            "Repaired %d orphan tool_call(s) on thread %s",
            len(stubs),
            config.get("configurable", {}).get("thread_id"),
        )
        return True
    except Exception:
        logger.exception("repair_orphan_tool_calls: update_state failed")
        return False
