import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UuidPkMixin, str_enum
from app.models.inventory import ScanStatus


class CatalogSnapshot(UuidPkMixin, Base):
    """A read of one connection's tables, relationships and code, for the schema explorer."""

    __tablename__ = "catalog_snapshots"

    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    status: Mapped[ScanStatus] = mapped_column(str_enum(ScanStatus), default=ScanStatus.QUEUED)
    requested_schemas: Mapped[list[Any]] = mapped_column(default=list)
    schemas: Mapped[list[Any]] = mapped_column(default=list)  # what was actually read
    server_version: Mapped[str | None] = mapped_column(String(200))
    dictionary: Mapped[str | None] = mapped_column(String(3))  # Oracle: DBA or ALL
    # Counts, per schema and overall.
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CatalogTable(UuidPkMixin, Base):
    """A table or view in a snapshot (partitions are folded into their parent)."""

    __tablename__ = "catalog_tables"
    __table_args__ = (UniqueConstraint("snapshot_id", "schema", "name", name="uq_catalog_tables_identity"),)

    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_snapshots.id", ondelete="CASCADE"), index=True)
    schema: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    columns: Mapped[list[Any]] = mapped_column(default=list)
    primary_key: Mapped[dict[str, Any] | None]
    unique_keys: Mapped[list[Any]] = mapped_column(default=list)
    indexes: Mapped[list[Any]] = mapped_column(default=list)
    row_estimate: Mapped[int | None] = mapped_column(BigInteger)
    partitions: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Denormalised counts for the table list.
    column_count: Mapped[int] = mapped_column(Integer, default=0)
    references_count: Mapped[int] = mapped_column(Integer, default=0)  # foreign keys out
    referenced_by_count: Mapped[int] = mapped_column(Integer, default=0)  # foreign keys in
    code_count: Mapped[int] = mapped_column(Integer, default=0)
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    index_count: Mapped[int] = mapped_column(Integer, default=0)
    unindexed_fk_count: Mapped[int] = mapped_column(Integer, default=0)  # foreign keys no index starts with


class CatalogRelation(UuidPkMixin, Base):
    """A foreign key, or a view reading a table. `to_table_id` is empty when the other
    table is outside the snapshot (another schema); `to_label` always names it."""

    __tablename__ = "catalog_relations"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_snapshots.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # foreign_key | view
    name: Mapped[str | None] = mapped_column(String(128))
    from_table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_tables.id", ondelete="CASCADE"), index=True)
    to_table_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("catalog_tables.id", ondelete="CASCADE"), index=True)
    to_label: Mapped[str] = mapped_column(String(300))
    from_columns: Mapped[list[Any]] = mapped_column(default=list)
    to_columns: Mapped[list[Any]] = mapped_column(default=list)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class CatalogCode(UuidPkMixin, Base):
    """A procedure, function, package or trigger in a snapshot."""

    __tablename__ = "catalog_code"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_snapshots.id", ondelete="CASCADE"), index=True)
    schema: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    signature: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(Text)
    table_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("catalog_tables.id", ondelete="CASCADE"), index=True)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)
    table_count: Mapped[int] = mapped_column(Integer, default=0)


class CatalogUsage(UuidPkMixin, Base):
    """Code that uses a table: which operations, where, and how the link was found."""

    __tablename__ = "catalog_usages"
    __table_args__ = (UniqueConstraint("code_id", "table_id", name="uq_catalog_usages_pair"),)

    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_snapshots.id", ondelete="CASCADE"), index=True)
    code_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_code.id", ondelete="CASCADE"), index=True)
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_tables.id", ondelete="CASCADE"), index=True)
    operations: Mapped[list[Any]] = mapped_column(default=list)
    # dictionary (Oracle dependency views), code (found in the source), dynamic_sql (inside a string)
    found_by: Mapped[list[Any]] = mapped_column(default=list)
    subprograms: Mapped[list[Any]] = mapped_column(default=list)  # package members that use it
    member_operations: Mapped[dict[str, Any]] = mapped_column(default=dict)  # member -> operations
    lines: Mapped[list[Any]] = mapped_column(default=list)


class CatalogPerfReview(UuidPkMixin, Base):
    """A Claude performance review of the code that uses one table.

    Keyed by connection and table name rather than by snapshot rows, so it survives a
    catalog refresh; each reviewed object keeps a hash of the source it saw, which marks
    the result stale when that code changes."""

    __tablename__ = "catalog_perf_reviews"

    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    table_schema: Mapped[str] = mapped_column(String(128))
    table_name: Mapped[str] = mapped_column(String(128))
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("catalog_snapshots.id", ondelete="SET NULL"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    status: Mapped[ScanStatus] = mapped_column(str_enum(ScanStatus), default=ScanStatus.QUEUED)
    scope: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(64))
    code_ids: Mapped[list[Any]] = mapped_column(default=list)  # CatalogCode ids in the reviewed snapshot
    # objects: [{code_key, source_hash, label, kind, priority, summary, issues}],
    # table_recommendations: [...], failed: [{label, error}]
    results: Mapped[dict[str, Any]] = mapped_column(default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)  # counts by priority, usage
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SequenceCheck(UuidPkMixin, Base):
    """Which PostgreSQL sequences are behind the migrated data (and, optionally, Oracle)."""

    __tablename__ = "sequence_checks"

    postgres_connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    oracle_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("connections.id", ondelete="SET NULL"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    status: Mapped[ScanStatus] = mapped_column(str_enum(ScanStatus), default=ScanStatus.QUEUED)
    requested_schemas: Mapped[list[Any]] = mapped_column(default=list)
    schemas: Mapped[list[Any]] = mapped_column(default=list)
    sequences: Mapped[list[Any]] = mapped_column(default=list)  # SequenceState.as_dict() per sequence
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)  # counts by status
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SequenceStats(UuidPkMixin, Base):
    """Per-schema sequence statistics on Oracle and/or PostgreSQL, and a table-by-table
    comparison of the two (Sequence statistics page)."""

    __tablename__ = "sequence_stats"

    oracle_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    postgres_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    status: Mapped[ScanStatus] = mapped_column(str_enum(ScanStatus), default=ScanStatus.QUEUED)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LobCheck(UuidPkMixin, Base):
    """Tables with LOB columns counted on Oracle and PostgreSQL, to find LOB data that did
    not migrate (Overview page). `result` keeps the raw counts of both sides; the
    comparison is rebuilt from them when read."""

    __tablename__ = "lob_checks"

    oracle_connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    postgres_connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    status: Mapped[ScanStatus] = mapped_column(str_enum(ScanStatus), default=ScanStatus.QUEUED)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
