"""Shared request parameter types for the API routers.

Found by fuzzing (Phase 7): ids larger than PostgreSQL's `integer` made the database raise
"integer out of range", a 500. Every id is now bounded to the column type, so an impossible id
is a 422 with the documented error shape, and anything else a plain 404.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Path, Query
from fastapi.exceptions import RequestValidationError

MAX_ID = 2**31 - 1  # PostgreSQL `integer`, the type of every primary key

Id = Annotated[int, Path(ge=1, le=MAX_ID)]
IdQuery = Annotated[int | None, Query(ge=1, le=MAX_ID)]


def invalid(location: tuple[str, ...], message: str, value: Any = None) -> RequestValidationError:
    """A 422 shaped like FastAPI's own validation errors (HTTPValidationError, `detail` a
    list), so every 422 in openapi.json means the same thing."""
    return RequestValidationError(
        [{"type": "value_error", "loc": location, "msg": message, "input": value}]
    )
