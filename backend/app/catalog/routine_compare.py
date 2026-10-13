"""Compare each procedure/function that uses a table with its migrated counterpart.

For the routines using the selected table on both sides (paired as in the table view):
- parameters: names, order, modes, types, defaults, return type (app.compare.signatures);
- tables used: every table each version reads or writes, and how;
- code checks: the Oracle-to-PostgreSQL pitfall checks of Scenario 2 (app.compare.static_checks).

Every difference has a category (signature | tables | code), a severity and, where it
applies, line numbers in each source.
"""

import re
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.table_compare import WRITES, match_code
from app.compare.signatures import compare_signatures
from app.compare.static_checks import check_pair
from app.compare.units import extract_member
from app.models import CatalogCode, CatalogTable, CatalogUsage

SEVERITY_STATUS = {"high": "error", "medium": "warning", "low": "info"}
STATUS_RANK = {"same": 0, "info": 1, "warning": 2, "error": 3}


# --- parameter lists ------------------------------------------------------------------------


def _split_top(text: str) -> list[str]:
    parts, depth, buf = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _paren_body(sig: str) -> tuple[str, str, str]:
    """(name, text inside the outer parentheses, text after them)."""
    start = sig.find("(")
    if start < 0:
        return sig.strip(), "", ""
    depth = 0
    for i in range(start, len(sig)):
        depth += sig[i] == "("
        depth -= sig[i] == ")"
        if depth == 0:
            return sig[:start].strip(), sig[start + 1:i], sig[i + 1:]
    return sig[:start].strip(), sig[start + 1:], ""


_ORA_ARG = re.compile(r"^(\S+)\s+(IN OUT|IN|OUT)\s+(.+?)(\s+DEFAULT)?$", re.I)


def parse_oracle_signature(sig: str) -> dict[str, Any]:
    """'PKG.NAME(P_ID IN NUMBER(10), P_X OUT VARCHAR2 DEFAULT) RETURN NUMBER' as recorded by the scan."""
    name, body, after = _paren_body(sig or "")
    args = []
    for i, part in enumerate(_split_top(body), start=1):
        m = _ORA_ARG.match(part)
        if not m:
            args.append({"position": i, "name": part, "mode": "IN", "type": "?", "data_type": None, "defaulted": False})
            continue
        typ = m.group(3).strip()
        args.append({"position": i, "name": m.group(1), "mode": m.group(2).upper(), "type": typ,
                     "data_type": re.sub(r"\(.*", "", typ).upper(), "defaulted": bool(m.group(4))})
    ret = re.search(r"\bRETURN\s+(.+)$", after.strip(), re.I)
    return {"name": name, "arguments": args, "return_type": ret.group(1).strip() if ret else None,
            "kind": "FUNCTION" if ret else "PROCEDURE"}


_PG_TYPE_WORDS = {
    "integer", "int", "int2", "int4", "int8", "bigint", "smallint", "numeric", "decimal", "real", "double", "float",
    "character", "char", "varchar", "text", "boolean", "bool", "date", "time", "timestamp", "timestamptz",
    "interval", "bytea", "json", "jsonb", "uuid", "xml", "money", "oid", "regclass", "refcursor", "record",
    "anyelement", "anyarray", "void", "inet", "cidr", "bit", "tsvector", "point",
}


def parse_pg_arguments(args: str) -> tuple[list[dict[str, Any]], int]:
    """pg_get_function_arguments / identity arguments -> (arguments, number of defaults)."""
    out, n_defaults = [], 0
    for i, part in enumerate(_split_top(args or ""), start=1):
        text = part.strip()
        default = re.search(r"\s+DEFAULT\s+.*$|\s*=\s*.*$", text, re.I)
        if default:
            n_defaults += 1
            text = text[:default.start()].strip()
        mode = "IN"
        m = re.match(r"^(INOUT|IN|OUT|VARIADIC)\s+", text, re.I)
        if m:
            mode = {"INOUT": "IN OUT"}.get(m.group(1).upper(), m.group(1).upper())
            text = text[m.end():]
        first, _, rest = text.partition(" ")
        if not rest or first.lower().strip('"') in _PG_TYPE_WORDS:
            name, typ = None, text
        else:
            name, typ = first.strip('"'), rest.strip()
        out.append({"position": i, "name": name, "mode": mode, "type": typ})
    return out, n_defaults


