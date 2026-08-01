"""Homepage banner ORM models (manager-editable carousel, P0).

Mirrors the CMS-lite idiom of :mod:`app.models.content_page`:

* Scheduling and placement (``position``, ``is_active``, ``starts_at``,
  ``ends_at``, ``link_url``) live on :class:`Banner`.
* Everything a visitor sees lives per-language in :class:`BannerTranslation`,
  keyed by ``(banner_id, lang)``.

Two deliberate shapes:

* **The creative is per language, not global.** A baked-in-text poster differs
  between ru and ro; a text-free background can simply be uploaded twice. Making
  the image translatable supports both without a second schema.
* **Copy is optional.** With ``title``/``subtitle``/``cta_label`` empty the slide
  is a bare poster (the reference storefront's model); filled in, the storefront
  renders real text over the image — which is indexable, translatable without a
  designer, and readable by a screen reader.

``alt`` is NOT optional: it is the only thing a screen reader and an image search
have to go on, and the reference site's habit of reusing the site title for every
slide is what this avoids.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

# Supported languages for v1 (ru + ro), as a CheckConstraint rather than a
# PostgreSQL ENUM (catalog §2.1.1).
ALLOWED_LANGS: tuple[str, ...] = ("ru", "ro")

_LANG_IN = ", ".join(f"'{lang}'" for lang in ALLOWED_LANGS)


class Banner(Base, TimestampMixin):
    """One slide of the homepage carousel, with its display window."""

    __tablename__ = "banner"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Display window. NULL on either side means "open-ended" — a banner with no
    # dates behaves exactly like a plain on/off switch, so scheduling costs the
    # manager nothing when they do not need it.
    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Where the slide points. Validated at the schema edge to an internal path or
    # an https URL; NULL renders a non-clickable slide.
    link_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    translations: Mapped[list["BannerTranslation"]] = relationship(
        back_populates="banner",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class BannerTranslation(Base):
    """Per-language creative and copy for one banner."""

    __tablename__ = "banner_translation"
    __table_args__ = (
        PrimaryKeyConstraint("banner_id", "lang"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
    )

    banner_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("banner.id", ondelete="CASCADE"),
        nullable=False,
    )
    lang: Mapped[str] = mapped_column(String, nullable=False)

    # Desktop creative (required) and the narrow-screen one (optional): a wide
    # hero crop is unreadable on a phone, so the storefront swaps art rather than
    # scaling it down.
    image_url: Mapped[str] = mapped_column(Text, nullable=False)
    image_mobile_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    alt: Mapped[str] = mapped_column(Text, nullable=False)

    # Per-language target. Storefront paths carry the locale (/ru/p/… vs /ro/p/…),
    # so a bilingual banner needs its own link per language; ``Banner.link_url``
    # stays as the fallback for banners that point somewhere language-neutral.
    link_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional overlay copy — empty means the creative speaks for itself.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtitle: Mapped[str | None] = mapped_column(Text, nullable=True)
    cta_label: Mapped[str | None] = mapped_column(Text, nullable=True)

    banner: Mapped["Banner"] = relationship(back_populates="translations")
