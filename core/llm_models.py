import os
from typing import List
from dotenv import load_dotenv
from django.conf import settings

from pydantic import BaseModel, Field
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_groq import ChatGroq
from langchain_community.tools import DuckDuckGoSearchRun


api_key = settings.GROQ_API_KEY

# AiView
search = DuckDuckGoSearchRun()

aimodel = ChatGroq(
    model = "openai/gpt-oss-120b",
    temperature=0.1,
    max_tokens=1000,
    timeout=30,
    api_key=api_key
)

sqlmodel = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.1,
    max_tokens=5000,
    timeout=60,
    api_key=api_key
)

class TitleName(BaseModel):
    """Column definition for a table."""
    name: str = Field(description="The catchy name of the project")
    description: str = Field(description="A short summary of what the database does")

title_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.1,
    max_tokens=1000,
    timeout=60,
    api_key=api_key
)

title_model = title_llm.with_structured_output(TitleName)


# ---ai_views---

llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.1,
    max_tokens=4000,
    timeout=60,
    api_key=api_key,
    max_retries=3,
)

