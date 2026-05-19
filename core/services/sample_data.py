"""Sample database provisioning.

Every new user gets one Connection row per entry in settings.SAMPLE_DBS,
pointing at a read-only SQLite file shipped with the repo. The underlying
files are shared across all users — only the Connection rows are per-user
(so each user can rename / delete / restore independently).

The agent already enforces read-only access via FORBIDDEN_KEYWORDS, so
concurrent SQLite reads from many users against the same file are safe.
"""

import logging

from django.conf import settings
from django.db import transaction

from core.models import Connection

logger = logging.getLogger(__name__)


def provision_sample_connections(user) -> int:
    """Create per-user Connection rows for each sample DB. Idempotent.

    Returns the number of new Connection rows created (0 if user already
    had all samples, or if files are missing on disk).
    """
    created_count = 0
    with transaction.atomic():
        for sample in settings.SAMPLE_DBS:
            path = sample["path"]
            if not path.exists():
                # Don't fail — signup should still succeed if a sample file
                # is missing on this deploy. Just log and skip.
                logger.warning(
                    "Sample DB file missing, skipping provision for %s: %s",
                    sample["key"],
                    path,
                )
                continue

            dsn = f"sqlite:///{path}"
            _, created = Connection.objects.get_or_create(
                user=user,
                dsn=dsn,
                defaults={
                    "name": sample["name"],
                    "database": sample["database"],
                    "type": sample["type"],
                    "dialect": sample["dialect"],
                    "is_sample": True,
                },
            )
            if created:
                created_count += 1
    return created_count
