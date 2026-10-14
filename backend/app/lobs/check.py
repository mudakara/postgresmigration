"""LOB data check: every table with BLOB/CLOB-style columns in Oracle, paired with its
PostgreSQL table, and counted on both sides to find LOB data that did not migrate.

Per table: COUNT(*). Per LOB column: how many values are not NULL, how many are not empty,
and (optionally) the total and largest length (bytes for binary, characters for text).
Rows missing, values that became NULL and values that got shorter (Ora2Pg truncates to
LONGREADLEN when LONGTRUNCOK is on) all show up as differences.

Read-only on both databases. The LOB contents never leave the database: only counts and
lengths are read.
"""

import threading
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from app.services import oracle, postgres
from app.services.endpoints import Endpoint

Progress = Callable[[float, str], None]

# Oracle column types holding large objects, and what each counts in.
LOB_TYPES = {"BLOB": "bytes", "CLOB": "chars", "NCLOB": "chars", "BFILE": None, "LONG": None, "LONG RAW": None, "XMLTYPE": None}
SIZED = {"BLOB", "CLOB", "NCLOB"}  # DBMS_LOB.GETLENGTH works on these without reading the data
# PostgreSQL types a migrated LOB can be in, per Oracle type (Ora2Pg defaults first).
EXPECTED_PG = {
    "BLOB": ("bytea", "oid", "lo"), "LONG RAW": ("bytea",), "BFILE": ("bytea", "efile", "text"),
    "CLOB": ("text", "character varying", "xml", "json", "jsonb"), "NCLOB": ("text", "character varying"),
    "LONG": ("text", "character varying"), "XMLTYPE": ("xml", "text"),
}
PG_BINARY = ("bytea", "oid", "lo")
WORKERS_PER_SIDE = 2


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _in(prefix: str, values: Sequence[str]) -> tuple[str, dict[str, str]]:
    binds = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return "(" + ", ".join(f":{k}" for k in binds) + ")", binds


def _first_line(exc: Exception) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]


# --- Oracle ---------------------------------------------------------------------------------------


def read_oracle_lob_tables(conn, schemas: Sequence[str], user_grants: bool) -> list[dict[str, Any]]:
    """Tables (not views, materialized views, temporary, nested or external tables) with LOB columns."""
    p = "ALL" if user_grants else "DBA"
    owners, binds = _in("o", list(schemas))
    types, t_binds = _in("t", list(LOB_TYPES))
    with conn.cursor() as cur:
        cur.arraysize = 5000
        cur.execute(
            f"""
            SELECT c.owner, c.table_name, c.column_name, c.data_type, t.num_rows
            FROM {p}_TAB_COLUMNS c
            JOIN {p}_TABLES t ON t.owner = c.owner AND t.table_name = c.table_name
            WHERE c.owner IN {owners} AND c.data_type IN {types}
              AND t.temporary = 'N' AND t.nested = 'NO' AND t.secondary = 'N' AND t.dropped = 'NO'
              AND (t.iot_type IS NULL OR t.iot_type = 'IOT')
              AND NOT EXISTS (SELECT 1 FROM {p}_MVIEWS m WHERE m.owner = t.owner AND m.mview_name = t.table_name)
              AND NOT EXISTS (SELECT 1 FROM {p}_EXTERNAL_TABLES x WHERE x.owner = t.owner AND x.table_name = t.table_name)
            ORDER BY c.owner, c.table_name, c.column_id
            """,
            {**binds, **t_binds},
        )
        rows = cur.fetchall()
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    for owner, table, column, data_type, num_rows in rows:
        t = tables.setdefault((owner, table), {"schema": owner, "table": table, "num_rows": num_rows, "columns": []})
        t["columns"].append({"column": column, "type": data_type})
    return list(tables.values())


def read_oracle_table_names(conn, schemas: Sequence[str], user_grants: bool) -> set[tuple[str, str]]:
    p = "ALL" if user_grants else "DBA"
    owners, binds = _in("o", list(schemas))
    with conn.cursor() as cur:
        cur.arraysize = 5000
        cur.execute(f"SELECT owner, table_name FROM {p}_TABLES WHERE owner IN {owners}", binds)
        return {(o, t) for o, t in cur.fetchall()}


