"""Sequence statistics: every schema's sequences on Oracle and/or PostgreSQL, which tables
they fill, sequences that serve another schema, and a table-by-table comparison of the
Oracle and PostgreSQL sequence values with the migrated data.

Read-only on both databases. Besides the dictionaries, the only data read is MAX() of the
key columns the sequences fill.
"""

import math
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from app.sequences.check import evaluate as evaluate_pg
from app.sequences.check import read_postgres
from app.sequences.parse import find_oracle_uses
from app.services import oracle
from app.services.endpoints import Endpoint

Progress = Callable[[float, str], None]
CODE_TYPES = ("TRIGGER", "PROCEDURE", "FUNCTION", "PACKAGE BODY")
MAX_BATCH = 25


@dataclass
class Link:
    schema: str
    table: str
    column: str
    via: str  # identity | trigger | insert | default | serial
    source: str | None = None
    max_value: int | None = None
    error: str | None = None


@dataclass
class OracleSequence:
    schema: str
    name: str
    last_number: int
    increment: int
    min_value: int
    max_value: int
    cache: int
    cycle: bool
    links: list[Link] = field(default_factory=list)
    used_by: list[str] = field(default_factory=list)  # code objects that reference it (dependency views)


def _in(prefix: str, values: Sequence[str]) -> tuple[str, dict[str, str]]:
    binds = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return "(" + ", ".join(f":{k}" for k in binds) + ")", binds


