"""Catalog snapshots for the schema explorer: read a connection, link code to tables, store."""

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from app.catalog.model import Catalog, TableKey
from app.catalog.references import Resolver, find_usages
from app.jobs.registry import JobContext
from app.models import (
    CatalogCode, CatalogRelation, CatalogSnapshot, CatalogTable, CatalogUsage, Connection, DbKind, ScanStatus,
)
from app.services.endpoints import Endpoint, use_user_grants

_OPERATION_ORDER = ["select", "insert", "update", "delete", "merge", "truncate", "ddl", "lock", "type"]


@dataclass
class Link:
    table: TableKey
    operations: list[str]
    found_by: list[str]  # dictionary | code | dynamic_sql
    subprograms: list[str]
    lines: list[int]
    member_operations: dict[str, list[str]] = field(default_factory=dict)


def link_code(catalog: Catalog) -> list[list[Link]]:
    """For each entry of `catalog.code`, the tables it uses.

    PostgreSQL does not track what a function body references, so links come from reading
    the source. Oracle does (the dependency views): those links are authoritative for
    static SQL, and the source adds how each table is used plus tables named only inside
    dynamic SQL strings.
    """
    fold = str.upper if catalog.fold == "upper" else str.lower
    resolver = Resolver(
        (t.key for t in catalog.tables), fold=fold, search_path=catalog.search_path, synonyms=catalog.synonyms
    )
    out = []
    for code in catalog.code:
        usages = {}
        if code.scan_source and code.source:
            usages = find_usages(
                code.source, resolver.bind(code.schema, code.search_path), package_body_from=code.package_body_from
            )
        links = []
        for key, usage in usages.items():
            found_by = set(usage.found_by)
            if code.dictionary_refs is not None:
                if key in code.dictionary_refs:
                    found_by.add("dictionary")
                elif "dynamic_sql" in found_by:
                    found_by = {"dynamic_sql"}
                else:
                    continue  # static text match Oracle doesn't confirm (a variable, a collection...)
            links.append(Link(
                key, sorted(usage.operations, key=_OPERATION_ORDER.index), sorted(found_by),
                sorted(usage.subprograms), usage.lines,
                {m: sorted(ops, key=_OPERATION_ORDER.index) for m, ops in sorted(usage.member_operations.items())},
            ))
        for key in sorted((code.dictionary_refs or set()) - usages.keys()):
            links.append(Link(key, [], ["dictionary"], [], []))
        out.append(sorted(links, key=lambda link: link.table))
    return out


def _label(key: TableKey) -> str:
    return f"{key[0]}.{key[1]}"


def _clean(text: str | None) -> str | None:
    return text.replace("\x00", "") if text else text  # PostgreSQL text can't hold NUL


def _plain(column: str) -> str:
    """Index key text -> column name: drop DESC and identifier quotes."""
    text = column.strip().removesuffix(" DESC").strip()
    if len(text) > 1 and text[0] == text[-1] == '"':
        text = text[1:-1].replace('""', '"')
    return text


def has_supporting_index(columns: list[str], indexes: list[dict[str, Any]]) -> bool:
    """Whether some index starts with exactly these columns (in any order). Without one,
    deleting or updating a parent row scans the whole child table, and Oracle also takes
    a table lock on the child."""
    wanted = set(columns)
    return any(
        len(ix["columns"]) >= len(columns) and {_plain(c) for c in ix["columns"][: len(columns)]} == wanted
        for ix in indexes
        if not ix.get("predicate")  # a partial index doesn't serve every row
    )


