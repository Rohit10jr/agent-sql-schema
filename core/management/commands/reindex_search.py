"""Backfill the conversation search index from existing checkpointer threads.

Run once after deploying chat search, or any time the index drifts:

    python manage.py reindex_search

It replays every ChatSession and SchemaProject thread through its agent's
LangGraph checkpointer and rebuilds the ConversationMessage rows.
"""

from django.core.management.base import BaseCommand

from core.models import ChatSession, SchemaProject
from core.services.search_index import reindex_thread


class Command(BaseCommand):
    help = "Rebuild the conversation full-text search index for all threads."

    def handle(self, *args, **options):
        # Imported here, not at module load — pulling in the agents triggers
        # heavy LangGraph / LLM imports that management commands shouldn't pay
        # for unless this command actually runs.
        from core.sql_agent import sql_agent
        from core.schema_agent import schema_agent

        sql_messages = self._reindex(
            "SQL chats",
            ChatSession.objects.select_related("user").iterator(),
            lambda chat: (chat.user, "sql", chat.thread_id),
            sql_agent,
        )
        schema_messages = self._reindex(
            "schema projects",
            SchemaProject.objects.select_related("user").iterator(),
            lambda project: (project.user, "schema", project.slug),
            schema_agent,
        )

        self.stdout.write(self.style.SUCCESS(
            f"Done. Indexed {sql_messages} SQL + {schema_messages} schema messages."
        ))

    def _reindex(self, label, rows, key_fn, agent) -> int:
        total = 0
        for row in rows:
            user, agent_name, thread_id = key_fn(row)
            try:
                state = agent.get_state({"configurable": {"thread_id": thread_id}})
                messages = state.values.get("messages", []) if state else []
                total += reindex_thread(user, agent_name, thread_id, messages)
            except Exception as exc:  # noqa: BLE001 — keep going on a bad thread
                self.stderr.write(f"  {label} {thread_id}: {exc}")
        self.stdout.write(f"{label}: indexed {total} messages.")
        return total