def _query(conn, sql: str, binds: dict | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.arraysize = 5000
        cur.prefetchrows = 5001
        cur.execute(sql, binds or {})
        return cur.fetchall()


def read_oracle(endpoint: Endpoint, schemas: Sequence[str], *, user_grants: bool, progress: Progress,
                max_seconds: int = 60) -> tuple[list[OracleSequence], list[str]]:
    p = "ALL" if user_grants else "DBA"
    warnings: list[str] = []
    with oracle.connect(endpoint) as conn:
        if not schemas:
            schemas = [r[0] for r in _query(conn, f"SELECT DISTINCT sequence_owner FROM {p}_SEQUENCES s JOIN {p}_USERS u "
                                                  f"ON u.username = s.sequence_owner WHERE u.oracle_maintained = 'N'")]
        owners, binds = _in("o", list(schemas))
        progress(0.05, f"Oracle: reading sequences of {len(schemas)} schema(s)")
        seqs = {
            (o, n): OracleSequence(o, n, int(last), int(inc), int(mn), int(mx), int(cache or 0), cyc == "Y")
            for o, n, last, inc, mn, mx, cache, cyc in _query(conn,
                f"SELECT sequence_owner, sequence_name, last_number, increment_by, min_value, max_value, cache_size, "
                f"cycle_flag FROM {p}_SEQUENCES WHERE sequence_owner IN {owners}", binds)
        }
        by_name = defaultdict(list)
        for key in seqs:
            by_name[key[1]].append(key)

        def link(seq_key, schema, table, column, via, source=None):
            seq = seqs.get(seq_key)
            if seq and not any((x.schema, x.table, x.column) == (schema, table, column) for x in seq.links):
                seq.links.append(Link(schema, table, column, via, source))

        progress(0.1, "Oracle: identity columns")
        try:
            for owner, table, column, seq_name in _query(conn,
                    f"SELECT owner, table_name, column_name, sequence_name FROM {p}_TAB_IDENTITY_COLS WHERE owner IN {owners}", binds):
                link((owner, seq_name), owner, table, column, "identity")
        except Exception as exc:  # pre-12c dictionaries have no identity columns
            warnings.append(f"Identity columns not read: {exc}"[:200])

        progress(0.12, "Oracle: column defaults that call NEXTVAL")
        try:  # 12c+: DEFAULT seq.NEXTVAL. DATA_DEFAULT is a LONG, so filter on DEFAULT_LENGTH.
            for owner, table, column, default in _query(conn,
                    f"SELECT owner, table_name, column_name, data_default FROM {p}_TAB_COLUMNS "
                    f"WHERE owner IN {owners} AND default_length > 0", binds):
                seq = default_sequence(default, owner)
                if seq and seq in seqs:
                    link(seq, owner, table, column, "default")
        except Exception as exc:
            warnings.append(f"Column defaults not read: {exc}"[:200])

        progress(0.15, "Oracle: code that uses sequences")
        users: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)  # code -> sequences it references
        for owner, name, typ, ref_owner, ref_name in _query(conn,
                f"SELECT owner, name, type, referenced_owner, referenced_name FROM {p}_DEPENDENCIES "
                f"WHERE referenced_type = 'SEQUENCE' AND type <> 'SYNONYM' "  # a synonym is a name, not a use
                f"AND (referenced_owner IN {owners} OR owner IN {owners})", binds):
            if (ref_owner, ref_name) in seqs:
                seqs[(ref_owner, ref_name)].used_by.append(f"{owner}.{name} ({typ.lower()})")
                users[(owner, typ, name)].append((ref_owner, ref_name))

        progress(0.25, f"Oracle: reading {len(users)} code object(s) to find the columns sequences fill")
        triggers = {(o, n): (to, tn) for o, n, to, tn in _query(conn,
            f"SELECT owner, trigger_name, table_owner, table_name FROM {p}_TRIGGERS WHERE owner IN {owners}", binds)}
        code_owners = sorted({o for o, _, _ in users})
        if code_owners:
            o_clause, o_binds = _in("c", code_owners)
            t_clause, t_binds = _in("t", CODE_TYPES)
            chunks: dict[tuple[str, str, str], list[str]] = defaultdict(list)
            for owner, typ, name, text in _query(conn,
                    f"SELECT owner, type, name, text FROM {p}_SOURCE WHERE owner IN {o_clause} AND type IN {t_clause} "
                    f"ORDER BY owner, type, name, line", {**o_binds, **t_binds}):
                if (owner, typ, name) in users:
                    chunks[(owner, typ, name)].append(text or "")
            tables = {(o, t) for o, t in _query(conn, f"SELECT owner, table_name FROM {p}_TABLES WHERE owner IN {owners}", binds)}

            for (owner, typ, name), parts in chunks.items():
                def resolve_seq(parts_: tuple[str, ...]):
                    if len(parts_) >= 2:
                        return (parts_[-2], parts_[-1])
                    if (owner, parts_[0]) in seqs:
                        return (owner, parts_[0])
                    candidates = by_name.get(parts_[0], [])
                    return candidates[0] if len(candidates) == 1 else None

                for use in find_oracle_uses("".join(parts)):
                    seq_key = resolve_seq(use.sequence)
                    if seq_key is None:
                        continue
                    if use.table is None and typ == "TRIGGER" and (owner, name) in triggers:
                        t_owner, t_name = triggers[(owner, name)]
                        link(seq_key, t_owner, t_name, use.column, "trigger", f"{owner}.{name}")
                    elif use.table is not None:
                        t = use.table
                        target = (t[-2], t[-1]) if len(t) >= 2 else (owner, t[0])
                        if len(t) < 2 and target not in tables:
                            target = next(((o, n) for o, n in tables if n == t[0]), target)
                        link(seq_key, target[0], target[1], use.column, "insert", f"{owner}.{name}")

        links = [ln for s in seqs.values() for ln in s.links]
        progress(0.35, f"Oracle: MAX() of {len(links)} column(s) filled by sequences")
        conn.call_timeout = max_seconds * 1000
        _oracle_maxima(conn, links)
    return list(seqs.values()), warnings


_DEFAULT_SEQ = re.compile(r"(?:([A-Z0-9_$#]+)\.)?([A-Z0-9_$#]+)\.NEXTVAL\b")


