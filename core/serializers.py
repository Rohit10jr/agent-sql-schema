from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import authenticate, get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.exceptions import AuthenticationFailed, ValidationError


User = get_user_model()

class SignupSerializer(serializers.ModelSerializer):
    password1 = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
        style={'input_type': 'password'}
    )
    password2 = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )

    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name', 'password1', 'password2']

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email=value).exists():
            raise ValidationError("An account with this email already exists.")
        return value

    def validate(self, data):
        if data['password1'] != data['password2']:
            raise ValidationError({
                "password2": "Passwords do not match."
            })
        return data

    def create(self, validated_data):
        validated_data.pop('password2')
        email = validated_data['email'].lower()

        user = User.objects.create_user(
            email=email,
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            password=validated_data['password1']
        )
                
        # refresh = RefreshToken.for_user(user)
        # user.tokens = {
        #     'refresh': str(refresh),
        #     'access': str(refresh.access_token),
        # }
        
        user.is_active = True
        user.email_verified = False
        user.save()

        return user


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = "email"
    
    def validate(self, attrs):
        email = attrs.get("email", "").lower()
        password = attrs.get("password")

        user = User.objects.filter(email=email).first()
        if user and not user.is_active:
            raise AuthenticationFailed("Account is deactivated. Contact support.")

        user = authenticate(request=self.context.get("request"),
                            email=email,
                            password=password)

        if not user:
            raise AuthenticationFailed("Invalid email or password.")

        if not getattr(user, "email_verified", False):
            raise AuthenticationFailed("Email not verified.")

        refresh = self.get_token(user)
        return {
            "success": True,
            "message": "Login successful",
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "user": {
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        }


class UpdateUserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['first_name', 'last_name']
        extra_kwargs = {
            'first_name': {'required': True},
            'last_name': {'required': True}
        }


class MessageSerializer(serializers.Serializer):
    """Used for AI prompt"""
    
    slug = serializers.CharField(max_length=15,  required=False)
    message = serializers.CharField(max_length=5000)


class PasswordResetRequestSerializer(serializers.Serializer):
    """Serializer for password reset request (forgot password)"""
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.lower()


class PasswordResetValidateSerializer(serializers.Serializer):
    """Serializer to validate password reset token"""
    uid = serializers.CharField()
    token = serializers.CharField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Serializer for password reset confirmation"""
    uid = serializers.CharField()
    token = serializers.CharField()
    password1 = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
        style={'input_type': 'password'}
    )
    password2 = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )

    def validate(self, data):
        if data['password1'] != data['password2']:
            raise serializers.ValidationError({
                "password2": "Passwords do not match."
            })
        return data


class PasswordChangeSerializer(serializers.Serializer):
    """Serializer for authenticated user password change"""
    old_password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )
    new_password1 = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
        style={'input_type': 'password'}
    )
    new_password2 = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )

    def validate(self, data):
        if data['new_password1'] != data['new_password2']:
            raise serializers.ValidationError({
                "new_password2": "New passwords do not match."
            })
        return data


# ── Connection Serializers ──────────────────────────────────────────

from core.models import Connection, Result


class ConnectionCreateSerializer(serializers.Serializer):
    """Validates input for creating a new DSN-based connection."""
    dsn = serializers.CharField(min_length=3)
    name = serializers.CharField(max_length=255)


class FileConnectionCreateSerializer(serializers.Serializer):
    """Validates input for creating a connection from an uploaded file."""
    FILE_TYPE_CHOICES = [
        ("sqlite", "SQLite"),
        ("csv", "CSV"),
        ("excel", "Excel"),
        ("sas7bdat", "SAS"),
    ]
    file = serializers.FileField()
    type = serializers.ChoiceField(choices=FILE_TYPE_CHOICES)
    name = serializers.CharField(max_length=255)


class ConnectionOutSerializer(serializers.ModelSerializer):
    """Serializes a Connection for API responses."""
    class Meta:
        model = Connection
        fields = ["id", "name", "dsn", "database", "type", "dialect", "is_sample", "options", "created_at"]


class ConnectionUpdateSerializer(serializers.Serializer):
    """Validates partial updates to a connection (name, dsn, or options)."""
    name = serializers.CharField(max_length=255, required=False)
    dsn = serializers.CharField(min_length=3, required=False)
    options = serializers.JSONField(required=False)


# ── Result Serializers ──────────────────────────────────────────────


class ResultOutSerializer(serializers.ModelSerializer):
    """Serializes a Result for API responses."""
    class Meta:
        model = Result
        fields = ["id", "thread_id", "content", "type", "linked_id", "created_at"]


class ResultUpdateSerializer(serializers.Serializer):
    """Validates input for updating a SQL query result."""
    sql = serializers.CharField()
    for_chart = serializers.BooleanField(default=False)


class ChartRefreshOutSerializer(serializers.Serializer):
    """Serializes refreshed chart data."""
    chartjs_json = serializers.CharField()
    created_at = serializers.DateTimeField()
