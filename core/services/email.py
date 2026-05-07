"""Transactional email helpers.

Centralises token generation, link building, and template rendering so views
don't carry inline HTML strings. Each public function takes a User instance
and sends one templated message via DEFAULT_FROM_EMAIL.

Templates live in core/templates/emails/. Each email has both .html and .txt
variants — recipients with HTML-blocking clients still get the text body.
"""

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


def _build_token_link(user, path: str) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}{path}?uid={uid}&token={token}"


def _send_templated(subject: str, template_base: str, context: dict, recipient: str) -> None:
    html_body = render_to_string(f"emails/{template_base}.html", context)
    text_body = render_to_string(f"emails/{template_base}.txt", context)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)


def send_verification_email(user) -> None:
    """Send an account-verification link to the user."""
    verify_url = _build_token_link(user, "/verify-email")
    _send_templated(
        subject=f"Verify your email — {settings.EMAIL_SITE_NAME}",
        template_base="verify_email",
        context={
            "user": user,
            "verify_url": verify_url,
            "site_name": settings.EMAIL_SITE_NAME,
        },
        recipient=user.email,
    )


def send_password_reset_email(user) -> None:
    """Send a password-reset link to the user."""
    reset_url = _build_token_link(user, "/reset-password")
    expiry_hours = max(1, settings.PASSWORD_RESET_TIMEOUT // 3600)
    _send_templated(
        subject=f"Reset your password — {settings.EMAIL_SITE_NAME}",
        template_base="password_reset",
        context={
            "user": user,
            "reset_url": reset_url,
            "site_name": settings.EMAIL_SITE_NAME,
            "expiry_hours": expiry_hours,
        },
        recipient=user.email,
    )
