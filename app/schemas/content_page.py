"""Pydantic v2 schemas for the content-page feature (CMS-lite, Phase 1).

Two contracts:

* **Public** — the compact list item and the rendered page detail the storefront
  needs (:class:`ContentPageListItem`, :class:`ContentPageDetail`).
* **Admin** — the full editable page with both-language translations
  (:class:`ContentPageAdminOut`, :class:`ContentPageCreate`,
  :class:`ContentPageUpdate`, :class:`ContentPageTranslationIn`/`Out`).

Validation enforced at the edge (so a bad payload fails with 422, never a DB
constraint): ``slug`` matches the shared clean-slug pattern; ``lang`` is one of
:data:`~app.models.content_page.ALLOWED_LANGS`; and create/update must carry
BOTH ru and ro translations (with non-empty ``title``/``body``) so the storefront
hreflang always resolves.
"""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.content_page import ALLOWED_LANGS

# Length bounds so payloads fail fast with a clear 422 instead of hitting the DB.
_SLUG_MIN: int = 2
_SLUG_MAX: int = 255
_TITLE_MAX: int = 512

# A URL slug must be lowercase alphanumeric words joined by single hyphens
# (no leading/trailing/double hyphens). Reused from the catalog admin schema.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _validate_slug(value: str) -> str:
    """Normalize and validate a URL slug.

    Whitespace is trimmed and the value is lowercased, then it must match a clean
    slug pattern and be at least two characters — so links can never degrade to a
    single letter or digit.

    Args:
        value: Candidate slug.

    Returns:
        str: The normalized, validated slug.

    Raises:
        ValueError: If the slug is too short or not a clean hyphenated slug.
    """
    slug = value.strip().lower()
    if len(slug) < _SLUG_MIN:
        raise ValueError(f"slug must be at least {_SLUG_MIN} characters")
    if not _SLUG_RE.match(slug):
        raise ValueError(
            "slug must be lowercase alphanumeric words separated by single hyphens"
        )
    return slug


def _validate_lang(value: str) -> str:
    """Reject any language code outside the supported set.

    Args:
        value: Candidate language code.

    Returns:
        str: The validated language code.

    Raises:
        ValueError: If the code is not one of :data:`ALLOWED_LANGS`.
    """
    if value not in ALLOWED_LANGS:
        raise ValueError(f"lang must be one of {sorted(ALLOWED_LANGS)}")
    return value


def _require_both_langs(
    translations: list["ContentPageTranslationIn"],
) -> list["ContentPageTranslationIn"]:
    """Require exactly one translation per supported language (ru + ro).

    Args:
        translations: The submitted per-language translations.

    Returns:
        list[ContentPageTranslationIn]: The validated translations.

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
class ContentPageListItem(BaseModel):
    """A footer/list entry the storefront links to (one language)."""

    slug: str
    title: str


class ContentPageDetail(BaseModel):
    """A fully rendered content page for one language."""

    slug: str
    title: str
    body: str
    seo_description: str | None = None


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class ContentPageTranslationIn(BaseModel):
    """A single per-language content payload (title + Markdown body + SEO)."""

    lang: str
    title: str = Field(min_length=1, max_length=_TITLE_MAX)
    body: str = Field(min_length=1)
    seo_description: str | None = None

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        return _validate_lang(value)


class ContentPageTranslationOut(BaseModel):
    """A page translation as returned to the back-office."""

    model_config = ConfigDict(from_attributes=True)

    lang: str
    title: str
    body: str
    seo_description: str | None = None


class ContentPageAdminOut(BaseModel):
    """Full admin view of a content page (structure + translations)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    is_published: bool
    show_in_footer: bool
    position: int
    translations: list[ContentPageTranslationOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ContentPageCreate(BaseModel):
    """Create a content page with both-language translations."""

    slug: str = Field(min_length=_SLUG_MIN, max_length=_SLUG_MAX)
    is_published: bool = False
    show_in_footer: bool = True
    position: int = 0
    translations: list[ContentPageTranslationIn] = Field(min_length=1)

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        return _validate_slug(value)

    @model_validator(mode="after")
    def _check_langs(self) -> "ContentPageCreate":
        _require_both_langs(self.translations)
        return self


class ContentPageUpdate(BaseModel):
    """Full update of a content page (both-language translations replaced)."""

    slug: str = Field(min_length=_SLUG_MIN, max_length=_SLUG_MAX)
    is_published: bool = False
    show_in_footer: bool = True
    position: int = 0
    translations: list[ContentPageTranslationIn] = Field(min_length=1)

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        return _validate_slug(value)

    @model_validator(mode="after")
    def _check_langs(self) -> "ContentPageUpdate":
        _require_both_langs(self.translations)
        return self