def oracle_count_sql(schema: str, table: str, columns: list[dict[str, Any]], sizes: bool) -> str:
    """One scan for the aggregates; a LONG can only be tested for NULL in a WHERE clause, so
    each LONG column is a scalar subquery around the aggregate (ORA-00937 inside it)."""
    target = f"{_q(schema)}.{_q(table)}"
    aggregates = ["COUNT(*)"]
    outer = ["a.c0"]

    def agg(expr: str) -> None:
        outer.append(f"a.c{len(aggregates)}")
        aggregates.append(expr)

    for c in columns:
        col = _q(c["column"])
        if c["type"] in ("LONG", "LONG RAW"):
            outer.append(f"(SELECT COUNT(*) FROM {target} WHERE {col} IS NOT NULL)")
        else:
            agg(f"COUNT(CASE WHEN {col} IS NOT NULL THEN 1 END)")
        if sizes and c["type"] in SIZED:
            length = f"DBMS_LOB.GETLENGTH({col})"
            for expr in (f"COUNT(CASE WHEN {length} > 0 THEN 1 END)", f"SUM({length})", f"MAX({length})"):
                agg(expr)
    if len(outer) == len(aggregates):  # no LONG columns: no need for the wrapper
        return f"SELECT {', '.join(aggregates)} FROM {target}"
    inner = ", ".join(f"{e} c{i}" for i, e in enumerate(aggregates))
    return f"SELECT {', '.join(outer)} FROM (SELECT {inner} FROM {target}) a"


def _unpack(row: Sequence[Any], columns: list[dict[str, Any]], sized: Callable[[dict[str, Any]], bool]) -> tuple[int, list[dict[str, Any]]]:
    values = list(row)
    total_rows = int(values.pop(0))
    out = []
    for c in columns:
        m = {"column": c["column"], "type": c["type"], "non_null": int(values.pop(0) or 0),
             "non_empty": None, "total": None, "max": None, "unit": c.get("unit")}
        if sized(c):
            ne, total, mx = values.pop(0), values.pop(0), values.pop(0)
            m.update(non_empty=int(ne or 0), total=int(total or 0), max=int(mx or 0))
        out.append(m)
    return total_rows, out


# --- PostgreSQL -----------------------------------------------------------------------------------