def parse_pg_signature(code: dict[str, Any]) -> dict[str, Any]:
    sig = code.get("signature") or ""
    name, body, after = _paren_body(sig)
    full = (code.get("extra") or {}).get("arguments")
    args, n_defaults = parse_pg_arguments(full if full is not None else body)
    ret = re.search(r"\bRETURNS\s+(.+)$", after.strip(), re.I)
    return {"name": name, "arguments": args, "n_defaults": n_defaults if full is not None else None,
            "result": ret.group(1).strip() if ret else None, "kind": code.get("kind")}


# --- sources and table usage ----------------------------------------------------------------------


def _oracle_unit(entry: dict[str, Any], code: CatalogCode, pg_arg_count: int) -> dict[str, Any]:
    """Signature, source and line range of an Oracle routine (a package member is cut out of its body)."""
    source = code.source or ""
    if not entry.get("package"):
        return {"signature": parse_oracle_signature(code.signature or ""), "source": source, "start": 1,
                "range": None}
    members = [m for m in (code.extra or {}).get("members", [])
               if _paren_body(m)[0].split(".")[-1].upper() == entry["member"].upper()]
    chosen = min(members, key=lambda m: abs(len(parse_oracle_signature(m)["arguments"]) - pg_arg_count)) if members else None
    occurrence = members.index(chosen) if chosen else 0
    body_at = source.find("CREATE OR REPLACE PACKAGE BODY")
    member_src, start, rng = source, 1, None
    if body_at >= 0:
        found = extract_member(source[body_at:], entry["member"], occurrence) or extract_member(source[body_at:], entry["member"])
        if found:
            member_src, rel = found
            start = source[:body_at].count("\n") + rel
            rng = (start, start + member_src.rstrip("\n").count("\n"))
    sig = parse_oracle_signature(chosen) if chosen else {"name": entry["name"], "arguments": [], "return_type": None, "kind": "PROCEDURE"}
    return {"signature": sig, "source": member_src, "start": start, "range": rng}


def _usage_map(db: Session, code_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[tuple[str, CatalogUsage]]]:
    out = defaultdict(list)
    for usage, schema, name in db.execute(
        select(CatalogUsage, CatalogTable.schema, CatalogTable.name)
        .join(CatalogTable, CatalogTable.id == CatalogUsage.table_id).where(CatalogUsage.code_id.in_(code_ids))
    ):
        out[usage.code_id].append((f"{schema}.{name}", usage))
    return out


def _tables_of(entry: dict[str, Any], usages: list[tuple[str, CatalogUsage]]) -> dict[str, set[str]]:
    """Tables a routine uses -> operations; for an Oracle package member, only that member's."""
    out: dict[str, set[str]] = {}
    for label, u in usages:
        if entry.get("member"):
            member_ops = u.member_operations or {}
            if entry["member"] in member_ops:
                out[label.lower()] = set(member_ops[entry["member"]])
            elif entry["member"] in (u.subprograms or []):
                out[label.lower()] = set(u.operations)
        else:
            out[label.lower()] = set(u.operations)
    return out


def _table_diffs(left: dict[str, set[str]], right: dict[str, set[str]], names) -> list[dict[str, Any]]:
    diffs = []
    for table in sorted(set(left) | set(right)):
        lo, ro = left.get(table), right.get(table)
        if ro is None:
            lost = (lo or set()) & WRITES
            diffs.append(_diff("tables", "high", f"No longer uses {table}",
                               f"{names[0]} {_ops(lo)} {table}; the {names[1]} version doesn't reference it"
                               + (" (writes are lost)" if lost else "") + "."))
        elif lo is None:
            diffs.append(_diff("tables", "low", f"Also uses {table}", f"Only the {names[1]} version references {table} ({_ops(ro)})."))
        elif lo and ro and lo != ro:
            lost = (lo & WRITES) - ro
            diffs.append(_diff("tables", "high" if lost else "medium", f"Uses {table} differently",
                               f"{names[0]}: {_ops(lo)}; {names[1]}: {_ops(ro)}"
                               + (f". No longer {', '.join(sorted(o.upper() for o in lost))}." if lost else ".")))
    return diffs


def _ops(ops: set[str] | None) -> str:
    return ", ".join(sorted(o.upper() for o in ops or [])) or "references"


def _diff(category, severity, title, detail, left_lines=(), right_lines=()) -> dict[str, Any]:
    return {"category": category, "severity": severity, "title": title, "detail": detail,
            "left_lines": list(left_lines), "right_lines": list(right_lines)}


# --- the comparison -------------------------------------------------------------------------------


