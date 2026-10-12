"""Choosing the code to review for a table, building the requests, running them in the worker."""

import hashlib
import queue
import re
import threading
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

import anthropic
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.catalog.perf_review import PerfObject, estimate, review_batch
from app.compare.review import make_client, numbered
from app.compare.review_service import credentials_configured
from app.compare.units import extract_member
from app.config import get_settings
from app.jobs.registry import JobContext
from app.models import (
    CatalogCode, CatalogPerfReview, CatalogRelation, CatalogSnapshot, CatalogTable, CatalogUsage, Connection, DbKind,
    ScanStatus,
)

Scope = Literal["triggers", "writers", "all", "selected"]
WRITES = {"insert", "update", "delete", "merge", "truncate", "ddl"}
_PRIORITY_ORDER = ["high", "medium", "low", "none"]
MAX_RELATED_TABLES = 40
MAX_COLUMNS = 80


def code_key(code: CatalogCode) -> list[str]:
    return [code.schema, code.kind, code.name, code.signature or ""]


def source_hash(source: str | None) -> str | None:
    return hashlib.sha256(source.encode()).hexdigest() if source is not None else None


def label(table: CatalogTable) -> str:
    return f"{table.schema}.{table.name}"


def select_code(
    session: Session, table: CatalogTable, scope: Scope, code_ids: list[uuid.UUID] | None = None
) -> list[tuple[CatalogCode, CatalogUsage | None]]:
    """The code to review for `table`, triggers first. On PostgreSQL a trigger already
    carries its function's source, so the function itself is not reviewed twice."""
    usages = session.execute(
        select(CatalogUsage, CatalogCode).join(CatalogCode, CatalogCode.id == CatalogUsage.code_id)
        .where(CatalogUsage.table_id == table.id)
    ).all()
    triggers = session.scalars(
        select(CatalogCode).where(CatalogCode.table_id == table.id, CatalogCode.kind == "trigger")
    ).all()
    usage_of = {code.id: usage for usage, code in usages}
    items: dict[uuid.UUID, tuple[CatalogCode, CatalogUsage | None]] = {t.id: (t, usage_of.get(t.id)) for t in triggers}
    for usage, code in usages:
        if scope == "triggers" or (scope == "writers" and not WRITES & set(usage.operations)):
            continue
        items.setdefault(code.id, (code, usage))
    if scope == "selected":
        wanted = set(code_ids or [])
        everything = {**{c.id: (c, u) for u, c in usages}, **{t.id: (t, usage_of.get(t.id)) for t in triggers}}
        items = {cid: item for cid, item in everything.items() if cid in wanted}

    in_triggers = {tuple(c.extra.get("function_key") or ()) for c, _ in items.values() if c.kind == "trigger"}
    chosen = [(c, u) for c, u in items.values() if tuple(code_key(c)) not in in_triggers]
    return sorted(chosen, key=lambda item: (item[0].kind != "trigger", item[0].schema, item[0].name))


def _member_listing(source: str, subprograms: list[str]) -> str | None:
    """Numbered listing of only the package members that use the table (line numbers as in the full source)."""
    body_at = source.find("CREATE OR REPLACE PACKAGE BODY")
    if body_at < 0:
        return None
    body, offset = source[body_at:], source[:body_at].count("\n")
    parts = []
    for name in subprograms:
        for occurrence in range(10):  # overloads
            member = extract_member(body, name, occurrence)
            if member is None:
                break
            text, start = member
            parts.append(numbered(text, offset + start))
    return "\n    ...\n".join(parts) if parts else None


