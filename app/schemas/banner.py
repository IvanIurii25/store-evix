"""Pydantic v2 schemas for homepage banners (P0).

Two contracts, like the content-page module:

* **Public** — what the storefront renders for one language
  (:class:`BannerOut`): creative, alt, optional copy and the link.
* **Admin** — the full editable banner with both-language creatives
  (:class:`BannerAdminOut`, :class:`BannerCreate`, :class:`BannerUpdate`).

Validation lives at the edge so a bad payload fails as a 422 instead of a DB
error or, worse, a rendered page:

* ``link_url`` is an internal path (``/ru/c/dom``) or an ``https://`` URL —
  anything else, ``javascript:`` above all, is refused. A banner is a
  staff-authored link rendered on every visitor's homepage, so this is the one
  spot where an XSS vector could be typed in by hand.
* ``alt`` is required and non-empty: it is what a screen reader announces.
* Both languages are required, so a visitor never meets a half-translated
  carousel — the same publication rule the catalog and content pages use.
* ``ends_at`` must not precede ``starts_at``, which would create a banner that
  can never show.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.banner import ALLOWED_LANGS

_ALT_MAX: int = 512
_TEXT_MAX: int = 512
_URL_MAX: int = 2048


def _validate_lang(value: str) -> str:
    """Reject any language code outside the supported set.

    Args:
        value: Candidate language code.

    Returns:
        str: The validated code.

    Raises:
        ValueError: If the code is not one of :data:`ALLOWED_LANGS`.
    """
    if value not in ALLOWED_LANGS:
        raise ValueError(f"lang must be one of {sorted(ALLOWED_LANGS)}")
    return value


def _validate_link(value: str | None) -> str | None:
    """Normalize and validate a banner link target.

    Accepts an internal absolute path (``/ru/c/dom``) or an ``https://`` URL.
    Everything else is refused — notably ``javascript:``/``data:``, which would
    otherwise be a staff-typed script on the homepage, and bare ``http://``,
    which would downgrade the connection.

    Args:
        value: Candidate link, or ``None`` for a non-clickable banner.

    Returns:
        str | None: The trimmed link, or ``None``.

    Raises:
        ValueError: If the link is neither an internal path nor an https URL.
    """
    if value is None:
        return None
    link = value.strip()
    if not link:
        return None
    # A protocol-relative URL ("//evil.tld") is an external link in disguise.
    if link.startswith("//"):
        raise ValueError("link_url must be an internal path or an https:// URL")
    if link.startswith("/"):
        return link
    if link.startswith("https://") and len(link) > len("https://"):
        return link
    raise ValueError("link_url must be an internal path or an https:// URL")


def _require_both_langs(
    translations: list["BannerTranslationIn"],
) -> list["BannerTranslationIn"]:
    """Require exactly one translation per supported language.

    Args:
        translations: The submitted per-language creatives.

    Returns:
        list[BannerTranslationIn]: The validated translations.

    Raises:
        ValueError: If a language is missing or duplicated.
    """
    langs = [tr.lang for tr in translations]
    if sorted(langs) != sorted(ALLOWED_LANGS):
        raise ValueError(
            f"translations must cover exactly {sorted(ALLOWED_LANGS)} "
            "(one entry per language)"
        )
    return translations


# --------------------------------------------------------------------------- #
# Public
# --------------------------------------------------------------------------- #
class BannerOut(BaseModel):
    """One carousel slide for the requested language."""

    id: int
    image_url: str
    image_mobile_url: str | None = None
    alt: str
    title: str | None = None
    subtitle: str | None = None
    cta_label: str | None = None
    link_url: str | None = None


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class BannerTranslationIn(BaseModel):
    """The creative and copy for one language."""

    lang: str
    image_url: str = Field(min_length=1, max_length=_URL_MAX)
    image_mobile_url: str | None = Field(default=None, max_length=_URL_MAX)
    alt: str = Field(min_length=1, max_length=_ALT_MAX)
    title: str | None = Field(default=None, max_length=_TEXT_MAX)
    subtitle: str | None = Field(default=None, max_length=_TEXT_MAX)
    cta_label: str | None = Field(default=None, max_length=_TEXT_MAX)

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        return _validate_lang(value)

    @field_validator("image_url", "alt")
    @classmethod
    def _require_content(cls, value: str) -> str:
        """Reject a field that only looks filled in.

        ``min_length`` counts characters, so a field of spaces passes it — and a
        blank ``alt`` is exactly as useless to a screen reader as a missing one.
        """
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("title", "subtitle", "cta_label", "image_mobile_url")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """Treat an empty form field as absent, not as empty copy."""
        if value is None:
            return None
        text = value.strip()
        return text or None


class BannerTranslationOut(BaseModel):
    """A banner translation as returned to the back-office."""

    model_config = ConfigDict(from_attributes=True)

    lang: str
    image_url: str
    image_mobile_url: str | None = None
    alt: str
    title: str | None = None
    subtitle: str | None = None
    cta_label: str | None = None


class BannerAdminOut(BaseModel):
    """Full admin view of a banner (schedule + both creatives)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    is_active: bool
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    link_url: str | None = None
    translations: list[BannerTranslationOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class _BannerWrite(BaseModel):
    """Fields shared by create and update."""

    position: int = 0
    is_active: bool = False
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    link_url: str | None = Field(default=None, max_length=_URL_MAX)
    translations: list[BannerTranslationIn] = Field(min_length=1)

    @field_validator("link_url")
    @classmethod
    def _check_link(cls, value: str | None) -> str | None:
        return _validate_link(value)

    @model_validator(mode="after")
    def _check_payload(self) -> "_BannerWrite":
        _require_both_langs(self.translations)
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at < self.starts_at
        ):
            raise ValueError("ends_at must not be earlier than starts_at")
        return self


class BannerCreate(_BannerWrite):
    """Create a banner with both-language creatives."""


class BannerUpdate(_BannerWrite):
    """Full update of a banner (both-language creatives replaced)."""


class BannerReorderItem(BaseModel):
    """One ``(banner_id, position)`` assignment in a reorder request."""

    banner_id: int
    position: int


class BannerReorderRequest(BaseModel):
    """Bulk position update, so the back-office can drag the carousel order."""

    items: list[BannerReorderItem] = Field(min_length=1)
