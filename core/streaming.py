from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny
from typing import Any

from langchain.agents import create_agent
from langchain.messages import AIMessage, AIMessageChunk, AnyMessage, ToolMessage

from langchain_groq import ChatGroq
from langgraph.config import get_stream_writer  
# from langgraph.checkpoint.postgres import PostgresSaver
# from psycopg_pool import ConnectionPool
from django.conf import settings

from pydantic import BaseModel, Field
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_community.tools import DuckDuckGoSearchRun
import json
from django.http import StreamingHttpResponse
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from .serializers import MessageSerializer

from typing import Any
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.messages import AIMessage, AIMessageChunk, AnyMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, Interrupt

from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from .serializers import SignupSerializer, UpdateUserProfileSerializer, EmailTokenObtainPairSerializer

search = DuckDuckGoSearchRun()
api_key = settings.GROQ_API_KEY


llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.1,
    max_tokens=4000,
    timeout=60,
    api_key=api_key,
    max_retries=3,
)

@tool
def web_search(query: str) -> str:
    """Perform a web search and return the results."""
    return search.invoke(query)

@tool
def get_weather(city: str) -> str:
    """Get weather for a given city."""
    writer = get_stream_writer()  
    # stream any arbitrary data
    writer(f"Looking up data for city: {city}")
    writer(f"Acquired data for city: {city}")
    return f"It's always sunny in {city}!"


agent = create_agent(
    model=llm,
    tools=[web_search, get_weather], 
    system_prompt="You are a helpful assistant")


# ========================
# Streaming + Persistance
# ========================

# 1. Define State
class StreamState(TypedDict):
    query: str
    answer1: str
    answer2: str

# 2. Define Nodes
def Node1(state: StreamState):
    # """Reply like a polite Pirate"""
    user_query = state["query"]
    writer = get_stream_writer()  
    writer("Pirate node")
    msg = llm.invoke(f"Be a very polite, grumpy, sarcastic pirate. Write a short joke about: {user_query}")
    return {"answer1": msg.content}

def Node2(state: StreamState):
    """Reply like a grumpy, rude robot"""
    user_query = state["query"]
    writer = get_stream_writer()  
    writer("Robot node")
    msg = llm.invoke(f"Be a very polite, grumpy, sarcastic robot. Write a short joke about: {user_query}")
    return {"answer2": msg.content}

# 3. Build Graph
builder = StateGraph(StreamState)
builder.add_node("Node1", Node1)
builder.add_node("Node2", Node2)

builder.add_edge(START, "Node1")
# builder.add_edge("Node1", END)
builder.add_edge("Node1", "Node2")
builder.add_edge("Node2", END)

checkpointer = InMemorySaver()
stream_agent = builder.compile(checkpointer=checkpointer)


@method_decorator(csrf_exempt, name='dispatch')
class StreamStateUpdateView(APIView):
    authentication_classes = [] 
    permission_classes = [AllowAny]

    def get(self, request):
        return HttpResponse("Hello, this is your personal AI, JARVIS.")

    def post(self, request):
        user_query = request.data.get('message')
        if not user_query:
            return HttpResponse("Missing message", status=400)
            
        config = {"configurable": {"thread_id": "stream-state-1"}}

        def stream_generator():
            try:
                # stream_mode="updates" yields when a node completes
                for chunk in agent.stream(
                    {"messages": [("user", user_query)]},
                # for chunk in stream_agent.stream(
                    # {"query": user_query}, 
                    config=config,
                    stream_mode="updates"
                ):
                    for node_name, data in chunk.items():
                        # The 'data' usually contains a list of messages
                        # We need to extract the serializable part
                        serializable_update = {}
                        
                        if "messages" in data:
                            last_msg = data["messages"][-1]
                            # Handle different message types (AIMessage, ToolMessage, etc)
                            serializable_update["content"] = last_msg.content
                            serializable_update["type"] = last_msg.type
                        else:
                            # Fallback for other types of node updates
                            serializable_update = str(data)

                        payload = json.dumps({
                            "node": node_name,
                            "update": serializable_update
                        })
                        yield f"data: {payload}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        response = StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        
        # KEY HEADERS TO STOP BUFFERING:
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'  # Important for Nginx
        return response
        

