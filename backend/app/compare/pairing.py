"""Pair Oracle units with PostgreSQL objects.

The naming convention used by the earlier migration is unknown, so every unit tries
several conventions; each candidate gets a confidence from the convention and from how
well the parameter lists line up. Pairs are assigned greedily, highest confidence first,
so a PostgreSQL object is never claimed twice. Manual matches are fixed beforehand.
"""

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.compare.units import OracleUnit

# Base confidence for each naming convention.
CONVENTIONS: dict[str, tuple[str, float]] = {
    "schema_per_package": ("Package → schema, member → function (Ora2Pg default)", 1.0),
    "same_schema": ("Same schema and name", 1.0),
    "flattened": ("Package and member joined: pkg_member", 0.95),
    "public_schema": ("Same name in the public schema", 0.9),
    "other_schema": ("Same name in another schema", 0.8),
    "manual": ("Chosen manually", 1.0),
}

ROUTINE_KINDS = ("function", "procedure")
VIEW_KINDS = ("view", "materialized view")


@dataclass
class TargetRef:
    id: uuid.UUID
    schema: str
    name: str
    kind: str
    arguments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PairResult:
    unit_key: str
    target_id: uuid.UUID | None
    method: str | None
    confidence: float | None
    alternatives: list[uuid.UUID] = field(default_factory=list)


def _norm(name: str | None) -> str:
    return (name or "").strip('"').lower()


def _comparable_args(args: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in args if a.get("mode") != "TABLE"]


def signature_similarity(oracle_args: list[dict[str, Any]], target_args: list[dict[str, Any]]) -> float:
    """0..1: how well two parameter lists line up (count, then names and modes by position)."""
    o, t = oracle_args, _comparable_args(target_args)
    if not o and not t:
        return 1.0
    if len(o) != len(t):
        return 0.3 * (1 - abs(len(o) - len(t)) / max(len(o), len(t)))
    names = sum(_norm(a.get("name")) == _norm(b.get("name")) for a, b in zip(o, t)) / len(o)
    modes = sum((a.get("mode") or "IN") == (b.get("mode") or "IN") for a, b in zip(o, t)) / len(o)
    return round(0.6 + 0.3 * names + 0.1 * modes, 3)


def _candidates(unit: OracleUnit, by_schema_name, by_name) -> list[tuple[str, TargetRef]]:
    owner, name, package = _norm(unit.owner), _norm(unit.name), _norm(unit.package)
    found: list[tuple[str, TargetRef]] = []
    seen: set[uuid.UUID] = set()

    def add(method: str, targets):
        for t in targets:
            if t.id not in seen:
                seen.add(t.id)
                found.append((method, t))

    if package:
        add("schema_per_package", by_schema_name.get((package, name), []))
        add("flattened", by_schema_name.get((owner, f"{package}_{name}"), []))
        add("flattened", [t for t in by_name.get(f"{package}_{name}", [])])
        add("same_schema", by_schema_name.get((owner, name), []))
    else:
        add("same_schema", by_schema_name.get((owner, name), []))
        add("public_schema", by_schema_name.get(("public", name), []))
    add("other_schema", by_name.get(name, []))
    return found


def _score(unit: OracleUnit, method: str, target: TargetRef, n_candidates: int) -> float:
    base = CONVENTIONS[method][1]
    if unit.kind == "routine":
        score = base * signature_similarity(unit.arguments, target.arguments)
        if unit.routine_kind == "FUNCTION" and target.kind == "procedure":
            score *= 0.8
    elif unit.kind == "trigger":
        table = _norm((unit.metadata.get("trigger") or {}).get("table", "")).split(".")[-1]
        target_table = _norm(target.metadata.get("table", "")).split(".")[-1]
        score = base * (1.0 if table and table == target_table else 0.7)
    else:
        score = base
    if method == "other_schema" and n_candidates > 1:
        score *= 0.85  # several same-named objects: less certain
    return round(score, 3)


def pair_units(
    units: list[OracleUnit],
    targets: list[TargetRef],
    fixed: dict[str, uuid.UUID | None],
) -> tuple[list[PairResult], dict[str, int]]:
    """`fixed` maps unit keys to manually chosen targets (None = deliberately unmatched)."""
    groups = {
        # Trigger functions are reached through their trigger, not paired with Oracle routines.
        "routine": [t for t in targets if t.kind in ROUTINE_KINDS and not t.metadata.get("is_trigger_function")],
        "view": [t for t in targets if t.kind in VIEW_KINDS],
        "trigger": [t for t in targets if t.kind == "trigger"],
    }
    indexes = {}
    for kind, items in groups.items():
        by_sn, by_n = defaultdict(list), defaultdict(list)
        for t in items:
            by_sn[(_norm(t.schema), _norm(t.name))].append(t)
            by_n[_norm(t.name)].append(t)
        indexes[kind] = (by_sn, by_n)

    used: set[uuid.UUID] = {t for t in fixed.values() if t is not None}
    options: list[tuple[float, str, OracleUnit, TargetRef]] = []
    per_unit: dict[str, list[tuple[float, uuid.UUID]]] = defaultdict(list)
    for unit in units:
        if unit.key in fixed:
            continue
        cands = _candidates(unit, *indexes[unit.kind])
        for method, target in cands:
            score = _score(unit, method, target, len(cands))
            options.append((score, method, unit, target))
            per_unit[unit.key].append((score, target.id))

    assigned: dict[str, PairResult] = {
        key: PairResult(key, target, "manual" if target else None, 1.0 if target else None)
        for key, target in fixed.items()
    }
    # Highest confidence first; ties prefer the stronger convention, then stable order.
    for score, method, unit, target in sorted(options, key=lambda o: (-o[0], -CONVENTIONS[o[1]][1], o[2].key)):
        if unit.key in assigned or target.id in used:
            continue
        alternatives = [t for s, t in sorted(per_unit[unit.key], reverse=True) if t != target.id][:3]
        assigned[unit.key] = PairResult(unit.key, target.id, method, score, alternatives)
        used.add(target.id)

    results = [assigned.get(u.key) or PairResult(u.key, None, None, None) for u in units]
    conventions = Counter(r.method for r in results if r.method)
    return results, dict(conventions)
