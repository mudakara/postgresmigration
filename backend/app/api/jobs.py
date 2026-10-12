import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Job, JobStatus
from app.schemas import JobRead, JobSummary

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobSummary])
def list_jobs(
    project_id: uuid.UUID | None = None,
    connection_id: uuid.UUID | None = None,
    type: str | None = None,
    status_: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if project_id is not None:
        query = query.where(Job.project_id == project_id)
    if connection_id is not None:
        query = query.where(Job.connection_id == connection_id)
    if type is not None:
        query = query.where(Job.type == type)
    if status_ is not None:
        query = query.where(Job.status == status_)
    return db.scalars(query).all()


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job