def default_sequence(default: str | None, owner: str) -> tuple[str, str] | None:
    """The sequence a column DEFAULT calls: Oracle stores it as "OWNER"."SEQ"."NEXTVAL"."""
    m = _DEFAULT_SEQ.search((default or "").replace('"', "").upper())
    return (m.group(1) or owner, m.group(2)) if m else None


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _oracle_maxima(conn, links: list[Link]) -> None:
    targets: dict[tuple[str, str, str], list[Link]] = defaultdict(list)
    for ln in links:
        targets[(ln.schema, ln.table, ln.column)].append(ln)
    keys = list(targets)

    def store(key, value=None, error=None):
        for ln in targets[key]:
            ln.max_value, ln.error = (math.ceil(value) if value is not None else None), error

    def sql(chunk):
        return "SELECT " + ", ".join(f"(SELECT MAX({_q(c)}) FROM {_q(s)}.{_q(t)})" for s, t, c in chunk) + " FROM DUAL"

    for i in range(0, len(keys), MAX_BATCH):
        chunk = keys[i:i + MAX_BATCH]
        try:
            row = _query(conn, sql(chunk))[0]
            for key, value in zip(chunk, row, strict=True):
                store(key, value)
        except Exception:
            for key in chunk:
                try:
                    store(key, _query(conn, sql([key]))[0][0])
                except Exception as exc:
                    store(key, error=str(exc).strip().splitlines()[0][:200])


# --- combining both sides ---------------------------------------------------------------------------


