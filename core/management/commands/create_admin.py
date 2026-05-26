"""Create a superuser from environment variables on first deploy.

Reads ADMIN_EMAIL + ADMIN_PASSWORD (required) and ADMIN_FIRST_NAME +
ADMIN_LAST_NAME (optional). Silently no-ops if the env vars aren't set or
a user with that email already exists, so it's safe to keep in the build
command on every deploy.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Create or update the admin superuser from ADMIN_* env vars."

    def handle(self, *args, **options):
        email = os.getenv("ADMIN_EMAIL")
        password = os.getenv("ADMIN_PASSWORD")
        first_name = os.getenv("ADMIN_FIRST_NAME", "Admin")
        last_name = os.getenv("ADMIN_LAST_NAME", "User")

        if not email or not password:
            self.stdout.write("ADMIN_EMAIL or ADMIN_PASSWORD not set — skipping admin creation.")
            return

        if User.objects.filter(email=email).exists():
            self.stdout.write(f"Admin user {email} already exists — skipping.")
            return

        User.objects.create_superuser(
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )
        self.stdout.write(self.style.SUCCESS(f"Admin user {email} created."))
