from django.urls import path
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView
)

from . import views
from . import sql_views
from . import schema_views
from .schema_views import SchemaProjectListView, SchemaProjectDetailView, SchemaProjectUpdateSerializer, GetSQLVariantView
from .schema_agent import SchemaAgent 
from .streaming import StreamStateUpdateView, StreamTokenView, StreamCustomView, StreamCommonView, StreamGuardrailView, StreamHumanLoopView, StreamSubAgentsView
from .connection_views import (
    ConnectView,
    FileConnectView,
    ConnectionListView,
    ConnectionDetailView,
    ConnectionRefreshView,
    RestoreSampleConnectionsView,
)
from .sql_views import SQLQueryView, RunSQLView, SQLConversationCreateView, SQLResultUpdateView, ChartRefreshView, ExportCSVView, ThreadResultsView 
from .sql_agent import SqlAgent



urlpatterns = [
    # JWT auth
    path('token/', views.EmailTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('token/verify/', TokenVerifyView.as_view(), name='token_verify'),

    # User
    path('signup/', views.signup, name='signup'),
    path('email/verify/', views.email_verify, name='email_verify'),
    path('email/verify/resend/', views.resend_verification, name='resend_verification'),
    path('logout/', views.logout, name='logout'),
    path('whoami/', views.current_user, name='current_user'),
    path('update-profile/', views.update_user_profile, name='update_profile'),

    # Password reset (forgot password)
    path('password/reset/', views.password_reset, name='password_reset'),
    path('password/reset/validate/', views.password_reset_validate, name='password_reset_validate'),
    path('password/reset/confirm/', views.password_reset_confirm, name='password_reset_confirm'),
    
    # Password change (authenticated)
    path('password/change/', views.password_change, name='password_change'),
    
    # generic chat session
    path("threads/<str:thread_id>/history/", views.ChatHistoryView.as_view()),
    path("threads/", views.ChatListView.as_view()),
    path("threads/<str:thread_id>/", views.ChatDetailView.as_view()),
    # path("threads/<str:thread_id>/delete/", views.ChatDeleteView.as_view()),

    # Bulk cleanup — removes every non-starred chat AND non-starred schema
    # project for the caller. Lives outside /threads/ now because it spans
    # both resources.
    path("cleanup/non-starred/", views.BulkDeleteNonStarredView.as_view()),
    path('aichat/', views.AiChatView.as_view(), name='AiChatView'),
    
    # Connections
    path('connect/', ConnectView.as_view(), name='connect'),
    path('connect/file/', FileConnectView.as_view(), name='connect_file'),
    path('connections/', ConnectionListView.as_view(), name='connections'),
    path('connections/restore-samples/', RestoreSampleConnectionsView.as_view(), name='connections_restore_samples'),
    path('connection/<uuid:connection_id>/', ConnectionDetailView.as_view(), name='connection_detail'),
    path('connection/<uuid:connection_id>/refresh/', ConnectionRefreshView.as_view(), name='connection_refresh'),

    # Token usage
    path('usage/', views.UsageView.as_view(), name='usage'),

    # SQL Conversations
    path('sql-agent/', SqlAgent.as_view(), name='sql_agent'),
    path('sql-conversation/', SQLConversationCreateView.as_view(), name='sql_conversation_create'),
    path('conversation/<str:thread_id>/query/', SQLQueryView.as_view(), name='sql_query'),
    path('conversation/<str:thread_id>/results/', ThreadResultsView.as_view(), name='thread_results'),
    path('conversation/<str:thread_id>/run-sql/', RunSQLView.as_view(), name='run_sql'),

    # SCHEMA Agent
    # path("variants/", GetSQLVariantView.as_view(), name="ai-project-variants"),
    path('schema-agent/', SchemaAgent.as_view(), name='schema_view'),
    path("schema-projects/", SchemaProjectListView.as_view(), name="ai-project-list"),
    path("schema-project/<slug:slug>/", SchemaProjectDetailView.as_view(), name="ai-project-detail"),
    path("schema-variants/", GetSQLVariantView.as_view(), name="ai-project-variants"),


    # Results
    path('result/sql/<uuid:result_id>/', SQLResultUpdateView.as_view(), name='result_sql_update'),
    path('result/chart/<uuid:result_id>/refresh/', ChartRefreshView.as_view(), name='chart_refresh'),
    path('result/<uuid:result_id>/export-csv/', ExportCSVView.as_view(), name='export_csv'),

    # Stream
    path('stream/', StreamStateUpdateView.as_view(), name='StreamStateUpdateView'),
    path('streamToken/', StreamTokenView.as_view(), name='StreamTokenView'),
    path('streamCustom/', StreamCustomView.as_view(), name='StreamCustomView'),
    path('streamCommon/', StreamCommonView.as_view(), name='StreamCommonView'),
    path('streamGuard/', StreamGuardrailView.as_view(), name='StreamGuardrailView'),
    path('streamHumanloop/', StreamHumanLoopView.as_view(), name='StreamHumanLoopView'),
    path('streamSubagent/', StreamSubAgentsView.as_view(), name='StreamSubAgentsView'),

]