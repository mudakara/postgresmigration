"""Comparison run: read the PostgreSQL catalog, pair, check, persist."""

import re
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compare.pairing import TargetRef, pair_units
from app.compare.signatures import compare_signatures
from app.compare.static_checks import check_pair
from app.compare.scratch_compile import compile_in_scratch
from app.compare.target_extract import TargetCatalog, read_catalog
from app.compare.units import OracleUnit, load_units
from app.config import get_settings
from app.jobs.registry import JobContext
from app.models import ComparisonRun, ObjectDependency, ObjectPair, Project, Scan, ScanStatus, TargetObject
from app.services.endpoints import Endpoint, scoped_schemas

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def _upsert_targets(session: Session, project_id: uuid.UUID, run: ComparisonRun, catalog: TargetCatalog) -> dict:
    existing = {
        (t.schema, t.kind, t.name, t.identity_args): t
        for t in session.scalars(select(TargetObject).where(TargetObject.project_id == project_id))
    }
    seen = set()
    counts: Counter[str] = Counter()
    for ext in catalog.objects:
        seen.add(ext.key)
        obj = existing.get(ext.key)
        if obj is None:
            obj = TargetObject(id=uuid.uuid4(), project_id=project_id, schema=ext.schema, kind=ext.kind,
                               name=ext.name, identity_args=ext.identity_args)
            session.add(obj)
            existing[ext.key] = obj
            counts["added"] += 1
        elif obj.source_hash != ext.source_hash:
            counts["changed"] += 1
        obj.result_type = ext.result_type
        obj.language = ext.language
        obj.arguments = ext.arguments
        obj.n_defaults = ext.n_defaults
        obj.source = ext.source
        obj.source_hash = ext.source_hash
        obj.metadata_ = ext.metadata
        obj.check_results = ext.check_results
        obj.last_seen_run_id = run.id
        obj.removed_at = None
    now = datetime.now(UTC)
    for key, obj in existing.items():
        if key not in seen and obj.removed_at is None:
            obj.removed_at = now
            counts["removed"] += 1
    session.flush()
    return {key: obj for key, obj in existing.items() if obj.removed_at is None}


def risk_of(status: str, signature_diffs: list[dict], findings: list[dict]) -> str:
    if status == "missing_in_target":
        return "high"
    if status == "ignored":
        return "none"
    worst = max([SEVERITY_RANK[d["severity"]] for d in signature_diffs + findings] or [0])
    return {3: "high", 2: "medium", 1: "low", 0: "none"}[worst]


def evaluate(unit: OracleUnit, target: TargetObject | None, *, orafce: bool) -> dict[str, Any]:
    """Signature comparison and static checks for one unit/target pair."""
    if target is None:
        return {"signature_status": None, "signature_diffs": [], "static_findings": []}
    signature_status, diffs = None, []
    if unit.kind == "routine":
        signature_status, sig = compare_signatures(
            unit.routine_kind, unit.arguments, unit.return_type, target.kind, target.arguments,
            target.result_type, target.n_defaults,
        )
        diffs = [d.as_dict() for d in sig]
    findings = check_pair(
        unit.kind, unit.source, unit.source_start_line, unit.arguments, target.kind, target.source or "",
        target.check_results, orafce=orafce, oracle_trigger=unit.metadata.get("trigger"),
        target_trigger_definition=(target.metadata_ or {}).get("definition"),
    )
    return {
        "signature_status": signature_status,
        "signature_diffs": diffs,
        "static_findings": [f.as_dict() for f in findings],
    }


