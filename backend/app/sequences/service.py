"""The sequence check job."""

import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from app.jobs.registry import JobContext
from app.models import Connection, ScanStatus, SequenceCheck
from app.sequences.check import evaluate, read_oracle, read_postgres
from app.services.endpoints import Endpoint, schema_scope, scoped_schemas, use_user_grants

STATUS_ORDER = ["behind", "overflow", "behind_oracle", "check_failed", "no_privilege", "descending", "unlinked", "ok"]


def run_check(ctx: JobContext, check_id: uuid.UUID) -> dict[str, Any]:
    session = ctx.session
    check = session.get(SequenceCheck, check_id)
    check.status = ScanStatus.RUNNING
    check.started_at = datetime.now(UTC)
    session.commit()

    pg = session.get(Connection, check.postgres_connection_id)
    seqs, schemas, warnings = read_postgres(Endpoint.from_connection(pg), scoped_schemas(pg, check.requested_schemas), ctx.progress)

    oracle_seqs = None
    if check.oracle_connection_id:
        ora = session.get(Connection, check.oracle_connection_id)
        ctx.progress(0.92, "Reading Oracle sequence values")
        try:
            owners = [s for s in (x.upper() for x in schemas) if not schema_scope(ora) or s in schema_scope(ora)]
            oracle_seqs = read_oracle(Endpoint.from_connection(ora), owners, user_grants=use_user_grants(ora)) if owners else []
        except Exception as exc:  # the PostgreSQL side is still worth reporting
            warnings.append(f"Oracle sequences could not be read: {type(exc).__name__}: {exc}"[:500])
        else:
            if use_user_grants(ora):
                warnings.append("The Oracle account reads ALL_SEQUENCES, so only sequences it has privileges on are "
                                "compared. Granting SELECT ANY DICTIONARY lets the check read DBA_SEQUENCES.")
            if not oracle_seqs:
                warnings.append(
                    "No Oracle sequences found in schemas named like the PostgreSQL ones "
                    f"({', '.join(s.upper() for s in schemas[:5])}{'...' if len(schemas) > 5 else ''}). "
                    f"The PostgreSQL connection is to database \"{pg.database}\", which has {len(schemas)} schema(s): "
                    "if the migrated schemas are in another database on that server, point the connection there."
                )

    evaluate(seqs, oracle_seqs)
    seqs.sort(key=lambda s: (STATUS_ORDER.index(s.status), s.schema, s.name))
    counts = Counter(s.status for s in seqs)
    check.sequences = [s.as_dict() for s in seqs]
    check.schemas = schemas
    check.warnings = warnings
    check.summary = {
        "sequences": len(seqs), "status": {k: counts.get(k, 0) for k in STATUS_ORDER},
        "linked": sum(1 for s in seqs if s.columns),
        "oracle_matched": sum(1 for s in seqs if s.oracle), "oracle_compared": oracle_seqs is not None,
    }
    check.status = ScanStatus.SUCCEEDED
    check.finished_at = datetime.now(UTC)
    session.commit()
    return {"check_id": str(check.id), **check.summary}


def run_stats(ctx: JobContext, stats_id: uuid.UUID) -> dict[str, Any]:
    from app.models import SequenceStats
    from app.sequences.stats import run

    session = ctx.session
    stats = session.get(SequenceStats, stats_id)
    stats.status = ScanStatus.RUNNING
    stats.started_at = datetime.now(UTC)
    session.commit()

    ora = session.get(Connection, stats.oracle_connection_id) if stats.oracle_connection_id else None
    pg = session.get(Connection, stats.postgres_connection_id) if stats.postgres_connection_id else None
    result = run(
        Endpoint.from_connection(ora) if ora else None, scoped_schemas(ora, []) if ora else [],
        use_user_grants(ora) if ora else False,
        Endpoint.from_connection(pg) if pg else None, scoped_schemas(pg, []) if pg else [],
        ctx.progress,
    )
    if ora and use_user_grants(ora):
        result["warnings"].insert(0, "The Oracle account reads ALL_* views: only sequences and code it has privileges on are included.")
    stats.result = {k: v for k, v in result.items() if k not in ("summary", "warnings")}
    stats.summary = result["summary"]
    stats.warnings = result["warnings"]
    stats.status = ScanStatus.SUCCEEDED
    stats.finished_at = datetime.now(UTC)
    session.commit()
    return {"stats_id": str(stats.id), **result["summary"]}
