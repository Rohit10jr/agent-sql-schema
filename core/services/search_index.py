"""Full-text search index maintenance for conversation messages.

LangGraph stores message content in checkpointer blobs the Django ORM cannot
query. After each agent turn we mirror the turn's text into the
ConversationMessage table so chat search can run Postgres full-text queries.

`reindex_thread` is the single entry point — it rebuilds every row for one
thread, so it is idempotent and safe to call after each turn or from the
`reindex_search` backfill command.
"""

import logging

from django.contrib.postgres.search import SearchVector
from django.db import transaction
from langchain_core.messages import AIMessage, HumanMessage

from core.models import ConversationMessage

logger = logging.getLogger(__name__)


def _text_of(message) -> str:
    """Extract plain searchable text from a LangChain message.

    Message content is usually a string but can be a list of content blocks
    (text / tool-use / reasoning) — we keep only the text.
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(p for p in parts if p).strip()
    return str(content or "").strip()


def extract_turns(raw_messages) -> list[tuple[str, str]]:
    """Return [(role, text)] for the human/assistant messages worth indexing.

    Tool messages and empty assistant chunks are skipped — only conversational
    text is searchable.
    """
    turns: list[tuple[str, str]] = []
    for message in raw_messages or []:
        if isinstance(message, HumanMessage):
            role = ConversationMessage.Role.USER
        elif isinstance(message, AIMessage):
            role = ConversationMessage.Role.ASSISTANT
        else:
            continue
        text = _text_of(message)
        if text:
            turns.append((role, text))
    return turns


def reindex_thread(user, agent: str, thread_id: str, raw_messages) -> int:
    """Rebuild the search rows for one thread. Idempotent.

    Deletes the thread's existing rows and recreates them from `raw_messages`,
    then populates the tsvector in a single UPDATE. Returns the row count.
    """
    turns = extract_turns(raw_messages)

    with transaction.atomic():
        ConversationMessage.objects.filter(
            user=user, agent=agent, thread_id=thread_id,
        ).delete()
        ConversationMessage.objects.bulk_create(
            ConversationMessage(
                user=user,
                agent=agent,
                thread_id=thread_id,
                role=role,
                text=text,
            )
            for role, text in turns
        )

    # Populate the tsvector after insert — the documented pattern for search
    # vectors that are refreshed occasionally rather than via a DB trigger.
    ConversationMessage.objects.filter(
        user=user, agent=agent, thread_id=thread_id,
    ).update(search_vector=SearchVector("text", config="english"))

    return len(turns)