def build_objects(session: Session, table: CatalogTable, items: list[tuple[CatalogCode, CatalogUsage | None]]) -> list[PerfObject]:
    budget = get_settings().perf_review_batch_chars
    ids = [c.id for c, _ in items]
    others: dict[uuid.UUID, list[str]] = defaultdict(list)
    for usage, other in session.execute(
        select(CatalogUsage, CatalogTable).join(CatalogTable, CatalogTable.id == CatalogUsage.table_id)
        .where(CatalogUsage.code_id.in_(ids), CatalogUsage.table_id != table.id)
        .order_by(CatalogTable.schema, CatalogTable.name)
    ):
        others[usage.code_id].append(f"{label(other)} ({', '.join(usage.operations) or 'referenced'})")

    objects = []
    for i, (code, usage) in enumerate(items, start=1):
        source = code.source or ""
        listing, note = numbered(source), ""
        if not source:
            listing, note = "(source not visible)", "The source is not visible to the account that read the catalog."
        elif len(source) > budget:
            members = _member_listing(source, usage.subprograms) if (code.kind == "package" and usage and usage.subprograms) else None
            if members:
                listing = members
                note = "The package is too large to include whole: only the members that use the table are shown."
            else:
                listing = numbered(source[:budget])
                note = f"The source is truncated to its first {budget:,} characters."
        if code.kind == "trigger":
            e = code.extra
            focus = (f"Trigger on {label(table)}: {e.get('timing') or ''} {e.get('events') or ''}"
                     f"{' FOR EACH ' + e['level'] if e.get('level') else ''}"
                     f"{'' if e.get('enabled', True) else ' (disabled)'}").strip()
        elif usage:
            ops = ", ".join(op.upper() for op in usage.operations) or "references"
            focus = f"Uses {label(table)}: {ops}" + (f" at lines {usage.lines[:25]}" if usage.lines else "")
            if usage.subprograms:
                focus += f" (in {', '.join(usage.subprograms)})"
            if "dynamic_sql" in usage.found_by:
                focus += "; partly through dynamic SQL"
        else:
            focus = f"Uses {label(table)}"
        objects.append(PerfObject(
            ref=f"C{i}", code_id=str(code.id), label=f"{code.schema}.{code.name} ({_kind(code)})",
            signature=code.signature, listing=listing, focus=focus, other_tables=others.get(code.id, []), note=note,
        ))
    return objects


def _kind(code: CatalogCode) -> str:
    return "trigger function" if code.extra.get("is_trigger_function") else code.kind


def batches(objects: list[PerfObject]) -> list[list[PerfObject]]:
    s = get_settings()
    out: list[list[PerfObject]] = []
    size = 0
    for obj in objects:
        cost = len(obj.listing) + 500
        if out and size + cost <= s.perf_review_batch_chars and len(out[-1]) < s.perf_review_batch_objects:
            out[-1].append(obj)
            size += cost
        else:
            out.append([obj])
            size = cost
    return out


def _table_text(t: CatalogTable, *, full: bool) -> list[str]:
    head = f"{label(t)} ({t.kind}"
    if t.row_estimate is not None:
        head += f", ≈ {t.row_estimate:,} rows"
    if t.partitions:
        head += f", {t.partitions} partitions"
    lines = [head + ")"]
    cols = t.columns[:MAX_COLUMNS]
    if full:
        lines.append("Columns:")
        lines += [f"  {c['name']} {c['type']}{'' if c.get('nullable', True) else ' NOT NULL'}" for c in cols]
    else:
        lines.append("Columns: " + ", ".join(f"{c['name']} {c['type']}" for c in cols))
    if len(t.columns) > MAX_COLUMNS:
        lines.append(f"  ... and {len(t.columns) - MAX_COLUMNS} more columns")
    if t.primary_key:
        lines.append(f"Primary key: ({', '.join(t.primary_key['columns'])})")
    for u in t.unique_keys:
        lines.append(f"Unique key {u['name']}: ({', '.join(u['columns'])})")
    if t.indexes:
        lines.append("Indexes:")
        for ix in t.indexes:
            text = f"  {ix['name']}: ({', '.join(ix['columns'])})"
            if ix.get("include"):
                text += f" INCLUDE ({', '.join(ix['include'])})"
            flags = [f for f, on in (("primary key", ix.get("primary")), ("unique", ix.get("unique") and not ix.get("primary"))) if on]
            if ix.get("method") and ix["method"] not in ("btree", "normal"):
                flags.append(ix["method"])
            if ix.get("status") and ix["status"] != "VALID":
                flags.append(ix["status"])
            text += f" {', '.join(flags)}" if flags else ""
            if ix.get("predicate"):
                text += f" WHERE {ix['predicate']}"
            lines.append(text)
    elif "view" not in t.kind:
        lines.append("Indexes: none")
    return lines


