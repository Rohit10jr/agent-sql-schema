"""Single source of truth for mapping agent exceptions to user-facing errors.

`classify_error(e)` turns any exception raised inside a streaming agent into an
`ErrorInfo` with a stable `code`, a friendly `message`, and a `retryable` flag.
The streaming view emits this as the SSE `error` event so the frontend can
render the right UI (countdown for RATE_LIMIT, "new chat" CTA for
CONTEXT_OVERFLOW, retry button for transient failures, etc.).

Ordering matters in the if-chain: more specific subclasses come before their
parents (e.g. RateLimitError before APIStatusError).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: str                              # stable identifier — frontend dispatches on this
    message: str                           # user-facing one-liner
    retryable: bool                        # show a retry button?
    retry_after_seconds: Optional[float]   # countdown for RATE_LIMIT
    http_status: int                       # for non-stream paths returning DRF Response

    def to_sse(self, *, run_id: str | None = None, node: str | None = None) -> dict:
        return {
            "type": "error",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "run_id": run_id,
            "node": node,
        }


def classify_error(e: BaseException) -> ErrorInfo:
    """Map an exception to a stable ErrorInfo. Never raises."""
    # ── Provider-specific (Groq SDK) ──────────────────────────────────
    try:
        import groq
    except ImportError:
        groq = None  # type: ignore[assignment]

    if groq is not None:
        if isinstance(e, groq.RateLimitError):
            return ErrorInfo(
                code="RATE_LIMIT",
                message="The AI service is busy. Please retry shortly.",
                retryable=True,
                retry_after_seconds=_extract_retry_after(e),
                http_status=429,
            )
        if isinstance(e, groq.BadRequestError):
            text = str(e).lower()
            if "context" in text or "tokens" in text or "too long" in text:
                return ErrorInfo(
                    code="CONTEXT_OVERFLOW",
                    message="This conversation is too long. Start a new chat.",
                    retryable=False,
                    retry_after_seconds=None,
                    http_status=400,
                )
            return ErrorInfo(
                code="BAD_REQUEST",
                message="The AI service rejected the request.",
                retryable=False,
                retry_after_seconds=None,
                http_status=400,
            )
        if isinstance(e, groq.AuthenticationError):
            return ErrorInfo(
                code="AUTH",
                message="AI service authentication failed. Please contact support.",
                retryable=False,
                retry_after_seconds=None,
                http_status=500,
            )
        if isinstance(e, groq.APITimeoutError):
            return ErrorInfo(
                code="PROVIDER_TIMEOUT",
                message="The AI service took too long to respond. Please retry.",
                retryable=True,
                retry_after_seconds=None,
                http_status=504,
            )
        if isinstance(e, groq.APIConnectionError):
            return ErrorInfo(
                code="PROVIDER_NETWORK",
                message="Couldn't reach the AI service. Please retry.",
                retryable=True,
                retry_after_seconds=None,
                http_status=503,
            )
        if isinstance(e, groq.InternalServerError):
            return ErrorInfo(
                code="PROVIDER_DOWN",
                message="The AI service had a temporary issue. Please retry.",
                retryable=True,
                retry_after_seconds=None,
                http_status=502,
            )

    # ── LangGraph internal ────────────────────────────────────────────
    try:
        from langgraph.errors import GraphRecursionError, InvalidUpdateError
    except ImportError:
        GraphRecursionError = InvalidUpdateError = ()  # type: ignore[assignment,misc]

    if GraphRecursionError and isinstance(e, GraphRecursionError):
        return ErrorInfo(
            code="RECURSION_LIMIT",
            message="The agent took too many steps. Please rephrase your question.",
            retryable=False,
            retry_after_seconds=None,
            http_status=500,
        )
    if InvalidUpdateError and isinstance(e, InvalidUpdateError):
        return ErrorInfo(
            code="INTERNAL",
            message="Something went wrong. We've logged it.",
            retryable=False,
            retry_after_seconds=None,
            http_status=500,
        )

    # ── Pydantic (structured-output validation) ───────────────────────
    try:
        import pydantic
        if isinstance(e, pydantic.ValidationError):
            return ErrorInfo(
                code="SCHEMA",
                message="The AI returned an unexpected response. Please retry.",
                retryable=True,
                retry_after_seconds=None,
                http_status=500,
            )
    except ImportError:
        pass

    # ── Database ──────────────────────────────────────────────────────
    try:
        import psycopg
        if isinstance(e, psycopg.OperationalError):
            return ErrorInfo(
                code="DB_DOWN",
                message="Lost connection to the database. Please retry.",
                retryable=True,
                retry_after_seconds=None,
                http_status=503,
            )
    except ImportError:
        pass

    # ── Network (httpx-level, when SDK didn't wrap) ───────────────────
    try:
        import httpx
        if isinstance(e, (httpx.ConnectError, httpx.ReadTimeout)):
            return ErrorInfo(
                code="PROVIDER_NETWORK",
                message="Network issue talking to the AI service. Please retry.",
                retryable=True,
                retry_after_seconds=None,
                http_status=503,
            )
    except ImportError:
        pass

    # ── Fallback ──────────────────────────────────────────────────────
    return ErrorInfo(
        code="INTERNAL",
        message="Something went wrong. We've logged it.",
        retryable=False,
        retry_after_seconds=None,
        http_status=500,
    )


def _extract_retry_after(e: Any) -> Optional[float]:
    """Pull retry-after from a Groq RateLimitError response headers."""
    try:
        headers = e.response.headers  # type: ignore[union-attr]
        if "retry-after-ms" in headers:
            return float(headers["retry-after-ms"]) / 1000.0
        if "retry-after" in headers:
            return float(headers["retry-after"])
    except Exception:
        return None
    return None
