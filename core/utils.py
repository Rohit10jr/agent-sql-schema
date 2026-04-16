"""Shared utilities for the core app."""

import os
import logging

from langchain_groq import ChatGroq
from pydantic import BaseModel, Field

from core.models import ChatSession

logger = logging.getLogger(__name__)


# ── Title Generation ────────────────────────────────────────────────

class ChatTitleSchema(BaseModel):
    """Structured output schema for chat title generation."""
    title: str = Field(description="A short, descriptive title for the conversation")


_title_llm = None


def _get_title_model():
    """Lazy-load the title generation model (avoids import-time API calls)."""
    global _title_llm
    if _title_llm is None:
        llm = ChatGroq(
            model="llama-3.3-70b-versatile",
            temperature=0,
            max_tokens=50,
            api_key=os.getenv("GROQ_API_KEY"),
            max_retries=2,
        )
        _title_llm = llm.with_structured_output(ChatTitleSchema)
    return _title_llm


def generate_chat_title(messages_text: str) -> str:
    """Generate a short conversation title from message content.

    Args:
        messages_text: The conversation text to summarize (typically first 1-2 messages).

    Returns:
        A short title string.
    """
    try:
        title_model = _get_title_model()
        response = title_model.invoke(
            f"Generate a short title (under 8 words) for this conversation:\n{messages_text}"
        )
        return response.title
    except Exception as e:
        logger.error(f"Title generation failed: {e}")
        return "New Chat"


def generate_and_save_title(thread_id: str, messages_text: str) -> str:
    """Generate a title and update the ChatSession in the database.

    Args:
        thread_id: The thread_id of the ChatSession to update.
        messages_text: The conversation text to summarize.

    Returns:
        The generated title string.
    """
    title = generate_chat_title(messages_text)
    ChatSession.objects.filter(thread_id=thread_id).update(title=title)
    return title
