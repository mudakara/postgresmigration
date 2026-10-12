"""What a catalog reader returns: tables, their keys and relationships, and the code."""

from dataclasses import dataclass, field
from typing import Any

TableKey = tuple[str, str]  # (schema, name)


@dataclass
class Table:
    schema: str
    name: str
    kind: str  # table | partitioned table | view | materialized view | foreign table | temporary table
    columns: list[dict[str, Any]] = field(default_factory=list)  # name, type, nullable, default
    primary_key: dict[str, Any] | None = None  # {"name", "columns"}
    unique_keys: list[dict[str, Any]] = field(default_factory=list)
    # name, columns (names or expressions, in key order), include, unique, primary, method,
    # predicate (partial index), status (Oracle VALID / UNUSABLE), definition (PostgreSQL)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    row_estimate: int | None = None
    partitions: int = 0
    comment: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> TableKey:
        return (self.schema, self.name)


@dataclass
class Relation:
    kind: str  # foreign_key | view (a view or materialized view reading another table)
    name: str | None
    from_table: TableKey
    to_table: TableKey
    from_columns: list[str] = field(default_factory=list)
    to_columns: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Code:
    schema: str
    name: str
    kind: str  # procedure | function | package | trigger
    signature: str | None = None
    language: str | None = None
    status: str | None = None  # Oracle VALID / INVALID
    source: str | None = None
    table: TableKey | None = None  # triggers: the table they fire on
    extra: dict[str, Any] = field(default_factory=dict)
    # Search path used to resolve unqualified names (PostgreSQL `SET search_path` on a function).
    search_path: list[str] = field(default_factory=list)
    # Oracle: offset in `source` where the package body starts.
    package_body_from: int | None = None
    # Oracle: tables the data dictionary says this code depends on (static references).
    dictionary_refs: set[TableKey] | None = None
    # Whether to look for table references in the source (PostgreSQL trigger objects carry
    # their function's source, whose references are recorded on the function instead).
    scan_source: bool = True

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.schema, self.kind, self.name, self.signature or "")


@dataclass
class Catalog:
    server_version: str
    dictionary: str | None  # Oracle: DBA or ALL
    schemas: list[str]
    fold: str  # how unquoted identifiers fold: "upper" (Oracle) or "lower" (PostgreSQL)
    search_path: list[str]
    tables: list[Table]
    relations: list[Relation]
    code: list[Code]
    synonyms: dict[TableKey, TableKey] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
