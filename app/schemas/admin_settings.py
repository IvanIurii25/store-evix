"""Pydantic v2 schemas for the admin settings API (§6.4).

Two narrow concerns:

* :class:`SeoSettings` — site-wide SEO defaults, persisted as ``site_setting``
  key/value rows by the service (keys ``seo.<field>``). Every field is optional
  with an empty-string default so a fresh install returns a fully-formed object.
* Staff management (:class:`StaffItem` / :class:`StaffCreate` / :class:`StaffUpdate`
  / :class:`StaffList`) — the roster of ``is_staff`` users the back office can
  create, promote and (de)activate.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Password floor for staff accounts (mirrors the storefront registration rule).
_PASSWORD_MIN: int = 8
# Minimal email shape check (email-validator is not a project dependency; the DB
# CITEXT column + unique constraint are the real integrity guards — as in auth).
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class SeoSettings(BaseModel):
    """Site-wide SEO defaults (per-language title/description + suffix, §6.4)."""

    title_ru: str = ""
    title_ro: str = ""
    description_ru: str = ""
    description_ro: str = ""
    title_suffix: str = ""
    og_image_url: str = ""


class StaffItem(BaseModel):
    """A staff user as shown in the back-office roster."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    phone: str | None = None
    is_active: bool
    is_staff: bool
    role: str | None = None
    created_at: datetime


class StaffCreate(BaseModel):
    """Create a new staff user, or promote + reset an existing one by email."""

    email: str = Field(pattern=_EMAIL_PATTERN, max_length=255)
    password: str = Field(min_length=_PASSWORD_MIN, max_length=128)
    phone: str | None = Field(default=None, min_length=3, max_length=32)


class StaffUpdate(BaseModel):
    """Partial update of a staff user's activation / staff flag."""

    is_active: bool | None = None
    is_staff: bool | None = None


class StaffList(BaseModel):
    """Envelope for the staff roster."""

    data: list[StaffItem] = Field(default_factory=list)
