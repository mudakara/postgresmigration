"""The LOB data check job."""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.jobs.registry import JobContext
from app.lobs.check import compare, run, summarize
from app.models import Connection, LobCheck, ScanStatus
from app.services.endpoints import Endpoint, scoped_schemas, use_user_grants


def run_lob_check(ctx: JobContext, check_id: uuid.UUID) -> dict[str, Any]:
    session = ctx.session
    check = session.get(LobCheck, check_id)
    check.status = ScanStatus.RUNNING
    check.started_at = datetime.now(UTC)
    session.commit()

    ora = session.get(Connection, check.oracle_connection_id)
    pg = session.get(Connection, check.postgres_connection_id)
    ora_schemas = scoped_schemas(ora, [])
    if not ora_schemas:
        raise ValueError(f"Connection {ora.name} has no schema scope: edit it and list the schemas to check, "
                         "so the check doesn't count every LOB table in the database.")
    options = check.options or {}
    result = run(
        Endpoint.from_connection(ora), ora_schemas, use_user_grants(ora),
        Endpoint.from_connection(pg), scoped_schemas(pg, []),
        ctx.progress, sizes=options.get("sizes", True), timeout_seconds=options.get("timeout_seconds", 300),
    )
    warnings = result.pop("warnings")
    if use_user_grants(ora):
        warnings.insert(0, "The Oracle account reads ALL_* views: only tables it has privileges on are included.")
    check.result = result
    check.summary = summarize(compare(result["oracle"], result["postgres"]))
    check.warnings = warnings
    check.status = ScanStatus.SUCCEEDED
    check.finished_at = datetime.now(UTC)
    session.commit()
    return {"check_id": str(check.id), **check.summary}
