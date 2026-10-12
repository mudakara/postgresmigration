"""Oracle-side comparison units, built from the scanned inventory.

A unit is what has a PostgreSQL counterpart: a standalone procedure/function, a public
package member, a view or a trigger. Package specs and bodies aren't compared as wholes;
their members are.
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SourceObject, SourceRoutine
from app.scan.plsql import mask


@dataclass
class OracleUnit:
    key: str  # "routine:<uuid>" | "object:<uuid>"
    kind: str  # routine | view | trigger
    owner: str
    name: str
    package: str | None
    source_object_id: uuid.UUID
    source_routine_id: uuid.UUID | None
    display_name: str
    routine_kind: str | None = None  # PROCEDURE | FUNCTION
    overload: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)
    return_type: str | None = None
    signature: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # The code that implements this unit, and where it starts in the object's source.
    source: str = ""
    source_start_line: int = 1
    object_source: str = ""  # the whole object (e.g. the package body), for context
    object_findings: list[dict[str, Any]] = field(default_factory=list)
    object_hash: str | None = None


_SUBPROGRAM = re.compile(r"^([ \t]*)(?:FUNCTION|PROCEDURE)\s+(\"[^\"]+\"|[\w$#]+)", re.I | re.M)


def extract_member(body: str, name: str, occurrence: int = 0) -> tuple[str, int] | None:
    """Text of a package member's implementation inside a package body, and its first line.

    Heuristic: the member runs until the next FUNCTION/PROCEDURE declared at the same or
    lower indentation, or the package's final END. `occurrence` picks among overloads.
    """
    masked = mask(body)
    starts = [m for m in _SUBPROGRAM.finditer(masked.code)]
    hits = [m for m in starts if m.group(2).strip('"').upper() == name.upper()]
    if len(hits) <= occurrence:
        return None
    hit = hits[occurrence]
    indent = len(hit.group(1).expandtabs())
    end = None
    for m in starts:
        if m.start() > hit.start() and len(m.group(1).expandtabs()) <= indent:
            end = m.start()
            break
    if end is None:
        final_end = list(re.finditer(r"^\s*END\b[^;]*;\s*$", masked.code, re.I | re.M))
        end = final_end[-1].start() if final_end else len(body)
    return body[hit.start():end].rstrip() + "\n", masked.line_of(hit.start())


def _routine_unit(r: SourceRoutine, parent: SourceObject, body: SourceObject | None, overload_index: int) -> OracleUnit:
    holder = parent
    source, start = parent.source or "", 1
    if r.package_name and body is not None and body.source:
        holder = body
        member = extract_member(body.source, r.name, overload_index)
        source, start = member if member else (body.source, 1)
    qualified = f"{r.package_name}.{r.name}" if r.package_name else r.name
    return OracleUnit(
        key=f"routine:{r.id}", kind="routine", owner=r.owner, name=r.name, package=r.package_name,
        source_object_id=holder.id, source_routine_id=r.id,
        display_name=f"{r.owner}.{qualified}" + (f" #{r.overload}" if r.overload else ""),
        routine_kind=r.kind, overload=r.overload, arguments=r.arguments, return_type=r.return_type,
        signature=r.signature, source=source, source_start_line=start, object_source=holder.source or "",
        object_findings=holder.findings, object_hash=holder.source_hash,
    )


def _object_unit(o: SourceObject) -> OracleUnit:
    return OracleUnit(
        key=f"object:{o.id}", kind="trigger" if o.object_type == "TRIGGER" else "view",
        owner=o.owner, name=o.name, package=None, source_object_id=o.id, source_routine_id=None,
        display_name=f"{o.owner}.{o.name}", metadata=dict(o.metadata_ or {}),
        source=o.source or "", object_source=o.source or "", object_findings=o.findings,
        object_hash=o.source_hash,
    )


def _overload_key(r: SourceRoutine) -> tuple:
    return (r.owner, r.package_name or "", r.name, int(r.overload) if r.overload.isdigit() else 0)


def load_units(session: Session, project_id: uuid.UUID) -> list[OracleUnit]:
    objects = {
        o.id: o
        for o in session.scalars(
            select(SourceObject).where(SourceObject.project_id == project_id, SourceObject.removed_at.is_(None))
        )
    }
    by_key = {(o.owner, o.object_type, o.name): o for o in objects.values()}
    units: list[OracleUnit] = []

    routines = sorted(
        session.scalars(select(SourceRoutine).where(SourceRoutine.project_id == project_id)).all(), key=_overload_key
    )
    overload_seen: dict[tuple, int] = {}
    for r in routines:
        parent = objects.get(r.object_id)
        if parent is None:
            continue
        body = by_key.get((r.owner, "PACKAGE BODY", r.package_name)) if r.package_name else None
        idx = overload_seen.get((r.owner, r.package_name, r.name), 0)
        overload_seen[(r.owner, r.package_name, r.name)] = idx + 1
        units.append(_routine_unit(r, parent, body, idx))

    for o in objects.values():
        if o.object_type in ("VIEW", "MATERIALIZED VIEW", "TRIGGER"):
            units.append(_object_unit(o))
    return units


def load_unit(session: Session, source_key: str) -> OracleUnit | None:
    """The unit for one pair, without building every unit in the project."""
    kind, _, raw_id = source_key.partition(":")
    if kind == "object":
        obj = session.get(SourceObject, uuid.UUID(raw_id))
        return _object_unit(obj) if obj is not None else None
    r = session.get(SourceRoutine, uuid.UUID(raw_id))
    if r is None:
        return None
    body = None
    index = 0
    if r.package_name:
        body = session.scalars(select(SourceObject).where(
            SourceObject.project_id == r.project_id, SourceObject.owner == r.owner,
            SourceObject.object_type == "PACKAGE BODY", SourceObject.name == r.package_name,
        )).first()
        siblings = session.scalars(select(SourceRoutine).where(
            SourceRoutine.object_id == r.object_id, SourceRoutine.name == r.name,
        )).all()
        index = sorted(siblings, key=_overload_key).index(r)
    return _routine_unit(r, r.object, body, index)
