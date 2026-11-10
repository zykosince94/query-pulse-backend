from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from query_pulse.accounts.dependencies import SESSION_USER_KEY, CurrentUser, SessionDep
from query_pulse.accounts.models import Role, User
from query_pulse.accounts.passwords import hash_password, needs_rehash, verify_password
from query_pulse.config import Settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    username: str
    role: Role
    read_only: bool

    @classmethod
    def from_user(cls, user: User) -> UserOut:
        return cls(username=user.username, role=user.role, read_only=user.is_read_only)


@router.post("/login", response_model=UserOut, operation_id="login")
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> UserOut:
    user = await session.scalar(select(User).where(User.username == payload.username))
    if not verify_password(user.password_hash if user else None, payload.password) or not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        await session.commit()

    return _start_session(request, user)


class AuthConfig(BaseModel):
    demo_enabled: bool


@router.get("/config", response_model=AuthConfig, operation_id="getAuthConfig")
async def auth_config(request: Request) -> AuthConfig:
    """Public: what the sign-in screen should offer."""
    settings: Settings = request.app.state.settings
    return AuthConfig(demo_enabled=settings.demo_enabled)


@router.post("/demo", response_model=UserOut, operation_id="loginAsDemo")
async def login_as_demo(request: Request, session: SessionDep) -> UserOut:
    """Public: sign in as the read-only demo user without a password ("Try the demo").

    Safe because the demo user can't change anything (every write returns 403) and the
    endpoint only works when the operator enabled the demo with QP_DEMO_ENABLED.
    """
    settings: Settings = request.app.state.settings
    user = (
        await session.scalar(select(User).where(User.role == Role.DEMO))
        if settings.demo_enabled
        else None
    )
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="The demo is not enabled")
    return _start_session(request, user)


def _start_session(request: Request, user: User) -> UserOut:
    # Start a fresh session on sign-in so a pre-login session can't be reused (fixation).
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id
    return UserOut.from_user(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, operation_id="logout")
async def logout(request: Request) -> None:
    request.session.clear()


@router.get("/me", response_model=UserOut, operation_id="getCurrentUser")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.from_user(user)
