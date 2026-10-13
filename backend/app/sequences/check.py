"""Find PostgreSQL sequences that are behind the data after a migration.

For every sequence: which columns it fills (identity/serial ownership, column defaults,
BEFORE INSERT triggers that assign NEW.col := nextval(...), INSERT statements in
procedures), the current max of those columns, and optionally the value Oracle's
sequence had reached. A sequence is behind when its next value is not above the data.

Everything here is read-only: the session is read-only, the only data read is max() of
the linked columns, and fixing is left to a generated script (app.sequences.script).
"""

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.sequences.parse import find_uses
from app.services import postgres
from app.services.endpoints import Endpoint

_USER_SCHEMA = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)'"
BIGINT_MAX = 9_223_372_036_854_775_807
MAX_BATCH = 25
Progress = Callable[[float, str], None]


def _not_extension_member(catalog: str, alias: str) -> str:
    return (f"NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{catalog}'::regclass "
            f"AND d.objid = {alias}.oid AND d.deptype = 'e')")


@dataclass
class ColumnLink:
    schema: str
    table: str
    column: str
    via: str  # identity | serial | default | trigger | insert
    source: str | None = None  # the trigger function or procedure that uses the sequence
    data_type: str | None = None
    max_value: int | None = None
    error: str | None = None


@dataclass
class SequenceState:
    schema: str
    name: str
    data_type: str
    increment: int
    min_value: int
    max_value: int
    start_value: int
    last_value: int | None
    is_called: bool | None
    can_read: bool
    locked: bool = False  # held exclusively by another session while checking
    columns: list[ColumnLink] = field(default_factory=list)
    oracle: dict[str, Any] | None = None  # {owner, name, last_number}
    next_value: int | None = None
    needed_next: int | None = None
    status: str = "ok"  # behind | behind_oracle | overflow | ok | unlinked | check_failed | no_privilege | descending
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _search_path(value: str, user: str) -> list[str]:
    out = []
    for part in value.split(","):
        name = part.strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('""', '"')
        out.append(user if name == "$user" else name)
    return [n for n in out if n]