def read_pg_tables(conn, schemas: Sequence[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Every table's columns: (schema, table) -> {schema, table, rows_estimate, columns: {name: {type, base, category}}}."""
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    clause, params = ("", []) if not schemas else ("AND n.nspname = ANY(%s)", [list(schemas)])
    for schema, table, estimate, column, ctype, base, category in conn.execute(
        f"""
        SELECT n.nspname, c.relname, c.reltuples::bigint, a.attname, format_type(a.atttypid, a.atttypmod),
               t.typname, t.typcategory
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
          AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)' {clause}
        ORDER BY 1, 2, a.attnum
        """, params,
    ):
        t = tables.setdefault((schema, table), {"schema": schema, "table": table, "rows_estimate": estimate, "columns": {}})
        t["columns"][column] = {"type": ctype, "base": base, "category": category}
    return tables


def pg_unit(col: dict[str, Any]) -> str | None:
    if col["base"] == "bytea":
        return "bytes"
    if col["category"] == "S":  # text, varchar, char
        return "chars"
    return None  # oid (large objects), xml, json...: counted, not measured


def pg_count_sql(schema: str, table: str, columns: list[dict[str, Any]], sizes: bool) -> sql.Composed:
    parts = [sql.SQL("count(*)")]
    for c in columns:
        col = sql.Identifier(c["column"])
        parts.append(sql.SQL("count({})").format(col))
        if sizes and c.get("unit"):
            length = sql.SQL("octet_length({})" if c["unit"] == "bytes" else "length({})").format(col)
            parts += [sql.SQL("count(*) FILTER (WHERE {} > 0)").format(length),
                      sql.SQL("sum({})").format(length), sql.SQL("max({})").format(length)]
    return sql.SQL("SELECT {} FROM {}.{}").format(sql.SQL(", ").join(parts), sql.Identifier(schema), sql.Identifier(table))


# --- pairing --------------------------------------------------------------------------------------


def pair(oracle_tables: list[dict[str, Any]], pg_tables: dict[tuple[str, str], dict[str, Any]],
         oracle_names: set[tuple[str, str]]) -> tuple[dict, list]:
    """Oracle table -> PostgreSQL table (case-insensitive schema.table, else a unique table name),
    and the PostgreSQL tables with binary columns that don't exist in Oracle at all (tables that
    do, without LOBs, have RAW columns that became bytea)."""
    by_key = {(s.lower(), t.lower()): (s, t) for s, t in pg_tables}
    by_name = defaultdict(list)
    for s, t in pg_tables:
        by_name[t.lower()].append((s, t))
    pairs = {}
    for o in oracle_tables:
        key = by_key.get((o["schema"].lower(), o["table"].lower()))
        if key is None and len(by_name.get(o["table"].lower(), [])) == 1:
            key = by_name[o["table"].lower()][0]
        pairs[(o["schema"], o["table"])] = key
    used = set(pairs.values())
    in_oracle = {(s.lower(), t.lower()) for s, t in oracle_names} | {(o["schema"].lower(), o["table"].lower()) for o in oracle_tables}
    oracle_tables_by_name = {t for _, t in in_oracle}
    pg_only = [key for key, t in pg_tables.items() if key not in used
               and (key[0].lower(), key[1].lower()) not in in_oracle and key[1].lower() not in oracle_tables_by_name
               and any(c["base"] in PG_BINARY for c in t["columns"].values())]
    return pairs, pg_only


# --- measuring ------------------------------------------------------------------------------------


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _measure(tasks: list[dict[str, Any]], open_conn: Callable[[], Any], run_one: Callable[[Any, dict[str, Any], bool], None],
             done: list[int], lock: threading.Lock, workers: int, sizes: bool) -> None:
    """Run the tasks on `workers` connections. When measuring sizes fails (a time limit, a
    lost session), the table is counted again without sizes on a new connection; whatever
    still fails is recorded on the task, so one table never stops the rest."""

    def worker(chunk: list[dict[str, Any]]) -> None:
        conn = None
        for task in chunk:
            started = time.monotonic()
            for full in ([True, False] if sizes else [False]):
                try:
                    if conn is None:
                        conn = open_conn()
                    run_one(conn, task, full)
                    task["error"] = None
                    break
                except Exception as exc:
                    message = _first_line(exc)
                    if full:
                        task["sizes_error"] = message
                    task["error"] = message
                    _close(conn)  # it may be unusable after a timeout
                    conn = None
            task["seconds"] = round(time.monotonic() - started, 1)
            with lock:
                done[0] += 1
        if conn is not None:
            _close(conn)

    chunks = [tasks[i::workers] for i in range(workers)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, [c for c in chunks if c]))


def run(oracle_endpoint: Endpoint, oracle_schemas: list[str], user_grants: bool,
        pg_endpoint: Endpoint, pg_schemas: list[str], progress: Progress,
        *, sizes: bool = True, timeout_seconds: int = 300) -> dict[str, Any]:
    warnings: list[str] = []
    progress(0.02, "Oracle: finding tables with LOB columns")
    with oracle.connect(oracle_endpoint) as conn:
        ora_tables = read_oracle_lob_tables(conn, oracle_schemas, user_grants)
        oracle_names = read_oracle_table_names(conn, oracle_schemas, user_grants)
    progress(0.05, f"PostgreSQL: reading the tables of {len(pg_schemas) or 'all'} schema(s)")
    with postgres.connect(pg_endpoint, autocommit=True, row_factory=tuple_row) as conn:
        pg_tables = read_pg_tables(conn, pg_schemas)
    pairs, pg_only = pair(ora_tables, pg_tables, oracle_names)

    # What to count on the PostgreSQL side: the columns named like Oracle's LOB columns, and the
    # binary columns of tables that exist only in PostgreSQL.
    pg_tasks: list[dict[str, Any]] = []
    for o in ora_tables:
        o["postgres_table"] = None
        key = pairs[(o["schema"], o["table"])]
        if key is None:
            continue
        o["postgres_table"] = f"{key[0]}.{key[1]}"
        cols = pg_tables[key]["columns"]
        lower = {name.lower(): name for name in cols}
        wanted = [lower[c["column"].lower()] for c in o["columns"] if c["column"].lower() in lower]
        pg_tasks.append({"schema": key[0], "table": key[1], "rows_estimate": pg_tables[key]["rows_estimate"],
                         "columns": [{"column": n, "type": cols[n]["type"], "unit": pg_unit(cols[n])} for n in wanted]})
    for key in pg_only:
        cols = pg_tables[key]["columns"]
        pg_tasks.append({"schema": key[0], "table": key[1], "rows_estimate": pg_tables[key]["rows_estimate"], "only_postgres": True,
                         "columns": [{"column": n, "type": c["type"], "unit": pg_unit(c)} for n, c in cols.items() if c["base"] in PG_BINARY]})

    ora_tasks = [{"schema": o["schema"], "table": o["table"], "num_rows": o["num_rows"], "postgres_table": o["postgres_table"],
                  "columns": [{**c, "unit": LOB_TYPES[c["type"]]} for c in o["columns"]]} for o in ora_tables]

    def run_oracle(conn, task, full):
        with conn.cursor() as cur:
            cur.execute(oracle_count_sql(task["schema"], task["table"], task["columns"], full))
            row = cur.fetchone()
        task["rows"], task["columns"] = _unpack(row, task["columns"], lambda c: full and c["type"] in SIZED)

    def open_oracle():
        conn = oracle.connect(oracle_endpoint)
        conn.call_timeout = timeout_seconds * 1000
        return conn

    def run_pg(conn, task, full):
        row = conn.execute(pg_count_sql(task["schema"], task["table"], task["columns"], full)).fetchone()
        task["rows"], task["columns"] = _unpack(row, task["columns"], lambda c: full and bool(c.get("unit")))

    def open_pg():
        conn = postgres.connect(pg_endpoint, autocommit=True, row_factory=tuple_row)
        conn.execute(f"SET statement_timeout = '{int(timeout_seconds)}s'")
        return conn

    for task in ora_tasks + pg_tasks:
        task.update(rows=None, error=None, sizes_error=None, seconds=None)
    # Smallest first, so most tables are done early and a few huge ones run last.
    ora_tasks.sort(key=lambda t: t["num_rows"] or 0)
    pg_tasks.sort(key=lambda t: t["rows_estimate"] or 0)

    lock = threading.Lock()
    ora_done, pg_done = [0], [0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_measure, ora_tasks, open_oracle, run_oracle, ora_done, lock, WORKERS_PER_SIDE, sizes),
            pool.submit(_measure, pg_tasks, open_pg, run_pg, pg_done, lock, WORKERS_PER_SIDE, sizes),
        ]
        total = max(1, len(ora_tasks) + len(pg_tasks))
        while not all(f.done() for f in futures):
            time.sleep(2)
            progress(0.08 + 0.9 * (ora_done[0] + pg_done[0]) / total,
                     f"Counting: Oracle {ora_done[0]}/{len(ora_tasks)} tables · PostgreSQL {pg_done[0]}/{len(pg_tasks)} tables")
        for f in futures:
            f.result()

    for task in ora_tasks + pg_tasks:
        for key in ("error", "sizes_error"):
            task[key] = _friendly(task[key], timeout_seconds)
    timeouts = sum(1 for t in ora_tasks + pg_tasks if t["error"] and "time limit" in t["error"])
    if timeouts:
        warnings.append(f"{timeouts} table(s) took longer than the {timeout_seconds // 60} minute limit even without measuring "
                        "sizes and were not counted: run again with a longer limit.")
    partial = sum(1 for t in ora_tasks + pg_tasks if t["sizes_error"] and not t["error"])
    if partial:
        warnings.append(f"{partial} table(s) were counted without sizes because measuring them failed (see each table).")
    if not ora_tables:
        warnings.append("No Oracle tables with LOB columns (BLOB, CLOB, NCLOB, BFILE, LONG, LONG RAW, XMLTYPE) in the schema scope.")
    ora_tasks.sort(key=lambda t: (t["schema"], t["table"]))
    pg_tasks.sort(key=lambda t: (t["schema"], t["table"]))
    return {"oracle": ora_tasks, "postgres": pg_tasks, "sizes": sizes, "timeout_seconds": timeout_seconds, "warnings": warnings}