def add_dependency_findings(
    session: Session,
    project_id: uuid.UUID,
    units: list[OracleUnit],
    pairs: dict[str, ObjectPair],
    targets_by_id: dict[uuid.UUID, TargetObject],
) -> None:
    """Flag pairs that call a standalone routine (or read a view) whose own migration has
    high-risk findings: the callee's behaviour change carries over to the caller.
    One level deep, so each finding names a direct callee."""
    unit_by_key = {u.key: u for u in units}
    callee_pairs: dict[uuid.UUID, ObjectPair] = {}
    for key, pair in pairs.items():
        unit = unit_by_key.get(key)
        if unit is not None and pair.risk == "high" and (unit.kind == "view" or (unit.kind == "routine" and not unit.package)):
            callee_pairs[unit.source_object_id] = pair
    if not callee_pairs:
        return
    deps = defaultdict(set)
    for object_id, referenced in session.execute(
        select(ObjectDependency.object_id, ObjectDependency.referenced_object_id).where(
            ObjectDependency.project_id == project_id, ObjectDependency.referenced_object_id.in_(callee_pairs))
    ):
        deps[object_id].add(referenced)

    for key, pair in pairs.items():
        unit = unit_by_key.get(key)
        if unit is None or pair.status != "matched":
            continue
        target = targets_by_id.get(pair.target_object_id)
        hits = []
        for referenced in deps.get(unit.source_object_id, ()):
            callee = callee_pairs[referenced]
            if callee is pair:
                continue
            name = callee.oracle_name.split(".")[-1]
            pattern = rf"\b{re.escape(name)}\b"
            if not re.search(pattern, unit.source, re.I):
                continue  # e.g. a package body dependency used by another member
            reasons = ", ".join(sorted({f["title"] for f in callee.static_findings if f["severity"] == "high"}
                                       | {d["message"].split(":")[0] for d in callee.signature_diffs if d["severity"] == "high"}))
            hits.append((callee.oracle_name, reasons or "missing in PostgreSQL", pattern))
        if not hits:
            continue
        def lines(text, pat, offset=0):
            return sorted({text.count("\n", 0, m.start()) + 1 + offset for m in re.finditer(pat, text or "", re.I)})[:20]
        for callee_name, reasons, pattern in hits:
            pair.static_findings = pair.static_findings + [{
                "code": "CALLS_RISKY_ROUTINE", "severity": "medium",
                "title": f"Calls {callee_name}, whose migration has high-risk findings",
                "detail": f"{callee_name}: {reasons}. That behaviour change also affects this object.",
                "oracle_lines": lines(unit.source, pattern, unit.source_start_line - 1),
                "postgres_lines": lines(target.source if target else "", pattern),
            }]
        pair.risk = risk_of(pair.status, pair.signature_diffs, pair.static_findings)