def context_block(session: Session, table: CatalogTable, objects: list[PerfObject], dialect: str) -> str:
    lines = [f"Database: {dialect}", "", "## Table under review", *_table_text(table, full=True)]
    other = CatalogTable.__table__.alias("other")
    outgoing = session.execute(
        select(CatalogRelation, other.c.schema, other.c.name)
        .outerjoin(other, other.c.id == CatalogRelation.to_table_id)
        .where(CatalogRelation.from_table_id == table.id, CatalogRelation.kind == "foreign_key")
    ).all()
    incoming = session.execute(
        select(CatalogRelation, other.c.schema, other.c.name, other.c.row_estimate)
        .join(other, other.c.id == CatalogRelation.from_table_id)
        .where(CatalogRelation.to_table_id == table.id, CatalogRelation.kind == "foreign_key")
    ).all()
    if outgoing:
        lines.append("Foreign keys from this table:")
        for rel, *_ in outgoing:
            lines.append(f"  ({', '.join(rel.from_columns)}) -> {rel.to_label} ({', '.join(rel.to_columns)})"
                         f"; index on the FK columns: {_yes_no(rel.details.get('indexed'))}")
    if incoming:
        lines.append("Foreign keys from other tables pointing at this one:")
        for rel, schema, name, rows in incoming:
            size = f", ≈ {rows:,} rows" if rows is not None else ""
            lines.append(f"  {schema}.{name} ({', '.join(rel.from_columns)}{size}) -> ({', '.join(rel.to_columns)})"
                         f"; index on the child's FK columns: {_yes_no(rel.details.get('indexed'))}")

    used = Counter()
    ids = [uuid.UUID(o.code_id) for o in objects]
    for (table_id,) in session.execute(
        select(CatalogUsage.table_id).where(CatalogUsage.code_id.in_(ids), CatalogUsage.table_id != table.id)
    ):
        used[table_id] += 1
    related = [session.get(CatalogTable, tid) for tid, _ in used.most_common(MAX_RELATED_TABLES)]
    if related:
        lines += ["", "## Other tables used by the code below (most used first)"]
        for t in related:
            lines += ["", *_table_text(t, full=False)]
        if len(used) > MAX_RELATED_TABLES:
            lines.append(f"\n({len(used) - MAX_RELATED_TABLES} less used tables are not shown.)")
    return "\n".join(lines)


def _yes_no(value: bool | None) -> str:
    return "unknown" if value is None else "yes" if value else "NO"


def dialect_of(session: Session, table: CatalogTable) -> str:
    snapshot = session.get(CatalogSnapshot, table.snapshot_id)
    conn = session.get(Connection, snapshot.connection_id)
    version = snapshot.server_version or ""
    if conn.kind is DbKind.ORACLE:
        return f"Oracle {version}, PL/SQL".strip()
    return f"PostgreSQL {version}, PL/pgSQL (code migrated from Oracle PL/SQL)".strip()


def prepare(session: Session, table: CatalogTable, items: list[tuple[CatalogCode, CatalogUsage | None]]):
    objects = build_objects(session, table, items)
    dialect = dialect_of(session, table)
    return objects, batches(objects), context_block(session, table, objects, dialect), dialect


def estimate_for(session: Session, table: CatalogTable, scope: Scope, code_ids: list[uuid.UUID] | None) -> dict[str, Any]:
    items = select_code(session, table, scope, code_ids)
    objects, groups, context, dialect = prepare(session, table, items)
    return {
        **estimate(context, groups, dialect, label(table), get_settings().review_model),
        "code_ids": [o.code_id for o in objects],
    }


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";").lower())


