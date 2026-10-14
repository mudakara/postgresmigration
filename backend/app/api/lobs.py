"""LOB data check: tables with LOB columns counted on Oracle and PostgreSQL (Overview page)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.db import get_db
from app.lobs.check import compare, summarize
from app.models import Connection, DbKind, LobCheck, ScanStatus

router = APIRouter(tags=["lobs"])


class LobCheckCreate(BaseModel):
    oracle_connection_id: uuid.UUID
    postgres_connection_id: uuid.UUID
    sizes: bool = True  # also total and largest length of the values (slower than counting)
    timeout_seconds: int = Field(default=300, ge=30, le=7200)  # per table


def _connection(db: Session, connection_id: uuid.UUID, kind: DbKind) -> Connection:
    conn = db.get(Connection, connection_id)
    if conn is None or conn.kind is not kind:
        raise HTTPException(422, f"Choose a {'PostgreSQL' if kind is DbKind.POSTGRES else 'Oracle'} connection")
    return conn


def _brief(c: LobCheck) -> dict:
    return {"id": str(c.id), "oracle_connection_id": str(c.oracle_connection_id), "postgres_connection_id": str(c.postgres_connection_id),
            "job_id": str(c.job_id) if c.job_id else None, "status": c.status.value, "options": c.options, "summary": c.summary,
            "warnings": c.warnings, "error": c.error, "created_at": c.created_at, "finished_at": c.finished_at}


@router.post("/lob-checks", status_code=status.HTTP_202_ACCEPTED)
def start_lob_check(body: LobCheckCreate, db: Session = Depends(get_db)):
    _connection(db, body.oracle_connection_id, DbKind.ORACLE)
    _connection(db, body.postgres_connection_id, DbKind.POSTGRES)
    active = db.scalars(select(LobCheck).where(
        LobCheck.oracle_connection_id == body.oracle_connection_id,
        LobCheck.postgres_connection_id == body.postgres_connection_id,
        LobCheck.status.in_([ScanStatus.QUEUED, ScanStatus.RUNNING]),
    )).first()
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A LOB check of these databases is already running")
    check = LobCheck(oracle_connection_id=body.oracle_connection_id, postgres_connection_id=body.postgres_connection_id,
                     options={"sizes": body.sizes, "timeout_seconds": body.timeout_seconds})
    db.add(check)
    db.flush()
    job = jobs.create_job(db, "lob_check", params={"check_id": str(check.id)}, connection_id=body.postgres_connection_id)
    check.job_id = job.id
    db.commit()
    jobs.enqueue(job)
    return _brief(check)


@router.get("/lob-checks")
def list_lob_checks(limit: int = Query(default=10, le=100), db: Session = Depends(get_db)):
    return [_brief(c) for c in db.scalars(select(LobCheck).order_by(LobCheck.created_at.desc()).limit(limit))]


@router.get("/lob-checks/{check_id}")
def get_lob_check(check_id: uuid.UUID, db: Session = Depends(get_db)):
    c = db.get(LobCheck, check_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LOB check not found")
    out = _brief(c)
    if c.status is ScanStatus.SUCCEEDED:
        # Rebuilt from the stored counts, so improvements to the rules apply to earlier checks too.
        tables = compare(c.result.get("oracle", []), c.result.get("postgres", []))
        out.update(tables=tables, summary=summarize(tables), sizes=c.result.get("sizes", True))
    return out
