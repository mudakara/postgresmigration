import uuid
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, or_, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app import jobs
from app.compare.pairing import CONVENTIONS
from app.compare.review_service import credentials_configured, estimate_for
from app.compare.service import evaluate, risk_of
from app.compare.units import load_unit
from app.config import get_settings
from app.db import get_db
from app.models import ComparisonRun, ObjectPair, Project, ScanStatus, SourceObject, TargetObject
from app.schemas import (
    ComparisonOverview,
    ComparisonRunRead,
    JobRead,
    OracleUnitRead,
    PairDetail,
    PairPage,
    PairSummary,
    PairUpdate,
    ReviewEstimate,
    ReviewRequest,
    TargetObjectDetail,
    TargetObjectSummary,
)

router = APIRouter(tags=["comparison"])


def _project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


def _pair_or_404(db: Session, pair_id: uuid.UUID) -> ObjectPair:
    pair = db.get(ObjectPair, pair_id)
    if pair is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pair not found")
    return pair


@router.post("/projects/{project_id}/comparisons", response_model=ComparisonRunRead, status_code=status.HTTP_202_ACCEPTED)
def start_comparison(project_id: uuid.UUID, db: Session = Depends(get_db)):
    project = _project_or_404(db, project_id)
    if project.postgres_connection_id is None:
        raise HTTPException(422, "Set the project's PostgreSQL target connection first")
    active = db.scalars(select(ComparisonRun).where(
        ComparisonRun.project_id == project_id, ComparisonRun.status.in_([ScanStatus.QUEUED, ScanStatus.RUNNING])
    )).first()
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A comparison is already running for this project")
    run = ComparisonRun(project_id=project_id)
    db.add(run)
    db.flush()
    job = jobs.create_job(db, "compare", params={"run_id": str(run.id)}, project_id=project_id,
                          connection_id=project.postgres_connection_id)
    run.job_id = job.id
    db.commit()
    jobs.enqueue(job)
    return run


