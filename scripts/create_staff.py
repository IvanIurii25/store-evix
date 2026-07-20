"""Bootstrap / promote a staff (admin) user.

Solves the chicken-and-egg: the admin-API is guarded by ``is_staff``, but nothing
in the request flow can set that flag. Run this once against a migrated DB:

    uv run python scripts/create_staff.py --email admin@evix.md --password secret

If the email exists, it is promoted to staff (and password reset when given);
otherwise a new active staff user is created.
"""

import argparse
import asyncio
import os

from sqlalchemy import select

from app.core.db import session_factory
from app.core.security import hash_password
from app.models.user import AppUser


async def create_or_promote(email: str, password: str | None) -> str:
    async with session_factory() as session:
        user = await session.scalar(select(AppUser).where(AppUser.email == email))
        if user is None:
            if not password:
                raise SystemExit("New user requires --password")
            user = AppUser(
                email=email,
                password_hash=hash_password(password),
                is_active=True,
                is_staff=True,
            )
            session.add(user)
            action = "created"
        else:
            user.is_staff = True
            user.is_active = True
            if password:
                user.password_hash = hash_password(password)
            action = "promoted"
        await session.commit()
        return action


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or promote a staff user.")
    parser.add_argument("--email", default=os.environ.get("STAFF_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("STAFF_PASSWORD"))
    args = parser.parse_args()
    if not args.email:
        raise SystemExit("Provide --email (or STAFF_EMAIL)")
    action = asyncio.run(create_or_promote(args.email, args.password))
    print(f"Staff user {args.email} {action}.")


if __name__ == "__main__":
    main()
