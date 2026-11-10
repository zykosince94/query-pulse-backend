from __future__ import annotations

import enum

from sqlalchemy import Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from query_pulse.db import Base, TimestampMixin


class Role(enum.StrEnum):
    ADMIN = "admin"
    DEMO = "demo"  # read-only: every write endpoint returns 403


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", values_callable=lambda roles: [r.value for r in roles])
    )

    @property
    def is_read_only(self) -> bool:
        return self.role is Role.DEMO
