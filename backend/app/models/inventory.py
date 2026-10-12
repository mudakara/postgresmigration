import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UuidPkMixin, str_enum


class ScanStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Scan(UuidPkMixin, Base):
    """One pass over the Oracle data dictionary for a project."""

    __tablename__ = "scans"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    status: Mapped[ScanStatus] = mapped_column(str_enum(ScanStatus), default=ScanStatus.QUEUED)
    include_ora2pg_cost: Mapped[bool] = mapped_column(default=True)
    schemas: Mapped[list[Any]] = mapped_column(default=list)  # resolved schema list
    oracle_version: Mapped[str | None] = mapped_column(String(64))
    dictionary: Mapped[str | None] = mapped_column(String(3))  # DBA or ALL
    # Snapshot counts at the end of the scan (objects, added/changed/removed, findings...).
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Every object type in the scanned schemas (tables, sequences... too), for context.
    object_counts: Mapped[list[Any]] = mapped_column(default=list)
    ora2pg_reports: Mapped[list[Any]] = mapped_column(default=list)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceObject(UuidPkMixin, TimestampMixin, Base):
    """An Oracle schema object in the inventory (procedure, package, view...)."""

    __tablename__ = "source_objects"
    __table_args__ = (
        UniqueConstraint("project_id", "owner", "object_type", "name", name="uq_source_objects_identity"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    owner: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    object_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(10))  # Oracle's VALID / INVALID
    oracle_created: Mapped[datetime | None] = mapped_column(DateTime)
    oracle_last_ddl: Mapped[datetime | None] = mapped_column(DateTime)

    source: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(String(64))
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)

    findings: Mapped[list[Any]] = mapped_column(default=list)
    finding_codes: Mapped[list[Any]] = mapped_column(default=list)
    sys_packages: Mapped[list[Any]] = mapped_column(default=list)
    unsafe_to_execute: Mapped[bool] = mapped_column(default=False)
    wrapped: Mapped[bool] = mapped_column(default=False)
    ora2pg_cost: Mapped[float | None] = mapped_column(Float)
    effort: Mapped[float] = mapped_column(Float, default=0)  # Ora2Pg cost, else heuristic
    complexity: Mapped[str] = mapped_column(String(16), default="simple")

    first_seen_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scans.id", ondelete="SET NULL"))
    last_seen_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scans.id", ondelete="SET NULL"))
    # Scan in which the Oracle source last changed (or first appeared).
    source_changed_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scans.id", ondelete="SET NULL"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    routines: Mapped[list["SourceRoutine"]] = relationship(
        back_populates="object", cascade="all, delete-orphan", order_by="SourceRoutine.subprogram_id"
    )


class SourceRoutine(UuidPkMixin, Base):
    """A callable unit: a standalone procedure/function or a public package member."""

    __tablename__ = "source_routines"
    __table_args__ = (
        UniqueConstraint("object_id", "name", "overload", name="uq_source_routines_identity"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    object_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_objects.id", ondelete="CASCADE"), index=True)
    owner: Mapped[str] = mapped_column(String(128))
    package_name: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    overload: Mapped[str] = mapped_column(String(40), default="")
    subprogram_id: Mapped[int | None]
    kind: Mapped[str] = mapped_column(String(10))
    return_type: Mapped[str | None] = mapped_column(String(300))
    arguments: Mapped[list[Any]] = mapped_column(default=list)
    signature: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ora2pg_cost: Mapped[float | None] = mapped_column(Float)

    object: Mapped[SourceObject] = relationship(back_populates="routines")


class ObjectDependency(UuidPkMixin, Base):
    """An edge from DBA_DEPENDENCIES; replaced wholesale on each scan."""

    __tablename__ = "object_dependencies"
    __table_args__ = (Index("ix_object_dependencies_referenced", "referenced_object_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    object_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_objects.id", ondelete="CASCADE"), index=True)
    referenced_owner: Mapped[str] = mapped_column(String(128))
    referenced_name: Mapped[str] = mapped_column(String(128))
    referenced_type: Mapped[str | None] = mapped_column(String(30))
    referenced_link_name: Mapped[str | None] = mapped_column(String(128))
    # Set when the referenced object is itself in the inventory.
    referenced_object_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_objects.id", ondelete="SET NULL")
    )