@method_decorator(csrf_exempt, name='dispatch')
class StreamTokenView(APIView):
    authentication_classes = [] 
    permission_classes = [AllowAny]

    def get(self, request):
        return HttpResponse("Hello, this is your personal AI, JARVIS.")

    def post(self, request):
        user_query = request.data.get('message')
        if not user_query:
            return StreamingHttpResponse(
                iter([f"data: {json.dumps({'error': 'No message provided'})}\n\n"]), 
                content_type='text/event-stream'
            )
                    
        config = {"configurable": {"thread_id": "stream-token-1"}}

        def stream_generator():
            try:
                for token, metadata in stream_agent.stream(
                    {"query": user_query},
                # for token, metadata in agent.stream(
                #     {"messages": [("user", user_query)]}, 
                    config=config,
                    stream_mode="messages"
                ):
                    # 'token' is often a MessageChunk. We extract text from content_blocks.
                    content = ""
                    if hasattr(token, 'content_blocks') and token.content_blocks:
                        # Extract text from the first content block if it's there
                        block = token.content_blocks[0]
                        # Some models return text as an attribute, others as a dict
                        content = getattr(block, 'text', str(block))
                    elif hasattr(token, 'content'):
                        # Fallback for standard AIMessageChunks
                        content = token.content

                    if content:
                        payload = json.dumps({
                            "node": metadata.get('langgraph_node', 'unknown'),
                            "text": content
                        })
                        yield f"data: {payload}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        response = StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response


@method_decorator(csrf_exempt, name='dispatch')
class StreamCustomView(APIView):
    authentication_classes = [] 
    permission_classes = [AllowAny]

    def get(self, request):
        return HttpResponse("Hello, this is your personal AI, JARVIS.")

    def post(self, request):
        user_query = request.data.get('message')
        if not user_query:
            return StreamingHttpResponse(
                iter([f"data: {json.dumps({'error': 'No message provided'})}\n\n"]), 
                content_type='text/event-stream'
            )
                    
        config = {"configurable": {"thread_id": "stream-token-1"}}

        def stream_generator():
            try:
                # stream_mode is a list here
                for stream_mode, chunk in agent.stream(
                    {"messages": [("user", user_query)]}, 
                # for stream_mode, chunk in stream_agent.stream(
                #     {"query": user_query},
                    config=config,
                    stream_mode=["updates", "custom"]
                ):
                    # 1. Handle Custom Writer Logs (from tools)
                    if stream_mode == "custom":
                        payload = {
                            "type": "tool_log",
                            "content": str(chunk),
                            "node": "tool_internal"
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                    
                    # 2. Handle Node Updates (when a node finishes)
                    elif stream_mode == "updates":
                        # chunk is a dict: {node_name: {state_updates}}
                        for node_name, data in chunk.items():
                            serializable_update = {}
                            
                            if isinstance(data, dict) and "messages" in data:
                                last_msg = data["messages"][-1]
                                # Handling the specific structure of AI/Tool messages
                                serializable_update = {
                                    "content": last_msg.content,
                                    "type": last_msg.type
                                }
                            else:
                                # Fallback for nodes that don't return "messages"
                                serializable_update = {"content": str(data)}

                            payload = {
                                'type': 'node_update',
                                "node": node_name,
                                "update": serializable_update
                            }
                            yield f"data: {json.dumps(payload)}\n\n"
                                    
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

        response = StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response
        

@method_decorator(csrf_exempt, name='dispatch')
class StreamCommonView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        user_query = request.data.get('message')
        config = {"configurable": {"thread_id": "docs-stream-session"}}

        def stream_generator():
            try:
                for stream_mode, data in stream_agent.stream(
                    {"query": user_query},
                # for stream_mode, data in agent.stream(
                #     {"messages": [{"role": "user", "content": user_query}]},
                    stream_mode=["messages", "updates"],
                    config=config
                ):
                    # --- 1. HANDLING TOKENS ---
                    if stream_mode == "messages":
                        token, metadata = data
                        if isinstance(token, AIMessageChunk):
                            text = getattr(token, 'content', "")
                            
                            if text:
                                payload = {
                                    "type": "token",
                                    "text": text,
                                    "tool_chunk": token.tool_call_chunks if token.tool_call_chunks else None
                                }
                                yield f"data: {json.dumps(payload)}\n\n"

                    # --- 2. HANDLING COMPLETED STEPS ---
                    elif stream_mode == "updates":
                        for node_name, update in data.items():
                            if node_name in ("agent", "tools", "model", "chatbot"):
                                if "messages" in update:
                                    message = update["messages"][-1]
                                    
                                    if isinstance(message, AIMessage) and message.tool_calls:
                                        yield f"data: {json.dumps({'type': 'tool_call', 'calls': message.tool_calls})}\n\n"
                                    
                                    # Handle Tool Responses (Function results)
                                    elif isinstance(message, ToolMessage):
                                        # FIX: Use .content instead of .content_blocks
                                        yield f"data: {json.dumps({'type': 'tool_response', 'content': message.content})}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

        response = StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        # Crucial for preventing proxy buffering
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response
    

import json
from pydantic import BaseModel
from typing import Any, Literal
from langgraph.runtime import Runtime
from rest_framework.views import APIView
from langchain.messages import AIMessage
from django.http import StreamingHttpResponse
from langgraph.config import get_stream_writer  
from rest_framework.permissions import AllowAny
from langchain.agents.middleware import after_agent, AgentState
from langchain.messages import AIMessage, AIMessageChunk, ToolMessage


class ResponseSafety(BaseModel):
    """Evaluate a response as safe or unsafe."""
    evaluation: Literal["safe", "unsafe"]

safety_model = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.1,
    max_tokens=4000,
    timeout=60,
    api_key=api_key,
    max_retries=3,
)