def _friendly(message: str | None, timeout_seconds: int) -> str | None:
    if not message:
        return message
    low = message.lower()
    if "lock timeout" in low:
        return "Locked by another session (DDL or maintenance in progress): check again later."
    if "statement timeout" in low or "dpy-4024" in low or "dpi-1067" in low or "call timeout" in low:
        return f"Took longer than the time limit ({timeout_seconds // 60} min). {message}"
    if "dpy-4011" in low or "closed the connection" in low:
        return f"Oracle closed the session while counting (a resource limit or network timeout on the server side?). {message}"
    return message


# --- comparing ------------------------------------------------------------------------------------


def _fmt(n: int) -> str:
    return f"{n:,}"


def _size(n: int, unit: str | None) -> str:
    if unit != "bytes":
        return f"{n:,} characters"
    for label, factor in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= factor:
            return f"{n / factor:,.1f} {label}"
    return f"{n:,} bytes"


def _issue(severity: str, message: str, kind: str | None = None) -> dict[str, str]:
    return {"severity": severity, "message": message, **({"kind": kind} if kind else {})}


# Ora2Pg's default LONGREADLEN: with LONGTRUNCOK, longer LOBs are cut to this many bytes or
# characters (bytea values end up one byte longer in the export).
LONGREADLEN = 1_047_552


