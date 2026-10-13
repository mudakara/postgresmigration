"""Sequence check: find sequences behind the migrated data and generate the fix script."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.db import get_db
from app.models import Connection, DbKind, ScanStatus, SequenceCheck, SequenceStats
from app.schemas import SequenceCheckCreate, SequenceCheckDetail, SequenceCheckRead
from app.sequences.script import fix_script

router = APIRouter(tags=["sequences"])


def _connection(db: Session, connection_id: uuid.UUID, kind: DbKind) -> Connection:
    conn = db.get(Connection, connection_id)
    if conn is None or conn.kind is not kind:
        label = "PostgreSQL" if kind is DbKind.POSTGRES else "Oracle"
        raise HTTPException(422, f"Choose a {label} connection")
    return conn


@router.post("/sequence-checks", response_model=SequenceCheckRead, status_code=status.HTTP_202_ACCEPTED)
def start_check(body: SequenceCheckCreate, db: Session = Depends(get_db)):
    pg = _connection(db, body.postgres_connection_id, DbKind.POSTGRES)
    if body.oracle_connection_id:
        _connection(db, body.oracle_connection_id, DbKind.ORACLE)
    active = db.scalars(select(SequenceCheck).where(
        SequenceCheck.postgres_connection_id == pg.id,
        SequenceCheck.status.in_([ScanStatus.QUEUED, ScanStatus.RUNNING]),
    )).first()
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A sequence check of this connection is already running")
    check = SequenceCheck(postgres_connection_id=pg.id, oracle_connection_id=body.oracle_connection_id,
                          requested_schemas=list(dict.fromkeys(body.schemas)))
    db.add(check)
    db.flush()
    job = jobs.create_job(db, "sequence_check", params={"check_id": str(check.id)}, connection_id=pg.id)
    check.job_id = job.id
    db.commit()
    jobs.enqueue(job)
    return check


@router.get("/sequence-checks", response_model=list[SequenceCheckRead])
def list_checks(postgres_connection_id: uuid.UUID | None = None, limit: int = Query(default=20, le=200),
                db: Session = Depends(get_db)):
    query = select(SequenceCheck).order_by(SequenceCheck.created_at.desc()).limit(limit)
    if postgres_connection_id:
        query = query.where(SequenceCheck.postgres_connection_id == postgres_connection_id)
    return db.scalars(query).all()


def _check(db: Session, check_id: uuid.UUID) -> SequenceCheck:
    check = db.get(SequenceCheck, check_id)
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sequence check not found")
    return check


@router.get("/sequence-checks/{check_id}", response_model=SequenceCheckDetail)
def get_check(check_id: uuid.UUID, db: Session = Depends(get_db)):
    return _check(db, check_id)


@router.get("/sequence-checks/{check_id}/fix.sql", response_class=PlainTextResponse)
def get_fix_script(check_id: uuid.UUID, scope: Literal["needed", "all"] = "needed", db: Session = Depends(get_db)):
    """The fix as a SQL script to run yourself: the app's own sessions are read-only."""
    check = _check(db, check_id)
    if check.status is not ScanStatus.SUCCEEDED:
        raise HTTPException(status.HTTP_409_CONFLICT, "The check has not finished")
    conn = db.get(Connection, check.postgres_connection_id)
    label = f"{conn.name} ({conn.host}:{conn.port}/{conn.database})" if conn else "PostgreSQL"
    text = fix_script(check.sequences, scope=scope, connection=label, checked_at=check.finished_at)
    filename = f"fix_sequences_{scope}_{check.finished_at:%Y%m%d_%H%M}.sql"
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --- sequence statistics -------------------------------------------------------------------


class SequenceStatsCreate(BaseModel):
    oracle_connection_id: uuid.UUID | None = None
    postgres_connection_id: uuid.UUID | None = None


