"""Content-page domain ORM models (CMS-lite info/legal pages, Phase 1).

Manager-editable static pages the storefront renders (e.g. ``about``,
``delivery``, ``terms``). Mirrors the catalog translation-table idiom (§2.1.1):

* Structural / publication fields (``slug``, ``is_published``, ``show_in_footer``,
  ``position``) live on the main :class:`ContentPage` table.
* Translatable content (``title``, ``body`` Markdown, ``seo_description``) lives
  in :class:`ContentPageTranslation`, keyed by ``(page_id, lang)``.
* ``lang`` is plain text constrained to :data:`ALLOWED_LANGS` via a
  ``CheckConstraint`` (no PostgreSQL ENUM type).

The ``slug`` is unique on the page (one URL per page across languages); it is not
duplicated onto the translation, so there is no ``(lang, slug)`` unique index
here — page-level slug uniqueness is sufficient.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

# Supported languages for v1 (ru + ro). Applied as a CheckConstraint on the
# ``lang`` column instead of a PostgreSQL ENUM (mirrors catalog §2.1.1).
ALLOWED_LANGS: tuple[str, ...] = ("ru", "ro")

# Rendered SQL fragment for the ``lang`` CheckConstraint, e.g. ``'ru', 'ro'``.
_LANG_IN = ", ".join(f"'{lang}'" for lang in ALLOWED_LANGS)


class ContentPage(Base, TimestampMixin):
    """A manager-editable static content page (info/legal)."""

    __tablename__ = "content_page"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    show_in_footer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    translations: Mapped[list["ContentPageTranslation"]] = relationship(
        back_populates="page",
        cascade="all, delete-orphan",
    )


class ContentPageTranslation(Base):
    """Per-language content of a page (title + Markdown body + SEO)."""

    __tablename__ = "content_page_translation"
    __table_args__ = (
        PrimaryKeyConstraint("page_id", "lang"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
    )

    page_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("content_page.id", ondelete="CASCADE"),
        nullable=False,
    )
    lang: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    seo_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    page: Mapped["ContentPage"] = relationship(back_populates="translations")