@after_agent(can_jump_to=["end"])
def safety_guardrail(state, runtime):
    stream_writer = get_stream_writer()
    
    # 1. Safety check for state
    if not state or not state.get("messages"):
        return None

    last_message = state["messages"][-1]
    
    # 2. Ensure it's an AI message and has actual content
    if not isinstance(last_message, AIMessage) or not last_message.content:
        return None

    # Bind tools inside or outside; 'any' forces the model to use ResponseSafety
    model_with_tools = safety_model.bind_tools([ResponseSafety], tool_choice="any")
    
    # 3. Invoke the model
    result = model_with_tools.invoke([
        {"role": "system", "content": "Evaluate this AI response as generally safe or unsafe."},
        {"role": "user", "content": f"AI response: {last_message.content}"}
    ])
    
    # 4. Critical: Stream the result BEFORE processing
    # This ensures the 'custom' mode in your View receives the data
    stream_writer(result)  

    # 5. Defensive extraction of the evaluation
    # We check if tool_calls exists AND is not empty
    if hasattr(result, 'tool_calls') and result.tool_calls:
        # Access the first tool call
        first_call = result.tool_calls[0]
        
        # Check if 'args' exists and get 'evaluation' safely
        args = first_call.get("args", {})
        evaluation = args.get("evaluation")

        if evaluation == "unsafe":
            last_message.content = "I cannot provide that response. Please rephrase your request."

    return None

guard_agent = create_agent(
    model=llm,
    tools=[get_weather],
    middleware=[safety_guardrail]
)

class StreamGuardrailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        user_query = request.data.get('message')
        config = {"configurable": {"thread_id": "guardrail-session-1"}}

        def stream_generator():
            try:
                for stream_mode, data in guard_agent.stream(
                    {"messages": [{"role": "user", "content": user_query}]},
                    stream_mode=["messages", "updates", "custom"],
                    config=config
                ):
                    # CHANNEL 1: LIVE TOKENS
                    if stream_mode == "messages":
                        token, metadata = data
                        # FIX: Only yield if there is actual text to avoid empty data packets
                        if isinstance(token, AIMessageChunk) and token.content:
                            yield f"data: {json.dumps({'type': 'token', 'text': token.content})}\n\n"

                    # CHANNEL 2: NODE UPDATES
                    elif stream_mode == "updates":
                        for source, update in data.items():
                            # source is the node name (agent, tools, etc.)
                            if "messages" in update:
                                msg = update["messages"][-1]
                                if isinstance(msg, AIMessage) and msg.tool_calls:
                                    yield f"data: {json.dumps({'type': 'tool_call', 'details': msg.tool_calls})}\n\n"
                                elif isinstance(msg, ToolMessage):
                                    yield f"data: {json.dumps({'type': 'tool_result', 'content': msg.content})}\n\n"

                    # CHANNEL 3: CUSTOM (Guardrail)
                    elif stream_mode == "custom":
                        if hasattr(data, 'tool_calls') and data.tool_calls:
                            eval_val = data.tool_calls[0]['args'].get('evaluation')
                            yield f"data: {json.dumps({'type': 'guardrail', 'status': eval_val})}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

        response = StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        # Crucial for real-time delivery
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no' 
        return response
    

human_loop_agent = create_agent(
    model=llm,
    tools=[get_weather],
    middleware=[  
        HumanInTheLoopMiddleware(interrupt_on={"get_weather": True}),  
    ],  
    checkpointer=checkpointer,
    system_prompt="You are a helpful assistant"
    )

