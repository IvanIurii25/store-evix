"""Auth business logic: register, login, refresh rotation, logout, profile, addresses (§7).

Rules enforced here (not in routers, not in the repo):

* Registration rejects duplicate email/phone (409).
* Login verifies the argon2 hash; bad credentials -> 401 (unified envelope).
* Refresh checks the token is not blacklisted, then **rotates**: the old refresh
  ``jti`` is blacklisted and a fresh pair is issued.
* Logout blacklists the refresh ``jti`` with a TTL equal to the token's remaining
  lifetime (§7). The blacklist lives in Redis, keyed by ``jti``.
* Addresses: at most one ``is_default`` per user.
"""

import secrets
import time

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    InvalidTokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import Address, AppUser
from app.repositories.cart_repo import CartRepository
from app.repositories.restock_repo import RestockRepository
from app.repositories.user_repo import UserRepository
from app.schemas.auth import (
    AddressCreate,
    AddressUpdate,
    LoginRequest,
    RegisterRequest,
    TokenPair,
    UserUpdate,
)

_BLACKLIST_PREFIX = "auth:blacklist:refresh:"

# Precomputed argon2 hash verified against when the login user is unknown, so a
# failed login costs one argon2 verify whether or not the email/phone exists —
# closing the response-time oracle that would otherwise reveal registered
# identifiers.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


