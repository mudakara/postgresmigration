import uuid

from celery import Celery
from celery.signals import worker_ready

from app.config import get_settings

celery_app = Celery("migration_studio", broker=get_settings().redis_url)
celery_app.conf.update(
    task_ignore_result=True,  # outcomes live on the Job row, not in a result backend
    worker_prefetch_multiplier=1,  # jobs are long; don't hoard them on one worker process
    broker_connection_retry_on_startup=True,
)


@celery_app.task(name="jobs.run")
def run_job(job_id: str) -> None:
    from app.db import get_sessionmaker
    from app.jobs import execute_job

    execute_job(uuid.UUID(job_id), get_sessionmaker())


@worker_ready.connect
def _recover_interrupted_jobs(**_) -> None:
    # Single local worker: anything still RUNNING belongs to a worker that is gone.
    from app.db import get_sessionmaker
    from app.jobs import fail_interrupted_jobs

    count = fail_interrupted_jobs(get_sessionmaker())
    if count:
        print(f"Marked {count} interrupted job(s) as failed")
