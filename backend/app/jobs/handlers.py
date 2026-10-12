"""Job handlers. Importing this module registers them with the job registry."""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.jobs.registry import JobContext, job_handler
from app.models import (
    CatalogPerfReview, CatalogSnapshot, ComparisonRun, Connection, DbKind, Scan, ScanStatus, SequenceCheck,
)
from app.services import ora2pg
from app.services.endpoints import Endpoint, use_user_grants


@job_handler("ora2pg_check")
def ora2pg_check(ctx: JobContext) -> dict[str, Any]:
    """Prove the full Ora2Pg toolchain (Perl, DBD::Oracle, Instant Client) can reach Oracle."""
    conn = ctx.session.get(Connection, ctx.job.connection_id)
    if conn is None or conn.kind is not DbKind.ORACLE:
        raise ValueError("ora2pg_check needs an existing Oracle connection")

    ctx.progress(0.1, "Running ora2pg -t SHOW_VERSION")
    with ora2pg.workspace() as work:
        result = ora2pg.run(
            Endpoint.from_connection(conn),
            "SHOW_VERSION",
            work,
            user_grants=use_user_grants(conn),
            timeout=120,
        )
    ctx.log(result.combined_output())
    if not result.succeeded:
        raise ora2pg.Ora2PgError(result.failure_summary())

    banner = next(
        (ln.strip() for ln in result.stdout.splitlines() if ln.strip().startswith("Oracle")),
        result.stdout.strip(),
    )
    return {
        "ora2pg_version": ora2pg.ora2pg_version(),
        "oracle_banner": banner,
        "user_grants": use_user_grants(conn),
        "duration_seconds": result.duration_seconds,
    }


@job_handler("scan")
def scan(ctx: JobContext) -> dict[str, Any]:
    """Inventory a project's Oracle schemas (see app.scan.service)."""
    from app.scan.service import run_scan

    scan_id = uuid.UUID(ctx.job.params["scan_id"])
    try:
        return run_scan(ctx, scan_id)
    except Exception as exc:
        # The job framework records the job failure; mirror it on the scan row.
        ctx.session.rollback()
        failed = ctx.session.get(Scan, scan_id)
        if failed is not None:
            failed.status = ScanStatus.FAILED
            failed.error = f"{type(exc).__name__}: {exc}"[:4000]
            failed.finished_at = datetime.now(UTC)
            ctx.session.commit()
        raise


@job_handler("compare")
def compare(ctx: JobContext) -> dict[str, Any]:
    """Pair the Oracle inventory with the migrated PostgreSQL objects and check each pair."""
    from app.compare.service import run_comparison

    run_id = uuid.UUID(ctx.job.params["run_id"])
    try:
        return run_comparison(ctx, run_id)
    except Exception as exc:
        ctx.session.rollback()
        failed = ctx.session.get(ComparisonRun, run_id)
        if failed is not None:
            failed.status = ScanStatus.FAILED
            failed.error = f"{type(exc).__name__}: {exc}"[:4000]
            failed.finished_at = datetime.now(UTC)
            ctx.session.commit()
        raise


@job_handler("ai_review")
def ai_review(ctx: JobContext) -> dict[str, Any]:
    """Claude review of the selected pairs."""
    from app.compare.review_service import run_reviews

    return run_reviews(ctx, ctx.job.params["pair_ids"])


@job_handler("catalog_snapshot")
def catalog_snapshot(ctx: JobContext) -> dict[str, Any]:
    """Read a connection's tables, relationships and code for the schema explorer."""
    from app.catalog.service import run_snapshot

    snapshot_id = uuid.UUID(ctx.job.params["snapshot_id"])
    try:
        return run_snapshot(ctx, snapshot_id)
    except Exception as exc:
        ctx.session.rollback()
        failed = ctx.session.get(CatalogSnapshot, snapshot_id)
        if failed is not None:
            failed.status = ScanStatus.FAILED
            failed.error = f"{type(exc).__name__}: {exc}"[:4000]
            failed.finished_at = datetime.now(UTC)
            ctx.session.commit()
        raise


@job_handler("table_perf_review")
def table_perf_review(ctx: JobContext) -> dict[str, Any]:
    """Claude performance review of the code that uses one table (schema explorer)."""
    from app.catalog.perf_service import run_perf_review

    review_id = uuid.UUID(ctx.job.params["review_id"])
    try:
        return run_perf_review(ctx, review_id)
    except Exception as exc:
        ctx.session.rollback()
        failed = ctx.session.get(CatalogPerfReview, review_id)
        if failed is not None:
            failed.status = ScanStatus.FAILED
            failed.error = f"{type(exc).__name__}: {exc}"[:4000]
            failed.finished_at = datetime.now(UTC)
            ctx.session.commit()
        raise


@job_handler("sequence_check")
def sequence_check(ctx: JobContext) -> dict[str, Any]:
    """Find PostgreSQL sequences that are behind the migrated data (app.sequences)."""
    from app.sequences.service import run_check

    check_id = uuid.UUID(ctx.job.params["check_id"])
    try:
        return run_check(ctx, check_id)
    except Exception as exc:
        ctx.session.rollback()
        failed = ctx.session.get(SequenceCheck, check_id)
        if failed is not None:
            failed.status = ScanStatus.FAILED
            failed.error = f"{type(exc).__name__}: {exc}"[:4000]
            failed.finished_at = datetime.now(UTC)
            ctx.session.commit()
        raise
