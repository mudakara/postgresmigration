"""Scan orchestration: read Oracle, estimate cost with Ora2Pg, analyse, persist."""

import hashlib
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.jobs.registry import JobContext
from app.models import ObjectDependency, Project, Scan, ScanStatus, SourceObject, SourceRoutine
from app.scan.cost_report import SchemaCost, run_schema_report
from app.scan.extract import DictionaryReader, ExtractedObject, Extraction
from app.scan.rules import Analysis, analyze, complexity_tier, mark_calls_unsafe, sys_packages
from app.services import oracle
from app.services.endpoints import Endpoint, use_user_grants

# Ora2Pg omits views from its per-object list when they need no rewriting; it still
# counts each one at this base score.
_ORA2PG_VIEW_BASE_COST = 1.0


@dataclass
class AnalyzedObject:
    extracted: ExtractedObject
    analysis: Analysis
    sys_packages: list[str]
    ora2pg_cost: float | None
    effort: float
    complexity: str
    source_hash: str | None


def _hash(source: str | None) -> str | None:
    return hashlib.sha256(source.encode()).hexdigest() if source is not None else None


def analyze_extraction(extraction: Extraction, costs: dict[str, SchemaCost]) -> list[AnalyzedObject]:
    deps_by_object = defaultdict(list)
    for dep in extraction.dependencies:
        deps_by_object[dep.key].append(dep)

    analyses: dict[tuple[str, str, str], Analysis] = {}
    results = []
    for obj in extraction.objects:
        deps = deps_by_object.get(obj.key, [])
        analysis = analyze(
            obj.object_type,
            obj.source,
            status=obj.status,
            has_db_link_dependency=any(d.ref_link for d in deps),
            uses_gtt=any((d.ref_owner, d.ref_name) in extraction.temporary_tables for d in deps),
        )
        cost = None
        schema_cost = costs.get(obj.owner)
        if schema_cost and schema_cost.ok:
            cost = schema_cost.object_costs.get((obj.object_type, obj.name.lower()))
            if cost is None and obj.object_type == "VIEW":
                cost = _ORA2PG_VIEW_BASE_COST

        effort = cost if cost is not None else analysis.heuristic_effort
        if analysis.wrapped:  # Ora2Pg can't see inside wrapped code
            effort = max(effort, analysis.heuristic_effort)
        if analysis.package_state:
            obj.metadata["package_state"] = analysis.package_state
        analyses[obj.key] = analysis

        results.append(AnalyzedObject(
            extracted=obj,
            analysis=analysis,
            sys_packages=sys_packages([(d.ref_owner, d.ref_name, d.ref_type) for d in deps]),
            ora2pg_cost=cost,
            effort=round(effort, 2),
            complexity=complexity_tier(effort),
            source_hash=_hash(obj.source),
        ))

    for key, via in propagate_unsafety(analyses, deps_by_object).items():
        mark_calls_unsafe(analyses[key], via)
    return results


def propagate_unsafety(
    analyses: dict[tuple[str, str, str], Analysis],
    deps_by_object: dict[tuple[str, str, str], list],
) -> dict[tuple[str, str, str], list[str]]:
    """Objects that are not unsafe themselves but reach unsafe code through dependencies,
    mapped to the unsafe objects they call directly. Callers reference a package's spec,
    so a spec is treated as unsafe when its body is (conservative: it may be another member)."""
    unsafe = {key for key, a in analyses.items() if a.unsafe}
    for owner, obj_type, name in list(unsafe):
        if obj_type == "PACKAGE BODY" and (owner, "PACKAGE", name) in analyses:
            unsafe.add((owner, "PACKAGE", name))

    via: dict[tuple[str, str, str], list[str]] = {}
    changed = True
    while changed:  # fixed point; terminates because `unsafe` only grows
        changed = False
        for key in analyses:
            if key in unsafe:
                continue
            hits = sorted({
                f"{d.ref_owner}.{d.ref_name}"
                for d in deps_by_object.get(key, [])
                if (d.ref_owner, d.ref_type, d.ref_name) in unsafe and (d.ref_owner, d.ref_type, d.ref_name) != key
            })
            if hits:
                via[key] = hits
                unsafe.add(key)
                owner, obj_type, name = key
                if obj_type == "PACKAGE BODY":
                    unsafe.add((owner, "PACKAGE", name))  # for propagation only; specs aren't flagged
                changed = True
    return via


