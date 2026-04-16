import json
import uuid

from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone
from django.conf import settings

class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email must be provided")

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")

        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    date_joined = models.DateTimeField(default=timezone.now)
    
    email_verified = models.BooleanField(default=False)
    
    # user_type = models.CharField(max_length=20, choices=USER_TYPES, default="Job_Seeker")
    # last_login = models.DateTimeField(auto_now=True)
    # is_verified = models.BooleanField(default=False)

    objects = CustomUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    def __str__(self):
        return f"{self.id} - {self.email}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()
    

DEFAULT_PROJECT_NAME = "New Project"
DEFAULT_DESCRIPTION = "Ai generated SQl and Schema"

class ChatSession(models.Model):
    thread_id = models.CharField(max_length=50, unique=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_projects",
    )
    title = models.CharField(
        max_length=255,
        default=DEFAULT_PROJECT_NAME,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # def save(self, *args, **kwargs):
    #     if not self.thread_id:
    #         # thread_id: userId-uuid
    #         self.thread_id = f"{self.user_id}-{uuid.uuid4().hex[:12]}"

    #     super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.thread_id})"