@router.get("/projects/{project_id}/comparisons", response_model=list[ComparisonRunRead])
def list_comparisons(project_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.scalars(select(ComparisonRun).where(ComparisonRun.project_id == project_id)
                      .order_by(ComparisonRun.created_at.desc())).all()


@router.get("/projects/{project_id}/comparison", response_model=ComparisonOverview)
def comparison_overview(project_id: uuid.UUID, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    runs = db.scalars(select(ComparisonRun).where(ComparisonRun.project_id == project_id)
                      .order_by(ComparisonRun.created_at.desc())).all()
    last_ok = next((r for r in runs if r.status == ScanStatus.SUCCEEDED), None)
    pairs = db.execute(select(
        ObjectPair.status, ObjectPair.risk, ObjectPair.signature_status, ObjectPair.ai_verdict,
        ObjectPair.ai_stale, ObjectPair.review_status, ObjectPair.unit_kind,
    ).where(ObjectPair.project_id == project_id)).all()
    paired = {t for (t,) in db.execute(select(ObjectPair.target_object_id).where(
        ObjectPair.project_id == project_id, ObjectPair.target_object_id.is_not(None)))}
    targets = db.execute(select(TargetObject.id, TargetObject.metadata_).where(
        TargetObject.project_id == project_id, TargetObject.removed_at.is_(None))).all()
    unpaired = sum(1 for t_id, meta in targets if t_id not in paired and not (meta or {}).get("is_trigger_function"))

    matched = [p for p in pairs if p.status == "matched"]
    counts = {
        "units": len(pairs),
        "status": dict(Counter(p.status for p in pairs)),
        "kind": dict(Counter(p.unit_kind for p in pairs)),
        "risk": dict(Counter(p.risk for p in pairs)),
        "signature": dict(Counter(p.signature_status for p in matched if p.signature_status)),
        "ai_verdict": dict(Counter(p.ai_verdict for p in matched if p.ai_verdict)),
        "review_status": dict(Counter(p.review_status for p in pairs)),
        "unpaired_targets": unpaired,
        "target_objects": len(targets),
    }
    settings = get_settings()
    return ComparisonOverview(
        last_run=runs[0] if runs else None,
        last_successful_run=last_ok,
        counts=counts,
        conventions={
            k: {"count": v, "label": CONVENTIONS.get(k, (k, 0))[0]}
            for k, v in ((last_ok.summary.get("conventions") if last_ok else {}) or {}).items()
        },
        review={
            "credentials_configured": credentials_configured(),
            "model": settings.review_model,
            "effort": settings.review_effort,
            "reviewed": sum(1 for p in matched if p.ai_verdict),
            "stale": sum(1 for p in matched if p.ai_stale),
        },
    )


_RISK_ORDER = case({"high": 0, "medium": 1, "low": 2, "none": 3}, value=ObjectPair.risk, else_=4)


@router.get("/projects/{project_id}/pairs", response_model=PairPage)
def list_pairs(
    project_id: uuid.UUID,
    status_: list[str] | None = Query(default=None, alias="status"),
    risk: list[str] | None = Query(default=None),
    signature: list[str] | None = Query(default=None),
    ai: str | None = Query(default=None, description="verdict, 'none' or 'stale'"),
    review: str | None = None,
    kind: str | None = None,
    finding: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    sort: Literal["risk", "name", "confidence"] = "risk",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    _project_or_404(db, project_id)
    query = select(ObjectPair, TargetObject).outerjoin(TargetObject, TargetObject.id == ObjectPair.target_object_id) \
        .where(ObjectPair.project_id == project_id)
    if status_:
        query = query.where(ObjectPair.status.in_(status_))
    if risk:
        query = query.where(ObjectPair.risk.in_(risk))
    if signature:
        query = query.where(ObjectPair.signature_status.in_(signature))
    if ai == "none":
        query = query.where(ObjectPair.ai_verdict.is_(None), ObjectPair.status == "matched")
    elif ai == "stale":
        query = query.where(ObjectPair.ai_stale.is_(True))
    elif ai:
        query = query.where(ObjectPair.ai_verdict == ai)
    if review:
        query = query.where(ObjectPair.review_status == review)
    if kind:
        query = query.where(ObjectPair.unit_kind == kind)
    if finding:
        query = query.where(type_coerce(ObjectPair.static_findings, JSONB).contains([{"code": finding}]))
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(or_(ObjectPair.oracle_name.ilike(pattern), TargetObject.name.ilike(pattern)))
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    ordering = {
        "risk": [_RISK_ORDER, ObjectPair.oracle_name],
        "name": [ObjectPair.oracle_name],
        "confidence": [ObjectPair.match_confidence.asc().nulls_first(), ObjectPair.oracle_name],
    }[sort]
    rows = db.execute(query.order_by(*ordering).limit(limit).offset(offset)).all()
    items = [_summary(pair, target) for pair, target in rows]
    return PairPage(total=total, items=items)


def _summary(pair: ObjectPair, target: TargetObject | None) -> PairSummary:
    summary = PairSummary.model_validate(pair)
    summary.target_name = target.display_name if target is not None else None
    summary.finding_count = len(pair.static_findings) + len([d for d in pair.signature_diffs if d["severity"] != "low"])
    return summary


def _detail(db: Session, pair: ObjectPair) -> PairDetail:
    target = db.get(TargetObject, pair.target_object_id) if pair.target_object_id else None
    unit = load_unit(db, pair.source_key)
    oracle = None
    if unit is not None:
        holder = db.get(SourceObject, unit.source_object_id)
        member_lines = unit.source.rstrip("\n").count("\n") + 1
        oracle = OracleUnitRead(
            kind=unit.kind, display_name=unit.display_name, routine_kind=unit.routine_kind,
            signature=unit.signature, arguments=unit.arguments, return_type=unit.return_type,
            source_object_id=unit.source_object_id,
            object_label=f"{holder.owner}.{holder.name} ({holder.object_type})" if holder else unit.display_name,
            source=unit.object_source,
            member_range=[unit.source_start_line, unit.source_start_line + member_lines - 1]
            if unit.source != unit.object_source else None,
        )
    alternatives = []
    for alt in pair.alternatives:
        t = db.get(TargetObject, uuid.UUID(alt))
        if t is not None and t.removed_at is None:
            alternatives.append(TargetObjectSummary.model_validate(t))
    base = _summary(pair, target).model_dump()
    return PairDetail(
        **base,
        alternatives=alternatives,
        signature_diffs=pair.signature_diffs,
        static_findings=pair.static_findings,
        ai_review=pair.ai_review,
        review_note=pair.review_note,
        oracle=oracle,
        target=TargetObjectDetail.model_validate(target) if target is not None else None,
    )


@router.get("/pairs/{pair_id}", response_model=PairDetail)
def get_pair(pair_id: uuid.UUID, db: Session = Depends(get_db)):
    return _detail(db, _pair_or_404(db, pair_id))


@router.patch("/pairs/{pair_id}", response_model=PairDetail)
def update_pair(pair_id: uuid.UUID, body: PairUpdate, db: Session = Depends(get_db)):
    pair = _pair_or_404(db, pair_id)
    match_changes = [f for f in ("target_object_id", "ignore", "reset") if f in body.model_fields_set and getattr(body, f) is not None]
    if len(match_changes) > 1:
        raise HTTPException(422, "Choose one of target_object_id, ignore or reset")

    if body.target_object_id is not None:
        target = db.get(TargetObject, body.target_object_id)
        if target is None or target.project_id != pair.project_id or target.removed_at is not None:
            raise HTTPException(422, "Target object not found in this project")
        taken = db.scalars(select(ObjectPair).where(
            ObjectPair.target_object_id == target.id, ObjectPair.id != pair.id)).first()
        if taken is not None:
            # A person's choice wins; the other unit becomes unmatched until the next run.
            taken.target_object_id = None
            taken.status = "missing_in_target"
            taken.manual = False
            taken.risk = "high"
        pair.target_object_id = target.id
        pair.status, pair.manual, pair.match_method, pair.match_confidence = "matched", True, "manual", 1.0
    elif body.ignore:
        pair.status, pair.manual, pair.target_object_id = "ignored", True, None
    elif body.reset:
        pair.manual = False
        if pair.status == "ignored":
            pair.status = "missing_in_target"
    if match_changes and not body.reset:
        # Re-check immediately so the page reflects the new match without a full run.
        unit = load_unit(db, pair.source_key)
        target = db.get(TargetObject, pair.target_object_id) if pair.target_object_id else None
        orafce = False
        last = db.scalars(select(ComparisonRun).where(
            ComparisonRun.project_id == pair.project_id, ComparisonRun.status == ScanStatus.SUCCEEDED
        ).order_by(ComparisonRun.created_at.desc())).first()
        if last is not None:
            orafce = bool(last.summary.get("orafce"))
        checks = evaluate(unit, target, orafce=orafce) if unit is not None and target is not None \
            else {"signature_status": None, "signature_diffs": [], "static_findings": []}
        pair.signature_status = checks["signature_status"]
        pair.signature_diffs = checks["signature_diffs"]
        pair.static_findings = checks["static_findings"]
        pair.risk = risk_of(pair.status, pair.signature_diffs, pair.static_findings)
        pair.target_hash = target.source_hash if target is not None else None
        if pair.ai_review:
            pair.ai_stale = True

    if "review_status" in body.model_fields_set and body.review_status is not None:
        pair.review_status = body.review_status
    if "review_note" in body.model_fields_set:
        pair.review_note = body.review_note
    db.commit()
    return _detail(db, pair)


@router.get("/projects/{project_id}/target-objects", response_model=list[TargetObjectSummary])
def list_target_objects(
    project_id: uuid.UUID,
    q: str | None = Query(default=None, max_length=200),
    unpaired: bool = False,
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    _project_or_404(db, project_id)
    query = select(TargetObject).where(TargetObject.project_id == project_id, TargetObject.removed_at.is_(None))
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(or_(TargetObject.name.ilike(pattern), TargetObject.schema.ilike(pattern)))
    if kind:
        query = query.where(TargetObject.kind == kind)
    if unpaired:
        paired = select(ObjectPair.target_object_id).where(
            ObjectPair.project_id == project_id, ObjectPair.target_object_id.is_not(None))
        query = query.where(TargetObject.id.not_in(paired),
                            type_coerce(TargetObject.metadata_, JSONB)["is_trigger_function"].astext.is_distinct_from("true"))
    return db.scalars(query.order_by(TargetObject.schema, TargetObject.name).limit(limit)).all()


@router.get("/target-objects/{target_id}", response_model=TargetObjectDetail)
def get_target_object(target_id: uuid.UUID, db: Session = Depends(get_db)):
    target = db.get(TargetObject, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target object not found")
    return target


@router.post("/projects/{project_id}/reviews/estimate", response_model=ReviewEstimate)
def review_estimate(project_id: uuid.UUID, body: ReviewRequest, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    result = estimate_for(db, project_id, body.scope, body.pair_ids)
    return ReviewEstimate(**{k: v for k, v in result.items() if k != "pair_ids"},
                          credentials_configured=credentials_configured())


@router.post("/projects/{project_id}/reviews", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def start_reviews(project_id: uuid.UUID, body: ReviewRequest, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    if not credentials_configured():
        raise HTTPException(422, "Claude reviews need ANTHROPIC_API_KEY in .env (then restart the api and worker)")
    result = estimate_for(db, project_id, body.scope, body.pair_ids)
    if not result["pair_ids"]:
        raise HTTPException(422, "No matched pairs in that selection need a review")
    job = jobs.create_job(db, "ai_review", params={"pair_ids": result["pair_ids"], "scope": body.scope,
                                                   "model": result["model"]}, project_id=project_id)
    jobs.enqueue(job)
    return job
