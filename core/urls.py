from django.urls import path
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView
)

from . import views
from .streaming import StreamStateUpdateView, StreamTokenView, StreamCustomView, StreamCommonView, StreamGuardrailView, StreamHumanLoopView, StreamSubAgentsView
from .langgraph_persistance import PersistView
from .langgraph_stream import GraphStreamView, GraphMultipleStreamView, GraphSubStreamView,GraphMessageStreamView, FilterTagsStreamView, FilterNodeStreamView
from .langgraph_memory import InMemoryView, PgMemoryView, PgLongTermMemoryView, PgSemanticLongTermMemory, TrimMemoryView, DeleteMessageView, SummarizeMessageView, ManualSummaryView, ManualGraphStateView


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
    
    # chat session
    path("threads/<str:thread_id>/history/", views.ChatHistoryView.as_view()),
    path("threads/", views.ChatListView.as_view()),
    path("threads/<str:thread_id>/", views.ChatDetailView.as_view()),
    # path("threads/<str:thread_id>/delete/", views.ChatDeleteView.as_view()),
    path('aichat/', views.AiChatView.as_view(), name='AiChatView'),
    path('aichat2/', views.AiStateDetailView.as_view(), name='AiStateDetailView'),
    
    # Stream
    path('stream/', StreamStateUpdateView.as_view(), name='StreamStateUpdateView'),
    path('streamToken/', StreamTokenView.as_view(), name='StreamTokenView'),
    path('streamCustom/', StreamCustomView.as_view(), name='StreamCustomView'),
    path('streamCommon/', StreamCommonView.as_view(), name='StreamCommonView'),
    path('streamGuard/', StreamGuardrailView.as_view(), name='StreamGuardrailView'),
    path('streamHumanloop/', StreamHumanLoopView.as_view(), name='StreamHumanLoopView'),
    path('streamSubagent/', StreamSubAgentsView.as_view(), name='StreamSubAgentsView'),

    path('persist/', PersistView.as_view(), name='PersistView'),

    path('graphStream/', GraphStreamView.as_view(), name='GraphStreamView'),
    path('graphStream2/', GraphMultipleStreamView.as_view(), name='GraphMultipleStreamView'),
    path('graphStream3/', GraphSubStreamView.as_view(), name='GraphSubStreamView'),
    path('graphStream4/', GraphMessageStreamView.as_view(), name='GraphMessageStreamView'),
    path('graphStream5/', FilterTagsStreamView.as_view(), name='FilterTagsStreamView'),
    path('graphStream6/', FilterNodeStreamView.as_view(), name='FilterNodeStreamView'),

    # Memory
    path('memory/', InMemoryView.as_view(), name='InMemoryView'),
    path('memory2/', PgMemoryView.as_view(), name='PgMemoryView'),
    path('memory3/', PgLongTermMemoryView.as_view(), name='PgLongTermMemoryView'),
    path('memory4/', PgSemanticLongTermMemory.as_view(), name='PgSemanticLongTermMemory'),

    path('trim/', TrimMemoryView.as_view(), name='TrimMemoryView'),
    path('trim2/', DeleteMessageView.as_view(), name='DeleteMessageView'),
    path('trim3/', SummarizeMessageView.as_view(), name='SummarizeMessageView'),
    path('trim4/', ManualSummaryView.as_view(), name='ManualSummaryView'),
    path('trim5/', ManualGraphStateView.as_view(), name='ManualGraphStateView'),
]