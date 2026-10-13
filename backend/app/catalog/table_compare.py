"""Compare one table between two catalog snapshots, side by side.

Typically the left side is the Oracle original and the right side its migrated
PostgreSQL table, but any two snapshots work (e.g. PostgreSQL UAT vs PreProd). Names are
matched case-insensitively (Oracle folds to upper case, PostgreSQL to lower). Data types
are judged by what a correct migration produces, not by their spelling.

Every row has a status: same | equivalent | info | warning | error.
"""

import re
from dataclasses import dataclass
from typing import Any

STATUS_RANK = {"same": 0, "equivalent": 0, "info": 1, "warning": 2, "error": 3}
WRITES = {"insert", "update", "delete", "merge", "truncate", "ddl"}


# --- data types ----------------------------------------------------------------------


@dataclass(frozen=True)
class Canon:
    kind: str  # num | int | float | varchar | char | text | date | datetime | timestamp | timestamptz | bytes | bool | other
    p: int | None = None  # precision, length, or integer digits
    s: int | None = None  # scale
    raw: str = ""


_INT_DIGITS = {"smallint": 4, "integer": 9, "bigint": 18}  # digits every value of that size can hold


def _nums(text: str) -> list[int]:
    """Numbers inside the type's parentheses: VARCHAR2(10 CHAR) -> [10], NUMBER(12,2) -> [12, 2],
    TIMESTAMP(6) WITH TIME ZONE -> [6]. Never the digit in a name like VARCHAR2."""
    m = re.search(r"\(([^)]*)\)", text)
    return [int(n) for n in re.findall(r"\d+", m.group(1))] if m else []


def canon(type_: str, dialect: str) -> Canon:
    t = (type_ or "").strip()
    u = t.upper()
    n = _nums(t)
    if dialect == "oracle":
        if u.startswith("NUMBER"):
            return Canon("num", n[0] if n else None, (n[1] if len(n) > 1 else 0) if n else None, t)
        if u in ("INTEGER", "INT", "SMALLINT"):
            return Canon("num", 38, 0, t)
        if u.startswith(("FLOAT", "BINARY_DOUBLE", "BINARY_FLOAT")):
            return Canon("float", raw=t)
        if u.startswith(("VARCHAR2", "NVARCHAR2", "VARCHAR")):
            return Canon("varchar", n[0] if n else None, raw=t)
        if u.startswith(("CHAR", "NCHAR")):
            return Canon("char", n[0] if n else 1, raw=t)
        if u in ("CLOB", "NCLOB", "LONG"):
            return Canon("text", raw=t)
        if u == "DATE":
            return Canon("datetime", 0, raw=t)  # Oracle DATE carries a time of day
        if u.startswith("TIMESTAMP"):
            if "TIME ZONE" in u:
                return Canon("timestamptz", n[0] if n else 6, raw=t)
            return Canon("timestamp", n[0] if n else 6, raw=t)
        if u in ("BLOB", "LONG RAW", "BFILE") or u.startswith("RAW"):
            return Canon("bytes", raw=t)
        if u in ("XMLTYPE", "SYS.XMLTYPE"):
            return Canon("xml", raw=t)
        return Canon("other", raw=t)
    low = t.lower()
    if low in _INT_DIGITS:
        return Canon("int", _INT_DIGITS[low], 0, t)
    if low.startswith(("numeric", "decimal")):
        return Canon("num", n[0] if n else None, (n[1] if len(n) > 1 else 0) if n else None, t)
    if low in ("real", "double precision") or low.startswith("float"):
        return Canon("float", raw=t)
    if low.startswith(("character varying", "varchar")):
        return Canon("varchar", n[0], raw=t) if n else Canon("text", raw=t)
    if low.startswith(("character", "char", "bpchar")):
        return Canon("char", n[0] if n else 1, raw=t)
    if low in ("text", "citext"):
        return Canon("text", raw=t)
    if low == "date":
        return Canon("date", raw=t)
    if low.startswith("timestamp"):
        precision = n[0] if n else 6
        return Canon("timestamptz" if "with time zone" in low else "timestamp", precision, raw=t)
    if low == "bytea":
        return Canon("bytes", raw=t)
    if low == "boolean":
        return Canon("bool", raw=t)
    if low == "xml":
        return Canon("xml", raw=t)
    return Canon("other", raw=t)