def _longreadlen_issue(p: dict[str, Any]) -> dict[str, str] | None:
    if p.get("max") in (LONGREADLEN, LONGREADLEN + 1):
        return _issue("warning", f"Largest value is exactly {_size(p['max'], p.get('unit'))}, Ora2Pg's default LONGREADLEN: "
                                 "longer values were probably cut to this size.", "truncated")
    return None


RANK = {"error": 0, "warning": 1, "info": 2, "ok": 3}


def _worst(issues: list[dict[str, str]]) -> str:
    return min((i["severity"] for i in issues), key=RANK.__getitem__, default="ok")


def _type_issue(ora_type: str, pg_type: str) -> dict[str, str] | None:
    base = pg_type.split("(")[0].strip()
    if base in EXPECTED_PG.get(ora_type, ()):
        if "(" in pg_type and base == "character varying":
            return _issue("warning", f"{ora_type} became {pg_type}: values longer than the limit can't be stored.")
        return None
    return _issue("warning", f"Type changed: {ora_type} in Oracle, {pg_type} in PostgreSQL.")


def compare_column(o: dict[str, Any], p: dict[str, Any] | None, rows_missing: int) -> list[dict[str, str]]:
    if p is None:
        return [_issue("error", "Column not found in the PostgreSQL table: its data was not migrated.")]
    issues = []
    t = _type_issue(o["type"], p["type"])
    if t:
        issues.append(t)
    unit = o.get("unit") or p.get("unit")
    because = f" ({_fmt(rows_missing)} row(s) are missing)" if rows_missing > 0 else ""
    sized = o.get("non_empty") is not None and p.get("non_empty") is not None and o.get("unit") == p.get("unit")

    if sized:
        if p["non_empty"] < o["non_empty"]:
            issues.append(_issue("error", f"{_fmt(o['non_empty'] - p['non_empty'])} LOB value(s) missing in PostgreSQL: "
                                          f"{_fmt(p['non_empty'])} of {_fmt(o['non_empty'])} have data{because}."))
        elif p["non_null"] < o["non_null"]:
            fewer, empty = o["non_null"] - p["non_null"], o["non_null"] - o["non_empty"]
            issues.append(_issue("info", f"{_fmt(fewer)} empty LOB(s) in Oracle (EMPTY_BLOB/EMPTY_CLOB) are NULL in PostgreSQL; "
                                         "every value with data is there." if fewer <= empty
                                 else f"{_fmt(fewer)} fewer non-NULL values in PostgreSQL."))
        elif p["non_empty"] > o["non_empty"]:
            issues.append(_issue("warning", f"{_fmt(p['non_empty'] - o['non_empty'])} more value(s) with data in PostgreSQL than in Oracle."))
        if p["max"] < o["max"]:
            issues.append(_issue("error", f"Largest value is {_size(p['max'], unit)} in PostgreSQL but {_size(o['max'], unit)} in Oracle: "
                                          "values were truncated (Ora2Pg LONGREADLEN with LONGTRUNCOK) or the largest row is missing.",
                                 "truncated"))
        if p["non_empty"] == o["non_empty"] and p["total"] != o["total"]:
            diff = abs(p["total"] - o["total"])
            amount = f"{_fmt(diff)} {'bytes' if unit == 'bytes' else 'characters'}"
            if p["total"] < o["total"] and p["max"] < o["max"]:
                issues.append(_issue("error", f"Same number of values but {amount} less data in PostgreSQL ({_size(p['total'], unit)} "
                                              f"against {_size(o['total'], unit)}): values are cut short.", "truncated"))
            else:
                more_less = "less" if p["total"] < o["total"] else "more"
                why = "changed after the migration?" if unit == "bytes" else "line endings, character conversion or changes after the migration?"
                issues.append(_issue("warning", f"Same number of values but {amount} {more_less} data in PostgreSQL ({why})."))
    else:
        if p["non_null"] < o["non_null"]:
            hedge = (" Without sizes, empty LOBs (EMPTY_BLOB/EMPTY_CLOB) count as set in Oracle and may be NULL in PostgreSQL: "
                     "measure sizes to tell them apart.") if o["type"] in SIZED else ""
            issues.append(_issue("error", f"{_fmt(o['non_null'] - p['non_null'])} value(s) missing in PostgreSQL: "
                                          f"{_fmt(p['non_null'])} of {_fmt(o['non_null'])} are set{because}.{hedge}"))
        elif p["non_null"] > o["non_null"]:
            issues.append(_issue("warning", f"{_fmt(p['non_null'] - o['non_null'])} more non-NULL value(s) in PostgreSQL than in Oracle."))
        cut = _longreadlen_issue(p)
        if cut:
            issues.append(cut)
    return issues


