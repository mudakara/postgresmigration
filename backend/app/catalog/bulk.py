"""Every table of a snapshot in the shape of GET /catalog-tables/{id}, loaded in bulk.

One query per kind of object instead of several per table, so all tables of two
snapshots can be compared at once (app.catalog.table_compare.overview).
"""

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.models import CatalogCode, CatalogRelation, CatalogTable, CatalogUsage
from app.schemas import (
    CatalogCodeSummary,
    CatalogCodeUsage,
    CatalogRelationRead,
    CatalogTableDetail,
)


def _relation(rel: CatalogRelation, other: CatalogTable | None, *, outgoing: bool) -> CatalogRelationRead:
    return CatalogRelationRead(
        id=rel.id, kind=rel.kind, name=rel.name,
        other_table_id=other.id if other else None,
        other_label=rel.to_label if outgoing else f"{other.schema}.{other.name}",
        other_kind=other.kind if other else None,
        columns=rel.from_columns if outgoing else rel.to_columns,
        other_columns=rel.to_columns if outgoing else rel.from_columns,
        details=rel.details,
    )


def snapshot_details(db: Session, snapshot_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
    tables = db.scalars(select(CatalogTable).where(CatalogTable.snapshot_id == snapshot_id)).all()
    by_id = {t.id: t for t in tables}
    codes = {c.id: c for c in db.scalars(
        select(CatalogCode).options(defer(CatalogCode.source)).where(CatalogCode.snapshot_id == snapshot_id)
    )}
    code_summary = {cid: CatalogCodeSummary.model_validate(c) for cid, c in codes.items()}

    outgoing, incoming = defaultdict(list), defaultdict(list)
    for rel in db.scalars(select(CatalogRelation).where(CatalogRelation.snapshot_id == snapshot_id)):
        outgoing[rel.from_table_id].append(rel)
        if rel.to_table_id:
            incoming[rel.to_table_id].append(rel)
    used_by = defaultdict(list)
    for u in db.scalars(select(CatalogUsage).where(CatalogUsage.snapshot_id == snapshot_id)):
        used_by[u.table_id].append(u)
    triggers = defaultdict(list)
    for c in codes.values():
        if c.kind == "trigger" and c.table_id:
            triggers[c.table_id].append(c)

    out = {}
    for t in tables:
        out_rels = sorted(outgoing[t.id], key=lambda r: (r.to_label, r.name or ""))
        in_rels = sorted(incoming[t.id], key=lambda r: (by_id[r.from_table_id].schema, by_id[r.from_table_id].name, r.name or ""))
        uses = sorted(used_by[t.id], key=lambda u: (codes[u.code_id].schema, codes[u.code_id].kind, codes[u.code_id].name))
        detail = CatalogTableDetail.model_validate(t).model_copy(update={
            "references": [_relation(r, by_id.get(r.to_table_id), outgoing=True) for r in out_rels if r.kind == "foreign_key"],
            "reads_from": [_relation(r, by_id.get(r.to_table_id), outgoing=True) for r in out_rels if r.kind == "view"],
            "referenced_by": [_relation(r, by_id[r.from_table_id], outgoing=False) for r in in_rels if r.kind == "foreign_key"],
            "read_by_views": [_relation(r, by_id[r.from_table_id], outgoing=False) for r in in_rels if r.kind == "view"],
            "used_by": [
                CatalogCodeUsage(code=code_summary[u.code_id], operations=u.operations, found_by=u.found_by,
                                 subprograms=u.subprograms, lines=u.lines, member_operations=u.member_operations or {})
                for u in uses
            ],
            "triggers": [code_summary[c.id] for c in sorted(triggers[t.id], key=lambda c: c.name)],
        })
        out[t.id] = detail.model_dump(mode="json")
    return out

