"""Background job framework.

The API creates a `Job` row and enqueues its id; the worker runs the handler that is
registered for the job's type and records the outcome on the row. Handlers receive a
`JobContext` to read the job, log output and report progress.
"""

import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Job, JobStatus

LOG_LIMIT = 500_000


class JobContext:
    def __init__(self, session: Session, job: Job):
        self.session = session
        self.job = job

    def log(self, text: str) -> None:
        combined = (self.job.log or "") + text.rstrip("\n") + "\n"
        self.job.log = combined[-LOG_LIMIT:]
        self.session.commit()

    def progress(self, fraction: float, message: str | None = None) -> None:
        self.job.progress = max(0.0, min(1.0, fraction))
        self.job.progress_message = message
        self.session.commit()


JobHandler = Callable[[JobContext], dict[str, Any] | None]
_HANDLERS: dict[str, JobHandler] = {}


def job_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    def register(fn: JobHandler) -> JobHandler:
        if job_type in _HANDLERS:
            raise ValueError(f"Duplicate handler for job type {job_type!r}")
        _HANDLERS[job_type] = fn
        return fn

    return register


def registered_types() -> list[str]:
    return sorted(_HANDLERS)


def create_job(
    session: Session,
    job_type: str,
    *,
    params: dict[str, Any] | None = None,
    project_id: uuid.UUID | None = None,
    connection_id: uuid.UUID | None = None,
) -> Job:
    if job_type not in _HANDLERS:
        raise ValueError(f"Unknown job type {job_type!r}")
    job = Job(
        type=job_type,
        status=JobStatus.QUEUED,
        params=params or {},
        project_id=project_id,
        connection_id=connection_id,
    )
    session.add(job)
    session.commit()
    return job


def enqueue(job: Job) -> None:
    from app.worker import run_job

    run_job.delay(str(job.id))


def execute_job(job_id: uuid.UUID, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        job = session.get(Job, job_id)
        # A redelivered message for a job that already started must not run it twice.
        if job is None or job.status is not JobStatus.QUEUED:
            return
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        session.commit()

        try:
            result = _HANDLERS[job.type](JobContext(session, job))
        except Exception as exc:
            session.rollback()
            job = session.get(Job, job_id)
            job.status = JobStatus.FAILED
            job.error = _describe(exc)[:4000]
            job.log = ((job.log or "") + traceback.format_exc())[-LOG_LIMIT:]
        else:
            job.status = JobStatus.SUCCEEDED
            job.result = result or {}
            job.progress = 1.0
            job.progress_message = None  # keep it on failure: it shows where the job stopped
        job.finished_at = datetime.now(UTC)
        session.commit()


def _describe(exc: Exception) -> str:
    if type(exc).__name__ == "LockNotAvailable":  # psycopg: our lock_timeout expired
        return ("LockNotAvailable: another session is changing a table this job needs (an exclusive lock is held, "
                "e.g. DDL or maintenance in progress), so the job stopped rather than wait. Try again when it has finished.")
    return f"{type(exc).__name__}: {exc}"


def fail_interrupted_jobs(session_factory: sessionmaker[Session]) -> int:
    """Mark jobs left RUNNING by a worker that stopped (restart, crash) as failed, together
    with the scan, comparison or catalog snapshot they were driving. Call when a worker
    starts, before it takes new work: no job can genuinely be running at that point."""
    from app.models import (
        CatalogPerfReview, CatalogSnapshot, ComparisonRun, Scan, ScanStatus, SequenceCheck, SequenceStats,
    )

    now = datetime.now(UTC)
    reason = "Interrupted: the worker stopped while this job was running. Start it again."
    with session_factory() as session:
        stuck = session.scalars(select(Job).where(Job.status == JobStatus.RUNNING)).all()
        for job in stuck:
            job.status = JobStatus.FAILED
            job.error = reason
            job.finished_at = now
            for model in (Scan, ComparisonRun, CatalogSnapshot, CatalogPerfReview, SequenceCheck, SequenceStats):
                for row in session.scalars(select(model).where(
                    model.job_id == job.id, model.status.in_([ScanStatus.QUEUED, ScanStatus.RUNNING])
                )):
                    row.status, row.error, row.finished_at = ScanStatus.FAILED, reason, now
        session.commit()
        return len(stuck)