def _schema_stats(seqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in seqs:
        row = out.setdefault(s["schema"], {"schema": s["schema"], "sequences": 0, "own": 0, "other_schema": 0, "unlinked": 0,
                                           "code_only": 0})
        row["sequences"] += 1
        row[s["ownership"]] += 1
    return sorted(out.values(), key=lambda r: (-r["sequences"], r["schema"].lower()))


def _ownership(schema: str, table_schemas: set[str], code_schemas: set[str]) -> tuple[str, list[str]]:
    """own: fills tables of its own schema; other_schema: fills (or is used from) another
    schema; code_only: used by code of its schema but no column found; unlinked: unused."""
    me = schema.lower()
    others = sorted({s for s in table_schemas if s.lower() != me})
    if others:
        return "other_schema", others
    if table_schemas:
        return "own", []
    other_code = sorted({s for s in code_schemas if s.lower() != me})
    if other_code:
        return "other_schema", other_code
    return ("code_only", []) if code_schemas else ("unlinked", [])


def summarize_oracle(seqs: list[OracleSequence]) -> list[dict[str, Any]]:
    out = []
    for s in seqs:
        code_schemas = {u.split(".", 1)[0] for u in s.used_by}
        ownership, others = _ownership(s.schema, {ln.schema for ln in s.links}, code_schemas)
        out.append({**asdict(s), "ownership": ownership, "other_schemas": others})
    return sorted(out, key=lambda r: (r["schema"].lower(), r["name"].lower()))


def summarize_pg(states) -> list[dict[str, Any]]:
    out = []
    for st in states:
        d = st.as_dict()
        links = d.pop("columns")
        code_schemas = {c["source"].split(".", 1)[0] for c in links if c.get("source")}
        ownership, others = _ownership(st.schema, {c["schema"] for c in links}, code_schemas)
        out.append({**d, "links": links, "ownership": ownership, "other_schemas": others})
    return sorted(out, key=lambda r: (r["schema"].lower(), r["name"].lower()))


def compare_tables(ora: list[dict[str, Any]], pg: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per table column filled by a sequence on either side."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}

    def row(link):
        key = (link["schema"].lower(), link["table"].lower(), link["column"].lower())
        return rows.setdefault(key, {"schema": link["schema"].lower(), "table": link["table"].lower(),
                                     "column": link["column"].lower(), "oracle": None, "postgres": None})

    for s in ora:
        for ln in s["links"]:
            row(ln)["oracle"] = {"sequence": f"{s['schema']}.{s['name']}", "last_number": s["last_number"],
                                 "max": ln["max_value"], "max_error": ln["error"], "via": ln["via"], "source": ln["source"],
                                 "table": f"{ln['schema']}.{ln['table']}"}
    for s in pg:
        for ln in s["links"]:
            row(ln)["postgres"] = {"sequence": f"{s['schema']}.{s['name']}", "next_value": s["next_value"],
                                   "max": ln["max_value"], "max_error": ln["error"], "via": ln["via"], "source": ln["source"],
                                   "table": f"{ln['schema']}.{ln['table']}", "column_name": ln["column"],
                                   "status": s["status"], "increment": s["increment"],
                                   "seq_schema": s["schema"], "seq_name": s["name"], "seq_max": s["max_value"]}

    # Columns only PostgreSQL fills from a sequence: in Oracle the application usually takes
    # the value from the sequence of the same name itself, so compare with that one.
    ora_by_name = {(s["schema"].lower(), s["name"].lower()): s for s in ora}
    for r in rows.values():
        p = r["postgres"]
        if p and not r["oracle"]:
            same = ora_by_name.get((p["seq_schema"].lower(), p["seq_name"].lower()))
            if same:
                r["oracle"] = {"sequence": f"{same['schema']}.{same['name']}", "last_number": same["last_number"], "max": None,
                               "max_error": None, "via": "same name", "source": None, "table": None, "by_name": True}

    for r in rows.values():
        o, p = r["oracle"], r["postgres"]
        issues: list[tuple[str, str]] = []
        if o and o.get("by_name"):
            issues.append(("info", f"No Oracle code fills this column from a sequence (the application probably calls "
                                   f"{o['sequence']}.NEXTVAL itself); compared with that sequence by name."))
        if o and not p:
            issues.append(("error", "No sequence fills this column in PostgreSQL: inserts that relied on "
                                    f"{o['sequence']} in Oracle must now supply the value."))
        elif p and not o:
            issues.append(("info", "Filled by a sequence only in PostgreSQL, and Oracle has no sequence of that name."))
        if p:
            if p["next_value"] is not None and p["max"] is not None and p["next_value"] <= p["max"]:
                issues.append(("error", f"PostgreSQL sequence is behind the data: next value {p['next_value']:,} ≤ "
                                        f"max {p['max']:,}. The next insert hits a duplicate key."))
            if o and p["next_value"] is not None and p["next_value"] < o["last_number"]:
                issues.append(("warning", f"PostgreSQL next value {p['next_value']:,} is below Oracle's {o['last_number']:,}: "
                                          "values Oracle already handed out can be issued again."))
        if o and p and o["max"] is not None and p["max"] is not None and p["max"] < o["max"]:
            issues.append(("warning", f"PostgreSQL max {p['max']:,} is below Oracle's {o['max']:,}: rows may not all be migrated."))
        for side, d in (("Oracle", o), ("PostgreSQL", p)):
            if d and d["max_error"]:
                issues.append(("info", f"{side} MAX() could not be read: {d['max_error']}"))
        if (o and p and o["via"] != "identity" and not o["sequence"].split(".")[-1].startswith("ISEQ$$")
                and o["sequence"].split(".")[-1].lower() != p["sequence"].split(".")[-1].lower()):
            # (Oracle names identity sequences itself, ISEQ$$_nnn: a different name is expected there.)
            issues.append(("info", f"Different sequence names: {o['sequence']} in Oracle, {p['sequence']} in PostgreSQL."))
        rank = {"error": 3, "warning": 2, "info": 1}
        r["issues"] = [{"severity": s, "message": m} for s, m in issues]
        r["status"] = max((s for s, _ in issues), key=rank.get, default="ok")
    order = {"error": 0, "warning": 1, "info": 2, "ok": 3}
    return sorted(rows.values(), key=lambda r: (order[r["status"]], r["schema"], r["table"], r["column"]))


def unlinked_by_name(ora: list[dict[str, Any]], pg: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sequences no column was found for, compared by schema and name."""
    pg_by = {(s["schema"].lower(), s["name"].lower()): s for s in pg}
    out = []
    for s in ora:
        if s["links"]:
            continue
        other = pg_by.get((s["schema"].lower(), s["name"].lower()))
        status, message = "info", ""
        if other is None:
            status, message = "warning", "No PostgreSQL sequence of that name."
        elif other["next_value"] is not None and other["next_value"] < s["last_number"]:
            status, message = "warning", (f"PostgreSQL next value {other['next_value']:,} is below Oracle's {s['last_number']:,}.")
        else:
            status, message = "ok", ""
        out.append({"sequence": f"{s['schema']}.{s['name']}", "schema": s["schema"], "last_number": s["last_number"],
                    "used_by": s["used_by"][:5], "postgres": f"{other['schema']}.{other['name']}" if other else None,
                    "next_value": other["next_value"] if other else None, "status": status, "message": message})
    return sorted(out, key=lambda r: ({"warning": 0, "info": 1, "ok": 2}[r["status"]], r["sequence"].lower()))


def run(oracle_endpoint: Endpoint | None, oracle_schemas: list[str], user_grants: bool,
        pg_endpoint: Endpoint | None, pg_schemas: list[str], progress: Progress) -> dict[str, Any]:
    warnings: list[str] = []
    ora_rows: list[dict[str, Any]] = []
    pg_rows: list[dict[str, Any]] = []
    if oracle_endpoint is not None:
        seqs, w = read_oracle(oracle_endpoint, oracle_schemas, user_grants=user_grants,
                              progress=lambda f, m: progress(f * (0.5 if pg_endpoint else 0.95), m))
        warnings += w
        ora_rows = summarize_oracle(seqs)
    if pg_endpoint is not None:
        states, read_schemas, w = read_postgres(pg_endpoint, pg_schemas, lambda f, m: progress(0.5 + f * 0.45, f"PostgreSQL: {m}"))
        warnings += w
        oracle_for_pg = [{"owner": s["schema"], "name": s["name"], "last_number": s["last_number"], "increment": s["increment"]}
                         for s in ora_rows] if oracle_endpoint is not None else None
        evaluate_pg(states, oracle_for_pg)
        pg_rows = summarize_pg(states)
    progress(0.97, "Comparing")
    both = oracle_endpoint is not None and pg_endpoint is not None
    tables = compare_tables(ora_rows, pg_rows) if both else []
    unlinked = unlinked_by_name(ora_rows, pg_rows) if both else []

    def cross(rows, side):
        return [{"side": side, "sequence": f"{r['schema']}.{r['name']}", "schema": r["schema"], "other_schemas": r["other_schemas"],
                 "tables": sorted({f"{ln['schema']}.{ln['table']}.{ln['column']}" for ln in r["links"]}),
                 "used_by": r.get("used_by", [])[:8]}
                for r in rows if r["ownership"] == "other_schema"]

    counts = {s: sum(1 for t in tables if t["status"] == s) for s in ("error", "warning", "info", "ok")}
    return {
        "oracle": {"sequences": ora_rows, "schemas": _schema_stats(ora_rows)} if oracle_endpoint is not None else None,
        "postgres": {"sequences": pg_rows, "schemas": _schema_stats(pg_rows)} if pg_endpoint is not None else None,
        "cross_schema": cross(ora_rows, "oracle") + cross(pg_rows, "postgres"),
        "tables": tables,
        "unlinked": unlinked,
        "summary": {
            "oracle_sequences": len(ora_rows), "postgres_sequences": len(pg_rows),
            "cross_schema": sum(1 for r in ora_rows + pg_rows if r["ownership"] == "other_schema"),
            "tables_compared": len(tables), "table_status": counts,
        },
        "warnings": warnings,
    }