def _values_missing(o: dict[str, Any], p: dict[str, Any] | None) -> int:
    """LOB values with data in Oracle that PostgreSQL doesn't have (all of them when the column is missing)."""
    if "non_null" not in o:
        return 0
    have = o["non_empty"] if o.get("non_empty") is not None else o["non_null"]
    if p is None:
        return have
    if "non_null" not in p:
        return 0
    if o.get("non_empty") is not None and p.get("non_empty") is not None:
        return max(0, o["non_empty"] - p["non_empty"])
    return max(0, o["non_null"] - p["non_null"])


def compare(oracle_tables: list[dict[str, Any]], pg_tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per Oracle table with LOB columns (plus PostgreSQL-only tables with binary
    columns), with the counts of both sides and the differences, problems first."""
    pg_by_key = {f"{t['schema']}.{t['table']}": t for t in pg_tables}
    out = []
    for o in oracle_tables:
        p = pg_by_key.get(o.get("postgres_table") or "")
        issues: list[dict[str, str]] = []
        rows_missing = 0
        if p is None:
            issues.append(_issue("error", "Table not found in PostgreSQL."))
        else:
            if p["schema"].lower() != o["schema"].lower():
                issues.append(_issue("info", f"Matched by name: the PostgreSQL table is in schema {p['schema']}."))
            if o["error"]:
                issues.append(_issue("warning", f"Oracle could not be counted: {o['error']}"))
            if p["error"]:
                issues.append(_issue("warning", f"PostgreSQL could not be counted: {p['error']}"))
            for side, t in (("Oracle", o), ("PostgreSQL", p)):
                if t.get("sizes_error") and not t["error"]:
                    issues.append(_issue("info", f"{side}: counted without sizes, because measuring them failed: {t['sizes_error']}"))
            if o["rows"] is not None and p["rows"] is not None:
                rows_missing = o["rows"] - p["rows"]
                if rows_missing > 0:
                    issues.append(_issue("error", f"{_fmt(rows_missing)} row(s) missing in PostgreSQL: {_fmt(p['rows'])} of {_fmt(o['rows'])}."))
                elif rows_missing < 0:
                    issues.append(_issue("warning", f"{_fmt(-rows_missing)} more row(s) in PostgreSQL than in Oracle."))
        if p is None and o["error"]:
            issues.append(_issue("warning", f"Oracle could not be counted: {o['error']}"))

        p_cols = {c["column"].lower(): c for c in (p or {}).get("columns", [])}
        columns = []
        for c in o["columns"]:
            pc = p_cols.get(c["column"].lower())
            if p is None:
                col_issues = []
            elif o["rows"] is None or p["rows"] is None:
                col_issues = [_issue("warning", "Not counted.")] if pc else compare_column(c, None, 0)
                cut = _longreadlen_issue(pc) if pc and p["rows"] is not None else None
                if cut:
                    col_issues.append(cut)
            else:
                col_issues = compare_column(c, pc, rows_missing)
            columns.append({"column": c["column"], "oracle": c, "postgres": pc, "status": _worst(col_issues) if p else "error",
                            "issues": col_issues})
        status = min([_worst(issues)] + [c["status"] for c in columns], key=RANK.__getitem__)
        out.append({"schema": o["schema"], "table": o["table"], "postgres_table": o.get("postgres_table"), "status": status,
                    "issues": issues, "columns": columns,
                    "oracle": {"rows": o["rows"], "num_rows": o.get("num_rows"), "error": o["error"], "seconds": o.get("seconds")},
                    "postgres": {"rows": p["rows"], "error": p["error"], "seconds": p.get("seconds")} if p else None,
                    "rows_missing": max(rows_missing, 0),
                    "values_missing": sum(_values_missing(c["oracle"], c["postgres"]) for c in columns) if p else 0})

    for p in pg_tables:
        if not p.get("only_postgres"):
            continue
        issues = [_issue("info", "Only in PostgreSQL: there is no such table in Oracle.")]
        if p["error"]:
            issues.append(_issue("warning", f"PostgreSQL could not be counted: {p['error']}"))
        out.append({"schema": p["schema"].upper(), "table": p["table"].upper(), "postgres_table": f"{p['schema']}.{p['table']}",
                    "status": _worst(issues), "issues": issues, "oracle": None, "rows_missing": 0, "values_missing": 0,
                    "postgres": {"rows": p["rows"], "error": p["error"], "seconds": p.get("seconds")},
                    "columns": [{"column": c["column"], "oracle": None, "postgres": c, "status": "info", "issues": []} for c in p["columns"]]})
        for c in out[-1]["columns"]:
            cut = _longreadlen_issue(c["postgres"])
            if cut:
                c["issues"].append(cut)
                c["status"] = "warning"
        out[-1]["status"] = min([out[-1]["status"]] + [c["status"] for c in out[-1]["columns"]], key=RANK.__getitem__)
    out.sort(key=lambda r: (RANK[r["status"]], r["oracle"] is None, r["schema"], r["table"]))
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ora = [r for r in rows if r["oracle"] is not None]
    return {
        "oracle_tables": len(ora),
        "oracle_columns": sum(len(r["columns"]) for r in ora),
        "matched_tables": sum(1 for r in ora if r["postgres"] is not None),
        "missing_tables": sum(1 for r in ora if r["postgres"] is None),
        "postgres_only_tables": len(rows) - len(ora),
        "status": {s: sum(1 for r in rows if r["status"] == s) for s in RANK},
        "rows_missing": sum(r["rows_missing"] for r in rows),
        "values_missing": sum(r["values_missing"] for r in rows),
        "truncated_columns": sum(1 for r in rows for c in r["columns"] if any(i.get("kind") == "truncated" for i in c["issues"])),
        "not_counted": sum(1 for r in rows if (r["oracle"] and r["oracle"]["rows"] is None) or (r["postgres"] and r["postgres"]["rows"] is None)),
    }