def persist(session: Session, snapshot: CatalogSnapshot, catalog: Catalog, links: list[list[Link]]) -> dict[str, Any]:
    """Store the catalog under `snapshot`. Does not commit."""
    table_ids: dict[TableKey, uuid.UUID] = {t.key: uuid.uuid4() for t in catalog.tables}

    tables_by_key = {t.key: t for t in catalog.tables}
    relations, seen = [], set()
    unindexed_fks: Counter[TableKey] = Counter()
    for r in catalog.relations:
        identity = (r.kind, r.name, r.from_table, r.to_table, tuple(r.from_columns))
        if identity in seen or r.from_table not in table_ids:
            continue
        seen.add(identity)
        details = dict(r.details)
        if r.kind == "foreign_key":
            details["indexed"] = has_supporting_index(r.from_columns, tables_by_key[r.from_table].indexes)
            if not details["indexed"]:
                unindexed_fks[r.from_table] += 1
        relations.append({
            "id": uuid.uuid4(), "snapshot_id": snapshot.id, "kind": r.kind, "name": r.name,
            "from_table_id": table_ids[r.from_table], "to_table_id": table_ids.get(r.to_table),
            "to_label": _label(r.to_table), "from_columns": r.from_columns, "to_columns": r.to_columns,
            "details": details,
        })

    code_rows, usage_rows = [], []
    code_count: Counter[uuid.UUID] = Counter()
    trigger_count: Counter[uuid.UUID] = Counter()
    for code, code_links in zip(catalog.code, links, strict=True):
        code_id = uuid.uuid4()
        table_id = table_ids.get(code.table) if code.table else None
        if code.kind == "trigger" and table_id:
            trigger_count[table_id] += 1
        code_rows.append({
            "id": code_id, "snapshot_id": snapshot.id, "schema": code.schema, "name": code.name, "kind": code.kind,
            "signature": code.signature, "language": code.language, "status": code.status,
            "source": _clean(code.source), "table_id": table_id, "extra": code.extra, "table_count": len(code_links),
        })
        for link in code_links:
            usage_rows.append({
                "id": uuid.uuid4(), "snapshot_id": snapshot.id, "code_id": code_id, "table_id": table_ids[link.table],
                "operations": link.operations, "found_by": link.found_by, "subprograms": link.subprograms,
                "lines": link.lines, "member_operations": link.member_operations,
            })
            code_count[table_ids[link.table]] += 1

    fk_out: Counter[uuid.UUID] = Counter(r["from_table_id"] for r in relations if r["kind"] == "foreign_key")
    fk_in: Counter[uuid.UUID] = Counter(r["to_table_id"] for r in relations if r["kind"] == "foreign_key" and r["to_table_id"])
    table_rows = []
    for t in catalog.tables:
        tid = table_ids[t.key]
        table_rows.append({
            "id": tid, "snapshot_id": snapshot.id, "schema": t.schema, "name": t.name, "kind": t.kind,
            "columns": t.columns, "primary_key": t.primary_key, "unique_keys": t.unique_keys, "indexes": t.indexes,
            "index_count": len(t.indexes), "unindexed_fk_count": unindexed_fks[t.key],
            "row_estimate": t.row_estimate, "partitions": t.partitions, "comment": _clean(t.comment), "extra": t.extra,
            "column_count": len(t.columns), "references_count": fk_out[tid], "referenced_by_count": fk_in[tid],
            "code_count": code_count[tid], "trigger_count": trigger_count[tid],
        })

    for model, rows in ((CatalogTable, table_rows), (CatalogRelation, relations), (CatalogCode, code_rows),
                        (CatalogUsage, usage_rows)):
        for i in range(0, len(rows), 2000):
            session.execute(insert(model), rows[i:i + 2000])

    per_schema: dict[str, Counter[str]] = defaultdict(Counter)
    for t in catalog.tables:
        per_schema[t.schema]["views" if "view" in t.kind else "tables"] += 1
    for c in catalog.code:
        per_schema[c.schema]["code"] += 1
    return {
        "tables": sum(1 for t in catalog.tables if "view" not in t.kind),
        "views": sum(1 for t in catalog.tables if "view" in t.kind),
        "code": dict(Counter(c.kind for c in catalog.code)),
        "foreign_keys": sum(1 for r in relations if r["kind"] == "foreign_key"),
        "indexes": sum(len(t.indexes) for t in catalog.tables),
        "unindexed_foreign_keys": sum(unindexed_fks.values()),
        "code_table_links": len(usage_rows),
        "schemas": [{"schema": s, **per_schema[s]} for s in catalog.schemas],
    }


def read(conn: Connection, schemas: list[str], progress) -> Catalog:
    endpoint = Endpoint.from_connection(conn)
    if conn.kind is DbKind.ORACLE:
        from app.catalog import oracle_reader

        return oracle_reader.read_catalog(endpoint, schemas, user_grants=use_user_grants(conn), progress=progress)
    from app.catalog import postgres_reader

    return postgres_reader.read_catalog(endpoint, schemas, progress=progress)


def run_snapshot(ctx: JobContext, snapshot_id: uuid.UUID) -> dict[str, Any]:
    session = ctx.session
    snapshot = session.get(CatalogSnapshot, snapshot_id)
    conn = session.get(Connection, snapshot.connection_id)
    snapshot.status = ScanStatus.RUNNING
    snapshot.started_at = datetime.now(UTC)
    session.commit()

    catalog = read(conn, snapshot.requested_schemas, ctx.progress)
    ctx.progress(0.8, f"Linking {len(catalog.code)} code objects to {len(catalog.tables)} tables")
    links = link_code(catalog)

    ctx.progress(0.9, "Saving")
    summary = persist(session, snapshot, catalog, links)
    snapshot.schemas = catalog.schemas
    snapshot.server_version = catalog.server_version[:200]
    snapshot.dictionary = catalog.dictionary
    snapshot.summary = summary
    snapshot.warnings = catalog.warnings
    snapshot.status = ScanStatus.SUCCEEDED
    snapshot.finished_at = datetime.now(UTC)
    # Only the latest snapshot of a connection is kept.
    session.execute(delete(CatalogSnapshot).where(
        CatalogSnapshot.connection_id == conn.id, CatalogSnapshot.id != snapshot.id,
        CatalogSnapshot.status.in_([ScanStatus.SUCCEEDED, ScanStatus.FAILED]),
    ))
    session.commit()
    return {"snapshot_id": str(snapshot.id), **{k: v for k, v in summary.items() if k != "schemas"}}
