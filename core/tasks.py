from celery import shared_task

from .models import SchemaProject


def persist_schema_project(
    generate_intent,
    project_id,
    schema_json,
    sql_table_json,
    sql_seed_json,
    complete_json=None,
):
    """Write the agent's structured outputs onto a SchemaProject row.

    Plain function so callers can run it synchronously (e.g. from inside the
    streaming view) without needing a Celery worker + result backend.
    """
    try:
        project = SchemaProject.objects.get(id=project_id)
    except SchemaProject.DoesNotExist:
        return

    if generate_intent and schema_json:
        project.schema_json = schema_json
        project.sql_json = sql_table_json
        project.seed_json = sql_seed_json

    project.save()


@shared_task
def save_schema_project(
    generate_intent,
    project_id,
    schema_json,
    sql_table_json,
    sql_seed_json,
    complete_json=None,
):
    """Celery wrapper around persist_schema_project (used only if a worker runs)."""
    persist_schema_project(
        generate_intent,
        project_id,
        schema_json,
        sql_table_json,
        sql_seed_json,
        complete_json,
    )