def compare_routines(db: Session, lt: dict[str, Any], rt: dict[str, Any], *, left_dialect: str, right_dialect: str,
                     names: tuple[str, str], orafce: bool) -> dict[str, Any]:
    pairs, unmatched = match_code(lt, rt)
    ids: set[uuid.UUID] = {uuid.UUID(e["code_id"]) for e in unmatched}
    for e, o in pairs:
        ids.update(uuid.UUID(x["code_id"]) for x in (e, o) if x)
    codes = {c.id: c for c in db.scalars(select(CatalogCode).where(CatalogCode.id.in_(ids)))} if ids else {}
    usages = _usage_map(db, list(ids)) if ids else {}
    cross = {left_dialect, right_dialect} == {"oracle", "postgres"}
    oracle_is_left = left_dialect == "oracle"

    rows = []
    for e, other in pairs:
        if other is None:
            left = _brief(e)
            lcode = codes[uuid.UUID(e["code_id"])]
            if left_dialect == "oracle":  # still show its parameters and, for a package member, only its lines
                unit = _oracle_unit(e, lcode, 0)
                left = {**left, "signature": unit["signature"], "range": unit["range"]}
            rows.append({"left": left, "right": None, "status": "error",
                         "differences": [_diff("tables", "high", f"Not found in {names[1]} code that uses this table",
                                               "Either it wasn't migrated, it was renamed, or its new version no longer uses this table. "
                                               "See the routines section above.")]})
            continue
        lcode, rcode = codes[uuid.UUID(e["code_id"])], codes[uuid.UUID(other["code_id"])]
        differences: list[dict[str, Any]] = []
        ranges: dict[str, Any] = {"left": None, "right": None}
        if cross:
            o_entry, o_code, p_code = (e, lcode, rcode) if oracle_is_left else (other, rcode, lcode)
            pg_sig = parse_pg_signature({"signature": p_code.signature, "extra": p_code.extra, "kind": p_code.kind})
            unit = _oracle_unit(o_entry, o_code, len(pg_sig["arguments"]))
            ranges["left" if oracle_is_left else "right"] = unit["range"]
            ora_sig = unit["signature"]
            aligned, moved = align_by_name(ora_sig["arguments"], pg_sig["arguments"])
            _, sig_diffs = compare_signatures(
                ora_sig["kind"], ora_sig["arguments"], ora_sig["return_type"],
                p_code.kind, aligned, pg_sig["result"], pg_sig["n_defaults"] or 0,
            )
            differences += _signature_diffs(sig_diffs, ora_sig, {**pg_sig, "arguments": aligned}, p_code.kind, moved)
            for f in check_pair("routine", unit["source"], unit["start"], ora_sig["arguments"], p_code.kind,
                                p_code.source or "", [], orafce=orafce):
                lines = (f.oracle_lines, f.postgres_lines) if oracle_is_left else (f.postgres_lines, f.oracle_lines)
                differences.append(_diff("code", f.severity, f.title, f.detail, *lines))
            left_sig, right_sig = (ora_sig, pg_sig) if oracle_is_left else (pg_sig, ora_sig)
        else:  # same dialect on both sides: parameters and code text
            left_sig = parse_pg_signature({"signature": lcode.signature, "extra": lcode.extra, "kind": lcode.kind})
            right_sig = parse_pg_signature({"signature": rcode.signature, "extra": rcode.extra, "kind": rcode.kind})
            if (lcode.signature or "").lower() != (rcode.signature or "").lower():
                differences.append(_diff("signature", "high", "Different signature", f"{lcode.signature} vs {rcode.signature}"))
            if _normalize_source(lcode.source) != _normalize_source(rcode.source):
                differences.append(_diff("code", "medium", "Source code differs", "The two definitions are not identical."))
        differences += _table_diffs(_tables_of(e, usages.get(lcode.id, [])), _tables_of(other, usages.get(rcode.id, [])), names)

        worst = max((STATUS_RANK[SEVERITY_STATUS[d["severity"]]] for d in differences), default=0)
        rows.append({
            "left": {**_brief(e), "signature": left_sig, "range": ranges["left"]},
            "right": {**_brief(other), "signature": right_sig, "range": ranges["right"]},
            "status": next(s for s, r in STATUS_RANK.items() if r == worst),
            "differences": sorted(differences, key=lambda d: -STATUS_RANK[SEVERITY_STATUS[d["severity"]]]),
        })
    rows += [{"left": None, "right": _brief(e), "status": "info",
              "differences": [_diff("tables", "low", f"Only in {names[1]}", "No routine on the other side uses this table under that name.")]}
             for e in unmatched]
    rows.sort(key=lambda r: (-STATUS_RANK[r["status"]], (r["left"] or r["right"])["name"].lower()))
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUS_RANK}
    return {"names": names, "routines": rows, "counts": counts,
            "differences": sum(len(r["differences"]) for r in rows if r["status"] != "same")}


