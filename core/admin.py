from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, ChatSession

@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    model = CustomUser
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

    # Fields for creating a new user
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "first_name", "last_name", "is_active", "is_staff"),
        }),
    )

    search_fields = ("email", "first_name", "last_name")

    readonly_fields = ("get_full_name",)

    @admin.display(description='Full Name', ordering='first_name')
    def get_full_name(self, obj):
        return obj.full_name
    search_fields = ("email",)


admin.site.register(ChatSession)
