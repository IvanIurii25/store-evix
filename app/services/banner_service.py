"""Banner service: the public carousel read + back-office CRUD (P0).

Owns the display-window rule and committing; the schema layer has already
guaranteed both languages, a safe ``link_url`` and a sane date range, so what is
left here is existence and ordering.
"""

from datetime import UTC, datetime

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models.banner import Banner, BannerTranslation
from app.repositories.banner_repo import BannerRepository
from app.schemas.banner import (
    BannerCreate,
    BannerOut,
    BannerReorderRequest,
    BannerTranslationIn,
    BannerUpdate,
)


class BannerError(DomainError):
    """Base class for banner domain errors (rendered by the unified handler)."""

    code: str = "banner_error"


class BannerNotFoundError(BannerError):
    """The referenced banner does not exist (renders as 404 ``not_found``)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class BannerService:
    """Public carousel reads and back-office CRUD for banners."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session
        self.repo = BannerRepository(session)

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #
    async def list_live(
        self, lang: str, now: datetime | None = None
    ) -> list[BannerOut]:
        """Return the carousel a visitor should see, in display order.

        Args:
            lang: Requested language code.
            now: Evaluation moment; defaults to the current UTC time (injected
                by tests so a schedule can be exercised without waiting).

        Returns:
            list[BannerOut]: Ordered slides; empty when nothing is scheduled, in
            which case the storefront falls back to its static hero.
        """
        moment = now if now is not None else datetime.now(UTC)
        pairs = await self.repo.list_live(lang, moment)
        return [
            BannerOut(
                id=banner.id,
                image_url=translation.image_url,
                image_mobile_url=translation.image_mobile_url,
                alt=translation.alt,
                title=translation.title,
                subtitle=translation.subtitle,
                cta_label=translation.cta_label,
                # Ссылка языка важнее общей: пути витрины содержат локаль, и
                # общая ссылка увела бы половину посетителей на чужой язык.
                link_url=translation.link_url or banner.link_url,
            )
            for banner, translation in pairs
        ]

    # ------------------------------------------------------------------ #
    # Admin
    # ------------------------------------------------------------------ #
    async def list_all(self) -> list[Banner]:
        """Return every banner (any state) with translations, in display order."""
        return await self.repo.list_all()

    async def get(self, banner_id: int) -> Banner:
        """Return one banner with its translations.

        Args:
            banner_id: Banner primary key.

        Returns:
            Banner: The banner.

        Raises:
            BannerNotFoundError: If no such banner exists.
        """
        banner = await self.repo.get(banner_id)
        if banner is None:
            raise BannerNotFoundError(f"Banner {banner_id} not found")
        return banner

    async def create(self, payload: BannerCreate) -> Banner:
        """Create a banner with both-language creatives.

        Args:
            payload: Validated create payload.

        Returns:
            Banner: The persisted banner with translations loaded.
        """
        banner = Banner(
            position=payload.position,
            is_active=payload.is_active,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            link_url=payload.link_url,
            translations=[self._translation(tr) for tr in payload.translations],
        )
        await self.repo.add(banner)
        await self.session.commit()
        return await self.get(banner.id)

    async def update(self, banner_id: int, payload: BannerUpdate) -> Banner:
        """Replace a banner's schedule and both creatives.

        Translations are replaced wholesale rather than merged: the editor always
        submits both languages, and a merge would silently keep a creative the
        manager thought they had removed.

        Args:
            banner_id: Banner to update.
            payload: Validated update payload.

        Returns:
            Banner: The updated banner.

        Raises:
            BannerNotFoundError: If no such banner exists.
        """
        banner = await self.get(banner_id)
        banner.position = payload.position
        banner.is_active = payload.is_active
        banner.starts_at = payload.starts_at
        banner.ends_at = payload.ends_at
        banner.link_url = payload.link_url

        await self.repo.clear_translations(banner_id)
        await self.session.flush()
        banner.translations = [self._translation(tr) for tr in payload.translations]
        await self.session.commit()
        return await self.get(banner_id)

    async def delete(self, banner_id: int) -> None:
        """Delete a banner and its creatives.

        Args:
            banner_id: Banner to remove.

        Raises:
            BannerNotFoundError: If no such banner exists.
        """
        banner = await self.get(banner_id)
        await self.repo.delete(banner)
        await self.session.commit()

    async def reorder(self, payload: BannerReorderRequest) -> list[Banner]:
        """Apply new positions in one write.

        Unknown ids are refused rather than ignored, so a stale editor cannot
        silently drop a banner out of the order it thinks it just saved.

        Args:
            payload: The ``(banner_id, position)`` assignments.

        Returns:
            list[Banner]: Every banner in the new display order.

        Raises:
            BannerNotFoundError: If any id does not exist.
        """
        wanted = {item.banner_id: item.position for item in payload.items}
        banners = await self.repo.get_many(list(wanted))
        missing = set(wanted) - {banner.id for banner in banners}
        if missing:
            raise BannerNotFoundError(
                f"Banner(s) not found: {', '.join(str(i) for i in sorted(missing))}"
            )
        for banner in banners:
            banner.position = wanted[banner.id]
        await self.session.commit()
        return await self.list_all()

    @staticmethod
    def _translation(payload: BannerTranslationIn) -> BannerTranslation:
        """Build a translation row from its validated payload."""
        return BannerTranslation(
            lang=payload.lang,
            image_url=payload.image_url,
            image_mobile_url=payload.image_mobile_url,
            alt=payload.alt,
            link_url=payload.link_url,
            title=payload.title,
            subtitle=payload.subtitle,
            cta_label=payload.cta_label,
        )
