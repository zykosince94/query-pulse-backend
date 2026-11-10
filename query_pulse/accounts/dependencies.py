"""Access guards used by every router.

- ``current_user``: any signed-in user, otherwise 401.
- ``require_writer``: a signed-in user who may change state, otherwise 403 (demo user).

Every write endpoint must depend on ``require_writer``. ``tests/test_access.py`` walks the
OpenAPI spec and fails if any write endpoint lets the demo user through (QP-AC-03).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.accounts.models import User
from query_pulse.db import get_session

SESSION_USER_KEY = "user_id"

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_user(request: Request, session: SessionDep) -> User:
    user_id = request.session.get(SESSION_USER_KEY)
    user = await session.get(User, user_id) if isinstance(user_id, int) else None
    if user is None:
        request.session.clear()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_writer(user: CurrentUser) -> User:
    if user.is_read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="The demo user is read-only")
    return user


Writer = Annotated[User, Depends(require_writer)]