def run_perf_review(ctx: JobContext, review_id: uuid.UUID) -> dict[str, Any]:
    if not credentials_configured():
        raise ValueError("Set ANTHROPIC_API_KEY in .env and restart the worker to enable Claude reviews")
    settings = get_settings()
    session = ctx.session
    review = session.get(CatalogPerfReview, review_id)
    table = session.scalars(select(CatalogTable).where(
        CatalogTable.snapshot_id == review.snapshot_id,
        CatalogTable.schema == review.table_schema, CatalogTable.name == review.table_name,
    )).first() if review.snapshot_id else None
    if table is None:
        raise ValueError("The catalog was refreshed after this review was requested; start the review again")
    review.status = ScanStatus.RUNNING
    review.started_at = datetime.now(UTC)
    review.model = settings.review_model
    session.commit()

    items = select_code(session, table, "selected", [uuid.UUID(i) for i in review.code_ids])
    codes = {str(c.id): c for c, _ in items}
    objects, groups, context, dialect = prepare(session, table, items)
    by_ref = {o.ref: o for o in objects}
    ctx.progress(0.02, f"Reviewing {len(objects)} object(s) in {len(groups)} request(s) with {settings.review_model}")

    work: queue.Queue = queue.Queue()
    for group in groups:
        work.put(group)
    results: queue.Queue = queue.Queue()
    stop = threading.Event()
    client = make_client()

    def worker():
        while not stop.is_set():
            try:
                group = work.get_nowait()
            except queue.Empty:
                return
            try:
                results.put((group, review_batch(client, settings, dialect=dialect, context=context,
                                                 table_label=label(table), objects=group), None))
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                stop.set()
                results.put((group, None, exc))
            except Exception as exc:  # anything else must still be reported, not lost with the thread
                results.put((group, None, exc))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, settings.review_concurrency))]
    for t in threads:
        t.start()

    reviewed: list[dict[str, Any]] = []
    recommendations: dict[str, dict[str, Any]] = {}
    failed: list[dict[str, Any]] = []
    usage: Counter[str] = Counter()
    fatal: Exception | None = None
    finished = 0
    while finished < len(groups):
        try:
            group, result, error = results.get(timeout=1)
        except queue.Empty:
            if not any(t.is_alive() for t in threads):
                break
            continue
        finished += 1
        if error is not None:
            ctx.log(f"Batch {', '.join(o.label for o in group)}: {type(error).__name__}: {error}")
            failed += [{"label": o.label, "error": f"{type(error).__name__}: {error}"[:500]} for o in group]
            if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                fatal = error
        else:
            usage.update(result.get("usage", {}))
            answered = {o["ref"]: o for o in result.get("objects", []) if o.get("ref") in by_ref}
            for obj in group:
                code = codes[obj.code_id]
                answer = answered.get(obj.ref)
                if answer is None:
                    failed.append({"label": obj.label, "error": "declined to review" if result.get("refused") else "not in the response"})
                    continue
                reviewed.append({
                    "code_key": code_key(code), "code_id": obj.code_id, "source_hash": source_hash(code.source),
                    "label": obj.label, "name": f"{code.schema}.{code.name}", "kind": _kind(code),
                    "priority": answer["priority"], "summary": answer["summary"], "issues": answer["issues"],
                })
            for rec in result.get("table_recommendations", []):
                helps = [by_ref[r].label for r in rec.get("helps", []) if r in by_ref]
                key = _normalize_sql(rec.get("sql") or rec["title"])
                if key in recommendations:
                    recommendations[key]["helps"] = sorted(set(recommendations[key]["helps"]) | set(helps))
                else:
                    recommendations[key] = {**rec, "helps": helps}
        review.results = {
            "objects": sorted(reviewed, key=lambda r: (_PRIORITY_ORDER.index(r["priority"]), r["name"])),
            "table_recommendations": list(recommendations.values()),
            "failed": failed,
        }
        session.commit()
        ctx.progress(finished / max(1, len(groups)), f"Reviewed {finished}/{len(groups)} request(s)")
    for t in threads:
        t.join(timeout=5)
    if fatal is not None:
        raise ValueError(f"Anthropic API rejected the credentials: {fatal}")
    if not reviewed and failed:
        raise ValueError(f"Every request failed; first error: {failed[0]['error']}")

    priorities = Counter(r["priority"] for r in reviewed)
    review.summary = {
        "reviewed": len(reviewed), "failed": len(failed),
        "priority": {p: priorities.get(p, 0) for p in _PRIORITY_ORDER},
        "issues": sum(len(r["issues"]) for r in reviewed),
        "table_recommendations": len(recommendations), "usage": dict(usage),
    }
    review.status = ScanStatus.SUCCEEDED
    review.finished_at = datetime.now(UTC)
    session.execute(delete(CatalogPerfReview).where(
        CatalogPerfReview.connection_id == review.connection_id, CatalogPerfReview.table_schema == review.table_schema,
        CatalogPerfReview.table_name == review.table_name, CatalogPerfReview.id != review.id,
        CatalogPerfReview.status.in_([ScanStatus.SUCCEEDED, ScanStatus.FAILED]),
    ))
    session.commit()
    return {"review_id": str(review.id), **{k: v for k, v in review.summary.items() if k != "usage"}, "usage": dict(usage)}