def _norm_type(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def compare_types(left: str, left_dialect: str, right: str, right_dialect: str) -> tuple[str, str]:
    """(status, message). With an Oracle and a PostgreSQL side, Oracle is the source."""
    if left_dialect == right_dialect:
        if _norm_type(left) == _norm_type(right):
            return "same", ""
        return "error", f"Different types: {left} vs {right}"
    swapped = left_dialect != "oracle"
    src, dst = (right, left) if swapped else (left, right)
    status, message = _cross(canon(src, "oracle"), canon(dst, "postgres"))
    return status, message


def _cross(o: Canon, p: Canon) -> tuple[str, str]:
    O, P = o.raw, p.raw
    if o.kind == "num":
        if p.kind == "int":
            if o.s:
                return "error", f"{O} has decimals; {P} drops them"
            if o.p is None:
                return "warning", f"Unconstrained {O} mapped to {P}: values over {P}'s range or with decimals would fail"
            if o.p <= p.p:
                return "equivalent", ""
            return "error", f"{O} can hold {o.p} digits; {P} holds at most {p.p}: large values will overflow"
        if p.kind == "num":
            if p.p is None:
                return "equivalent", ""
            if o.p is None:
                return "warning", f"Unconstrained {O} mapped to {P}: larger values or more decimals would fail or round"
            if (o.s or 0) > (p.s or 0):
                return "error", f"{O} keeps {o.s} decimals; {P} rounds to {p.s}"
            if o.p - (o.s or 0) > p.p - (p.s or 0):
                return "error", f"{O} allows larger values than {P}"
            return "equivalent", ""
        if p.kind == "float":
            return "warning", f"Exact {O} mapped to approximate {P}: rounding can change values"
        if p.kind == "bool" and o.p == 1:
            return "warning", f"{O} mapped to boolean: check how 0/1 values are converted and compared"
        return "error", f"{O} is numeric; {P} is not"
    if o.kind == "float":
        if p.kind in ("float", "num"):
            return "equivalent", ""
        return "error", f"{O} is numeric; {P} is not"
    if o.kind == "varchar":
        if p.kind == "text":
            return "equivalent", ""
        if p.kind == "varchar":
            if p.p is None or o.p is None or p.p >= o.p:
                return "equivalent", ""
            return "error", f"{O} holds {o.p} characters; {P} only {p.p}"
        if p.kind == "char":
            return "warning", f"{O} mapped to fixed-length {P}: values are blank-padded"
        return "error", f"{O} is text; {P} is not"
    if o.kind == "char":
        if p.kind == "char":
            if p.p == o.p:
                return "equivalent", ""
            return ("error" if (p.p or 0) < (o.p or 0) else "warning"), f"{O} vs {P}: different length"
        if p.kind in ("varchar", "text"):
            if p.kind == "varchar" and p.p is not None and o.p is not None and p.p < o.p:
                return "error", f"{O} holds {o.p} characters; {P} only {p.p}"
            return "warning", f"Fixed-length {O} mapped to {P}: trailing blanks and comparisons behave differently"
        if p.kind == "bool" and o.p == 1:
            return "warning", f"{O} mapped to boolean: check how Y/N or 0/1 values are converted"
        return "error", f"{O} is text; {P} is not"
    if o.kind == "text":
        if p.kind == "text":
            return "equivalent", ""
        if p.kind == "varchar":
            return "warning", f"{O} is unlimited; {P} limits the length"
        return "error", f"{O} is text; {P} is not"
    if o.kind == "datetime":
        if p.kind == "timestamp":
            return "equivalent", ""
        if p.kind == "date":
            return "warning", f"Oracle {O} includes a time of day; PostgreSQL {P} drops it"
        if p.kind == "timestamptz":
            return "warning", f"{O} has no time zone; {P} converts values with the session time zone"
        return "error", f"{O} is a date/time; {P} is not"
    if o.kind == "timestamp":
        if p.kind == "timestamp":
            return ("equivalent", "") if p.p >= o.p else ("warning", f"{O} keeps more fractional seconds than {P}")
        if p.kind == "timestamptz":
            return "warning", f"{O} has no time zone; {P} converts values with the session time zone"
        if p.kind == "date":
            return "error", f"{O} has a time of day; {P} drops it"
        return "error", f"{O} is a date/time; {P} is not"
    if o.kind == "timestamptz":
        if p.kind == "timestamptz":
            return "equivalent", ""
        if p.kind == "timestamp":
            return "warning", f"{O} keeps the time zone; {P} doesn't"
        return "error", f"{O} is a date/time; {P} is not"
    if o.kind in ("bytes", "xml"):
        return ("equivalent", "") if p.kind == o.kind else ("error", f"{O} vs {P}: different kinds of data")
    if _norm_type(O) == _norm_type(P):
        return "same", ""
    return "warning", f"{O} vs {P}: check the mapping"


# --- helpers ----------------------------------------------------------------------------


def _key(name: str | None) -> str:
    text = (name or "").strip()
    if len(text) > 1 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.lower()


def _cols(cols) -> tuple[str, ...]:
    return tuple(_key(c) for c in cols or [])


def _index_key(columns: list[str]) -> tuple[str, ...]:
    return tuple(re.sub(r'[\s"]', "", re.sub(r"\s+desc$", "", c.strip(), flags=re.I)).lower() for c in columns)


def _row(status: str, left: Any, right: Any, message: str = "", **extra) -> dict[str, Any]:
    return {"status": status, "left": left, "right": right, "message": message, **extra}


def _missing(left, right, side_names, severity, what) -> dict[str, Any]:
    side = side_names[1] if left is not None else side_names[0]
    return _row(severity, left, right, f"{what} missing in {side}")


# --- sections -----------------------------------------------------------------------------


def compare_columns(lt, rt, ld, rd, names) -> list[dict[str, Any]]:
    rows = []
    right_by = {_key(c["name"]): (i, c) for i, c in enumerate(rt["columns"])}
    seen = set()
    for i, lc in enumerate(lt["columns"]):
        k = _key(lc["name"])
        match = right_by.get(k)
        left = {**lc, "position": i + 1}
        if match is None:
            rows.append(_missing(left, None, names, "error", "Column"))
            continue
        seen.add(k)
        j, rc = match
        right = {**rc, "position": j + 1}
        status, message = compare_types(lc["type"], ld, rc["type"], rd)
        messages = [message] if message else []
        if lc.get("nullable", True) != rc.get("nullable", True):
            messages.append("NOT NULL on " + (names[0] if not lc.get("nullable", True) else names[1]) + " only")
            status = max(status, "warning", key=STATUS_RANK.get)
        # Defaults are only read from PostgreSQL (Oracle keeps them in a LONG column).
        if ld == rd == "postgres" and _norm_type(lc.get("default") or "") != _norm_type(rc.get("default") or ""):
            messages.append(f"Defaults differ: {lc.get('default') or 'none'} vs {rc.get('default') or 'none'}")
            status = max(status, "warning", key=STATUS_RANK.get)
        if i != j and status in ("same", "equivalent"):
            messages.append(f"Column position {i + 1} vs {j + 1}")
            status = "info"
        rows.append(_row(status, left, right, "; ".join(messages)))
    for j, rc in enumerate(rt["columns"]):
        if _key(rc["name"]) not in seen:
            rows.append(_missing(None, {**rc, "position": j + 1}, names, "warning", "Column"))
    return rows


def compare_keys(lt, rt, names) -> list[dict[str, Any]]:
    rows = []
    lpk, rpk = lt.get("primary_key"), rt.get("primary_key")
    if lpk or rpk:
        if not lpk or not rpk:
            rows.append(_row("error", lpk and {**lpk, "kind": "primary key"}, rpk and {**rpk, "kind": "primary key"},
                             f"Primary key missing in {names[1] if lpk else names[0]}"))
        elif set(_cols(lpk["columns"])) != set(_cols(rpk["columns"])):
            rows.append(_row("error", {**lpk, "kind": "primary key"}, {**rpk, "kind": "primary key"}, "Primary key columns differ"))
        else:
            status = "same" if _key(lpk["name"]) == _key(rpk["name"]) else "equivalent"
            rows.append(_row(status, {**lpk, "kind": "primary key"}, {**rpk, "kind": "primary key"},
                             "" if status == "same" else "Same columns, different constraint name"))
    right = {frozenset(_cols(u["columns"])): u for u in rt.get("unique_keys", [])}
    used = set()
    for u in lt.get("unique_keys", []):
        k = frozenset(_cols(u["columns"]))
        if k in right:
            used.add(k)
            rows.append(_row("equivalent" if _key(u["name"]) != _key(right[k]["name"]) else "same",
                             {**u, "kind": "unique"}, {**right[k], "kind": "unique"}))
        else:
            rows.append(_row("error", {**u, "kind": "unique"}, None, f"Unique key missing in {names[1]}"))
    for k, u in right.items():
        if k not in used:
            rows.append(_row("warning", None, {**u, "kind": "unique"}, f"Unique key only in {names[1]}"))
    return rows


def compare_indexes(lt, rt, names) -> list[dict[str, Any]]:
    rows = []
    right = {}
    for ix in rt.get("indexes", []):
        right.setdefault(_index_key(ix["columns"]), ix)
    used = set()
    for ix in lt.get("indexes", []):
        k = _index_key(ix["columns"])
        other = right.get(k)
        if other is None:
            sev = "error" if ix.get("unique") else "warning"
            why = "unique index: duplicates are no longer prevented" if ix.get("unique") else "queries that used it may scan the table"
            rows.append(_row(sev, ix, None, f"No index on these columns in {names[1]} ({why})"))
            continue
        used.add(k)
        messages, status = [], "same" if _key(ix["name"]) == _key(other["name"]) else "equivalent"
        if bool(ix.get("unique")) != bool(other.get("unique")):
            status, messages = "error", ["Unique on " + (names[0] if ix.get("unique") else names[1]) + " only"]
        if (ix.get("predicate") or "") != (other.get("predicate") or "") and (ix.get("predicate") or other.get("predicate")):
            status = max(status, "warning", key=STATUS_RANK.get)
            messages.append("Partial index condition differs")
        for side, x in ((names[0], ix), (names[1], other)):
            if x.get("status") and x["status"] not in ("VALID", "N/A"):
                status = max(status, "warning", key=STATUS_RANK.get)
                messages.append(f"{x['status']} in {side}")
        rows.append(_row(status, ix, other, "; ".join(messages)))
    for k, ix in right.items():
        if k not in used:
            rows.append(_row("info", None, ix, f"Only in {names[1]}"))
    return rows


def _fk_key(r) -> tuple:
    return (frozenset(_cols(r["columns"])), _key(r["other_label"]))


def compare_foreign_keys(left_rels, right_rels, names, *, incoming: bool) -> list[dict[str, Any]]:
    rows = []
    right = {_fk_key(r): r for r in right_rels}
    used = set()
    for r in left_rels:
        k = _fk_key(r)
        other = right.get(k)
        if other is None:
            rows.append(_row("warning" if incoming else "error", r, None,
                             f"{'Incoming foreign key' if incoming else 'Foreign key'} missing in {names[1]}"))
            continue
        used.add(k)
        messages, status = [], "same"
        if _cols(r["other_columns"]) != _cols(other["other_columns"]):
            status, messages = "error", ["Referenced columns differ"]
        ld, rd = (r["details"].get("on_delete") or "NO ACTION"), (other["details"].get("on_delete") or "NO ACTION")
        if ld.replace("NO ACTION", "RESTRICT") != rd.replace("NO ACTION", "RESTRICT"):
            status = max(status, "warning", key=STATUS_RANK.get)
            messages.append(f"ON DELETE {ld} vs {rd}")
        if not incoming and r["details"].get("indexed") != other["details"].get("indexed") and None not in (
                r["details"].get("indexed"), other["details"].get("indexed")):
            status = max(status, "info", key=STATUS_RANK.get)
            messages.append("Supporting index on one side only")
        if status == "same" and _key(r["name"]) != _key(other["name"]):
            status = "equivalent"
        rows.append(_row(status, r, other, "; ".join(messages)))
    for k, r in right.items():
        if k not in used:
            rows.append(_row("info" if incoming else "warning", None, r,
                             f"{'Incoming foreign key' if incoming else 'Foreign key'} only in {names[1]}"))
    return rows


def _trigger_shape(t) -> dict[str, Any]:
    e = t.get("extra") or {}
    timing = (e.get("timing") or "").upper()
    when = "INSTEAD OF" if "INSTEAD" in timing else "BEFORE" if "BEFORE" in timing else "AFTER" if "AFTER" in timing else timing
    level = (e.get("level") or ("ROW" if "EACH ROW" in timing else "STATEMENT" if timing else "")).upper()
    events = frozenset(x.strip() for x in re.split(r"\s+OR\s+", (e.get("events") or "").upper()) if x.strip())
    return {"when": when, "level": level, "events": events, "enabled": e.get("enabled", True)}


def compare_triggers(lt, rt, names) -> list[dict[str, Any]]:
    rows = []
    right = {_key(t["name"]): t for t in rt.get("triggers", [])}
    used = set()
    for t in lt.get("triggers", []):
        other = right.get(_key(t["name"]))
        if other is None:  # renamed? match a single trigger with the same timing and events
            shape = _trigger_shape(t)
            candidates = [o for k, o in right.items() if k not in used and
                          (_trigger_shape(o)["when"], _trigger_shape(o)["events"]) == (shape["when"], shape["events"])]
            other = candidates[0] if len(candidates) == 1 else None
        if other is None:
            rows.append(_row("error", t, None, f"No such trigger in {names[1]}: its logic is missing unless it moved "
                                               "elsewhere (e.g. an identity column or default)"))
            continue
        used.add(_key(other["name"]))
        a, b = _trigger_shape(t), _trigger_shape(other)
        messages, status = [], "same" if _key(t["name"]) == _key(other["name"]) else "equivalent"
        if a["when"] != b["when"]:
            status = "error"
            messages.append(f"Fires {a['when']} vs {b['when']}")
        if a["events"] != b["events"]:
            status = "error"
            messages.append(f"Events {' OR '.join(sorted(a['events']))} vs {' OR '.join(sorted(b['events']))}")
        if a["level"] and b["level"] and a["level"] != b["level"]:
            status = "error"
            messages.append(f"FOR EACH {a['level']} vs {b['level']}")
        if bool(a["enabled"]) != bool(b["enabled"]):
            status = max(status, "warning", key=STATUS_RANK.get)
            messages.append("Disabled in " + (names[0] if not a["enabled"] else names[1]))
        if status == "equivalent":
            messages.append("Different name")
        rows.append(_row(status, t, other, "; ".join(messages)))
    for k, t in right.items():
        if k not in used:
            rows.append(_row("warning", None, t, f"Trigger only in {names[1]}"))
    return rows


def _code_entries(used_by: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per routine that uses the table; Oracle packages expand to their members."""
    out = []
    for u in used_by:
        c = u["code"]
        if c["kind"] == "trigger" or (c.get("extra") or {}).get("is_trigger_function"):
            continue  # compared in the triggers section
        base = {"code_id": c["id"], "kind": c["kind"], "operations": u["operations"], "found_by": u["found_by"],
                "schema": c["schema_name"], "signature": c.get("signature"), "lines": u["lines"]}
        if c["kind"] == "package" and u["subprograms"]:
            member_ops = u.get("member_operations") or {}
            for member in u["subprograms"]:
                # Snapshots taken before per-member operations were recorded only know the package's.
                ops, scope = (member_ops[member], "member") if member in member_ops else (u["operations"], "package")
                out.append({**base, "name": f"{c['name']}.{member}", "package": c["name"], "member": member,
                            "operations": ops, "operations_scope": scope})
        else:
            out.append({**base, "name": c["name"], "package": None, "member": None})
    return out


def _code_keys(e: dict[str, Any]) -> list[tuple[str, str]]:
    """Ways the same routine can be named after a migration (Ora2Pg puts a package's
    members in a schema named after the package)."""
    if e["package"]:
        pkg, member = e["package"].lower(), e["member"].lower()
        return [(pkg, member), (e["schema"].lower(), f"{pkg}_{member}"), (e["schema"].lower(), f"{pkg}.{member}")]
    return [(e["schema"].lower(), e["name"].lower())]


def match_code(lt, rt) -> tuple[list[tuple[dict[str, Any], dict[str, Any] | None]], list[dict[str, Any]]]:
    """Pair the routines using one table with those using the other: (left entry, its
    match or None) for every left routine, and the right routines left unmatched."""
    left, right = _code_entries(lt.get("used_by", [])), _code_entries(rt.get("used_by", []))
    right_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in right:
        for k in _code_keys(e):
            right_index.setdefault(k, []).append(e)
    pairs, used = [], set()
    for e in left:
        match = next((right_index[k] for k in _code_keys(e) if k in right_index), None)
        other = None
        if match:
            other = next((m for m in match if id(m) not in used), match[0])
            used.add(id(other))
        pairs.append((e, other))
    return pairs, [e for e in right if id(e) not in used]


def compare_code(lt, rt, names, right_code_names: set[str]) -> list[dict[str, Any]]:
    pairs, unmatched = match_code(lt, rt)
    rows = []
    for e, other in pairs:
        if other is None:
            exists = any(k[1] in right_code_names or f"{k[0]}.{k[1]}" in right_code_names for k in _code_keys(e))
            rows.append(_row(
                "error" if exists else "warning", e, None,
                f"Exists in {names[1]} but doesn't use this table (logic changed, or the table is named dynamically)"
                if exists else f"Not found in {names[1]} code that uses this table (not migrated, or renamed)",
            ))
            continue
        ops_l, ops_r = set(e["operations"]), set(other["operations"])
        status, message = "same", ""
        if e.get("operations_scope") == "package":
            ops_l = set()  # only the whole package's operations are known: don't compare them per member
            message = "Refresh the catalog to compare operations per package member"
        if ops_l and ops_r and ops_l != ops_r:
            lost = (ops_l & WRITES) - ops_r
            status = "error" if lost else "warning"
            message = (f"{names[0]}: {', '.join(sorted(o.upper() for o in ops_l))}; "
                       f"{names[1]}: {', '.join(sorted(o.upper() for o in ops_r))}")
            if lost:
                message += f" (no longer {', '.join(sorted(o.upper() for o in lost))})"
        rows.append(_row(status, e, other, message))
    rows += [_row("info", None, e, f"Only in {names[1]}") for e in unmatched]
    return rows


def compare_views(lt, rt, names) -> list[dict[str, Any]]:
    def short(label):
        return label.split(".", 1)[-1].lower()

    right = {short(r["other_label"]): r for r in rt.get("read_by_views", [])}
    rows, used = [], set()
    for r in lt.get("read_by_views", []):
        other = right.get(short(r["other_label"]))
        if other is None:
            rows.append(_row("warning", r, None, f"View missing in {names[1]} (or no longer reads this table)"))
        else:
            used.add(short(r["other_label"]))
            rows.append(_row("same", r, other))
    for k, r in right.items():
        if k not in used:
            rows.append(_row("info", None, r, f"Only in {names[1]}"))
    return rows


def compare_table_props(lt, rt, names) -> list[dict[str, Any]]:
    rows = []
    lk, rk = lt["kind"], rt["kind"]
    rows.append(_row("same" if lk == rk else "warning", lk, rk, "" if lk == rk else "Different kind of object", label="Kind"))
    if lt["partitions"] or rt["partitions"]:
        status = "same" if lt["partitions"] == rt["partitions"] else "info"
        rows.append(_row(status, lt["partitions"], rt["partitions"], "" if status == "same" else "Different number of partitions",
                         label="Partitions"))
    lr, rr = lt.get("row_estimate"), rt.get("row_estimate")
    if lr is not None and rr is not None:
        big = max(lr, rr)
        off = abs(lr - rr) / big if big else 0
        status = "same" if off == 0 else "info" if off < 0.02 else "warning"
        rows.append(_row(status, lr, rr, "" if status == "same" else
                         f"Row-count estimates differ by {off:.0%}: statistics can be stale, but a large gap may mean an incomplete data load",
                         label="Rows (estimate)"))
    else:
        rows.append(_row("info", lr, rr, "No statistics on one side", label="Rows (estimate)"))
    rows.append(_row("same" if len(lt["columns"]) == len(rt["columns"]) else "warning",
                     len(lt["columns"]), len(rt["columns"]), "", label="Columns"))
    if (lt.get("comment") or "") != (rt.get("comment") or ""):
        rows.append(_row("info", lt.get("comment"), rt.get("comment"), "Comment differs", label="Comment"))
    return rows


def compare(lt: dict[str, Any], rt: dict[str, Any], *, left_dialect: str, right_dialect: str,
            names: tuple[str, str], right_code_names: set[str]) -> dict[str, Any]:
    sections = {
        "table": compare_table_props(lt, rt, names),
        "columns": compare_columns(lt, rt, left_dialect, right_dialect, names),
        "keys": compare_keys(lt, rt, names),
        "indexes": compare_indexes(lt, rt, names),
        "foreign_keys": compare_foreign_keys(lt["references"], rt["references"], names, incoming=False),
        "referenced_by": compare_foreign_keys(lt["referenced_by"], rt["referenced_by"], names, incoming=True),
        "triggers": compare_triggers(lt, rt, names),
        "code": compare_code(lt, rt, names, right_code_names),
        "views": compare_views(lt, rt, names),
    }
    # Problems first, except where the order itself means something (table properties, column order).
    for key, rows in sections.items():
        if key not in ("table", "columns"):
            rows.sort(key=lambda r: -STATUS_RANK[r["status"]])
    counts = {s: 0 for s in STATUS_RANK}
    per_section = {}
    for key, rows in sections.items():
        c = {s: 0 for s in STATUS_RANK}
        for r in rows:
            c[r["status"]] += 1
            counts[r["status"]] += 1
        per_section[key] = c
    return {"sections": sections, "counts": counts, "section_counts": per_section}


# --- every table at once ----------------------------------------------------------------

SECTION_LABELS = {
    "table": "table", "columns": "columns", "keys": "keys", "indexes": "indexes", "foreign_keys": "foreign keys",
    "referenced_by": "incoming foreign keys", "triggers": "triggers", "code": "routines", "views": "views",
}


def pair_tables(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[tuple[dict | None, dict | None]]:
    """Match tables like the page does: same schema.name ignoring case, else the only
    table with that name. Tables left over on either side are paired with None."""
    right_exact = {(t["schema_name"].lower(), t["name"].lower()): t for t in right}
    right_by_name: dict[str, list[dict]] = {}
    for t in right:
        right_by_name.setdefault(t["name"].lower(), []).append(t)
    pairs, used = [], set()
    pending = []
    for t in left:
        other = right_exact.get((t["schema_name"].lower(), t["name"].lower()))
        if other is not None:
            used.add(other["id"])
            pairs.append((t, other))
        else:
            pending.append(t)
    for t in pending:  # name-only fallback, once exact matches have claimed their tables
        candidates = [o for o in right_by_name.get(t["name"].lower(), []) if o["id"] not in used]
        if len(candidates) == 1:
            used.add(candidates[0]["id"])
            pairs.append((t, candidates[0]))
        else:
            pairs.append((t, None))
    pairs += [(None, t) for t in right if t["id"] not in used]
    return pairs


def _brief(t: dict[str, Any] | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {"id": t["id"], "key": f"{t['schema_name']}.{t['name']}", "kind": t["kind"], "row_estimate": t["row_estimate"]}


def overview(left: dict[Any, dict[str, Any]], right: dict[Any, dict[str, Any]], *, left_dialect: str,
             right_dialect: str, names: tuple[str, str], right_code_names: set[str]) -> dict[str, Any]:
    rows = []
    for lt, rt in pair_tables(list(left.values()), list(right.values())):
        if lt is None or rt is None:
            present = names[0] if lt else names[1]
            rows.append({
                "left": _brief(lt), "right": _brief(rt), "status": "error", "missing": names[1] if lt else names[0],
                "counts": {"error": 1, "warning": 0, "info": 0}, "sections": {},
                "headline": f"Only in {present}",
            })
            continue
        result = compare(lt, rt, left_dialect=left_dialect, right_dialect=right_dialect, names=names,
                         right_code_names=right_code_names)
        c = result["counts"]
        sections = {
            key: {s: n for s, n in counts.items() if s in ("error", "warning") and n}
            for key, counts in result["section_counts"].items()
            if counts["error"] or counts["warning"]
        }
        worst = next((r for key in SECTION_LABELS for r in result["sections"][key] if r["status"] == "error"), None) \
            or next((r for key in SECTION_LABELS for r in result["sections"][key] if r["status"] == "warning"), None)
        worst_section = next((k for k in SECTION_LABELS if worst is not None and worst in result["sections"][k]), None)
        status = "error" if c["error"] else "warning" if c["warning"] else "info" if c["info"] else "same"
        rows.append({
            "left": _brief(lt), "right": _brief(rt), "status": status, "missing": None,
            "counts": {"error": c["error"], "warning": c["warning"], "info": c["info"]},
            "sections": {k: v for k, v in sections.items()},
            "headline": f"{SECTION_LABELS[worst_section]}: {worst['message']}" if worst else "",
        })
    rank = {"error": 0, "warning": 1, "info": 2, "same": 3}
    rows.sort(key=lambda r: (rank[r["status"]], -r["counts"]["error"], -r["counts"]["warning"],
                             (r["left"] or r["right"])["key"].lower()))
    summary = {
        "pairs": len(rows),
        "matched": sum(1 for r in rows if r["left"] and r["right"]),
        "with_problems": sum(1 for r in rows if r["left"] and r["right"] and r["status"] == "error"),
        "with_differences": sum(1 for r in rows if r["status"] == "warning"),
        "clean": sum(1 for r in rows if r["status"] in ("same", "info")),
        "only_left": sum(1 for r in rows if r["left"] and not r["right"]),
        "only_right": sum(1 for r in rows if r["right"] and not r["left"]),
    }
    return {"names": names, "summary": summary, "tables": rows}