def persist(
    session: Session,
    project: Project,
    scan: Scan,
    extraction: Extraction,
    analyzed: list[AnalyzedObject],
    costs: dict[str, SchemaCost],
) -> dict[str, Any]:
    """Upsert the inventory. Does not commit: the caller commits once, at the end."""
    now = datetime.now(UTC)
    existing = {
        (o.owner, o.object_type, o.name): o
        for o in session.scalars(select(SourceObject).where(SourceObject.project_id == project.id))
    }
    changes: Counter[str] = Counter()
    seen: set[tuple[str, str, str]] = set()
    changed_keys: set[tuple[str, str, str]] = set()  # new, or source differs from last scan
    complexity: Counter[str] = Counter()

    for item in analyzed:
        ext = item.extracted
        seen.add(ext.key)
        obj = existing.get(ext.key)
        if obj is None:
            obj = SourceObject(
                project_id=project.id, owner=ext.owner, object_type=ext.object_type, name=ext.name,
                first_seen_scan_id=scan.id, source_changed_scan_id=scan.id,
            )
            session.add(obj)
            existing[ext.key] = obj
            changes["added"] += 1
            changed_keys.add(ext.key)
        else:
            if obj.removed_at is not None:
                obj.removed_at = None
                changes["restored"] += 1
            if obj.source_hash != item.source_hash:
                obj.source_changed_scan_id = scan.id
                changes["changed"] += 1
                changed_keys.add(ext.key)

        cost, effort, tier = item.ora2pg_cost, item.effort, item.complexity
        if (cost is None and not _costed(costs, ext.owner) and ext.key not in changed_keys
                and obj.ora2pg_cost is not None):
            # This scan skipped Ora2Pg; the earlier estimate still describes unchanged source.
            cost = obj.ora2pg_cost
            effort = max(cost, item.analysis.heuristic_effort) if item.analysis.wrapped else cost
            tier = complexity_tier(effort)
        complexity[tier] += 1

        obj.status = ext.status
        obj.oracle_created = ext.created
        obj.oracle_last_ddl = ext.last_ddl
        obj.source = ext.source
        obj.source_hash = item.source_hash
        obj.line_count = ext.source.count("\n") + 1 if ext.source else 0
        obj.metadata_ = ext.metadata
        obj.findings = [f.as_dict() for f in item.analysis.findings]
        obj.finding_codes = item.analysis.codes
        obj.sys_packages = item.sys_packages
        obj.unsafe_to_execute = item.analysis.unsafe
        obj.wrapped = item.analysis.wrapped
        obj.ora2pg_cost = cost
        obj.effort = effort
        obj.complexity = tier
        obj.last_seen_scan_id = scan.id

    scanned_owners = set(extraction.schemas)
    for key, obj in existing.items():
        if key not in seen and obj.owner in scanned_owners and obj.removed_at is None:
            obj.removed_at = now
            changes["removed"] += 1

    session.flush()  # assign ids before linking routines and dependencies
    routine_count = _persist_routines(session, project, existing, extraction, costs, changed_keys)
    _persist_dependencies(session, project, existing, extraction)

    by_type = Counter(item.extracted.object_type for item in analyzed)
    return {
        "objects": len(analyzed),
        "routines": routine_count,
        "lines": sum(item.extracted.source.count("\n") + 1 for item in analyzed if item.extracted.source),
        "unsafe": sum(1 for item in analyzed if item.analysis.unsafe),
        "by_type": dict(sorted(by_type.items())),
        "complexity": dict(complexity),
        "added": changes["added"],
        "changed": changes["changed"],
        "removed": changes["removed"],
        "restored": changes["restored"],
    }


def _costed(costs: dict[str, SchemaCost], owner: str) -> bool:
    report = costs.get(owner)
    return bool(report and report.ok)


def _persist_routines(session, project, objects, extraction, costs, changed_keys) -> int:
    existing = {
        (r.object_id, r.name, r.overload): r
        for r in session.scalars(select(SourceRoutine).where(SourceRoutine.project_id == project.id))
    }
    keep: set[uuid.UUID] = set()
    for ext in extraction.routines:
        parent = objects.get(ext.parent_key)
        if parent is None:
            continue  # e.g. a package member whose spec is outside the inventory
        routine = existing.get((parent.id, ext.name, ext.overload))
        if routine is None:
            routine = SourceRoutine(
                id=uuid.uuid4(), project_id=project.id, object_id=parent.id, name=ext.name, overload=ext.overload
            )
            session.add(routine)
            existing[(parent.id, ext.name, ext.overload)] = routine
        if _costed(costs, ext.owner):
            schema_cost = costs[ext.owner]
            if ext.package_name:
                cost = schema_cost.member_costs.get((ext.package_name.lower(), ext.name.lower()))
            else:
                cost = schema_cost.object_costs.get((ext.kind, ext.name.lower()))
        else:
            # Keep the earlier estimate unless the routine's code (spec or body) changed.
            body_key = (ext.owner, "PACKAGE BODY", ext.package_name) if ext.package_name else None
            unchanged = ext.parent_key not in changed_keys and body_key not in changed_keys
            cost = routine.ora2pg_cost if unchanged else None
        routine.owner = ext.owner
        routine.package_name = ext.package_name
        routine.subprogram_id = ext.subprogram_id
        routine.kind = ext.kind
        routine.return_type = ext.return_type
        routine.arguments = ext.arguments
        routine.signature = ext.signature
        routine.attributes = ext.attributes
        routine.ora2pg_cost = cost
        keep.add(routine.id)

    scanned_object_ids = {o.id for o in objects.values() if o.owner in set(extraction.schemas)}
    for routine in existing.values():
        if routine.id not in keep and routine.object_id in scanned_object_ids:
            session.delete(routine)
    return len(keep)


