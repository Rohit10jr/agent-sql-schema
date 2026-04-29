import os
import json
from uuid import uuid4
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny, IsAuthenticated
from typing import Any

from langchain.agents import create_agent
from langchain.messages import AIMessage, AIMessageChunk, AnyMessage, ToolMessage

from langchain_groq import ChatGroq
from langgraph.config import get_stream_writer  
# from langgraph.checkpoint.postgres import PostgresSaver
# from psycopg_pool import ConnectionPool
from django.conf import settings

from django.core.mail import send_mail
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str

from pydantic import BaseModel, Field
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_community.tools import DuckDuckGoSearchRun
import json
from django.http import StreamingHttpResponse
from .serializers import MessageSerializer, PasswordResetRequestSerializer, PasswordResetValidateSerializer, PasswordResetConfirmSerializer

from django.contrib.auth import get_user_model, update_session_auth_hash
User = get_user_model()

from typing import Any
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.messages import AIMessage, AIMessageChunk, AnyMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, Interrupt

from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from .serializers import SignupSerializer, UpdateUserProfileSerializer, EmailTokenObtainPairSerializer, PasswordChangeSerializer
from django.conf import settings
from langchain_groq import ChatGroq
from django.http import StreamingHttpResponse

from langchain.messages import AnyMessage
from langchain_core.messages.utils import count_tokens_approximately
from langmem.short_term import SummarizationNode, RunningSummary 
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore
from django.http import StreamingHttpResponse
from psycopg_pool import ConnectionPool
from dataclasses import dataclass
from langgraph.runtime import Runtime
from typing import Annotated, TypedDict, Union, Dict, Any
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from .models import ChatSession
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.decorators import api_view, throttle_classes
from django.core.mail import EmailMultiAlternatives
from django.utils.html import strip_tags
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from core.utils import generate_chat_title
# from rest_framework.throttling import UserRateThrottle


class EmailTokenObtainPairView(TokenObtainPairView):
    serializer_class = EmailTokenObtainPairSerializer
    permission_classes = [AllowAny]


class FivePerMinuteThrottle(SimpleRateThrottle):
    scope = 'signup'

    def get_cache_key(self, request, view):
        # throttle by IP address
        return self.get_ident(request)

class PasswordResetThrottle(SimpleRateThrottle):
    scope = "password_reset"

    def get_cache_key(self, request, view):
        return self.get_ident(request)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([FivePerMinuteThrottle])
