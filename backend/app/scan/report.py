"""Aggregate the current inventory into the assessment report."""

import uuid
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Scan, ScanStatus, SourceObject, SourceRoutine
from app.scan.cost_report import COST_UNIT_MINUTES
from app.scan.rules import RULES

TIERS = ["simple", "moderate", "complex", "very_complex"]
CATEGORIES = ["blocker", "execution_safety", "semantic_risk", "conversion"]


def build_report(session: Session, project_id: uuid.UUID) -> dict[str, Any]:
    rows = session.execute(
        select(
            SourceObject.id, SourceObject.owner, SourceObject.name, SourceObject.object_type,
            SourceObject.status, SourceObject.line_count, SourceObject.complexity, SourceObject.effort,
            SourceObject.ora2pg_cost, SourceObject.unsafe_to_execute, SourceObject.wrapped,
            SourceObject.findings, SourceObject.sys_packages,
        ).where(SourceObject.project_id == project_id, SourceObject.removed_at.is_(None))
    ).all()

    routine_rows = session.execute(
        select(SourceRoutine.package_name.is_(None), SourceRoutine.kind, func.count())
        .join(SourceObject, SourceRoutine.object_id == SourceObject.id)
        .where(SourceRoutine.project_id == project_id, SourceObject.removed_at.is_(None))
        .group_by(SourceRoutine.package_name.is_(None), SourceRoutine.kind)
    ).all()
    routines = {"standalone": 0, "package_members": 0, "procedures": 0, "functions": 0}
    for standalone, kind, count in routine_rows:
        routines["standalone" if standalone else "package_members"] += count
        routines["procedures" if kind == "PROCEDURE" else "functions"] += count

    by_type: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "invalid": 0, "lines": 0, "effort": 0.0})
    by_owner: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "lines": 0, "effort": 0.0})
    tiers: dict[str, dict[str, Any]] = {t: {"count": 0, "effort": 0.0} for t in TIERS}
    finding_objects: Counter[str] = Counter()
    finding_occurrences: Counter[str] = Counter()
    category_objects: Counter[str] = Counter()
    sys_usage: Counter[str] = Counter()
    ora2pg_costed = 0

    for r in rows:
        t = by_type[r.object_type]
        t["count"] += 1
        t["invalid"] += r.status != "VALID"
        t["lines"] += r.line_count
        t["effort"] += r.effort
        o = by_owner[r.owner]
        o["count"] += 1
        o["lines"] += r.line_count
        o["effort"] += r.effort
        tiers[r.complexity]["count"] += 1
        tiers[r.complexity]["effort"] += r.effort
        ora2pg_costed += r.ora2pg_cost is not None
        categories = set()
        for f in r.findings:
            finding_objects[f["code"]] += 1
            finding_occurrences[f["code"]] += f["count"]
            categories.add(f["category"])
        for c in categories:
            category_objects[c] += 1
        sys_usage.update(r.sys_packages)

    findings = [
        {
            "code": rule.code, "title": rule.title, "category": rule.category, "severity": rule.severity,
            "unsafe": rule.unsafe, "advice": rule.advice,
            "objects": finding_objects[rule.code], "occurrences": finding_occurrences[rule.code],
        }
        for rule in RULES if finding_objects[rule.code]
    ]
    findings.sort(key=lambda f: (CATEGORIES.index(f["category"]), ["high", "medium", "low"].index(f["severity"]), -f["objects"]))

    total_effort = round(sum(r.effort for r in rows), 1)
    top = sorted(rows, key=lambda r: r.effort, reverse=True)[:10]

    last_scan = session.scalars(
        select(Scan).where(Scan.project_id == project_id).order_by(Scan.created_at.desc()).limit(1)
    ).first()
    last_success = session.scalars(
        select(Scan).where(Scan.project_id == project_id, Scan.status == ScanStatus.SUCCEEDED)
        .order_by(Scan.created_at.desc()).limit(1)
    ).first()
    ora2pg_days = None
    if last_success and last_success.ora2pg_reports:
        days = [r["human_days"] for r in last_success.ora2pg_reports if r.get("ok") and r.get("human_days") is not None]
        ora2pg_days = round(sum(days), 1) if days else None

    return {
        "project_id": project_id,
        "last_scan": last_scan,
        "last_successful_scan": last_success,
        "totals": {
            "objects": len(rows),
            "lines": sum(r.line_count for r in rows),
            "invalid": sum(1 for r in rows if r.status != "VALID"),
            "wrapped": sum(1 for r in rows if r.wrapped),
            "unsafe_to_execute": sum(1 for r in rows if r.unsafe_to_execute),
            "routines": routines,
        },
        "effort": {
            "total_units": total_effort,
            "unit_minutes": COST_UNIT_MINUTES,
            "estimated_hours": round(total_effort * COST_UNIT_MINUTES / 60, 1),
            "ora2pg_human_days": ora2pg_days,
            "objects_costed_by_ora2pg": ora2pg_costed,
            "objects_costed_by_heuristic": len(rows) - ora2pg_costed,
        },
        "by_type": [{"object_type": k, **v, "effort": round(v["effort"], 1)} for k, v in sorted(by_type.items())],
        "by_owner": [{"owner": k, **v, "effort": round(v["effort"], 1)} for k, v in sorted(by_owner.items())],
        "complexity": [{"tier": t, "count": v["count"], "effort": round(v["effort"], 1)} for t, v in tiers.items()],
        "categories": [{"category": c, "objects": category_objects[c]} for c in CATEGORIES],
        "findings": findings,
        "sys_packages": [{"name": n, "objects": c} for n, c in sys_usage.most_common()],
        "top_effort": [
            {"id": r.id, "owner": r.owner, "name": r.name, "object_type": r.object_type,
             "effort": r.effort, "complexity": r.complexity, "unsafe_to_execute": r.unsafe_to_execute}
            for r in top
        ],
    }


def rule_catalogue() -> list[dict[str, Any]]:
    return [
        {"code": r.code, "title": r.title, "category": r.category, "severity": r.severity,
         "unsafe": r.unsafe, "advice": r.advice}
        for r in RULES
    ]

