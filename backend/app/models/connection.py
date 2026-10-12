import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UuidPkMixin, str_enum


class DbKind(str, enum.Enum):
    ORACLE = "oracle"
    POSTGRES = "postgres"


class Connection(UuidPkMixin, TimestampMixin, Base):
    """A saved Oracle or Postgres endpoint. The password is stored Fernet-encrypted."""

    __tablename__ = "connections"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[DbKind] = mapped_column(str_enum(DbKind))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int]
    # Oracle: service name (or SID when options.use_sid). Postgres: database name.
    database: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(128))
    password_encrypted: Mapped[str] = mapped_column(Text)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)

    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None]
    last_test_message: Mapped[str | None] = mapped_column(Text)
    server_version: Mapped[str | None] = mapped_column(String(255))
    # Capabilities found by the last test, e.g. DBA_* view access or PG extensions.
    capabilities: Mapped[dict[str, Any]] = mapped_column(default=dict)