@router.post("/sequence-stats", status_code=status.HTTP_202_ACCEPTED)
def start_stats(body: SequenceStatsCreate, db: Session = Depends(get_db)):
    if not body.oracle_connection_id and not body.postgres_connection_id:
        raise HTTPException(422, "Choose an Oracle connection, a PostgreSQL connection, or both")
    if body.oracle_connection_id:
        _connection(db, body.oracle_connection_id, DbKind.ORACLE)
    if body.postgres_connection_id:
        _connection(db, body.postgres_connection_id, DbKind.POSTGRES)
    active = db.scalars(select(SequenceStats).where(
        SequenceStats.oracle_connection_id.is_not_distinct_from(body.oracle_connection_id),
        SequenceStats.postgres_connection_id.is_not_distinct_from(body.postgres_connection_id),
        SequenceStats.status.in_([ScanStatus.QUEUED, ScanStatus.RUNNING]),
    )).first()
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This analysis is already running")
    stats = SequenceStats(oracle_connection_id=body.oracle_connection_id, postgres_connection_id=body.postgres_connection_id)
    db.add(stats)
    db.flush()
    job = jobs.create_job(db, "sequence_stats", params={"stats_id": str(stats.id)},
                          connection_id=body.postgres_connection_id or body.oracle_connection_id)
    stats.job_id = job.id
    db.commit()
    jobs.enqueue(job)
    return _stats_brief(stats)


def _stats_brief(s: SequenceStats) -> dict:
    return {"id": str(s.id), "oracle_connection_id": str(s.oracle_connection_id) if s.oracle_connection_id else None,
            "postgres_connection_id": str(s.postgres_connection_id) if s.postgres_connection_id else None,
            "job_id": str(s.job_id) if s.job_id else None, "status": s.status.value, "summary": s.summary,
            "warnings": s.warnings, "error": s.error, "created_at": s.created_at, "finished_at": s.finished_at}


@router.get("/sequence-stats")
def list_stats(limit: int = Query(default=10, le=100), db: Session = Depends(get_db)):
    return [_stats_brief(s) for s in db.scalars(select(SequenceStats).order_by(SequenceStats.created_at.desc()).limit(limit))]


@router.get("/sequence-stats/{stats_id}")
def get_stats(stats_id: uuid.UUID, db: Session = Depends(get_db)):
    s = db.get(SequenceStats, stats_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")
    return {**_stats_brief(s), "result": _recomputed(s)}


def _recomputed(s: SequenceStats) -> dict:
    """The comparison rebuilt from the stored sequences, so improvements to the rules apply
    to earlier analyses too (it is quick; reading the databases is the slow part)."""
    from app.sequences.stats import compare_tables, unlinked_by_name

    result = dict(s.result or {})
    if result.get("oracle") and result.get("postgres"):
        ora, pg = result["oracle"]["sequences"], result["postgres"]["sequences"]
        result["tables"] = compare_tables(ora, pg)
        result["unlinked"] = unlinked_by_name(ora, pg)
    return result


@router.get("/sequence-stats/{stats_id}/fix.sql", response_class=PlainTextResponse)
def stats_fix_script(stats_id: uuid.UUID, db: Session = Depends(get_db)):
    """Fix for the PostgreSQL sequences behind the data or behind Oracle, table by table."""
    s = db.get(SequenceStats, stats_id)
    if s is None or s.status is not ScanStatus.SUCCEEDED:
        raise HTTPException(status.HTTP_409_CONFLICT, "The analysis has not finished")
    grouped: dict[str, dict] = {}
    for row in _recomputed(s).get("tables", []):
        p, o = row.get("postgres"), row.get("oracle")
        if not p or row["status"] not in ("error", "warning"):
            continue
        seq = grouped.setdefault(p["sequence"], {
            "schema": p["seq_schema"], "name": p["seq_name"], "increment": p["increment"], "max_value": p["seq_max"],
            "columns": [], "oracle": None, "status": "behind", "needed_next": None,
            "message": "; ".join(i["message"] for i in row["issues"] if i["severity"] in ("error", "warning"))[:300],
        })
        schema, table = p["table"].split(".", 1)
        seq["columns"].append({"schema": schema, "table": table, "column": p["column_name"], "via": p["via"], "source": p["source"]})
        if o and (seq["oracle"] is None or o["last_number"] > seq["oracle"]["last_number"]):
            owner, name = o["sequence"].split(".", 1)
            seq["oracle"] = {"owner": owner, "name": name, "last_number": o["last_number"]}
    conn = db.get(Connection, s.postgres_connection_id) if s.postgres_connection_id else None
    label = f"{conn.name} ({conn.host}:{conn.port}/{conn.database})" if conn else "PostgreSQL"
    text = fix_script(list(grouped.values()), scope="needed", connection=label, checked_at=s.finished_at)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="fix_sequences_by_table_{s.finished_at:%Y%m%d_%H%M}.sql"'})
