import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.db import get_db
from app.models import Project, Scan, ScanStatus
from app.schemas import ScanCreate, ScanRead
from app.services.endpoints import scoped_schemas

router = APIRouter(tags=["scans"])


@router.post("/projects/{project_id}/scans", response_model=ScanRead, status_code=status.HTTP_202_ACCEPTED)
def start_scan(project_id: uuid.UUID, body: ScanCreate | None = None, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    active = db.scalars(
        select(Scan).where(Scan.project_id == project_id, Scan.status.in_([ScanStatus.QUEUED, ScanStatus.RUNNING]))
    ).first()
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A scan is already in progress for this project")

    body = body or ScanCreate()
    try:
        schemas = scoped_schemas(project.oracle_connection, project.schemas)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    scan = Scan(project_id=project_id, include_ora2pg_cost=body.include_ora2pg_cost, schemas=schemas)
    db.add(scan)
    db.flush()
    job = jobs.create_job(db, "scan", params={"scan_id": str(scan.id)}, project_id=project_id,
                          connection_id=project.oracle_connection_id)
    scan.job_id = job.id
    db.commit()
    jobs.enqueue(job)
    return scan


@router.get("/projects/{project_id}/scans", response_model=list[ScanRead])
def list_scans(project_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.scalars(select(Scan).where(Scan.project_id == project_id).order_by(Scan.created_at.desc())).all()


@router.get("/scans/{scan_id}", response_model=ScanRead)
def get_scan(scan_id: uuid.UUID, db: Session = Depends(get_db)):
    scan = db.get(Scan, scan_id)
    if scan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found")
    return scan
