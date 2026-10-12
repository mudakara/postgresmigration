import csv
import io
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, func, or_, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, aliased, defer

from app.db import get_db
from app.models import ObjectDependency, Project, SourceObject
from app.scan.report import build_report, rule_catalogue
from app.scan.rules import RULES, UNREMARKABLE_SYS
from app.schemas import (
    DependencyRead,
    FindingRead,
    InventoryReport,
    ObjectPage,
    RuleRead,
    SourceObjectDetail,
    SourceObjectSummary,
    SourceRoutineRead,
)

router = APIRouter(tags=["inventory"])

SortKey = Literal["name", "type", "effort", "lines", "findings", "changed"]


def _project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


def _has_code(code: str):
    return type_coerce(SourceObject.finding_codes, JSONB).contains([code])


def _filtered(
    project_id: uuid.UUID,
    object_type: list[str] | None,
    owner: list[str] | None,
    complexity: list[str] | None,
    finding: list[str] | None,
    category: str | None,
    unsafe: bool | None,
    invalid: bool | None,
    q: str | None,
    include_removed: bool,
) -> Select:
    query = select(SourceObject).where(SourceObject.project_id == project_id)
    if not include_removed:
        query = query.where(SourceObject.removed_at.is_(None))
    if object_type:
        query = query.where(SourceObject.object_type.in_(object_type))
    if owner:
        query = query.where(SourceObject.owner.in_(owner))
    if complexity:
        query = query.where(SourceObject.complexity.in_(complexity))
    for code in finding or []:  # all listed findings must be present
        query = query.where(_has_code(code))
    if category:
        codes = [r.code for r in RULES if r.category == category]
        query = query.where(or_(*[_has_code(c) for c in codes]))
    if unsafe is not None:
        query = query.where(SourceObject.unsafe_to_execute.is_(unsafe))
    if invalid is not None:
        query = query.where((SourceObject.status != "VALID") if invalid else (SourceObject.status == "VALID"))
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(or_(SourceObject.name.ilike(pattern), SourceObject.owner.ilike(pattern)))
    return query


_SORT = {
    "name": [SourceObject.owner, SourceObject.name, SourceObject.object_type],
    "type": [SourceObject.object_type, SourceObject.owner, SourceObject.name],
    "effort": [SourceObject.effort],
    "lines": [SourceObject.line_count],
    "findings": [func.jsonb_array_length(type_coerce(SourceObject.finding_codes, JSONB))],
    "changed": [SourceObject.oracle_last_ddl],
}


def filter_params(
    object_type: list[str] | None = Query(default=None, alias="type"),
    owner: list[str] | None = Query(default=None),
    complexity: list[str] | None = Query(default=None),
    finding: list[str] | None = Query(default=None),
    category: str | None = None,
    unsafe: bool | None = None,
    invalid: bool | None = None,
    q: str | None = Query(default=None, max_length=200),
    include_removed: bool = False,
):
    return dict(object_type=object_type, owner=owner, complexity=complexity, finding=finding, category=category,
                unsafe=unsafe, invalid=invalid, q=q, include_removed=include_removed)


@router.get("/projects/{project_id}/report", response_model=InventoryReport)
def project_report(project_id: uuid.UUID, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    return build_report(db, project_id)


@router.get("/projects/{project_id}/objects", response_model=ObjectPage)
def list_objects(
    project_id: uuid.UUID,
    filters: dict = Depends(filter_params),
    sort: SortKey = "name",
    order: Literal["asc", "desc"] = "asc",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    _project_or_404(db, project_id)
    query = _filtered(project_id, **filters)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    columns = _SORT[sort]
    ordering = [c.desc().nulls_last() if order == "desc" else c.asc() for c in columns]
    if sort not in ("name",):
        ordering += [SourceObject.owner, SourceObject.name]
    items = db.scalars(
        query.options(defer(SourceObject.source), defer(SourceObject.findings))
        .order_by(*ordering).limit(limit).offset(offset)
    ).all()
    return ObjectPage(total=total, items=items)


@router.get("/projects/{project_id}/objects.csv")
def export_objects(project_id: uuid.UUID, filters: dict = Depends(filter_params), db: Session = Depends(get_db)):
    project = _project_or_404(db, project_id)
    query = _filtered(project_id, **filters).options(defer(SourceObject.source))
    rows = db.scalars(query.order_by(SourceObject.owner, SourceObject.object_type, SourceObject.name)).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "owner", "object_type", "name", "oracle_status", "lines", "complexity", "effort", "ora2pg_cost",
        "unsafe_to_execute", "wrapped", "findings", "sys_packages", "oracle_last_ddl",
    ])
    for o in rows:
        writer.writerow([
            o.owner, o.object_type, o.name, o.status, o.line_count, o.complexity, o.effort,
            "" if o.ora2pg_cost is None else o.ora2pg_cost, o.unsafe_to_execute, o.wrapped,
            " ".join(o.finding_codes), " ".join(o.sys_packages),
            o.oracle_last_ddl.isoformat() if o.oracle_last_ddl else "",
        ])
    filename = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in project.name) + "-inventory.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/objects/{object_id}", response_model=SourceObjectDetail)
def get_object(object_id: uuid.UUID, db: Session = Depends(get_db)):
    obj = db.get(SourceObject, object_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Object not found")

    depends_on = [
        DependencyRead(owner=d.referenced_owner, name=d.referenced_name, object_type=d.referenced_type,
                       link=d.referenced_link_name, object_id=d.referenced_object_id)
        for d in db.scalars(
            select(ObjectDependency).where(ObjectDependency.object_id == obj.id)
            .order_by(ObjectDependency.referenced_owner, ObjectDependency.referenced_name)
        )
        # Every PL/SQL unit depends on these; listing them is noise.
        if not (d.referenced_owner == "SYS" and d.referenced_name in UNREMARKABLE_SYS)
    ]
    user = aliased(SourceObject)
    used_by = [
        DependencyRead(owner=u.owner, name=u.name, object_type=u.object_type, object_id=u.id)
        for u in db.scalars(
            select(user).join(ObjectDependency, ObjectDependency.object_id == user.id)
            .where(ObjectDependency.referenced_object_id == obj.id, user.removed_at.is_(None))
            .order_by(user.owner, user.name)
        )
    ]
    summary = SourceObjectSummary.model_validate(obj).model_dump()
    return SourceObjectDetail(
        **summary,
        source=obj.source,
        findings=[FindingRead(**f) for f in obj.findings],
        metadata=obj.metadata_,
        oracle_created=obj.oracle_created,
        first_seen_scan_id=obj.first_seen_scan_id,
        last_seen_scan_id=obj.last_seen_scan_id,
        routines=[SourceRoutineRead.model_validate(r) for r in obj.routines],
        depends_on=depends_on,
        used_by=used_by,
    )


@router.get("/rules", response_model=list[RuleRead])
def list_rules():
    return rule_catalogue()
