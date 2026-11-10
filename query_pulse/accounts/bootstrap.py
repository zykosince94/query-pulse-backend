"""Create or update the admin and optional demo user from settings.

Run with ``python -m query_pulse.accounts.bootstrap`` after migrations. It is idempotent:
re-running it resets passwords and roles to what the environment says, and removes the demo
user when ``QP_DEMO_ENABLED`` is false.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.accounts.models import Role, User
from query_pulse.accounts.passwords import hash_password, verify_password
from query_pulse.config import Settings, get_settings
from query_pulse.db import create_engine, create_sessionmaker

logger = logging.getLogger(__name__)


async def _upsert(session: AsyncSession, username: str, password: str, role: Role) -> str:
    user = await session.scalar(select(User).where(User.username == username))
    if user is None:
        session.add(User(username=username, password_hash=hash_password(password), role=role))
        return "created"
    changed = False
    if not verify_password(user.password_hash, password):
        user.password_hash = hash_password(password)
        changed = True
    if user.role is not role:
        user.role = role
        changed = True
    return "updated" if changed else "unchanged"


async def bootstrap_users(session: AsyncSession, settings: Settings) -> dict[str, str]:
    result = {
        settings.admin_username: await _upsert(
            session, settings.admin_username, settings.admin_password.get_secret_value(), Role.ADMIN
        )
    }

    if settings.demo_enabled and settings.demo_password is not None:
        result[settings.demo_username] = await _upsert(
            session, settings.demo_username, settings.demo_password.get_secret_value(), Role.DEMO
        )
    else:
        removed = await session.execute(delete(User).where(User.role == Role.DEMO))
        if getattr(removed, "rowcount", 0):
            result["demo users"] = "removed"

    await session.commit()
    return result


async def _main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with create_sessionmaker(engine)() as session:
            for username, outcome in (await bootstrap_users(session, settings)).items():
                logger.info("bootstrap_users: %s %s", username, outcome)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_main())
