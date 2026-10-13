"""Counts of what a catalog snapshot holds, overall and per schema, and the items behind each count.

Snapshots never change, so the loaded data is cached per snapshot.
"""

import re
import uuid
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.catalog.routine_compare import parse_oracle_signature
from app.models import CatalogCode, CatalogRelation, CatalogTable, CatalogUsage

AREAS = ("tables", "views", "columns", "indexes", "foreign_keys", "procedures", "functions", "trigger_functions",
         "packages", "triggers", "invalid_code")
_CACHE: dict[uuid.UUID, dict[str, list[dict[str, Any]]]] = {}


def _plain(key: str) -> str:
    text = re.sub(r"\s+DESC$", "", key.strip(), flags=re.I)
    return text[1:-1] if len(text) > 1 and text[0] == text[-1] == '"' else text


def _code_area(c: CatalogCode) -> str | None:
    if c.kind == "function":
        return "trigger_functions" if (c.extra or {}).get("is_trigger_function") else "functions"
    return {"procedure": "procedures", "package": "packages", "trigger": "triggers"}.get(c.kind)


def _items(db: Session, snapshot_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    """Every countable item of the snapshot, by area."""
    if snapshot_id in _CACHE:
        return _CACHE[snapshot_id]
    tables = db.scalars(select(CatalogTable).where(CatalogTable.snapshot_id == snapshot_id)).all()
    by_id = {t.id: t for t in tables}
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in tables:
        is_view = "view" in t.kind
        out["views" if is_view else "tables"].append({
            "table_id": str(t.id), "schema": t.schema, "name": t.name, "kind": t.kind, "columns": t.column_count,
            "indexes": t.index_count, "rows": t.row_estimate, "partitions": t.partitions,
            "unindexed_fks": t.unindexed_fk_count, "code": t.code_count, "triggers": t.trigger_count,
        })
        keys = [_plain(k).lower() for ix in t.indexes or [] for k in ix.get("columns", []) + (ix.get("include") or [])]
        # A column counts as indexed when it is a key, an INCLUDE column, or used in an expression such as lower(name).
        words = {w for k in keys for w in re.findall(r"[a-z_][\w$#]*", k.replace('"', ""))}
        indexed = set(keys) | words
        pk = {c.lower() for c in ((t.primary_key or {}).get("columns") or [])}
        for i, c in enumerate(t.columns or [], start=1):
            out["columns"].append({
                "table_id": str(t.id), "schema": t.schema, "table": t.name, "name": c["name"], "position": i,
                "type": c.get("type"), "nullable": c.get("nullable", True), "primary_key": c["name"].lower() in pk,
                "indexed": c["name"].lower() in indexed, "view": is_view,
            })
        for ix in t.indexes or []:
            out["indexes"].append({
                "table_id": str(t.id), "schema": t.schema, "table": t.name, "name": ix["name"],
                "columns": ix.get("columns", []), "include": ix.get("include") or [], "unique": bool(ix.get("unique")),
                "primary": bool(ix.get("primary")), "method": ix.get("method"), "status": ix.get("status"),
                "predicate": ix.get("predicate"),
            })
    for r in db.scalars(select(CatalogRelation).where(CatalogRelation.snapshot_id == snapshot_id,
                                                      CatalogRelation.kind == "foreign_key")):
        t = by_id.get(r.from_table_id)
        if t is None:
            continue
        out["foreign_keys"].append({
            "table_id": str(t.id), "schema": t.schema, "table": t.name, "name": r.name, "columns": r.from_columns,
            "references": r.to_label, "references_id": str(r.to_table_id) if r.to_table_id else None,
            "referenced_columns": r.to_columns, "on_delete": (r.details or {}).get("on_delete"),
            "indexed": (r.details or {}).get("indexed"),
        })
    codes = db.scalars(select(CatalogCode).options(defer(CatalogCode.source)).where(CatalogCode.snapshot_id == snapshot_id)).all()
    member_tables = _member_table_counts(db, [c.id for c in codes if c.kind == "package"])
    for c in codes:
        area = _code_area(c)
        if area is None:
            continue
        table = by_id.get(c.table_id) if c.table_id else None
        e = c.extra or {}
        out[area].append({
            "code_id": str(c.id), "kind": area.removesuffix("s").replace("_", " "), "schema": c.schema, "name": c.name,
            "signature": c.signature, "status": c.status,
            "tables_used": c.table_count, "members": len(e.get("members") or []) if c.kind == "package" else None,
            "table": f"{table.schema}.{table.name}" if table else None, "table_id": str(table.id) if table else None,
            "timing": e.get("timing"), "events": e.get("events"), "level": e.get("level"),
            "enabled": e.get("enabled"), "function": e.get("function"), "package": None,
        })
        if c.kind == "package":  # its public members are procedures and functions too
            seen = set()
            for sig in e.get("members") or []:
                parsed = parse_oracle_signature(sig)
                member = parsed["name"].split(".")[-1]
                kind = parsed["kind"].lower()
                out["procedures" if kind == "procedure" else "functions"].append({
                    "code_id": str(c.id), "kind": f"{kind} in package", "schema": c.schema, "name": f"{c.name}.{member}",
                    "signature": sig, "status": c.status, "package": c.name,
                    "tables_used": member_tables.get((c.id, member)), "overload": (c.name, member) in seen,
                })
                seen.add((c.name, member))
    out["invalid_code"] = [r for a in ("procedures", "functions", "trigger_functions", "packages", "triggers")
                           for r in out.get(a, []) if r["status"] and r["status"] != "VALID"]
    for rows in out.values():
        rows.sort(key=lambda r: (r["schema"].lower(), (r.get("table") or "").lower(), r["name"].lower()))
    if len(_CACHE) >= 8:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[snapshot_id] = dict(out)
    return _CACHE[snapshot_id]


def _member_table_counts(db: Session, package_ids: list[uuid.UUID]) -> dict[tuple[uuid.UUID, str], int]:
    """How many tables each package member uses."""
    out: dict[tuple[uuid.UUID, str], int] = Counter()
    if package_ids:
        for code_id, subprograms in db.execute(
            select(CatalogUsage.code_id, CatalogUsage.subprograms).where(CatalogUsage.code_id.in_(package_ids))
        ):
            for member in subprograms or []:
                out[(code_id, member)] += 1
    return out


def summary(db: Session, snapshot_id: uuid.UUID) -> dict[str, Any]:
    items = _items(db, snapshot_id)
    totals = {area: len(items.get(area, [])) for area in AREAS}
    per_schema: dict[str, Counter] = defaultdict(Counter)
    for area in AREAS:
        for row in items.get(area, []):
            per_schema[row["schema"]][area] += 1
    tables = items.get("tables", [])
    code = [r for a in ("procedures", "functions", "trigger_functions", "packages", "triggers") for r in items.get(a, [])]
    extras = {
        "partitioned_tables": sum(1 for t in tables if t["kind"] == "partitioned table"),
        "partitions": sum(t["partitions"] or 0 for t in tables),
        "rows_estimate": sum(t["rows"] or 0 for t in tables),
        "tables_without_primary_key": None,
        "unique_indexes": sum(1 for ix in items.get("indexes", []) if ix["unique"]),
        "unindexed_foreign_keys": sum(1 for fk in items.get("foreign_keys", []) if fk["indexed"] is False),
        "indexed_columns": sum(1 for c in items.get("columns", []) if c["indexed"] and not c["view"]),
        "invalid_code": sum(1 for c in code if c["status"] and c["status"] != "VALID"),
        "disabled_triggers": sum(1 for t in items.get("triggers", []) if t["enabled"] is False),
        "package_members": sum(p["members"] or 0 for p in items.get("packages", [])),
        "package_procedures": sum(1 for r in items.get("procedures", []) if r.get("package")),
        "package_functions": sum(1 for r in items.get("functions", []) if r.get("package")),
    }
    pk_tables = {c["table_id"] for c in items.get("columns", []) if c["primary_key"]}
    extras["tables_without_primary_key"] = sum(1 for t in tables if t["table_id"] not in pk_tables)
    return {
        "totals": totals,
        "extras": extras,
        "by_schema": sorted(({"schema": s, **{a: c.get(a, 0) for a in AREAS}} for s, c in per_schema.items()),
                            key=lambda r: (-(r["tables"] + r["views"] + r["procedures"] + r["functions"]), r["schema"].lower())),
    }


FILTERS = {
    "tables": {"without_pk": "Without a primary key", "partitioned": "Partitioned", "unindexed_fk": "With an unindexed foreign key"},
    "columns": {"indexed": "Indexed", "not_indexed": "Not indexed", "primary_key": "In the primary key"},
    "indexes": {"unique": "Unique", "not_unique": "Not unique", "invalid": "Invalid / unusable"},
    "foreign_keys": {"unindexed": "Without a supporting index", "indexed": "With a supporting index"},
    "triggers": {"disabled": "Disabled", "enabled": "Enabled"},
    "procedures": {"standalone": "Standalone", "in_package": "In packages", "invalid": "Invalid"},
    "functions": {"standalone": "Standalone", "in_package": "In packages", "invalid": "Invalid"},
    "packages": {"invalid": "Invalid"},
}


def _matches_filter(area: str, row: dict[str, Any], f: str, pk_tables: set[str]) -> bool:
    if area == "tables":
        return {"without_pk": row["table_id"] not in pk_tables, "partitioned": row["kind"] == "partitioned table",
                "unindexed_fk": (row["unindexed_fks"] or 0) > 0}.get(f, True)
    if area == "columns":
        return {"indexed": row["indexed"], "not_indexed": not row["indexed"], "primary_key": row["primary_key"]}.get(f, True)
    if area == "indexes":
        return {"unique": row["unique"], "not_unique": not row["unique"],
                "invalid": bool(row["status"]) and row["status"] not in ("VALID", "N/A")}.get(f, True)
    if area == "foreign_keys":
        return {"unindexed": row["indexed"] is False, "indexed": row["indexed"] is True}.get(f, True)
    if area == "triggers":
        return {"disabled": row["enabled"] is False, "enabled": row["enabled"] is not False}.get(f, True)
    if f == "invalid":
        return bool(row["status"]) and row["status"] != "VALID"
    if f in ("standalone", "in_package"):
        return bool(row.get("package")) == (f == "in_package")
    return True


def drill(db: Session, snapshot_id: uuid.UUID, area: str, *, schema: str | None, q: str | None, filter_: str | None,
          limit: int, offset: int) -> dict[str, Any]:
    items = _items(db, snapshot_id)
    rows = items.get(area, [])
    if area == "columns":
        rows = [r for r in rows if not r["view"]]
    if schema:
        rows = [r for r in rows if r["schema"] == schema]
    if filter_:
        pk_tables = {c["table_id"] for c in items.get("columns", []) if c["primary_key"]}
        rows = [r for r in rows if _matches_filter(area, r, filter_, pk_tables)]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in f"{r['schema']}.{r.get('table') or ''}.{r['name']}".lower()]
    return {"area": area, "total": len(rows), "items": rows[offset:offset + limit],
            "filters": FILTERS.get(area, {})}