_INT_TYPES = ("smallint", "integer", "bigint", "int", "int2", "int4", "int8")


def align_by_name(oracle_args: list[dict[str, Any]], pg_args: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """PostgreSQL parameters put in Oracle's order when every Oracle parameter has a
    namesake, so each is compared with its real counterpart. Returns the reordered list
    and the parameters that moved (empty when the order is the same)."""
    names = [(a.get("name") or "").lower() for a in pg_args]
    wanted = [(a.get("name") or "").lower() for a in oracle_args]
    if not all(wanted) or len(set(wanted)) != len(wanted) or not set(wanted) <= set(names) or wanted == names[:len(wanted)]:
        return pg_args, []
    by_name = {(a.get("name") or "").lower(): a for a in pg_args}
    aligned = [by_name[n] for n in wanted] + [a for a in pg_args if (a.get("name") or "").lower() not in set(wanted)]
    moved = [o.get("name") for i, o in enumerate(oracle_args) if names.index(wanted[i]) != i]
    return [{**a, "position": i} for i, a in enumerate(aligned, start=1)], moved


def _signature_diffs(sig_diffs, ora_sig, pg_sig, pg_kind, moved_by_name=()) -> list[dict[str, Any]]:
    """Signature differences, with the conversions Ora2Pg makes on purpose shown as notes."""
    out, moved = [], list(moved_by_name)
    by_pos = {a["position"]: a for a in ora_sig["arguments"]}
    pg_by_pos = {a["position"]: a for a in pg_sig["arguments"]}
    for d in sig_diffs:
        severity, message = d.severity, d.message
        if d.code == "DEFAULT_MISSING" and pg_sig["n_defaults"] is None:
            continue  # this snapshot doesn't record PostgreSQL defaults; refresh it to compare them
        if d.code == "ARG_ORDER":
            o = by_pos.get(d.position)
            moved.append((o or {}).get("name") or str(d.position))
            continue
        if d.code == "ARG_MODE" and pg_kind == "procedure" and "is OUT in Oracle, IN OUT in PostgreSQL" in message:
            severity = "low"
            message += ". This is how Ora2Pg converts procedure OUT parameters: callers must pass a value (e.g. NULL) for it"
        if d.code in ("ARG_TYPE", "RETURN_TYPE"):
            o_type = (by_pos.get(d.position) or {}).get("type") if d.code == "ARG_TYPE" else ora_sig.get("return_type")
            p_type = (pg_by_pos.get(d.position) or {}).get("type") if d.code == "ARG_TYPE" else pg_sig.get("result")
            if (o_type or "").upper() == "NUMBER" and (p_type or "").lower() in _INT_TYPES:
                # PL/SQL parameters have no precision: NUMBER -> bigint is the usual choice for ids
                severity = "low"
                what = "Return type" if d.code == "RETURN_TYPE" else (message.split(":", 1)[0] if ":" in message else "Parameter")
                message = (f"{what}: NUMBER became {p_type}, as usual for whole numbers such as ids. "
                           "Only a value with decimals would be rounded.")
        out.append(_diff("signature", severity, _SIG_TITLES.get(d.code, d.code.title()), message))
    if moved:
        out.append(_diff("signature", "high", "Parameters reordered",
                         f"{', '.join(moved)} {'is' if len(moved) == 1 else 'are'} at a different position in PostgreSQL: "
                         "callers passing parameters by position send values to the wrong parameter."))
    return out


_SIG_TITLES = {
    "KIND": "Procedure/function changed", "ARG_COUNT": "Different number of parameters", "ARG_ORDER": "Parameters reordered",
    "ARG_NAME": "Parameter renamed", "ARG_MODE": "Parameter mode changed", "ARG_TYPE": "Parameter type changed",
    "DEFAULT_MISSING": "Parameter default missing", "RETURN_TYPE": "Return type changed",
}


def _brief(e: dict[str, Any]) -> dict[str, Any]:
    return {"code_id": e["code_id"], "schema": e["schema"], "name": e["name"], "kind": e["kind"],
            "package": e.get("package"), "member": e.get("member"), "operations": e["operations"]}


def _normalize_source(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", text or "")).strip().lower()
