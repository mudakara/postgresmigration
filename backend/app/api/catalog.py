"""Schema explorer: catalog snapshots of a connection, its tables, relationships and code."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import exists, func, not_, or_, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, aliased

from app import jobs
from app.db import get_db
from app.catalog import perf_service
from app.compare.review_service import credentials_configured
from app.config import get_settings
from app.models import (
    CatalogCode, CatalogPerfReview, CatalogRelation, CatalogSnapshot, CatalogTable, CatalogUsage, Connection, DbKind,
    ScanStatus,
)
from app.schemas import (
    CatalogCodeDetail,
    CatalogCodePage,
    CatalogCodeSummary,
    CatalogCodeUsage,
    CatalogRelationRead,
    CatalogSnapshotCreate,
    CatalogSnapshotRead,
    CatalogTableDetail,
    CatalogTablePage,
    CatalogTableSummary,
    CatalogTableUsage,
    PerfReviewEstimate,
    PerfReviewRead,
    PerfReviewRequest,
    PerfReviewState,
)

router = APIRouter(tags=["schema explorer"])

_ACTIVE = (ScanStatus.QUEUED, ScanStatus.RUNNING)
_WRITES = ("insert", "update", "delete", "merge", "truncate", "ddl")


def _like(q: str) -> str:
    return "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _get_or_404(db: Session, model, object_id: uuid.UUID, label: str):
    obj = db.get(model, object_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label} not found")
    return obj


@router.post(
    "/connections/{connection_id}/catalog-snapshots",
    response_model=CatalogSnapshotRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_snapshot(connection_id: uuid.UUID, body: CatalogSnapshotCreate | None = None, db: Session = Depends(get_db)):
    conn = _get_or_404(db, Connection, connection_id, "Connection")
    active = db.scalars(select(CatalogSnapshot).where(
        CatalogSnapshot.connection_id == conn.id, CatalogSnapshot.status.in_(_ACTIVE)
    )).first()
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The catalog of this connection is already being read")
    schemas = (body or CatalogSnapshotCreate()).schemas
    if conn.kind is DbKind.ORACLE:
        schemas = [s.upper() for s in schemas]
    snapshot = CatalogSnapshot(connection_id=conn.id, requested_schemas=list(dict.fromkeys(schemas)))
    db.add(snapshot)
    db.flush()
    job = jobs.create_job(db, "catalog_snapshot", params={"snapshot_id": str(snapshot.id)}, connection_id=conn.id)
    snapshot.job_id = job.id
    db.commit()
    jobs.enqueue(job)
    return snapshot


@router.get("/connections/{connection_id}/catalog-snapshots", response_model=list[CatalogSnapshotRead])
def list_snapshots(connection_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.scalars(
        select(CatalogSnapshot).where(CatalogSnapshot.connection_id == connection_id)
        .order_by(CatalogSnapshot.created_at.desc())
    ).all()


@router.get("/catalog-snapshots/{snapshot_id}", response_model=CatalogSnapshotRead)
def get_snapshot(snapshot_id: uuid.UUID, db: Session = Depends(get_db)):
    return _get_or_404(db, CatalogSnapshot, snapshot_id, "Snapshot")


@router.delete("/catalog-snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_snapshot(snapshot_id: uuid.UUID, db: Session = Depends(get_db)):
    snapshot = _get_or_404(db, CatalogSnapshot, snapshot_id, "Snapshot")
    if snapshot.status in _ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "The snapshot is still being read")
    db.delete(snapshot)
    db.commit()


@router.get("/catalog-snapshots/{snapshot_id}/tables", response_model=CatalogTablePage)
def list_tables(
    snapshot_id: uuid.UUID,
    schema: str | None = None,
    kind: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    linked: bool = False,  # only tables with relationships, code or triggers
    indexes: Literal["none", "unindexed_fk"] | None = None,
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    _get_or_404(db, CatalogSnapshot, snapshot_id, "Snapshot")
    query = select(CatalogTable).where(CatalogTable.snapshot_id == snapshot_id)
    if schema:
        query = query.where(CatalogTable.schema == schema)
    if kind == "view":
        query = query.where(CatalogTable.kind.contains("view"))
    elif kind == "table":
        query = query.where(CatalogTable.kind.not_like("%view%"))
    if q:
        pattern = _like(q)
        query = query.where(or_(CatalogTable.name.ilike(pattern), (CatalogTable.schema + "." + CatalogTable.name).ilike(pattern)))
    if linked:
        query = query.where(or_(
            CatalogTable.references_count > 0, CatalogTable.referenced_by_count > 0,
            CatalogTable.code_count > 0, CatalogTable.trigger_count > 0,
        ))
    if indexes == "none":
        query = query.where(CatalogTable.index_count == 0, CatalogTable.kind.not_like("%view%"))
    elif indexes == "unindexed_fk":
        query = query.where(CatalogTable.unindexed_fk_count > 0)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(CatalogTable.schema, CatalogTable.name).limit(limit)).all()
    return {"total": total, "items": items}


@router.get("/catalog-snapshots/{snapshot_id}/code", response_model=CatalogCodePage)
def list_code(
    snapshot_id: uuid.UUID,
    schema: str | None = None,
    kind: Literal["procedure", "function", "trigger_function", "package", "trigger"] | None = None,
    q: str | None = Query(default=None, max_length=200),
    in_source: bool = False,  # also match q anywhere in the source code
    access: Literal["writes", "reads_only", "no_tables"] | None = None,
    invalid: bool = False,  # Oracle: only objects that don't compile
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    _get_or_404(db, CatalogSnapshot, snapshot_id, "Snapshot")
    query = select(CatalogCode).where(CatalogCode.snapshot_id == snapshot_id)
    if schema:
        query = query.where(CatalogCode.schema == schema)
    is_trigger_function = CatalogCode.extra["is_trigger_function"].as_boolean().is_(True)
    if kind == "trigger_function":
        query = query.where(CatalogCode.kind == "function", is_trigger_function)
    elif kind == "function":
        query = query.where(CatalogCode.kind == "function", not_(func.coalesce(is_trigger_function, False)))
    elif kind:
        query = query.where(CatalogCode.kind == kind)
    if q:
        pattern = _like(q)
        matches = [CatalogCode.name.ilike(pattern), CatalogCode.signature.ilike(pattern),
                   (CatalogCode.schema + "." + CatalogCode.name).ilike(pattern)]
        if in_source:
            matches.append(CatalogCode.source.ilike(pattern))
        query = query.where(or_(*matches))
    if access:
        writes = exists().where(
            CatalogUsage.code_id == CatalogCode.id,
            or_(*[type_coerce(CatalogUsage.operations, JSONB).contains([op]) for op in _WRITES]),
        )
        query = query.where(
            writes if access == "writes"
            else (CatalogCode.table_count > 0) & ~writes if access == "reads_only"
            else CatalogCode.table_count == 0
        )
    if invalid:
        query = query.where(CatalogCode.status.is_not(None), CatalogCode.status != "VALID")
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(CatalogCode.schema, CatalogCode.name, CatalogCode.kind).limit(limit)).all()
    return {"total": total, "items": items}


def _relations(rows, *, outgoing: bool) -> list[CatalogRelationRead]:
    """Relations seen from one table: the other end is `to` for outgoing ones, `from` for incoming."""
    out = []
    for rel, other in rows:
        out.append(CatalogRelationRead(
            id=rel.id, kind=rel.kind, name=rel.name,
            other_table_id=other.id if other else None,
            other_label=rel.to_label if outgoing else f"{other.schema}.{other.name}",
            other_kind=other.kind if other else None,
            columns=rel.from_columns if outgoing else rel.to_columns,
            other_columns=rel.to_columns if outgoing else rel.from_columns,
            details=rel.details,
        ))
    return out


@router.get("/catalog-tables/{table_id}", response_model=CatalogTableDetail)
def get_table(table_id: uuid.UUID, db: Session = Depends(get_db)):
    table = _get_or_404(db, CatalogTable, table_id, "Table")
    other = aliased(CatalogTable)
    outgoing = db.execute(
        select(CatalogRelation, other).outerjoin(other, other.id == CatalogRelation.to_table_id)
        .where(CatalogRelation.from_table_id == table.id).order_by(CatalogRelation.to_label, CatalogRelation.name)
    ).all()
    incoming = db.execute(
        select(CatalogRelation, other).join(other, other.id == CatalogRelation.from_table_id)
        .where(CatalogRelation.to_table_id == table.id).order_by(other.schema, other.name, CatalogRelation.name)
    ).all()
    usages = db.execute(
        select(CatalogUsage, CatalogCode).join(CatalogCode, CatalogCode.id == CatalogUsage.code_id)
        .where(CatalogUsage.table_id == table.id).order_by(CatalogCode.schema, CatalogCode.kind, CatalogCode.name)
    ).all()
    triggers = db.scalars(
        select(CatalogCode).where(CatalogCode.table_id == table.id, CatalogCode.kind == "trigger").order_by(CatalogCode.name)
    ).all()
    return CatalogTableDetail.model_validate(table).model_copy(update={
        "references": _relations([r for r in outgoing if r[0].kind == "foreign_key"], outgoing=True),
        "reads_from": _relations([r for r in outgoing if r[0].kind == "view"], outgoing=True),
        "referenced_by": _relations([r for r in incoming if r[0].kind == "foreign_key"], outgoing=False),
        "read_by_views": _relations([r for r in incoming if r[0].kind == "view"], outgoing=False),
        "used_by": [
            CatalogCodeUsage(code=CatalogCodeSummary.model_validate(code), operations=u.operations,
                             found_by=u.found_by, subprograms=u.subprograms, lines=u.lines,
                             member_operations=u.member_operations or {})
            for u, code in usages
        ],
        "triggers": [CatalogCodeSummary.model_validate(t) for t in triggers],
    })


@router.get("/catalog-code/{code_id}", response_model=CatalogCodeDetail)
def get_code(code_id: uuid.UUID, db: Session = Depends(get_db)):
    code = _get_or_404(db, CatalogCode, code_id, "Code object")
    uses = db.execute(
        select(CatalogUsage, CatalogTable).join(CatalogTable, CatalogTable.id == CatalogUsage.table_id)
        .where(CatalogUsage.code_id == code.id).order_by(CatalogTable.schema, CatalogTable.name)
    ).all()
    table = db.get(CatalogTable, code.table_id) if code.table_id else None
    return CatalogCodeDetail.model_validate(code).model_copy(update={
        "table": CatalogTableSummary.model_validate(table) if table else None,
        "uses": [
            CatalogTableUsage(table=CatalogTableSummary.model_validate(t), operations=u.operations,
                              found_by=u.found_by, subprograms=u.subprograms, lines=u.lines,
                             member_operations=u.member_operations or {})
            for u, t in uses
        ],
    })


# --- Claude performance review of a table's code -------------------------------------------


def _perf_reviews(db: Session, table: CatalogTable):
    snapshot = db.get(CatalogSnapshot, table.snapshot_id)
    return select(CatalogPerfReview).where(
        CatalogPerfReview.connection_id == snapshot.connection_id,
        CatalogPerfReview.table_schema == table.schema, CatalogPerfReview.table_name == table.name,
    ), snapshot


@router.post("/catalog-tables/{table_id}/perf-reviews/estimate", response_model=PerfReviewEstimate)
def estimate_perf_review(table_id: uuid.UUID, body: PerfReviewRequest, db: Session = Depends(get_db)):
    table = _get_or_404(db, CatalogTable, table_id, "Table")
    result = perf_service.estimate_for(db, table, body.scope, body.code_ids)
    return {**result, "credentials_configured": credentials_configured(),
            "max_objects": get_settings().perf_review_max_objects}


@router.post("/catalog-tables/{table_id}/perf-reviews", response_model=PerfReviewRead, status_code=status.HTTP_202_ACCEPTED)
def start_perf_review(table_id: uuid.UUID, body: PerfReviewRequest, db: Session = Depends(get_db)):
    table = _get_or_404(db, CatalogTable, table_id, "Table")
    if not credentials_configured():
        raise HTTPException(status.HTTP_409_CONFLICT, "Set ANTHROPIC_API_KEY in .env and restart the worker to enable Claude reviews")
    query, snapshot = _perf_reviews(db, table)
    if db.scalars(query.where(CatalogPerfReview.status.in_(_ACTIVE))).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A performance review of this table is already running")
    items = perf_service.select_code(db, table, body.scope, body.code_ids)
    if not items:
        raise HTTPException(422, "No procedures, functions or triggers to review in this scope")
    limit = get_settings().perf_review_max_objects
    if len(items) > limit:
        raise HTTPException(
            422,
            f"{len(items)} objects are in this scope; a review takes at most {limit}. Choose a narrower scope.",
        )
    review = CatalogPerfReview(
        connection_id=snapshot.connection_id, table_schema=table.schema, table_name=table.name,
        snapshot_id=snapshot.id, scope=body.scope, code_ids=[str(c.id) for c, _ in items],
    )
    db.add(review)
    db.flush()
    job = jobs.create_job(db, "table_perf_review", params={"review_id": str(review.id)}, connection_id=snapshot.connection_id)
    review.job_id = job.id
    db.commit()
    jobs.enqueue(job)
    return review


@router.get("/catalog-tables/{table_id}/perf-reviews", response_model=PerfReviewState)
def get_perf_review(table_id: uuid.UUID, db: Session = Depends(get_db)):
    """The latest finished review of this table (from any snapshot) and any running one.
    Each reviewed object is matched to the current snapshot: `current_code_id`, and
    `stale` when its source changed since the review."""
    table = _get_or_404(db, CatalogTable, table_id, "Table")
    query, snapshot = _perf_reviews(db, table)
    active = db.scalars(query.where(CatalogPerfReview.status.in_(_ACTIVE)).order_by(CatalogPerfReview.created_at.desc())).first()
    latest = db.scalars(query.where(CatalogPerfReview.status.in_([ScanStatus.SUCCEEDED, ScanStatus.FAILED]))
                        .order_by(CatalogPerfReview.created_at.desc())).first()
    latest_read = None
    if latest is not None:
        latest_read = PerfReviewRead.model_validate(latest)
        objects = latest.results.get("objects", [])
        names = {o["code_key"][2] for o in objects}
        current = {
            tuple(perf_service.code_key(c)): c
            for c in db.scalars(select(CatalogCode).where(CatalogCode.snapshot_id == snapshot.id, CatalogCode.name.in_(names)))
        }
        annotated = []
        for o in objects:
            code = current.get(tuple(o["code_key"]))
            annotated.append({
                **o, "current_code_id": str(code.id) if code else None,
                "stale": code is None or perf_service.source_hash(code.source) != o["source_hash"],
            })
        latest_read = latest_read.model_copy(update={"results": {**latest.results, "objects": annotated}})
    return {"latest": latest_read, "active": active, "credentials_configured": credentials_configured(),
            "model": get_settings().review_model}


# --- table compare -------------------------------------------------------------------------


@router.get("/catalog-snapshots/{snapshot_id}/find-table")
def find_table(snapshot_id: uuid.UUID, schema: str = Query(max_length=128), name: str = Query(max_length=128),
               db: Session = Depends(get_db)):
    """The table matching schema.name in this snapshot, ignoring case (Oracle names are
    upper case, PostgreSQL lower). Falls back to a table of that name in another schema
    when exactly one exists."""
    _get_or_404(db, CatalogSnapshot, snapshot_id, "Snapshot")
    base = select(CatalogTable).where(CatalogTable.snapshot_id == snapshot_id)
    exact = db.scalars(base.where(func.lower(CatalogTable.schema) == schema.lower(),
                                  func.lower(CatalogTable.name) == name.lower())).first()
    if exact is not None:
        return {"matched_by": "exact", "table": CatalogTableSummary.model_validate(exact)}
    by_name = db.scalars(base.where(func.lower(CatalogTable.name) == name.lower()).limit(2)).all()
    if len(by_name) == 1:
        return {"matched_by": "name", "table": CatalogTableSummary.model_validate(by_name[0])}
    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        f"No table {schema}.{name} here" + (f" (and {len(by_name)}+ tables named {name} in other schemas)" if by_name else ""),
    )


_OVERVIEW_CACHE: dict[tuple[uuid.UUID, uuid.UUID], dict] = {}


def _right_code_names(db: Session, snapshot_id: uuid.UUID) -> set[str]:
    names = set()
    for schema, name in db.execute(select(CatalogCode.schema, CatalogCode.name).where(CatalogCode.snapshot_id == snapshot_id)):
        names.update({name.lower(), f"{schema.lower()}.{name.lower()}"})
    return names


def _side_names(db: Session, left: CatalogSnapshot, right: CatalogSnapshot) -> tuple[tuple[str, str], tuple[str, str]]:
    lc, rc = db.get(Connection, left.connection_id), db.get(Connection, right.connection_id)
    names = (lc.name, rc.name) if lc.name != rc.name else (f"{lc.name} (left)", f"{rc.name} (right)")
    return names, (lc.kind.value, rc.kind.value)


@router.get("/catalog-compare/overview")
def compare_overview(left_snapshot: uuid.UUID, right_snapshot: uuid.UUID, db: Session = Depends(get_db)):
    """Every table of two snapshots compared at once: which have problems, differences, or
    exist on one side only. Snapshots never change, so results are cached per pair."""
    from app.catalog.bulk import snapshot_details
    from app.catalog.table_compare import overview

    key = (left_snapshot, right_snapshot)
    if key not in _OVERVIEW_CACHE:
        left = _get_or_404(db, CatalogSnapshot, left_snapshot, "Snapshot")
        right = _get_or_404(db, CatalogSnapshot, right_snapshot, "Snapshot")
        names, kinds = _side_names(db, left, right)
        result = overview(
            snapshot_details(db, left.id), snapshot_details(db, right.id), left_dialect=kinds[0], right_dialect=kinds[1],
            names=names, right_code_names=_right_code_names(db, right.id),
        )
        if len(_OVERVIEW_CACHE) >= 6:
            _OVERVIEW_CACHE.pop(next(iter(_OVERVIEW_CACHE)))
        _OVERVIEW_CACHE[key] = result
    return _OVERVIEW_CACHE[key]


@router.get("/catalog-compare")
def compare_tables(left: uuid.UUID, right: uuid.UUID, db: Session = Depends(get_db)):
    """Side-by-side comparison of two tables from (usually) two connections."""
    from app.catalog.table_compare import compare

    sides = []
    for table_id in (left, right):
        detail = get_table(table_id, db)
        snapshot = db.get(CatalogSnapshot, detail.snapshot_id)
        conn = db.get(Connection, snapshot.connection_id)
        sides.append({
            "connection_id": str(conn.id), "connection_name": conn.name, "kind": conn.kind.value,
            "snapshot_id": str(snapshot.id), "read_at": snapshot.finished_at, "table": detail.model_dump(mode="json"),
        })
    right_code_names = _right_code_names(db, uuid.UUID(sides[1]["snapshot_id"]))
    names = (sides[0]["connection_name"], sides[1]["connection_name"])
    if names[0] == names[1]:
        names = (f"{names[0]} (left)", f"{names[1]} (right)")
    result = compare(sides[0]["table"], sides[1]["table"], left_dialect=sides[0]["kind"], right_dialect=sides[1]["kind"],
                     names=names, right_code_names=right_code_names)
    return {"left": sides[0], "right": sides[1], "names": names, **result}
