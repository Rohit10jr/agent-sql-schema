import os

from langgraph.store.postgres import PostgresStore
from langgraph.checkpoint.postgres import PostgresSaver

from google import genai
from google.genai import types
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from langchain_google_genai import GoogleGenerativeAIEmbeddings

class Command(BaseCommand):
    help = "Create LangGraph Postgres checkpoint + store tables"

    def handle(self, *args, **options):
        DB_URI= settings.DB_URI
        gemini_api_key = settings.GEMINI_API_KEY

        embeddings = GoogleGenerativeAIEmbeddings(
            model="gemini-embedding-001",
            google_api_key=gemini_api_key
        )

        if not DB_URI:
            raise CommandError(
                "DB_URI environment variable is not set"
            )

        with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
            checkpointer.setup()
        # with PostgresStore.from_conn_string(DB_URI) as store:
        with PostgresStore.from_conn_string(
            DB_URI,
            index={
                "embed": embeddings,
                "dims": 1536,
            }
        ) as store:
            store.setup()  

        self.stdout.write(
            self.style.SUCCESS("LangGraph Postgres checkpoint + store tables created successfully")
        )
