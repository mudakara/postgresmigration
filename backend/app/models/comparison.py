import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UuidPkMixin, str_enum
from app.models.inventory import ScanStatus


class TargetObject(UuidPkMixin, TimestampMixin, Base):
    """An object in the migrated PostgreSQL database (function, procedure, view, trigger)."""

    __tablename__ = "target_objects"
    __table_args__ = (
        UniqueConstraint("project_id", "schema", "kind", "name", "identity_args", name="uq_target_objects_identity"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    schema: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(20))  # function | procedure | view | materialized view | trigger
    identity_args: Mapped[str] = mapped_column(Text, default="")  # disambiguates overloads
    result_type: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(64))
    arguments: Mapped[list[Any]] = mapped_column(default=list)
    n_defaults: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    # plpgsql_check messages, when the extension is installed in the target.
    check_results: Mapped[list[Any]] = mapped_column(default=list)
    last_seen_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("comparison_runs.id", ondelete="SET NULL"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def display_name(self) -> str:
        args = f"({self.identity_args})" if self.kind in ("function", "procedure") else ""
        return f"{self.schema}.{self.name}{args}"


class ComparisonRun(UuidPkMixin, Base):
    """One pass that reads the PostgreSQL catalog, pairs objects and runs static checks."""

    __tablename__ = "comparison_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    status: Mapped[ScanStatus] = mapped_column(str_enum(ScanStatus), default=ScanStatus.QUEUED)
    target_version: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObjectPair(UuidPkMixin, TimestampMixin, Base):
    """An Oracle unit (routine, view or trigger) and its PostgreSQL counterpart."""

    __tablename__ = "object_pairs"
    __table_args__ = (UniqueConstraint("project_id", "source_key", name="uq_object_pairs_source"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    # "routine:<uuid>" or "object:<uuid>"; stable across scans because inventory ids are.
    source_key: Mapped[str] = mapped_column(String(64))
    unit_kind: Mapped[str] = mapped_column(String(20))  # routine | view | trigger
    source_routine_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("source_routines.id", ondelete="CASCADE"))
    # The object holding the Oracle code (the package body for package members).
    source_object_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_objects.id", ondelete="CASCADE"), index=True)
    oracle_name: Mapped[str] = mapped_column(String(300))
    target_object_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("target_objects.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[str] = mapped_column(String(24))  # matched | missing_in_target | ignored
    match_method: Mapped[str | None] = mapped_column(String(32))
    match_confidence: Mapped[float | None] = mapped_column(Float)
    manual: Mapped[bool] = mapped_column(default=False)  # a person chose this match; auto-pairing keeps it
    alternatives: Mapped[list[Any]] = mapped_column(default=list)  # other plausible target ids

    signature_status: Mapped[str | None] = mapped_column(String(16))  # match | compatible | mismatch
    signature_diffs: Mapped[list[Any]] = mapped_column(default=list)
    static_findings: Mapped[list[Any]] = mapped_column(default=list)
    risk: Mapped[str] = mapped_column(String(8), default="none")  # high | medium | low | none

    oracle_hash: Mapped[str | None] = mapped_column(String(64))
    target_hash: Mapped[str | None] = mapped_column(String(64))
    ai_review: Mapped[dict[str, Any] | None]
    ai_verdict: Mapped[str | None] = mapped_column(String(24))  # mirrored for filtering
    ai_stale: Mapped[bool] = mapped_column(default=False)  # code changed since the review

    review_status: Mapped[str] = mapped_column(String(16), default="unreviewed")
    review_note: Mapped[str | None] = mapped_column(Text)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("comparison_runs.id", ondelete="SET NULL"))
