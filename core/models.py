import json
import uuid

from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone
from django.conf import settings

class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email must be provided")

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")

        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    date_joined = models.DateTimeField(default=timezone.now)
    
    email_verified = models.BooleanField(default=False)
    
    # user_type = models.CharField(max_length=20, choices=USER_TYPES, default="Job_Seeker")
    # last_login = models.DateTimeField(auto_now=True)
    # is_verified = models.BooleanField(default=False)

    objects = CustomUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    def __str__(self):
        return f"{self.id} - {self.email}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()
    

DEFAULT_PROJECT_NAME = "New Project"
DEFAULT_DESCRIPTION = "Ai generated SQl and Schema"


class Connection(models.Model):
    """Stores a database connection that the user wants to query via the SQL agent."""

    class ConnectionType(models.TextChoices):
        POSTGRES = "postgres", "PostgreSQL"
        MYSQL = "mysql", "MySQL"
        MSSQL = "mssql", "Microsoft SQL Server"
        SQLITE = "sqlite", "SQLite"
        CSV = "csv", "CSV"
        EXCEL = "excel", "Excel"
        SAS = "sas", "SAS"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="connections",
    )
    dsn = models.CharField(max_length=500)
    database = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=20, choices=ConnectionType.choices)
    dialect = models.CharField(max_length=50, blank=True, null=True)
    is_sample = models.BooleanField(default=False)
    options = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "dsn")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.type})"


class ChatSession(models.Model):
    thread_id = models.CharField(max_length=50, unique=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_projects",
    )
    connection = models.ForeignKey(
        Connection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )
    title = models.CharField(
        max_length=255,
        default=DEFAULT_PROJECT_NAME,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({self.thread_id})"


class Result(models.Model):
    """Stores structured LLM outputs (SQL queries, run results, charts) linked to a conversation."""

    class ResultType(models.TextChoices):
        SQL_QUERY_STRING = "SQL_QUERY_STRING", "SQL Query String"
        SQL_QUERY_RUN_RESULT = "SQL_QUERY_RUN_RESULT", "SQL Query Run Result"
        CHART_GENERATION_RESULT = "CHART_GENERATION_RESULT", "Chart Generation Result"
        SELECTED_TABLES = "SELECTED_TABLES", "Selected Tables"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread_id = models.CharField(max_length=50, db_index=True)
    content = models.TextField()
    type = models.CharField(max_length=30, choices=ResultType.choices)
    linked_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.type} ({self.id})"