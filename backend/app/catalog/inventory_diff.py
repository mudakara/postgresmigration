"""What one catalog has that the other doesn't, area by area (Overview "Differences" report).

Typically Oracle (left) against its migrated PostgreSQL (right). Tables and views are
paired as on the Table compare page; columns, indexes and foreign keys are compared
inside each matched table pair; procedures and functions are matched across the
migration (an Oracle package member PKG.MEMBER is PostgreSQL pkg.member, or
schema.pkg_member), and triggers by schema and name.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.catalog.bulk import snapshot_details
from app.catalog.routine_compare import parse_oracle_signature
from app.catalog.table_compare import _cols, _index_key, _key, pair_tables
from app.models import CatalogCode

AREAS = ["tables", "views", "columns", "indexes", "foreign_keys", "procedures", "functions", "triggers"]


def _table_item(t: dict[str, Any]) -> dict[str, Any]:
    return {"name": f"{t['schema_name']}.{t['name']}", "table_id": t["id"], "kind": t["kind"]}


def _routines(db: Session, snapshot_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every procedure and function, Oracle package members included (one entry per name)."""
    out, seen = [], set()
    for c in db.scalars(select(CatalogCode).options(defer(CatalogCode.source)).where(
            CatalogCode.snapshot_id == snapshot_id, CatalogCode.kind.in_(["procedure", "function", "package"]))):
        extra = c.extra or {}
        if c.kind == "function" and extra.get("is_trigger_function"):
            continue
        if c.kind == "package":
            for sig in extra.get("members") or []:
                parsed = parse_oracle_signature(sig)
                member = parsed["name"].split(".")[-1]
                key = (c.schema.lower(), c.name.lower(), member.lower())
                if key in seen:
                    continue  # overloads: one entry per name
                seen.add(key)
                out.append({"name": f"{c.schema}.{c.name}.{member}", "kind": parsed["kind"].lower(), "code_id": str(c.id),
                            "schema": c.schema, "package": c.name, "member": member})
        else:
            key = (c.schema.lower(), "", c.name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": f"{c.schema}.{c.name}", "kind": c.kind, "code_id": str(c.id), "schema": c.schema,
                        "package": None, "member": None, "plain": c.name})
    return out


def _routine_keys(r: dict[str, Any]) -> list[tuple[str, str]]:
    if r["package"]:
        pkg, member = r["package"].lower(), r["member"].lower()
        return [(pkg, member), (r["schema"].lower(), f"{pkg}_{member}")]
    return [(r["schema"].lower(), r["plain"].lower())]


