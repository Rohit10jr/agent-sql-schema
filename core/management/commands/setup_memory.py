"""Create the long-term memory store tables (PostgresStore + pgvector).

Run once after deploying the LTM feature:

    python manage.py setup_memory

Idempotent — everything is created with IF NOT EXISTS, so it is safe to re-run.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Set up the long-term memory store (PostgresStore) tables and vector index."

    def handle(self, *args, **options):
        from core.services.memory import setup_memory_store

        setup_memory_store()
        self.stdout.write(self.style.SUCCESS("Long-term memory store is ready."))
