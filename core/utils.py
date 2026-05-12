"""Shared utilities for the core app."""

import os
import logging

from langchain_groq import ChatGroq
from pydantic import BaseModel, Field

from core.models import ChatSession

logger = logging.getLogger(__name__)


# ── Title Generation ────────────────────────────────────────────────

class ChatTitleSchema(BaseModel):
    """Structured output schema for chat title generation."""
    title: str = Field(description="A short, descriptive title for the conversation")


_title_llm = None


def _get_title_model():
    """Lazy-load the title generation model (avoids import-time API calls)."""
    global _title_llm
    if _title_llm is None:
        llm = ChatGroq(
            model="llama-3.3-70b-versatile",
            temperature=0,
            max_tokens=50,
            api_key=os.getenv("GROQ_API_KEY"),
            max_retries=2,
        )
        _title_llm = llm.with_structured_output(ChatTitleSchema)
    return _title_llm


def generate_chat_title(messages_text: str) -> str:
    """Generate a short conversation title from message content.

    Args:
        messages_text: The conversation text to summarize (typically first 1-2 messages).

    Returns:
        A short title string.
    """
    try:
        title_model = _get_title_model()
        response = title_model.invoke(
            f"Generate a short title (under 8 words) for this conversation:\n{messages_text}"
        )
        return response.title
    except Exception as e:
        logger.error(f"Title generation failed: {e}")
        return "New Chat"


def generate_and_save_title(thread_id: str, messages_text: str) -> str:
    """Generate a title and update the ChatSession in the database.

    Args:
        thread_id: The thread_id of the ChatSession to update.
        messages_text: The conversation text to summarize.

    Returns:
        The generated title string.
    """
    title = generate_chat_title(messages_text)
    ChatSession.objects.filter(thread_id=thread_id).update(title=title)
    return title




# embedding semantic 

# import google.generativeai as genai 
# from django.db import models 
# from django.core.exceptions import ValidationError
# from dotenv import load_dotenv
# import environ 
# import os 

# # env = environ.Env()
# # environ.Env.read_env()

# # GOOGLE_API_KEY = env("GOOGLE_API_KEY")
# # genai.configure(api_key=GOOGLE_API_KEY)

# load_dotenv()

# GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# genai.configure(api_key=GOOGLE_API_KEY)

# def generate_embedding(text, model="models/text-embedding-004"):
#     if not text:
#         raise ValidationError("Cannot generate embedding for empty content.")
#     try:
#         result = genai.embed_content(model=model, content=text)
#         return result['embedding']
#     except Exception as e:
#         raise ValidationError(f"Failed to generate Embedding: {e}")



# # semantic search based on user prompts/query
# # @csrf_exempt
# # @api_view(["POST"])
# # def search_jobs(request):
# #     query = request.data.get("query", "")
# #     query_embedding = generate_embedding(query)

# #     similar_jobs = JobPost.objects.annotate(
# #         distance=CosineDistance("embedding", query_embedding)
# #     ).order_by("distance")[:3]  # Retrieve top 5 most similar
# #     serializer = JobPostSerializer(similar_jobs, many=True)
# #     return Response(serializer.data)

# from .utils import generate_otp, send_otp_via_email, generate_embedding
# from pgvector.django import L2Distance, CosineDistance
# from django.db import transaction
# from rest_framework.decorators import action

# class SearchJobs(APIView):
#     permission_classes = [IsAuthenticated]

#     def get(self, request):
#         return Response({"message": "Use POST to perform job search."}, status=405)

#     def get_serializer_class(self):
#         return JobPostSerializer

#     def post(self, request):
#         query = request.data.get("query", "").strip()

#         # Validate the query
#         if not query:
#             return Response({"error": "Query cannot be empty"}, status=400)

#         query_embedding = generate_embedding(query)
#         if query_embedding is None:
#             return Response({"error": "Failed to generate embedding"}, status=500)

#         # Perform semantic search
#         # similar_jobs = JobPost.objects.annotate(distance=CosineDistance(F("embedding"), query_embedding)).order_by("distance")[:3]
#         similar_jobs = JobPost.objects.annotate(distance=CosineDistance("embedding", query_embedding)).order_by("distance")[:3] 
#         serializer = self.get_serializer_class()(similar_jobs, many=True)
#         return Response(serializer.data)


# # Define weights
# WEIGHTS = {
#     "work_experience": 0.5,  
#     "education": 0.2,        
#     "skills": 0.3,           
#     "personal_info": 0.05    # Reduced impact for location-based match
# }


# class JobSearchByProfileView(APIView):
#     permission_classes = [IsAuthenticated]  # Require JWT authentication

#     def get(self, request):
#         return Response({"message": "Use POST to perform job search."}, status=405)

#     def get_user_embedding(self, user):
#         """
#         Computes the weighted average embedding for the user's profile.
#         """
#         # Fetch embeddings for work experience and education
#         work_exp_embeddings = list(WorkExperience.objects.filter(user=user).values_list("embedding", flat=True))
#         education_embeddings = list(Education.objects.filter(user=user).values_list("embedding", flat=True))

#         # Fetch embeddings for skills and personal information
#         skills_embedding = SkillSet.objects.filter(user=user).first()
#         personal_info_embedding = PersonalInformation.objects.filter(user=user).first()

#         # Convert lists to NumPy arrays and handle missing data
#         def avg_embedding(embeddings):
#             if not embeddings:
#                 return np.zeros(768)  # Assume a 768-dimensional zero vector if no data
#             return np.mean(np.stack(embeddings), axis=0)

#         work_embedding = avg_embedding(work_exp_embeddings) * WEIGHTS["work_experience"]
#         education_embedding = avg_embedding(education_embeddings) * WEIGHTS["education"]
#         skills_embedding = skills_embedding.embedding * WEIGHTS["skills"] if skills_embedding else np.zeros(768)
#         personal_info_embedding = personal_info_embedding.embedding * WEIGHTS["personal_info"] if personal_info_embedding else np.zeros(768)

#         # Compute final weighted profile embedding
#         return work_embedding + education_embedding + skills_embedding + personal_info_embedding

#     def post(self, request):
#         """
#         Perform semantic job search based on user's profile embedding.
#         """
#         user = request.user  # Get authenticated user

#         # Compute profile embedding
#         profile_embedding = self.get_user_embedding(user)

#         # Perform semantic search using Cosine Similarity
#         similar_jobs = JobPost.objects.annotate(
#             distance=CosineDistance("embedding", profile_embedding)
#         ).order_by("distance")[:3]  # Retrieve top 3 similar jobs

#         # Serialize and return the results
#         serializer = JobPostSerializer(similar_jobs, many=True)
#         return Response(serializer.data)