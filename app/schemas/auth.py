"""Pydantic v2 request/response schemas for auth and user profile (B4, §4, §7).

Covers registration, login (by email or phone), the access/refresh token pair,
refresh, the current-user view, and address CRUD DTOs.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Minimal email shape check (email-validator is not a project dependency; the DB
# CITEXT column and unique constraint are the real integrity guards).
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    """Payload for ``POST /auth/register``."""

    email: str = Field(pattern=_EMAIL_PATTERN, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    phone: str | None = Field(default=None, min_length=3, max_length=32)


class LoginRequest(BaseModel):
    """Payload for ``POST /auth/login`` — login by email OR phone.

    Exactly one of ``email`` / ``phone`` must be supplied together with the
    password.
    """

    email: str | None = Field(default=None, pattern=_EMAIL_PATTERN, max_length=255)
    phone: str | None = Field(default=None, min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _require_one_identifier(self) -> "LoginRequest":
        """Ensure exactly one of email/phone is provided."""
        if bool(self.email) == bool(self.phone):
            raise ValueError("Provide exactly one of 'email' or 'phone'")
        return self


class TokenPair(BaseModel):
    """Access + refresh token pair returned by login/refresh."""

    access: str
    refresh: str


class RefreshRequest(BaseModel):
    """Payload for ``POST /auth/refresh``.

    The refresh token may also arrive via the ``refresh`` cookie; this body form
    is the fallback when the cookie is absent.
    """

    refresh: str | None = None


class UserMe(BaseModel):
    """Current-user profile view (``GET /users/me``)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    phone: str | None = None
    is_active: bool
    is_staff: bool


class UserUpdate(BaseModel):
    """Partial profile update (``PATCH /users/me``)."""

    phone: str | None = Field(default=None, min_length=3, max_length=32)


class AddressCreate(BaseModel):
    """Payload to create an address (``POST /users/me/addresses``)."""

    full_name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=3, max_length=32)
    city: str = Field(min_length=1, max_length=255)
    street: str = Field(min_length=1, max_length=255)
    zip: str | None = Field(default=None, max_length=32)
    is_default: bool = False


class AddressUpdate(BaseModel):
    """Partial address update (``PATCH /users/me/addresses/{id}``)."""

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, min_length=3, max_length=32)
    city: str | None = Field(default=None, min_length=1, max_length=255)
    street: str | None = Field(default=None, min_length=1, max_length=255)
    zip: str | None = Field(default=None, max_length=32)
    is_default: bool | None = None


class AddressOut(BaseModel):
    """Address response view."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    phone: str
    city: str
    street: str
    zip: str | None = None
    is_default: bool
