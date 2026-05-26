"""Unit tests for the error classifier.

No DB or HTTP — pure function tests. Verifies that known exception classes map
to stable, frontend-visible codes with the expected retry semantics.
"""

from unittest.mock import MagicMock

from core.errors import ErrorInfo, classify_error


def test_unknown_exception_is_internal() -> None:
    info = classify_error(RuntimeError("boom"))
    assert info.code == "INTERNAL"
    assert info.retryable is False
    assert info.http_status == 500


def test_pydantic_validation_error_is_schema() -> None:
    import pydantic

    class Model(pydantic.BaseModel):
        x: int

    try:
        Model(x="not-an-int")
    except pydantic.ValidationError as e:
        info = classify_error(e)
        assert info.code == "SCHEMA"
        assert info.retryable is True


def test_groq_rate_limit_extracts_retry_after_ms() -> None:
    """RateLimitError with retry-after-ms header → retry_after_seconds populated."""
    import groq

    fake_response = MagicMock()
    fake_response.headers = {"retry-after-ms": "12500"}
    err = groq.RateLimitError(message="rate limited", response=fake_response, body={})

    info = classify_error(err)
    assert info.code == "RATE_LIMIT"
    assert info.retryable is True
    assert info.retry_after_seconds == 12.5
    assert info.http_status == 429


def test_groq_bad_request_with_context_keyword_is_context_overflow() -> None:
    """BadRequestError whose message mentions 'context' → CONTEXT_OVERFLOW."""
    import groq

    fake_response = MagicMock()
    fake_response.headers = {}
    err = groq.BadRequestError(
        message="The conversation context is too long.",
        response=fake_response,
        body={},
    )

    info = classify_error(err)
    assert info.code == "CONTEXT_OVERFLOW"
    assert info.retryable is False


def test_error_info_to_sse_includes_run_id_and_node() -> None:
    """The SSE serializer attaches run_id + node to the payload."""
    info = ErrorInfo(
        code="RATE_LIMIT",
        message="busy",
        retryable=True,
        retry_after_seconds=5.0,
        http_status=429,
    )
    payload = info.to_sse(run_id="abc123", node="agent")

    assert payload == {
        "type": "error",
        "code": "RATE_LIMIT",
        "message": "busy",
        "retryable": True,
        "retry_after_seconds": 5.0,
        "run_id": "abc123",
        "node": "agent",
    }
