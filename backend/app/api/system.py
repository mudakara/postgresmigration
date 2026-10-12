import os
import re

import redis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.jobs import registered_types
from app.schemas import HealthStatus, SystemInfo
from app.services.ora2pg import ora2pg_version

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthStatus)
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    try:
        redis_ok = bool(redis.Redis.from_url(get_settings().redis_url, socket_timeout=2).ping())
    except Exception:
        redis_ok = False
    return HealthStatus(
        status="ok" if db_ok and redis_ok else "degraded", database=db_ok, redis=redis_ok
    )


def _instant_client_version() -> str | None:
    # The image symlinks ORACLE_HOME to e.g. /opt/oracle/instantclient_23_9.
    target = os.path.realpath(get_settings().oracle_home)
    match = re.search(r"instantclient_(\d+)_(\d+)", target)
    return f"{match[1]}.{match[2]}" if match else None


@router.get("/system", response_model=SystemInfo)
def system_info():
    settings = get_settings()
    return SystemInfo(
        ora2pg_version=ora2pg_version(),
        instant_client=_instant_client_version(),
        oracle_client_mode=settings.oracle_client_mode,
        target_pg_version=settings.target_pg_version,
        job_types=registered_types(),
    )
