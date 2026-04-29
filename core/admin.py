from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from .models import CustomUser, ChatSession, Connection, Result


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = CustomUser
        fields = ("email", "first_name", "last_name")


class CustomUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = CustomUser


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    model = CustomUser
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm

    # Fields to show in the table view
    list_display = ("email", "first_name", "last_name", "get_full_name", "is_staff", "email_verified")
    list_filter = ("is_staff", "is_active", "email_verified")
    ordering = ("email",)

    # Fields for editing an existing user
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name", "get_full_name")}),
        ("Status", {"fields": ("email_verified", "is_active", "is_staff", "is_superuser")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
        ("Permissions", {"fields": ("groups", "user_permissions")}),
    )

    # Fields for creating a new user — MUST include password1/password2
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "email",
                "first_name",
                "last_name",
                "password1",
                "password2",
                "is_active",
                "is_staff",
            ),
        }),
    )

    search_fields = ("email", "first_name", "last_name")
    readonly_fields = ("get_full_name",)

    @admin.display(description="Full Name", ordering="first_name")
    def get_full_name(self, obj):
        return obj.full_name


admin.site.register(ChatSession)


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "type", "database", "user", "is_sample", "created_at")
    list_display_links = ("id", "name")
    search_fields = ("id", "name", "database", "dsn", "user__email")
    list_filter = ("type", "is_sample", "created_at")
    readonly_fields = ("id", "created_at")
    ordering = ("-created_at",)


admin.site.register(Result)
