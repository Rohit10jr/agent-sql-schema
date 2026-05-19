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
    is_starred = models.BooleanField(default=False, db_index=True)
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

class TokenUsage(models.Model):
    """Append-only ledger of LLM token usage. One row per LLM round-trip.

    Written by the agent stream loop (sql_agent.SqlAgent.stream_generator) when
    each AIMessage update arrives carrying `usage_metadata`.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_usage",
    )
    thread_id = models.CharField(max_length=50, db_index=True, blank=True)
    model_name = models.CharField(max_length=128)
    provider = models.CharField(max_length=32, default="groq")

    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    reasoning_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} · {self.model_name} · {self.total_tokens}t"



# semantic search example
# from pgvector.django import VectorField
# from utils.utils import generate_embedding

# class JobPost(models.Model):
#     JOB_TYPES = [
#         ("Full-Time", "Full-Time"),
#         ("Part-Time", "Part-Time"),
#         ("Contract", "Contract"),
#         ("Internship", "Internship"),
#         ("Temporary", "Temporary"),
#         ("Volunteer", "Volunteer"),
#         ("Other", "Other")
#     ]

#     company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="job_post")
#     author = models.ForeignKey(EmployerProfile, on_delete = models.SET_NULL, null=True, blank=True, related_name="job_post_author")
#     title = models.CharField(max_length=255)
#     description = models.TextField()
#     requirements = models.TextField()
#     location = models.CharField(max_length=255)
#     salary_range = models.CharField(max_length=10, null=True, blank=True)
#     job_type = models.CharField(max_length=30, choices=JOB_TYPES)
#     posted_date = models.DateField(auto_now_add=True)
#     embedding = VectorField(dimensions=768, blank=True, null=True)
    
#     def save(self, *args, **kwargs):
#         """ Override save to generate embeddings before saving. """
#         content = f"{self.company} {self.title} {self.description} {self.requirements} {self.salary_range} {self.job_type}"
#         self.embedding = generate_embedding(content)
#         super(JobPost, self).save(*args, **kwargs)

#     def __str__(self):
#         return f"{self.title} at {self.company.name}"



# schema model
import json
import uuid

from django.db import models
from django.conf import settings
from .llm_models import title_model
from .prompt import AI_SQL_TITLE_PROMPT


DEFAULT_PROJECT_NAME = "New Project"
DEFAULT_DESCRIPTION = "Ai generated SQl and Schema"

class SchemaProject(models.Model):
    """
    Stores AI-generated SQL schema projects.
    JSON is treated as the source of truth.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="schema_projects",
    )
    name = models.CharField(
        max_length=255,
        default=DEFAULT_PROJECT_NAME,
        null=True,
        blank=True,
    )
    description = models.CharField(
        max_length=255,
        default=DEFAULT_DESCRIPTION,
        null=True,
        blank=True,
    )
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    complete_json = models.JSONField(null=True, blank=True)
    schema_json = models.JSONField(null=True, blank=True)
    sql_json = models.JSONField(null=True, blank=True)
    seed_json = models.JSONField(null=True, blank=True)
    variants = models.JSONField(default=dict, blank=True)
    is_starred = models.BooleanField(default=False, db_index=True)
    # True when the user has hand-edited sql_json / seed_json since the last
    # agent regeneration. UI uses this to show a "manually edited" badge on
    # the Tables / ER diagram views (which are derived from schema_json and
    # may have diverged from the edited SQL).
    sql_edited_manually = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save_variant(self, dialect, sql_table, sql_seed):
        """Helper to save a variant into the nested JSON structure"""
        if not self.variants:
            self.variants = {}
        
        self.variants[dialect] = {
            "sql_table": sql_table,
            "sql_seed_data": sql_seed
        }
        # Only update the 'variants' column for performance
        self.save(update_fields=['variants'])

    def save(self, *args, **kwargs):
        if not self.slug:
            # slug: userId-uuid
            self.slug = f"{self.user_id}-{uuid.uuid4().hex[:12]}"

        if self.schema_json and (not self.name or self.name == DEFAULT_PROJECT_NAME):
            try:
                print("---Inside title generation---")

                # Load the entire schema JSON (no picking tables, no restructuring)
                schema_data = json.loads(self.schema_json)

                clean_schema_for_ai = json.dumps(schema_data, indent=2)

                title_prompt = AI_SQL_TITLE_PROMPT.format(
                    schema=clean_schema_for_ai
                )

                result = title_model.invoke(title_prompt)

                self.name = result.name.strip()
                self.description = result.description.strip()

            except Exception as e:
                print("Title generation failed:", e)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.slug})"