def _persist_dependencies(session, project, objects, extraction) -> None:
    scanned_ids = [o.id for o in objects.values() if o.owner in set(extraction.schemas)]
    if scanned_ids:
        session.execute(delete(ObjectDependency).where(ObjectDependency.object_id.in_(scanned_ids)))

    # Calls into a package resolve to its spec; anything else resolves by exact type.
    rows = []
    for dep in extraction.dependencies:
        source = objects.get(dep.key)
        if source is None:
            continue
        target = objects.get((dep.ref_owner, dep.ref_type, dep.ref_name))
        rows.append({
            "id": uuid.uuid4(),
            "project_id": project.id,
            "object_id": source.id,
            "referenced_owner": dep.ref_owner,
            "referenced_name": dep.ref_name,
            "referenced_type": dep.ref_type,
            "referenced_link_name": dep.ref_link,
            "referenced_object_id": target.id if target is not None and target.removed_at is None else None,
        })
    if rows:
        session.execute(insert(ObjectDependency), rows)


def run_scan(ctx: JobContext, scan_id: uuid.UUID) -> dict[str, Any]:
    session = ctx.session
    scan = session.get(Scan, scan_id)
    if scan is None:
        raise ValueError(f"Scan {scan_id} not found")
    project = session.get(Project, scan.project_id)
    conn = project.oracle_connection

    scan.status = ScanStatus.RUNNING
    scan.started_at = datetime.now(UTC)
    session.commit()

    endpoint = Endpoint.from_connection(conn)
    user_grants = use_user_grants(conn)
    dictionary = "ALL" if user_grants else "DBA"
    warnings: list[str] = []
    if user_grants:
        warnings.append(
            "Scanned through ALL_* views (no DBA_* access): only objects granted to "
            f"{conn.username} are visible, so the inventory may be incomplete."
        )

    ctx.progress(0.03, f"Connecting to Oracle ({conn.name})")
    with oracle.connect(endpoint) as oracle_conn:
        extraction = DictionaryReader(oracle_conn, dictionary).extract(project.schemas, ctx.progress)
    if extraction.missing_schemas:
        warnings.append(f"Schemas not found in Oracle: {', '.join(extraction.missing_schemas)}")
    ctx.log(
        f"Read {len(extraction.objects)} objects, {len(extraction.routines)} routines and "
        f"{len(extraction.dependencies)} dependencies from {', '.join(extraction.schemas)} "
        f"via {dictionary}_* views (Oracle {extraction.oracle_version})"
    )

    costs: dict[str, SchemaCost] = {}
    if scan.include_ora2pg_cost:
        total = len(extraction.schemas)
        for i, schema in enumerate(extraction.schemas):
            fraction = 0.5 + 0.4 * i / total
            label = f"Ora2Pg cost estimate: {schema} ({i + 1}/{total})"
            ctx.progress(fraction, label)

            def report_progress(line: str, elapsed: float, fraction=fraction, label=label) -> None:
                ctx.progress(fraction, f"{label}, {int(elapsed)}s" + (f": {line[:200]}" if line else ""))

            report = run_schema_report(endpoint, schema, user_grants=user_grants, on_progress=report_progress)
            costs[schema] = report
            if report.ok:
                ctx.log(f"Ora2Pg {schema}: total cost {report.total_cost}, level {report.migration_level} "
                        f"({report.duration_seconds}s)")
            else:
                hint = (" Without DBA_* access Ora2Pg reads the slow ALL_* views; granting SELECT ANY DICTIONARY "
                        "usually fixes this." if user_grants and "timed out" in (report.error or "") else "")
                warnings.append(f"Ora2Pg cost estimate failed for {schema}; using heuristic effort. {report.error}.{hint}")
                ctx.log(f"Ora2Pg {schema} failed: {report.error}")

    ctx.progress(0.92, "Analysing objects")
    analyzed = analyze_extraction(extraction, costs)

    ctx.progress(0.95, "Saving inventory")
    summary = persist(session, project, scan, extraction, analyzed, costs)
    scan.schemas = extraction.schemas
    scan.oracle_version = extraction.oracle_version
    scan.dictionary = dictionary
    scan.object_counts = extraction.object_counts
    scan.ora2pg_reports = [c.summary() for c in costs.values()]
    scan.warnings = warnings
    scan.summary = summary
    scan.status = ScanStatus.SUCCEEDED
    scan.finished_at = datetime.now(UTC)
    session.commit()

    ctx.log(f"Saved: {summary['objects']} objects ({summary['added']} new, {summary['changed']} changed, "
            f"{summary['removed']} removed), {summary['routines']} routines")
    return {"scan_id": str(scan.id), **summary, "warnings": len(warnings)}
