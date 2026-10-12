import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UuidPkMixin
from app.models.connection import Connection


class Project(UuidPkMixin, TimestampMixin, Base):
    """A migration workspace: an Oracle source, an optional Postgres target, and the
    Oracle schemas in scope. Scans, conversions and comparisons hang off a project."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    oracle_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT")
    )
    # Scenario 2 compares against this database; Scenario 1 may deploy to it.
    postgres_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT")
    )
    # Oracle owners (schemas) to scan, upper-case as stored in the data dictionary.
    schemas: Mapped[list[Any]] = mapped_column(default=list)
    # Per-project overrides, e.g. extra Ora2Pg directives.
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)

    oracle_connection: Mapped[Connection] = relationship(foreign_keys=[oracle_connection_id])
    postgres_connection: Mapped[Connection | None] = relationship(
        foreign_keys=[postgres_connection_id]
    )