def _match(left: list[dict], right: list[dict], keys_left, keys_right, *, exclusive: bool = True
           ) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Pair items by key. Non-exclusive matching lets several left items share one right
    item: packages of the same name in two Oracle schemas both migrate into one PostgreSQL schema."""
    index: dict[Any, list[dict]] = {}
    for r in right:
        for k in keys_right(r):
            index.setdefault(k, []).append(r)
    pairs, used = [], set()
    only_left = []
    for item in left:
        candidates = [o for k in keys_left(item) for o in index.get(k, [])]
        other = next((o for o in candidates if id(o) not in used), None if exclusive else (candidates[0] if candidates else None))
        if other is None:
            only_left.append(item)
        else:
            used.add(id(other))
            pairs.append((item, other))
    return pairs, only_left, [r for r in right if id(r) not in used]


def diff(db: Session, left_snapshot: uuid.UUID, right_snapshot: uuid.UUID) -> dict[str, Any]:
    left, right = snapshot_details(db, left_snapshot), snapshot_details(db, right_snapshot)
    areas: dict[str, dict[str, Any]] = {}

    def area(name, left_count, right_count, matched, only_left, only_right, notes=None, **extra):
        areas[name] = {"left": left_count, "right": right_count, "matched": matched,
                       "only_left": only_left, "only_right": only_right, "notes": notes or [], **extra}

    # Tables and views (partitions are folded into their parent on PostgreSQL).
    pairs_all = pair_tables(list(left.values()), list(right.values()))
    for name, is_view in (("tables", False), ("views", True)):
        def want(t):
            return t is not None and ("view" in t["kind"]) == is_view
        pairs = [(lt, rt) for lt, rt in pairs_all if want(lt) or want(rt)]
        area(name,
             sum(1 for t in left.values() if want(t)), sum(1 for t in right.values() if want(t)),
             sum(1 for lt, rt in pairs if lt and rt),
             [_table_item(lt) for lt, rt in pairs if lt and not rt],
             [_table_item(rt) for lt, rt in pairs if rt and not lt])
    matched_tables = [(lt, rt) for lt, rt in pairs_all if lt and rt and "view" not in lt["kind"]]

    # Columns, indexes and foreign keys: inside matched table pairs only.
    cols_l = cols_r = cols_m = 0
    only_l, only_r, type_diff = [], [], 0
    idx_l = idx_r = idx_m = 0
    idx_only_l, idx_only_r = [], []
    fk_l = fk_r = fk_m = 0
    fk_only_l, fk_only_r = [], []
    for lt, rt in matched_tables:
        lnames = {_key(c["name"]): c for c in lt["columns"]}
        rnames = {_key(c["name"]): c for c in rt["columns"]}
        cols_l, cols_r = cols_l + len(lnames), cols_r + len(rnames)
        cols_m += len(lnames.keys() & rnames.keys())
        base = {"table": f"{lt['schema_name']}.{lt['name']}", "left_table_id": lt["id"], "right_table_id": rt["id"]}
        only_l += [{**base, "name": f"{lt['schema_name']}.{lt['name']}.{c['name']}", "type": c["type"]}
                   for k, c in lnames.items() if k not in rnames]
        only_r += [{**base, "name": f"{rt['schema_name']}.{rt['name']}.{c['name']}", "type": c["type"]}
                   for k, c in rnames.items() if k not in lnames]

        lix = {_index_key(ix["columns"]): ix for ix in lt["indexes"]}
        rix = {_index_key(ix["columns"]): ix for ix in rt["indexes"]}
        idx_l, idx_r, idx_m = idx_l + len(lix), idx_r + len(rix), idx_m + len(lix.keys() & rix.keys())
        idx_only_l += [{**base, "name": f"{lt['schema_name']}.{ix['name']}", "columns": ", ".join(ix["columns"]),
                        "unique": bool(ix.get("unique"))} for k, ix in lix.items() if k not in rix]
        idx_only_r += [{**base, "name": f"{rt['schema_name']}.{ix['name']}", "columns": ", ".join(ix["columns"]),
                        "unique": bool(ix.get("unique"))} for k, ix in rix.items() if k not in lix]

        def fk_key(r):
            return (frozenset(_cols(r["columns"])), r["other_label"].split(".")[-1].lower())
        lfk = {fk_key(r): r for r in lt["references"]}
        rfk = {fk_key(r): r for r in rt["references"]}
        fk_l, fk_r, fk_m = fk_l + len(lfk), fk_r + len(rfk), fk_m + len(lfk.keys() & rfk.keys())
        fk_only_l += [{**base, "name": f"{lt['schema_name']}.{r['name']}", "columns": f"({', '.join(r['columns'])}) → {r['other_label']}"}
                      for k, r in lfk.items() if k not in rfk]
        fk_only_r += [{**base, "name": f"{rt['schema_name']}.{r['name']}", "columns": f"({', '.join(r['columns'])}) → {r['other_label']}"}
                      for k, r in rfk.items() if k not in lfk]
    in_pairs = "Counted inside the tables that exist on both sides."
    area("columns", cols_l, cols_r, cols_m, only_l, only_r, [in_pairs])
    area("indexes", idx_l, idx_r, idx_m, idx_only_l, idx_only_r, [in_pairs + " Matched by key columns, so renamed indexes still match."])
    area("foreign_keys", fk_l, fk_r, fk_m, fk_only_l, fk_only_r, [in_pairs])

    # Procedures and functions, matched together: a procedure may have become a function.
    lr, rr = _routines(db, left_snapshot), _routines(db, right_snapshot)
    pairs, only_left, only_right = _match(lr, rr, _routine_keys, _routine_keys, exclusive=False)
    kind_changed = [{"name": a["name"], "kind": f"{a['kind']} → {b['kind']}", "code_id": a["code_id"], "right_code_id": b["code_id"]}
                    for a, b in pairs if a["kind"] != b["kind"]]
    for kind, label in (("procedure", "procedures"), ("function", "functions")):
        area(label, sum(1 for r in lr if r["kind"] == kind), sum(1 for r in rr if r["kind"] == kind),
             sum(1 for a, b in pairs if a["kind"] == kind),
             [r for r in only_left if r["kind"] == kind], [r for r in only_right if r["kind"] == kind],
             ["Oracle package members are counted with the standalone ones (PKG.MEMBER matches pkg.member).",
              "Overloaded routines (same name, different parameters) count once here, so totals can be slightly "
              "lower than on the statistics cards."]
             + ([f"{sum(1 for k in kind_changed if k['kind'].startswith(kind))} {label} changed kind during the migration "
                 f"(e.g. procedure → function); they count as matched."] if any(k["kind"].startswith(kind) for k in kind_changed) else []),
             kind_changed=[k for k in kind_changed if k["kind"].startswith(kind)])

    # Triggers, by schema and name.
    def triggers(details):
        return [{"name": f"{t['schema_name']}.{tr['name']}", "table": f"{t['schema_name']}.{t['name']}", "code_id": tr["id"],
                 "key": (t["schema_name"].lower(), tr["name"].lower())} for t in details.values() for tr in t["triggers"]]
    lt_, rt_ = triggers(left), triggers(right)
    tpairs, t_only_l, t_only_r = _match(lt_, rt_, lambda t: [t["key"]], lambda t: [t["key"]])
    area("triggers", len(lt_), len(rt_), len(tpairs), [_strip(t) for t in t_only_l], [_strip(t) for t in t_only_r])

    for a in areas.values():
        for side in ("only_left", "only_right"):
            a[side].sort(key=lambda r: r["name"].lower())
    return {"areas": {k: areas[k] for k in AREAS}}


def _strip(t: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in t.items() if k != "key"}


def counts_only(result: dict[str, Any]) -> dict[str, Any]:
    """The report without the item lists (for the summary table)."""
    return {"areas": {k: {**{x: y for x, y in a.items() if x not in ("only_left", "only_right", "kind_changed")},
                          "only_left_count": len(a["only_left"]), "only_right_count": len(a["only_right"]),
                          "kind_changed_count": len(a.get("kind_changed") or [])}
                      for k, a in result["areas"].items()}}

