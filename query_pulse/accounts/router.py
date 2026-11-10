from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from query_pulse.accounts.dependencies import SESSION_USER_KEY, CurrentUser, SessionDep
from query_pulse.accounts.models import Role, User
from query_pulse.accounts.passwords import hash_password, needs_rehash, verify_password

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