def run_comparison(ctx: JobContext, run_id: uuid.UUID) -> dict[str, Any]:
    session = ctx.session
    run = session.get(ComparisonRun, run_id)
    project = session.get(Project, run.project_id)
    if project.postgres_connection is None:
        raise ValueError("The project has no PostgreSQL connection to compare against")
    if not session.scalars(select(Scan.id).where(Scan.project_id == project.id, Scan.status == ScanStatus.SUCCEEDED)).first():
        raise ValueError("Run an Oracle scan first: pairing needs the Oracle inventory")

    run.status = ScanStatus.RUNNING
    run.started_at = datetime.now(UTC)
    session.commit()

    target_schemas = scoped_schemas(project.postgres_connection, (project.settings or {}).get("target_schemas") or [])
    ctx.progress(0.05, f"Connecting to PostgreSQL ({project.postgres_connection.name})")
    target_endpoint = Endpoint.from_connection(project.postgres_connection)
    catalog = read_catalog(target_endpoint, target_schemas, progress=ctx.progress)
    ctx.log(f"Read {len(catalog.objects)} PostgreSQL objects (PostgreSQL {catalog.server_version}); "
            f"plpgsql_check {'available' if catalog.plpgsql_check else 'not installed'}")
    warnings = []
    compiled_in = "target" if catalog.plpgsql_check else None
    if not catalog.plpgsql_check and get_settings().compile_in_scratch:
        result = compile_in_scratch(target_endpoint, target_schemas, progress=ctx.progress)
        if result.ok:
            compiled_in = "scratch copy"
            for obj in catalog.objects:
                obj.check_results = result.checks.get(obj.key, [])
            stages = ", ".join(f"{k} {v}s" for k, v in result.stage_seconds.items())
            ctx.log(f"plpgsql_check ran on a structure-only copy of the schema in scratch-pg "
                    f"({result.duration_seconds}s: {stages}; {len(result.restore_errors)} statement(s) failed to load)")
            if result.restore_errors:
                warnings.append(
                    f"{len(result.restore_errors)} statement(s) of the schema copy could not be loaded into the scratch "
                    f"database (usually objects from extensions it doesn't have), so plpgsql_check may report missing "
                    f"objects for code that uses them. First: {result.restore_errors[0][:300]}"
                )
                ctx.log("Schema copy load errors:\n" + "\n".join(result.restore_errors[:50]))
        else:
            warnings.append(f"plpgsql_check is not installed in the target, and checking a scratch copy of the schema "
                            f"failed: {result.error}. PostgreSQL code was not statically compiled.")
    elif not catalog.plpgsql_check:
        warnings.append("plpgsql_check is not installed in the target database, so PostgreSQL code was not "
                        "statically compiled. Install it, or enable COMPILE_IN_SCRATCH.")
    orafce = "orafce" in catalog.extensions

    ctx.progress(0.75, "Pairing Oracle and PostgreSQL objects")
    targets = _upsert_targets(session, project.id, run, catalog)
    targets_by_id = {t.id: t for t in targets.values()}
    units = load_units(session, project.id)
    existing = {p.source_key: p for p in session.scalars(select(ObjectPair).where(ObjectPair.project_id == project.id))}

    fixed: dict[str, uuid.UUID | None] = {}
    for key, pair in existing.items():
        if pair.manual or pair.status == "ignored":
            if pair.target_object_id is not None and pair.target_object_id not in targets_by_id:
                warnings.append(f"Manual match for {pair.oracle_name} points at a PostgreSQL object that no longer exists")
                fixed[key] = None
            else:
                fixed[key] = pair.target_object_id
    refs = [TargetRef(t.id, t.schema, t.name, t.kind, t.arguments, t.metadata_) for t in targets.values()]
    results, conventions = pair_units(units, refs, fixed)

    ctx.progress(0.85, f"Checking {len(units)} pairs")
    statuses: Counter[str] = Counter()
    seen_keys = set()
    for unit, result in zip(units, results):
        seen_keys.add(unit.key)
        pair = existing.get(unit.key)
        if pair is None:
            pair = ObjectPair(id=uuid.uuid4(), project_id=project.id, source_key=unit.key)
            session.add(pair)
        target = targets_by_id.get(result.target_id) if result.target_id else None
        if pair.status == "ignored":
            status = "ignored"
        else:
            status = "matched" if target is not None else "missing_in_target"
        checks = evaluate(unit, target, orafce=orafce) if status == "matched" else evaluate(unit, None, orafce=orafce)

        pair.unit_kind = unit.kind
        pair.source_routine_id = unit.source_routine_id
        pair.source_object_id = unit.source_object_id
        pair.oracle_name = unit.display_name
        pair.target_object_id = target.id if target is not None else None
        pair.status = status
        if not pair.manual:
            pair.match_method = result.method
            pair.match_confidence = result.confidence
        pair.alternatives = [str(a) for a in result.alternatives]
        pair.signature_status = checks["signature_status"]
        pair.signature_diffs = checks["signature_diffs"]
        pair.static_findings = checks["static_findings"]
        pair.risk = risk_of(status, pair.signature_diffs, pair.static_findings)
        new_target_hash = target.source_hash if target is not None else None
        if pair.ai_review and (pair.oracle_hash != unit.object_hash or pair.target_hash != new_target_hash):
            pair.ai_stale = True
        pair.oracle_hash = unit.object_hash
        pair.target_hash = new_target_hash
        pair.last_run_id = run.id
        statuses[status] += 1

    for key, pair in list(existing.items()):
        if key not in seen_keys:
            session.delete(pair)  # the Oracle unit no longer exists in the inventory
            del existing[key]
    session.flush()
    current = {
        key: pair for key, pair in
        ((p.source_key, p) for p in session.scalars(select(ObjectPair).where(ObjectPair.project_id == project.id)))
    }
    add_dependency_findings(session, project.id, units, current, targets_by_id)
    risks = Counter(p.risk for p in current.values())

    paired_ids = {r.target_id for r in results if r.target_id}
    unpaired_targets = [t for t in targets.values() if t.id not in paired_ids and not t.metadata_.get("is_trigger_function")]
    summary = {
        "units": len(units),
        "matched": statuses["matched"],
        "missing_in_target": statuses["missing_in_target"],
        "ignored": statuses["ignored"],
        "target_objects": len(targets),
        "unpaired_targets": len(unpaired_targets),
        "risk": dict(risks),
        "conventions": conventions,
        "plpgsql_check": compiled_in,  # "target", "scratch copy" or None
        "orafce": orafce,
    }
    run.target_version = catalog.server_version
    run.summary = summary
    run.warnings = warnings
    run.status = ScanStatus.SUCCEEDED
    run.finished_at = datetime.now(UTC)
    session.commit()
    ctx.log(f"Paired {summary['matched']}/{summary['units']} units; {summary['missing_in_target']} missing in PostgreSQL; "
            f"{summary['unpaired_targets']} PostgreSQL objects without an Oracle counterpart")
    return {"run_id": str(run.id), **summary}
