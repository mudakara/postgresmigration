"""Ora2Pg migration assessment (`SHOW_REPORT --estimate_cost`) per schema.

Ora2Pg reports per-object costs inside a free-text `details` field
("name: 3. pkg.member: 4.1. "), lower-cased. One Ora2Pg cost unit is roughly five
minutes of a migration engineer's time (its default COST_UNIT_VALUE).
"""

import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.services import ora2pg
from app.services.endpoints import Endpoint

COST_UNIT_MINUTES = 5
# Report types whose `details` list per-object costs.
_COSTED_TYPES = {"FUNCTION", "PROCEDURE", "PACKAGE BODY", "TRIGGER", "VIEW"}
_DETAIL = re.compile(r"([A-Za-z0-9_$#.\"]+): (\d+(?:\.\d+)?)")


@dataclass
class SchemaCost:
    schema: str
    ok: bool
    error: str | None = None
    total_cost: float | None = None
    human_days: float | None = None
    migration_level: str | None = None
    by_type: list[dict[str, Any]] = field(default_factory=list)
    # (object_type, lower-case name) -> cost; package costs are summed over members.
    object_costs: dict[tuple[str, str], float] = field(default_factory=dict)
    # (lower-case package, lower-case member) -> cost
    member_costs: dict[tuple[str, str], float] = field(default_factory=dict)
    duration_seconds: float | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "ok": self.ok, "error": self.error, "total_cost": self.total_cost,
            "human_days": self.human_days, "migration_level": self.migration_level,
            "by_type": self.by_type, "duration_seconds": self.duration_seconds,
        }


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _leading_number(value: Any) -> float | None:
    match = re.match(r"\s*(\d+(?:\.\d+)?)", str(value or ""))
    return float(match.group(1)) if match else None


def parse_report(schema: str, text: str) -> SchemaCost:
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in Ora2Pg output")
    data = json.loads(text[start:])

    result = SchemaCost(
        schema=schema,
        ok=True,
        total_cost=_number(data.get("total cost")),
        human_days=_leading_number(data.get("human days cost")),  # e.g. "1 person-day(s)"
        migration_level=str(data.get("migration level") or "") or None,
    )
    member_costs: dict[tuple[str, str], float] = defaultdict(float)
    package_costs: dict[str, float] = defaultdict(float)

    for item in data.get("objects", []):
        obj_type = item.get("object")
        result.by_type.append({
            "object_type": obj_type,
            "count": item.get("number"),
            "invalid": item.get("invalid"),
            "cost": _number(item.get("cost value")),
        })
        if obj_type not in _COSTED_TYPES:
            continue
        for name, cost in _DETAIL.findall(item.get("details") or ""):
            name = name.strip('"').lower()
            value = float(cost)
            if obj_type == "PACKAGE BODY" and "." in name:
                package, member = name.split(".", 1)
                member_costs[(package, member)] += value  # overloads share a name
                package_costs[package] += value
            else:
                result.object_costs[(obj_type, name)] = value

    for package, cost in package_costs.items():
        result.object_costs[("PACKAGE BODY", package)] = round(cost, 2)
    result.member_costs = dict(member_costs)
    return result


def run_schema_report(
    endpoint: Endpoint,
    schema: str,
    *,
    user_grants: bool,
    timeout: int | None = None,
    on_progress: Callable[[str, float], None] | None = None,
) -> SchemaCost:
    """Never raises for Ora2Pg problems: a failed or timed-out report is returned with
    ok=False so the scan can carry on with heuristic effort."""
    try:
        with ora2pg.workspace() as work:
            run = ora2pg.run(
                endpoint,
                "SHOW_REPORT",
                work,
                args=["--estimate_cost", "--dump_as_json"],
                # DEBUG makes Ora2Pg print its progress to stderr; the JSON stays on stdout.
                config={"SCHEMA": schema, "DEBUG": 1},
                user_grants=user_grants,
                timeout=timeout or get_settings().ora2pg_report_timeout_seconds,
                on_progress=on_progress,
            )
    except ora2pg.Ora2PgError as exc:
        return SchemaCost(schema=schema, ok=False, error=str(exc))
    if not run.succeeded:
        return SchemaCost(schema=schema, ok=False, error=run.failure_summary(), duration_seconds=run.duration_seconds)
    try:
        report = parse_report(schema, run.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        return SchemaCost(schema=schema, ok=False, error=f"Could not parse Ora2Pg report: {exc}",
                          duration_seconds=run.duration_seconds)
    report.duration_seconds = run.duration_seconds
    return report