def read_postgres(endpoint: Endpoint, schemas: Sequence[str], progress: Progress) -> tuple[list[SequenceState], list[str], list[str]]:
    """Sequences with their linked columns and max values. Returns (sequences, schemas, warnings)."""
    warnings: list[str] = []
    with postgres.connect(endpoint, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET statement_timeout = '300s'")
        user = conn.execute("SELECT current_user AS u").fetchone()["u"]
        session_path = _search_path(conn.execute("SHOW search_path").fetchone()["search_path"], user)
        clause, params = ("", []) if not schemas else (" AND n.nspname = ANY(%s)", [list(schemas)])
        present = [r["nspname"] for r in conn.execute(
            f"SELECT n.nspname FROM pg_namespace n WHERE {_USER_SCHEMA} AND {_not_extension_member('pg_namespace', 'n')}"
            f"{clause} ORDER BY 1", params)]
        warnings += [f"Schema not found: {s}" for s in schemas if s not in present]
        if not present:
            raise ValueError("No schemas to check")
        scope = [present]

        progress(0.1, "Reading sequences")
        seqs: dict[int, SequenceState] = {}
        for r in conn.execute(
            f"""
            SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name, s.data_type::text AS data_type,
                   s.start_value, s.min_value, s.max_value, s.increment_by, s.last_value,
                   has_sequence_privilege(c.oid, 'SELECT') AS can_read
            FROM pg_sequences s
            JOIN pg_namespace n ON n.nspname = s.schemaname
            JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = s.sequencename
            WHERE n.nspname = ANY(%s) AND {_not_extension_member('pg_class', 'c')}
            ORDER BY 2, 3
            """, scope,
        ):
            seqs[r["oid"]] = SequenceState(
                schema=r["schema"], name=r["name"], data_type=r["data_type"], increment=r["increment_by"],
                min_value=r["min_value"], max_value=r["max_value"], start_value=r["start_value"],
                last_value=r["last_value"], is_called=None, can_read=r["can_read"],
            )
        _read_sequence_values(conn, seqs)
        by_name = {(s.schema, s.name): oid for oid, s in seqs.items()}
        names = defaultdict(list)
        for (schema, name), oid in by_name.items():
            names[name].append(oid)

        progress(0.2, "Reading tables and columns")
        columns: dict[tuple[str, str], dict[str, tuple[str, bool]]] = defaultdict(dict)  # (schema, table) -> col -> (type, numeric)
        table_oid: dict[int, tuple[str, str]] = {}
        for r in conn.execute(
            f"""
            SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS table, a.attname AS column,
                   format_type(a.atttypid, a.atttypmod) AS type, t.typcategory = 'N' AS numeric
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname = ANY(%s)
            """, scope,
        ):
            columns[(r["schema"], r["table"])][r["column"]] = (r["type"], r["numeric"])
            table_oid[r["oid"]] = (r["schema"], r["table"])
        tables_by_name = defaultdict(list)
        for key in columns:
            tables_by_name[key[1]].append(key)

        def link(oid: int, schema: str, table: str, column: str, via: str, source: str | None = None) -> None:
            col = columns.get((schema, table), {}).get(column)
            if col is None:
                return
            state = seqs[oid]
            if any((c.schema, c.table, c.column) == (schema, table, column) for c in state.columns):
                return
            state.columns.append(ColumnLink(schema, table, column, via, source, col[0]))

        progress(0.3, "Reading identity, serial and default links")
        for r in conn.execute(
            """
            SELECT d.objid::bigint AS seq, d.refobjid::bigint AS tbl, a.attname AS column, d.deptype::text AS deptype
            FROM pg_depend d
            JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
            JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
            WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass
              AND d.refobjsubid > 0 AND d.deptype IN ('a', 'i')
            UNION ALL
            SELECT d.refobjid::bigint, ad.adrelid::bigint, a.attname, 'default'
            FROM pg_depend d
            JOIN pg_attrdef ad ON d.classid = 'pg_attrdef'::regclass AND d.objid = ad.oid
            JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
            JOIN pg_class s ON s.oid = d.refobjid AND s.relkind = 'S'
            """
        ):
            if r["seq"] in seqs and r["tbl"] in table_oid:
                schema, table = table_oid[r["tbl"]]
                via = {"i": "identity", "a": "serial", "default": "default"}[r["deptype"]]
                link(r["seq"], schema, table, r["column"], via)

        progress(0.4, "Reading triggers and procedures that call nextval()")
        trigger_tables: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for r in conn.execute("SELECT tgfoid::bigint AS fn, tgrelid::bigint AS tbl FROM pg_trigger WHERE NOT tgisinternal"):
            if r["tbl"] in table_oid:
                trigger_tables[r["fn"]].append(table_oid[r["tbl"]])
        for r in conn.execute(
            f"""
            SELECT p.oid::bigint AS oid, n.nspname AS schema, p.proname AS name, p.prosrc AS source,
                   (SELECT substr(cfg, 13) FROM unnest(p.proconfig) cfg WHERE cfg LIKE 'search_path=%%') AS search_path
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE {_USER_SCHEMA} AND p.prokind IN ('f', 'p') AND p.prosrc ~* 'nextval'
              AND {_not_extension_member('pg_proc', 'p')}
            """
        ):
            path = [r["schema"], *(_search_path(r["search_path"], user) if r["search_path"] else []), *session_path]

            def resolve_seq(parts: tuple[str, ...]) -> int | None:
                if len(parts) >= 2:
                    return by_name.get((parts[-2], parts[-1]))
                for schema in path:
                    if (schema, parts[0]) in by_name:
                        return by_name[(schema, parts[0])]
                candidates = names.get(parts[0], [])
                return candidates[0] if len(candidates) == 1 else None

            def resolve_table(parts: tuple[str, ...]) -> tuple[str, str] | None:
                if len(parts) >= 2:
                    return (parts[-2], parts[-1]) if (parts[-2], parts[-1]) in columns else None
                for schema in path:
                    if (schema, parts[0]) in columns:
                        return (schema, parts[0])
                candidates = tables_by_name.get(parts[0], [])
                return candidates[0] if len(candidates) == 1 else None

            source = f"{r['schema']}.{r['name']}"
            for use in find_uses(r["source"] or ""):
                seq = resolve_seq(use.sequence)
                if seq is None:
                    continue
                if use.table is None:
                    for schema, table in trigger_tables.get(r["oid"], []):
                        link(seq, schema, table, use.column, "trigger", source)
                else:
                    target = resolve_table(use.table)
                    if target:
                        link(seq, target[0], target[1], use.column, "insert", source)

        progress(0.55, "Reading the current maximum of each linked column")
        _read_maxima(conn, [c for s in seqs.values() for c in s.columns], progress)

    return list(seqs.values()), present, warnings


def _read_sequence_values(conn, seqs: dict[int, SequenceState]) -> None:
    """last_value and is_called straight from each sequence. pg_sequences can't be used:
    it shows NULL both for a sequence never used and for one reset with setval(..., false)."""
    readable = [(oid, s) for oid, s in seqs.items() if s.can_read]
    def query(chunk):
        return sql.SQL(" UNION ALL ").join(
            sql.SQL("SELECT {}::bigint AS oid, last_value, is_called FROM {}.{}").format(
                sql.Literal(oid), sql.Identifier(s.schema), sql.Identifier(s.name))
            for oid, s in chunk)

    for i in range(0, len(readable), 100):
        chunk = readable[i:i + 100]
        try:
            rows = conn.execute(query(chunk)).fetchall()
        except psycopg.errors.LockNotAvailable:  # one of them is locked: read them one by one
            rows = []
            for item in chunk:
                try:
                    rows += conn.execute(query([item])).fetchall()
                except psycopg.errors.LockNotAvailable:
                    item[1].locked = True
        for r in rows:
            seqs[r["oid"]].last_value, seqs[r["oid"]].is_called = r["last_value"], r["is_called"]


def _read_maxima(conn, links: list[ColumnLink], progress: Progress) -> None:
    targets: dict[tuple[str, str, str], list[ColumnLink]] = defaultdict(list)
    for c in links:
        targets[(c.schema, c.table, c.column)].append(c)
    numeric = []
    for key, group in targets.items():
        if not _is_numeric(group[0].data_type):
            for c in group:
                c.error = f"column type {c.data_type} is not numeric"
        else:
            numeric.append(key)

    def query(keys):
        return sql.SQL("SELECT {}").format(sql.SQL(", ").join(
            sql.SQL("(SELECT max({}) FROM {}.{})").format(sql.Identifier(c), sql.Identifier(s), sql.Identifier(t))
            for s, t, c in keys))

    def store(key, value=None, error=None):
        for c in targets[key]:
            c.max_value = None if value is None else math.ceil(value)
            c.error = error

    conn.execute("SET statement_timeout = '60s'")
    for i in range(0, len(numeric), MAX_BATCH):
        chunk = numeric[i:i + MAX_BATCH]
        progress(0.55 + 0.35 * i / max(1, len(numeric)), f"max() of {i + len(chunk)}/{len(numeric)} columns")
        try:
            row = conn.execute(query(chunk)).fetchone()
            values = list(row.values())
            for key, value in zip(chunk, values, strict=True):
                store(key, value)
        except Exception:  # a timeout or a missing privilege: retry one by one to isolate it
            for key in chunk:
                try:
                    store(key, next(iter(conn.execute(query([key])).fetchone().values())))
                except Exception as exc:
                    store(key, error=str(exc).strip().splitlines()[0][:300])


def _is_numeric(data_type: str | None) -> bool:
    t = (data_type or "").lower()
    return t.startswith(("integer", "bigint", "smallint", "numeric", "decimal", "real", "double"))


def read_oracle(endpoint: Endpoint, owners: Sequence[str], *, user_grants: bool) -> list[dict[str, Any]]:
    """Oracle sequences (LAST_NUMBER: the next value Oracle would hand out, past its cache)."""
    from app.services import oracle

    prefix = "ALL" if user_grants else "DBA"
    out = []
    with oracle.connect(endpoint) as conn:
        owners = sorted({o.upper() for o in owners})
        for i in range(0, len(owners), 500):
            chunk = owners[i:i + 500]
            binds = {f"o{j}": o for j, o in enumerate(chunk)}
            with conn.cursor() as cur:
                cur.arraysize = 5000
                cur.execute(
                    f"SELECT sequence_owner, sequence_name, last_number, increment_by FROM {prefix}_SEQUENCES "
                    f"WHERE sequence_owner IN ({', '.join(':' + k for k in binds)})",
                    binds,
                )
                out += [{"owner": o, "name": n, "last_number": int(last), "increment": int(inc)}
                        for o, n, last, inc in cur.fetchall()]
    return out


def evaluate(seqs: list[SequenceState], oracle_seqs: list[dict[str, Any]] | None) -> None:
    """Fill next_value, needed_next, status and message on each sequence."""
    oracle_by_key = {(o["owner"].lower(), o["name"].lower()): o for o in oracle_seqs or []}
    oracle_by_name = defaultdict(list)
    for o in oracle_seqs or []:
        oracle_by_name[o["name"].lower()].append(o)

    for s in seqs:
        if oracle_seqs is not None:
            match = oracle_by_key.get((s.schema.lower(), s.name.lower()))
            if match is None and len(oracle_by_name.get(s.name.lower(), [])) == 1:
                match = oracle_by_name[s.name.lower()][0]
            if match:
                s.oracle = {"owner": match["owner"], "name": match["name"], "last_number": match["last_number"]}
        if s.can_read:
            s.next_value = s.last_value + s.increment if s.is_called else s.last_value

        maxima = [c for c in s.columns if c.max_value is not None]
        candidates = [c.max_value + 1 for c in maxima]
        if s.oracle:
            candidates.append(s.oracle["last_number"])
        s.needed_next = max(candidates) if candidates else None

        if s.locked:
            s.status, s.message = "check_failed", "Locked by another session while checking (e.g. ALTER SEQUENCE): check again later."
        elif not s.can_read:
            s.status, s.message = "no_privilege", "This login has no SELECT privilege on the sequence, so its value can't be read."
        elif s.increment < 0:
            s.status, s.message = "descending", "Descending sequence: check it manually."
        elif s.needed_next is not None and s.needed_next > min(s.max_value, BIGINT_MAX):
            s.status = "overflow"
            s.message = (f"Needs to reach {s.needed_next:,} but its maximum is {s.max_value:,} ({s.data_type}): "
                         "widen it (ALTER SEQUENCE ... AS bigint MAXVALUE ...) before fixing.")
        elif any(c.max_value >= s.next_value for c in maxima):
            worst = max(maxima, key=lambda c: c.max_value)
            s.status = "behind"
            s.message = (f"Next value {s.next_value:,} is not above max({worst.table}.{worst.column}) = {worst.max_value:,}: "
                         "the next insert will hit a duplicate key.")
        elif s.oracle and s.next_value < s.oracle["last_number"]:
            s.status = "behind_oracle"
            s.message = (f"Next value {s.next_value:,} is below Oracle's {s.oracle['last_number']:,}: values Oracle already "
                         "handed out (to other tables, files or systems) can be issued again.")
        elif any(c.error for c in s.columns):
            s.status = "check_failed"
            s.message = "; ".join(f"{c.table}.{c.column}: {c.error}" for c in s.columns if c.error)
        elif not s.columns and not s.oracle:
            s.status = "unlinked"
            s.message = "No column found that this sequence fills (it may be used from application code)."
        else:
            s.status = "ok"
            s.message = "Ahead of the data."