def signup(request):
    serializer = SignupSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()

        # prepare verification token and url
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        print("UID:", uid)
        print("TOKEN:", token)
        verify_url = f"http://localhost:3000/verify-email?uid={uid}&token={token}"
        subject = "Verify your email"
        html_content = (
            f"<p>Hi {user.first_name},</p>"
            f"<p>Please click the link below to verify your email address:</p>"
            f"<p><a href='{verify_url}'>{verify_url}</a></p>"
            f"<p>If you didn't request this, please ignore this email.</p>"
        )
        text_content = strip_tags(html_content)
        email = EmailMultiAlternatives(
            subject, 
            text_content, 
            settings.DEFAULT_FROM_EMAIL, 
            [user.email]
        )
        email.attach_alternative(html_content, "text/html")
        try:
            email.send(fail_silently=False)
        except Exception:
            return Response(
                {
                    "message": "Account created successfully, but we could not send the verification email right now. Please use the resend verification option."
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(
            {"message": "Account created. Please check your email to verify."},
            status=status.HTTP_201_CREATED,
        )

    return Response(
        {
            "success": False,
            "message": "Signup failed",
            "errors": serializer.errors,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


@api_view(['POST'])
@throttle_classes([FivePerMinuteThrottle])
def logout(request):
    try:
        refresh_token = request.data.get("refresh")
        token = RefreshToken(refresh_token)
        token.blacklist()
        return Response({"message": "Logged out successfully"})
    except Exception:
        return Response(
            {"error": "Invalid refresh token"},
            status=status.HTTP_400_BAD_REQUEST
        )


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([FivePerMinuteThrottle])
def email_verify(request):
    """Verify an account using uid/token pair and auto-login (return JWT tokens)."""
    uid = request.data.get('uid')
    token = request.data.get('token')
    user = None

    if not uid or not token:
        return Response({"message": "UID and Token are required."}, status=status.HTTP_400_BAD_REQUEST)

    if uid and token:
        try:
            uid_decoded = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=uid_decoded)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"message": "Invalid user ID."}, status=status.HTTP_400_BAD_REQUEST)

    if user and default_token_generator.check_token(user, token):
        # Idempotent: if the user clicks the link twice, the second call still issues tokens.
        if not user.email_verified:
            user.is_active = True
            user.email_verified = True
            user.save()

        # Auto-login: issue JWT pair so the frontend can drop the user straight into the app.
        refresh = RefreshToken.for_user(user)
        return Response({
            "success": True,
            "message": "Email verified successfully.",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        })

    return Response({"message": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([FivePerMinuteThrottle])
def resend_verification(request):
    """Resend verification email if account exists and is unverified."""
    email = request.data.get('email', '').lower()
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        user = None

    if user and not getattr(user, 'email_verified', False):
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        print("UID:", uid)
        print("TOKEN:", token)
        verify_url = f"http://localhost:3000/verify-email?uid={uid}&token={token}"
        send_mail(
            "Verify your email",
            f"Please click the link to verify your email:\n{verify_url}",
            settings.EMAIL_HOST_USER,
            [email],
            fail_silently=False,
        )
    return Response({
        "message": "If the account exists and is not verified, a verification email has been sent."
    })


@api_view(['GET'])
def current_user(request):
    user = request.user
    return Response({
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        # "username": user.username,
    })


@api_view(['PUT'])
def update_user_profile(request):
    """
    Update the currently authenticated user's first name and last name.
    Required fields: first_name, last_name
    """    
    serializer = UpdateUserProfileSerializer(request.user, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        return Response({
            'user': {
                'id': request.user.id,
                'email': request.user.email,
                'first_name': request.user.first_name,
                'last_name': request.user.last_name,
                # 'username': request.user.username
            },
            'message': 'Profile updated successfully'
        }, status=status.HTTP_200_OK)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ===== PASSWORD RESET ENDPOINTS =====

@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset(request):
    """
    Forgot password endpoint.
    Sends password reset link to user's email.
    Always returns generic response to prevent user enumeration.
    """
    serializer = PasswordResetRequestSerializer(data=request.data)
    if serializer.is_valid():
        email = serializer.validated_data['email']
        try:
            user = User.objects.get(email=email)
            # user = User.objects.get(email__iexact=email, is_active=True)
            # Generate reset token
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            print("UID:", uid)
            print("TOKEN:", token)
            # Create reset URL
            reset_url = f"http://localhost:3000/reset-password?uid={uid}&token={token}"
            
            # Send email
            subject = "Reset your password"
            html_content = (
                f"<p>Hi {user.first_name},</p>"
                f"<p>We received a request to reset your password. Click the link below:</p>"
                f"<p><a href='{reset_url}'>{reset_url}</a></p>"
                f"<p>This link expires in 1 hour.</p>"
                f"<p>If you didn't request this, please ignore this email.</p>"
            )
            text_content = strip_tags(html_content)
            email_obj = EmailMultiAlternatives(
                subject,
                text_content,
                settings.DEFAULT_FROM_EMAIL,
                [user.email]
            )
            email_obj.attach_alternative(html_content, "text/html")
            email_obj.send(fail_silently=False)
        except User.DoesNotExist:
            # Generic response - don't reveal if email exists
            pass
    
    return Response(
        {"message": "If the account exists, a password reset link has been sent."},
        status=status.HTTP_200_OK
    )


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_validate(request):
    """
    Validate password reset token before showing reset form.
    Frontend calls this to check if token is valid.
    """
    serializer = PasswordResetValidateSerializer(data=request.data)
    if serializer.is_valid():
        uid = serializer.validated_data['uid']
        token = serializer.validated_data['token']
        
        try:
            uid_decoded = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=uid_decoded)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response(
                {"valid": False, "message": "Invalid or expired token."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check token validity
        if default_token_generator.check_token(user, token):
            return Response(
                {"valid": True},
                status=status.HTTP_200_OK
            )
        else:
            return Response(
                {"valid": False, "message": "Invalid or expired token."},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_confirm(request):
    """
    Reset password with new password.
    Requires valid uid and token. Re-validates token for security.
    """
    serializer = PasswordResetConfirmSerializer(data=request.data)
    if serializer.is_valid():
        uid = serializer.validated_data['uid']
        token = serializer.validated_data['token']
        password = serializer.validated_data['password1']
        
        try:
            uid_decoded = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=uid_decoded)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response(
                {"message": "Invalid or expired token."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Re-validate token (security measure)
        if not default_token_generator.check_token(user, token):
            return Response(
                {"message": "Invalid or expired token."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Set new password
        user.set_password(password)
        # logger.info(f"Password reset for user {user.email}")
        # logger.info(f"Password reset for user {user.email}")
        user.save()

        # Blacklist all existing refresh tokens
        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)
        
        return Response(
            {"message": "Password reset successful."},
            status=status.HTTP_200_OK
        )
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])  # Requires authentication
@throttle_classes([PasswordResetThrottle])
def password_change(request):
    """
    Authenticated user password change endpoint.
    User must provide old password and new password.
    """
    user = request.user
    
    serializer = PasswordChangeSerializer(data=request.data)
    if serializer.is_valid():
        old_password = serializer.validated_data['old_password']
        new_password = serializer.validated_data['new_password1']
        
        # Check old password matches
        if not user.check_password(old_password):
            return Response(
                {"old_password": ["Old password is incorrect."]},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Set new password
        user.set_password(new_password)
        user.save()
        
        # Keep user logged in after password change
        update_session_auth_hash(request, user)
        
        return Response(
            {"message": "Password changed successfully."},
            status=status.HTTP_200_OK
        )
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# def password_change(request):
#     user = request.user
    
#     serializer = PasswordChangeSerializer(data=request.data)
#     if serializer.is_valid():
#         old_password = serializer.validated_data['old_password']
#         new_password = serializer.validated_data['new_password1']
        
#         if not user.check_password(old_password):
#             return Response(
#                 {"old_password": ["Old password is incorrect."]},
#                 status=status.HTTP_400_BAD_REQUEST
#             )
        
#         user.set_password(new_password)
#         user.save()

#         # Invalidate all existing refresh tokens
#         from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
#         for token in OutstandingToken.objects.filter(user=user):
#             BlacklistedToken.objects.get_or_create(token=token)

#         return Response(
#             {"message": "Password changed successfully. Please log in again."},
#             status=status.HTTP_200_OK
#         )
    
#     return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# =================
# --- CHAT VIEW ---
# =================

DB_URI = settings.DB_URI
search = DuckDuckGoSearchRun()
api_key = settings.GROQ_API_KEY
gemini_api_key = settings.GEMINI_API_KEY

llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.1,
    max_tokens=4000,
    timeout=60,
    api_key=api_key,
    max_retries=3,
    # streaming=False
)

gemini_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.1,
    max_tokens=4000,
    timeout=60,
    api_key=gemini_api_key,
    max_retries=3,
    streaming=False
)


class MemoryExtraction(BaseModel):
    """Extracted facts to remember about the user."""
    facts: list[str] = Field(description="New facts about the user's preferences, identity, or history.")

memory_extractor = llm.with_structured_output(MemoryExtraction)

@dataclass
class UserContext:
    user_id: str

class SummaryState(MessagesState):
    context: dict[str, RunningSummary]

class LLMInputState(TypedDict):  
    summarized_messages: list[AnyMessage]
    context: dict[str, RunningSummary]

summarization_model = llm.bind(max_tokens=256)

summarization_node = SummarizationNode(  
    token_counter=count_tokens_approximately,
    model=summarization_model,
    max_tokens=1000,
    max_tokens_before_summary=2000,
    max_summary_tokens=256,
)

def manage_memories(state: SummaryState, runtime: Runtime[UserContext]):
    user_id = runtime.context.user_id
    namespace = (user_id, "memories")
    
    # Get the last turn of the conversation
    last_user_msg = state["messages"][-2].content
    last_ai_msg = state["messages"][-1].content
    
    # Analyze if there's anything worth remembering

    prompt = f"""Analyze the following exchange. Extract any permanent user facts (hobbies, likes, dislikes, profession).
    If no new permanent facts are found, return an empty list.
    
    User: {last_user_msg}
    AI: {last_ai_msg}"""
    
    extracted = memory_extractor.invoke(prompt)
    
    if extracted.facts:
        for fact in extracted.facts:
            # We use a UUID or hash as the key to avoid overwriting "profile_test"
            memory_id = str(uuid4())
            runtime.store.put(
                namespace,
                memory_id,
                {"data": fact}
            )
            
    return state # Passing state through


# def call_model(state: MessagesState, runtime: Runtime[UserContext]):
def call_model(state: LLMInputState, runtime: Runtime[UserContext]):
    user_id = runtime.context.user_id
    namespace = (user_id, "memories")
    user_input = state["summarized_messages"][-1].content
    
    # print("=== Summarized Messages ===")
    # print(state["summarized_messages"])
    # context = state.get("context")
    # if context:
    #     print("=== Context ===")
    #     for key, value in context.items():
    #         print(f"Key: {key}")
    #         print(f"Value: {value}")
    #         print(f"Summary Text: {value.summary}")
    #         print(f"Last Summarized ID: {value.last_summarized_message_id}")

    # SEMANTIC SEARCH: 
    memories = runtime.store.search(
        namespace, 
        query=user_input, 
        limit=3
    )
    
    memory_list = []
    for m in memories:
        data_value = m.value.get("data", "")
        memory_list.append(str(data_value))
    
    memory_context = "\n".join(memory_list) if memory_list else ""
    
    if memory_context:
        system_message = {
            "role": "system",
            "content": f"Relevant memories about user:\n{memory_context}"
        }
        updated_messages = [system_message] + state["summarized_messages"]
    else:
        updated_messages = state["summarized_messages"]

    response = llm.invoke(updated_messages)
    
    # runtime.store.put(
    #     namespace, 
    #     "profile_test",
    #     {"data": "user likes cricket"}
    # )
    return {"messages": [response]}

pool = ConnectionPool(DB_URI)
pg_checkpointer = PostgresSaver(pool)
# pg_store = PostgresStore(pool)

embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-001",
    google_api_key=gemini_api_key
    )

pg_store = PostgresStore(
    pool,
    index={
        "embed": embeddings,
        "dims": 1536, 
        # "fields": ["data"] 
    }
)
messagegraph = (
    # StateGraph(MessagesState, context_schema=UserContext)
    StateGraph(SummaryState, context_schema=UserContext)
    .add_node("summarize", summarization_node)
    .add_node("call_model", call_model)
    .add_node("manage_memories", manage_memories)

    .add_edge(START, "summarize")
    .add_edge("summarize", "call_model")
    .add_edge("call_model", "manage_memories")
    .add_edge("manage_memories", END)
)

chat_agent = messagegraph.compile(checkpointer=pg_checkpointer, store=pg_store)


class AiChatView(APIView):
    # authentication_classes = [] 
    # permission_classes = [AllowAny]
    
    def post(self, request):
        user_query = request.data.get('prompt')
        thread_id = request.data.get('thread_id')
        user_id = str(request.user.id)

        if not user_query:
            return Response({"error": "Prompt is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        new_thread = False
        if not thread_id:
            thread_id = uuid4().hex[:12]
            new_thread = True
        
        config = {"configurable": {"thread_id": thread_id}}
        def stream_generator():
            try:
                # When stream_mode is a list, the generator yields: (mode, data)
                for mode, data in chat_agent.stream(
                    {"messages": [{"role": "user", "content": user_query}]},
                    stream_mode=["messages", "updates"],
                    config=config,
                    context=UserContext(user_id=user_id)
                ):
                    # -------------------------
                    # 1. MESSAGE EVENTS (Tokens)
                    # -------------------------
                    if mode == "messages":
                        # In 'messages' mode, data is (BaseMessageChunk, metadata)
                        token, metadata = data
                        content = ""

                        if hasattr(token, 'content_blocks') and token.content_blocks:
                            block = token.content_blocks[0]
                            content = getattr(block, 'text', str(block))
                        elif hasattr(token, 'content'):
                            content = token.content

                        if content:
                            payload = json.dumps({
                                "type": "message",
                                "node": metadata.get('langgraph_node', 'unknown'),
                                "text": content
                            })
                            yield f"data: {payload}\n\n"

                    # -----------------------------------
                    # 2. UPDATE EVENTS (Node completions)
                    # -----------------------------------
                    # elif mode == "updates":
                    #     sanitized_data = {}
                    #     for node_name, state_update in data.items():
                    #         node_output = {}
                    #         for key, value in state_update.items():
                    #             # If the value is a list (like summarized_messages or messages)
                    #             if isinstance(value, list):
                    #                 node_output[key] = [
                    #                     m.content if hasattr(m, 'content') else str(m) 
                    #                     for m in value
                    #                 ]
                    #             # If the value is a single message object
                    #             elif hasattr(value, 'content'):
                    #                 node_output[key] = value.content
                    #             else:
                    #                 node_output[key] = value
                            
                    #         sanitized_data[node_name] = node_output

                    #     payload = json.dumps({
                    #         "type": "update",
                    #         "data": sanitized_data
                    #     })
                    #     yield f"data: {payload}\n\n"

                # --------------------------------
                # AFTER STREAM FINISHES
                # --------------------------------

                # [!!] do this as background task ?
                if new_thread:
                    snapshot = chat_agent.get_state(config)
                    messages = snapshot.values.get("messages", [])[-2:]

                    title_input = "\n".join(
                        dict(m).get("content", "")
                        for m in messages
                        if dict(m).get("content")
                    )

                    title = generate_chat_title(title_input)

                    ChatSession.objects.create(
                        user_id=request.user.id,
                        thread_id=thread_id,
                        title=title,
                    )

                    payload = json.dumps({
                        "type": "title",
                        "thread_id": thread_id,
                        "title": title
                    })

                    yield f"data: {payload}\n\n"
        
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"  

        response = StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response
        

class ChatListView(APIView):

    def get(self, request):
        chats = ChatSession.objects.filter(user=request.user).order_by("-created_at")

        data = [
            {
                "thread_id": c.thread_id,
                "title": c.title,
                # "created_at": c.created_at
            }
            for c in chats
        ]

        return Response(data)
    
class ChatDetailView(APIView):

    def patch(self, request, thread_id):
        title = request.data.get("title")

        if not title:
            return Response(
                {"error": "Title is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            chat = ChatSession.objects.get(thread_id=thread_id, user=request.user)
            chat.title = title
            chat.save()

            return Response({
                "message": "Title updated",
                "thread_id": thread_id,
                "title": title
            })

        except ChatSession.DoesNotExist:
            return Response(
                {"error": "Chat not found or access denied"},
                status=status.HTTP_404_NOT_FOUND
            )

    def delete(self, request, thread_id):
        try:
            chat = ChatSession.objects.get(thread_id=thread_id, user=request.user)

            pg_checkpointer.delete_thread(thread_id)
            chat.delete()

            return Response({
                "message": "Chat deleted",
                "thread_id": thread_id
            })

        except ChatSession.DoesNotExist:
            return Response(
                {"error": "Chat not found or access denied"},
                status=status.HTTP_404_NOT_FOUND
            )


def get_clean_chat_history(raw_messages, reverse=True):
    formatted_history = []
    
    for message_tuple in raw_messages:
        # Convert list of lists [["content", "..."], ["type", "human"]] to dict
        msg_dict = dict(message_tuple)
        role_type = msg_dict.get("type")
        
        # We only care about human and ai roles
        if role_type in ["human", "ai"]:
            formatted_history.append({
                "role": "user" if role_type == "human" else "assistant",
                "content": msg_dict.get("content", ""),
                "id": msg_dict.get("id"),
                "created_at": msg_dict.get("response_metadata", {}).get("created_at") # Optional
            })

    if reverse:
        formatted_history.reverse()
        
    return formatted_history

class ChatHistoryView(APIView):

    def get(self, request, thread_id):
        user = request.user
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 10))

        # 1. Check if the thread exists and belongs to the logged-in user
        thread_exists = ChatSession.objects.filter(
            thread_id=thread_id, 
            user=user
        ).exists()

        if not thread_exists:
            return Response(
                {"error": "Forbidden: You do not have permission to access this chat state."},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            # 2. If ownership is verified, fetch the state from LangGraph
            config = {"configurable": {"thread_id": thread_id}}
            state = chat_agent.get_state(config)

            if not state or "messages" not in state.values:
                return Response({"messages": []}, status=status.HTTP_200_OK)

            # Extract and format
            raw_messages = state.values.get("messages", [])
            # full_history = get_clean_chat_history(raw_messages)

            # start = (page - 1) * page_size
            # end = start + page_size
            # paginated_history = full_history[start:end]

            # has_next = len(full_history) > end

            # return Response({
            #     "thread_id": thread_id,
            #     "meta": {
            #         "total_messages": len(full_history),
            #         "current_page": page,
            #         "has_next": has_next
            #     },
            #     "chat_history": paginated_history
            # }, status=status.HTTP_200_OK)

            # ── Raw message response (no cleaning, all messages incl. tool calls/results) ──
            # def _serialize_message(m):
            #     content = getattr(m, "content", "")
            #     if not isinstance(content, (str, list, dict, int, float, bool, type(None))):
            #         content = str(content)
            #     return {
            #         "id": getattr(m, "id", None),
            #         "type": getattr(m, "type", m.__class__.__name__),
            #         "content": content,
            #         "name": getattr(m, "name", None),
            #         "tool_calls": getattr(m, "tool_calls", None) or [],
            #         "tool_call_id": getattr(m, "tool_call_id", None),
            #         "additional_kwargs": getattr(m, "additional_kwargs", {}) or {},
            #         "response_metadata": getattr(m, "response_metadata", {}) or {},
            #     }
            #
            # serialized = [_serialize_message(m) for m in raw_messages]
            #
            # start = (page - 1) * page_size
            # end = start + page_size
            # page_messages = serialized[start:end]
            # has_next = len(serialized) > end
            #
            # return Response({
            #     "thread_id": thread_id,
            #     "meta": {
            #         "total_messages": len(serialized),
            #         "current_page": page,
            #         "has_next": has_next,
            #     },
            #     "raw_messages": page_messages,
            # }, status=status.HTTP_200_OK)

            return Response({"raw_message": raw_messages}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

