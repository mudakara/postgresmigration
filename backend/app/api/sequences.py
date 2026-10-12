"""Sequence check: find sequences behind the migrated data and generate the fix script."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.db import get_db
from app.models import Connection, DbKind, ScanStatus, SequenceCheck
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