class StreamHumanLoopView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        user_query = request.data.get('message')
        # Decisions come from the frontend when resuming an interrupted state
        decisions = request.data.get('decisions') 
        thread_id = request.data.get('thread_id', 'default_human_thread')
        
        config = {"configurable": {"thread_id": thread_id}}

        def stream_generator():
            try:
                # Determine input: Is it a new message or a Resume command?
                if decisions:
                    # User is responding to an interrupt
                    if not thread_id:
                        yield f"data: {json.dumps({'type': 'error', 'content': 'Thread ID required to resume'})}\n\n"
                        return
                    input_data = Command(resume=decisions)
                else:
                    # Fresh request from the user
                    input_data = {"messages": [{"role": "user", "content": user_query}]}

                for stream_mode, data in human_loop_agent.stream(
                    input_data,
                    stream_mode=["messages", "updates"],
                    config=config
                ):
                    # --- 1. LIVE TOKENS ---
                    if stream_mode == "messages":
                        token, metadata = data
                        if isinstance(token, AIMessageChunk) and token.content:
                            yield f"data: {json.dumps({'type': 'token', 'text': token.content})}\n\n"

                    # --- 2. UPDATES & INTERRUPTS ---
                    elif stream_mode == "updates":
                        for source, update in data.items():
                            # Handle standard node completion
                            if source in ("agent", "tools", "model"):
                                message = update["messages"][-1]
                                if isinstance(message, AIMessage) and message.tool_calls:
                                    yield f"data: {json.dumps({'type': 'tool_call', 'calls': message.tool_calls})}\n\n"
                                elif isinstance(message, ToolMessage):
                                    yield f"data: {json.dumps({'type': 'tool_result', 'content': message.content})}\n\n"
                            
                            # HITL: Handle the Interrupt
                            if source == "__interrupt__":
                                # 'update' is a list of Interrupt objects
                                # We send the ID and Value so the frontend knows what to approve
                                interrupt_data = []
                                for i in update:
                                    interrupt_data.append({
                                        "id": i.id,
                                        "value": i.value # contains action_requests
                                    })
                                
                                yield f"data: {json.dumps({'type': 'interrupt', 'data': interrupt_data})}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

        response = StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response


def get_weather_tool(city: str) -> str:
    """Get weather for a given city"""
    return f"It's always Windy and Sunny in {city}!"

weather_agent = create_agent(
    model=llm,
    tools=[get_weather_tool],
    name="weather_agent",
)

def call_weather_agent(query: str) -> str:
    """Query the weather agent."""
    result = weather_agent.invoke({
        "messages": [{"role": "user", "content": query}]
    })
    return result["messages"][-1].text

supervisor_agent = create_agent(
    model=llm,
    tools=[call_weather_agent],
    name="supervisor",  
)

class StreamSubAgentsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        user_query = request.data.get('message')
        config = {"configurable": {"thread_id": "subagent-session-1"}}

        def stream_generator():
            try:
                current_agent = None

                for namespace, stream_mode, data in supervisor_agent.stream(
                    {"messages": [{"role": "user", "content": user_query}]},
                    stream_mode=["messages", "updates"],
                    subgraphs=True,
                    config=config
                ):
                    # --- 1. HANDLING TOKENS & SENDER DISAMBIGUATION ---
                    if stream_mode == "messages":
                        token, metadata = data
                        
                        # Identify which agent is speaking
                        agent_name = metadata.get("lc_agent_name", "supervisor")
                        
                        # If the agent switched, notify the frontend first
                        if agent_name != current_agent:
                            current_agent = agent_name
                            yield f"data: {json.dumps({'type': 'sender_shift', 'agent': agent_name})}\n\n"

                        if isinstance(token, AIMessageChunk) and token.content:
                            yield f"data: {json.dumps({
                                'type': 'token', 
                                'text': token.content,
                                'agent': agent_name,
                                "tool_chunks": token.tool_call_chunks if hasattr(token, 'tool_call_chunks') else None
                            })}\n\n"

                    # --- 2. HANDLING COMPLETED STEPS ---
                    elif stream_mode == "updates":
                        # data is a dict of {node_name: update_values}
                        for node_name, update in data.items():
                            if "messages" in update:
                                msg = update["messages"][-1]
                                
                                # Tool Calls from either Supervisor or Sub-agent
                                if isinstance(msg, AIMessage) and msg.tool_calls:
                                    yield f"data: {json.dumps({
                                        'type': 'tool_call', 
                                        'node': node_name,
                                        'calls': msg.tool_calls
                                    })}\n\n"
                                
                                # Tool Results
                                elif isinstance(msg, ToolMessage):
                                    yield f"data: {json.dumps({
                                        'type': 'tool_result', 
                                        'node': node_name,
                                        'content': msg.content
                                    })}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

        response = StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response



# ----------------------------------
# This is with reasoning / thinking
# ----------------------------------

# for token, metadata in messagegraph.stream(
#     {"topic": topic},
#     stream_mode="messages"
# ):
#     content = ""
#     if hasattr(token, 'content_blocks') and token.content_blocks:
#         block = token.content_blocks[0]
#         content = getattr(block, 'text', str(block))
#     elif hasattr(token, 'content'):
#         content = token.content

#     if content:
#         payload = json.dumps({
#             "node": metadata.get('langgraph_node', 'unknown'),
#             "text": content
#         })
#         yield f"data: {payload}\n\n"

