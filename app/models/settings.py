"""Site-settings domain model: :class:`SiteSetting` (admin §6.4).

A tiny key/value store for admin-editable, site-wide configuration that is not a
deploy-time secret (e.g. SEO defaults). Single-tenant, so this is a flat table
keyed by a string ``key`` with a text ``value`` — no tenant scoping. Values are
plain strings; the settings service owns the (de)serialization of typed groups
(like the SEO block) to/from these rows.
"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SiteSetting(Base, TimestampMixin):
    """One admin-editable site-wide setting, addressed by ``key`` (§6.4)."""

    __tablename__ = "site_setting"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
