"""Declarative base and shared column types for Control Plane ORM models (T09).

SQLAlchemy 2.0 style with typed ``Mapped[...]`` annotations. The ORM models
mirror the Pydantic entities in ``schemas/entities.py``; the schemas are the
single source of truth for field names and types, and the ORM is a mechanical
translation with an added primary-key index and foreign-key constraints.

Round 3: SQLite is the default backend (laptop vertical slice). The schema
stays Postgres-compatible — no SQLite-specific types, no pragmas at the model
layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, DateTime, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase


class UUIDString(TypeDecorator[UUID]):
    """Store UUIDs as 36-char strings for cross-DB portability.

    Postgres has a native UUID type, but SQLite does not. To keep the models
    identical across both backends, we serialize UUIDs to strings. This is a
    small performance cost that we accept in exchange for schema portability.
    """

    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, str):
            return str(UUID(value))
        raise TypeError(f"Expected UUID or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Any) -> UUID | None:
        if value is None:
            return None
        return UUID(value)


class Base(DeclarativeBase):
    """Declarative base for all Control Plane ORM models.

    We only register mappings for the custom ``UUID -> UUIDString`` type and
    ``datetime``. List/dict columns use explicit ``mapped_column(JSON, ...)``
    declarations in each model to avoid SQLAlchemy's inference quirks with
    generic collection types.
    """

    type_annotation_map = {
        UUID: UUIDString,
        datetime: DateTime(timezone=True),
        str: String(),
    }