class AuthService:
    """Coordinates auth use-cases over the user repo and the Redis blacklist.

    The DB session and Redis client are injected once and reused (§15).
    """

    def __init__(self, session: AsyncSession, redis: Redis) -> None:
        """Wire the service to its collaborators.

        Args:
            session: Active async DB session (owns the transaction).
            redis: Async Redis client backing the refresh blacklist.
        """
        self.session = session
        self.redis = redis
        self.repo = UserRepository(session)

    async def register(self, data: RegisterRequest) -> AppUser:
        """Register a new user, rejecting duplicate email/phone.

        Args:
            data: Registration payload.

        Returns:
            AppUser: The created user.

        Raises:
            HTTPException: 409 if the email or phone is already taken.
        """
        if await self.repo.get_by_email(data.email):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            )
        if data.phone and await self.repo.get_by_phone(data.phone):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Phone already registered",
            )
        user = await self.repo.create_user(
            email=data.email,
            password_hash=hash_password(data.password),
            phone=data.phone,
        )
        await self.session.commit()
        return user

    async def login(self, data: LoginRequest) -> TokenPair:
        """Authenticate by email or phone and issue a token pair.

        Args:
            data: Login payload (exactly one of email/phone + password).

        Returns:
            TokenPair: Fresh access + refresh tokens.

        Raises:
            HTTPException: 401 on unknown user, inactive account, or bad password.
        """
        user = await self._resolve_login_user(data)
        # Always run one argon2 verify (against a dummy hash for an unknown user)
        # so the response time does not reveal whether the identifier exists.
        password_hash = user.password_hash if user else _DUMMY_PASSWORD_HASH
        password_ok = verify_password(data.password, password_hash)
        if user is None or not password_ok or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        return self._issue_pair(user.id)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Validate + rotate a refresh token, blacklisting the old one.

        Args:
            refresh_token: The current refresh token.

        Returns:
            TokenPair: A new access + refresh pair.

        Raises:
            HTTPException: 401 if the token is invalid or already revoked.
        """
        claims = self._decode_refresh(refresh_token)
        if await self._is_blacklisted(claims.jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token revoked",
            )
        await self._blacklist(claims.jti, claims.exp)
        return self._issue_pair(claims.sub)

    async def logout(self, refresh_token: str) -> None:
        """Revoke a refresh token by blacklisting its ``jti``.

        Args:
            refresh_token: The refresh token to invalidate.

        Raises:
            HTTPException: 401 if the token is invalid.
        """
        claims = self._decode_refresh(refresh_token)
        await self._blacklist(claims.jti, claims.exp)

    async def get_me(self, user_id: int) -> AppUser:
        """Return the user record or raise 404 (should not happen for a valid token)."""
        user = await self.repo.get_by_id(user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        return user

    async def update_me(self, user_id: int, data: UserUpdate) -> AppUser:
        """Apply a partial profile update, rejecting a duplicate phone.

        Args:
            user_id: The authenticated user's id.
            data: Fields to update (only set fields are applied).

        Returns:
            AppUser: The updated user.

        Raises:
            HTTPException: 409 if the new phone belongs to another user.
        """
        user = await self.get_me(user_id)
        changes = data.model_dump(exclude_unset=True)
        new_phone = changes.get("phone")
        if new_phone and new_phone != user.phone:
            existing = await self.repo.get_by_phone(new_phone)
            if existing and existing.id != user.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Phone already registered",
                )
        for field, value in changes.items():
            setattr(user, field, value)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def list_addresses(self, user_id: int) -> list[Address]:
        """Return all of the user's addresses."""
        return await self.repo.list_addresses(user_id)

    async def create_address(self, user_id: int, data: AddressCreate) -> Address:
        """Create an address, keeping at most one default per user.

        Args:
            user_id: Owner user id.
            data: Address payload.

        Returns:
            Address: The created address.
        """
        if data.is_default:
            await self.repo.clear_default(user_id)
        address = Address(
            user_id=user_id,
            full_name=data.full_name,
            phone=data.phone,
            city=data.city,
            street=data.street,
            zip=data.zip,
            is_default=data.is_default,
        )
        address = await self.repo.add_address(address)
        await self.session.commit()
        await self.session.refresh(address)
        return address

    async def update_address(
        self,
        user_id: int,
        address_id: int,
        data: AddressUpdate,
    ) -> Address:
        """Apply a partial address update, keeping one default per user.

        Args:
            user_id: Owner user id.
            address_id: Target address id.
            data: Fields to update.

        Returns:
            Address: The updated address.

        Raises:
            HTTPException: 404 if the address is not owned by the user.
        """
        address = await self._get_owned_address(user_id, address_id)
        changes = data.model_dump(exclude_unset=True)
        if changes.get("is_default") is True and not address.is_default:
            await self.repo.clear_default(user_id)
        for field, value in changes.items():
            setattr(address, field, value)
        await self.session.commit()
        await self.session.refresh(address)
        return address

    async def delete_address(self, user_id: int, address_id: int) -> None:
        """Delete one of the user's addresses.

        Args:
            user_id: Owner user id.
            address_id: Target address id.

        Raises:
            HTTPException: 404 if the address is not owned by the user.
        """
        address = await self._get_owned_address(user_id, address_id)
        await self.repo.delete_address(address)
        await self.session.commit()

    async def anonymize_account(self, user_id: int) -> None:
        """Erase a user account — right to erasure (LP195/2024 Art.17).

        Scrubs the profile in place (email/phone → non-identifying placeholders,
        password replaced by an unguessable random hash, account deactivated so
        no existing session survives — ``current_user`` rejects inactive users)
        and deletes the non-essential personal data: addresses, restock
        subscriptions and carts. **Orders are kept** — their retention is a lawful
        accounting/fiscal obligation (Art.17(3)(b)); the anonymized, inactive
        account can no longer sign in to reach them.

        Args:
            user_id: The account to erase (the authenticated caller).
        """
        user = await self.repo.get_by_id(user_id)
        if user is None:  # pragma: no cover - guarded by current_user
            return
        await self.repo.delete_all_addresses(user_id)
        await RestockRepository(self.session).delete_all_for_user(user_id)
        await CartRepository(self.session).delete_all_for_user(user_id)
        user.email = f"deleted-{user.id}@anonymized.local"
        user.phone = None
        user.password_hash = hash_password(secrets.token_urlsafe(32))
        user.is_active = False
        user.is_staff = False
        user.role = None
        await self.session.commit()

    async def _resolve_login_user(self, data: LoginRequest) -> AppUser | None:
        """Look up the login user by whichever identifier was supplied."""
        if data.email:
            return await self.repo.get_by_email(data.email)
        return await self.repo.get_by_phone(data.phone)

    async def _get_owned_address(self, user_id: int, address_id: int) -> Address:
        """Fetch an address owned by the user or raise 404."""
        address = await self.repo.get_address(user_id, address_id)
        if address is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Address not found",
            )
        return address

    def _issue_pair(self, user_id: int) -> TokenPair:
        """Mint a fresh access + refresh token pair for ``user_id``."""
        access = create_access_token(user_id)
        refresh, _ = create_refresh_token(user_id)
        return TokenPair(access=access, refresh=refresh)

    @staticmethod
    def _decode_refresh(refresh_token: str):
        """Decode a refresh token or raise 401 on any failure."""
        try:
            return decode_token(refresh_token, TokenType.REFRESH)
        except InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            ) from exc

    async def _is_blacklisted(self, jti: str) -> bool:
        """Return whether a refresh ``jti`` is currently revoked."""
        return await self.redis.exists(f"{_BLACKLIST_PREFIX}{jti}") == 1

    async def _blacklist(self, jti: str, exp: int) -> None:
        """Blacklist a refresh ``jti`` until its own expiry (TTL = remaining life)."""
        ttl = exp - int(time.time())
        if ttl <= 0:
            return
        await self.redis.set(f"{_BLACKLIST_PREFIX}{jti}", "1", ex=ttl)
