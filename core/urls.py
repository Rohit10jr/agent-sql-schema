from django.urls import path
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView
)

from . import views
from .streaming import StreamStateUpdateView, StreamTokenView, StreamCustomView, StreamCommonView, StreamGuardrailView, StreamHumanLoopView, StreamSubAgentsView



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

]