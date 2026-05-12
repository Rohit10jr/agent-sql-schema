from celery import shared_task
from .models import SchemaProject


@shared_task
def save_schema_project(generate_intent, project_id, schema_json, sql_table_json, sql_seed_json, complete_json=None):
    print("inside celery task")
    try:
        project = SchemaProject.objects.get(id=project_id)

        if generate_intent and schema_json:
            project.schema_json = schema_json

        # if generate_intent and sql_table_json:
            project.sql_json = sql_table_json
            project.seed_json = sql_seed_json

        # if complete_json:
        #     project.complete_json = complete_json

        project.save()

    except SchemaProject.DoesNotExist:
        pass
