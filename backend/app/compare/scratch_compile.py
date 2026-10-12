"""Run plpgsql_check for a target that doesn't have it, using a local scratch copy.

1. A handful of bulk catalog queries read the target's structure (no data) in a read-only
   session: extensions, schemas, types, sequences, tables (columns and unique keys), views,
   routines and triggers. Partitions and extension-owned objects are skipped.
2. A new temporary database on the local scratch-pg server (which has plpgsql_check) is
   built from that structure, in dependency order, retrying until nothing more succeeds.
3. The same catalog reader used for the target runs there with plpgsql_check, and the
   results are mapped back to the target's objects by schema, kind, name and arguments.
4. The temporary database is always dropped.

pg_dump isn't used: it sends many small queries per table, which over a slow link (VPN)
takes many minutes for a few thousand tables. Nothing is written to the target; only the
app's own scratch server is written to.
"""

import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.compare.target_extract import read_catalog
from app.config import get_settings
from app.models import DbKind
from app.services import postgres
from app.services.endpoints import Endpoint

Key = tuple[str, str, str, str]

_USER_SCHEMAS = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)'"
)


def _not_extension_member(catalog: str, alias: str) -> str:
    return (f"NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{catalog}'::regclass "
            f"AND d.objid = {alias}.oid AND d.deptype = 'e')")


@dataclass
class ScratchCompileResult:
    ok: bool
    checks: dict[Key, list[dict[str, Any]]] = field(default_factory=dict)
    restore_errors: list[str] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0
    stage_seconds: dict[str, float] = field(default_factory=dict)


@dataclass
class Statement:
    phase: int  # lower runs first; failures are retried in later passes
    label: str
    sql: str


def search_path_sql(value: str) -> sql.Composable:
    """Rebuild a SHOW search_path value (e.g. '"$user", public, "Odd, Name"') from quoted
    identifiers instead of splicing the text into SQL."""
    names, current, quoted, i = [], "", False, 0
    while i < len(value):
        ch = value[i]
        if quoted:
            if ch == '"' and value[i + 1:i + 2] == '"':
                current += '"'
                i += 1
            elif ch == '"':
                quoted = False
            else:
                current += ch
        elif ch == '"':
            quoted = True
        elif ch == ",":
            names.append(current.strip())
            current = ""
        else:
            current += ch
        i += 1
    names.append(current.strip())
    return sql.SQL(", ").join(sql.Identifier(n) for n in names if n)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class Structure:
    statements: list[Statement]
    # The target session's effective search_path (database and role defaults included).
    # Unqualified names in routines resolve through it, so the copy must use the same one.
    search_path: str


def read_structure(target: Endpoint, schemas: Sequence[str] = ()) -> Structure:
    """Build DDL for the target's structure from a few bulk catalog queries."""
    in_scope = " AND n.nspname = ANY(%(schemas)s)" if schemas else ""
    params = {"schemas": list(schemas)}
    out: list[Statement] = []
    with postgres.connect(target, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET statement_timeout = '300s'")
        search_path = conn.execute("SHOW search_path").fetchone()["search_path"]

        for r in conn.execute(
            "SELECT e.extname, n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname NOT IN ('plpgsql', 'plpgsql_check')"
        ):
            out.append(Statement(0, f"schema {r['nspname']}", f"CREATE SCHEMA IF NOT EXISTS {_q(r['nspname'])}"))
            out.append(Statement(1, f"extension {r['extname']}",
                                 f"CREATE EXTENSION IF NOT EXISTS {_q(r['extname'])} SCHEMA {_q(r['nspname'])}"))

        for r in conn.execute(
            f"SELECT n.nspname FROM pg_namespace n WHERE {_USER_SCHEMAS} AND {_not_extension_member('pg_namespace', 'n')}{in_scope}",
            params,
        ):
            out.append(Statement(0, f"schema {r['nspname']}", f"CREATE SCHEMA IF NOT EXISTS {_q(r['nspname'])}"))

        # Types: enums, domains, standalone composites, ranges.
        for r in conn.execute(
            f"""
            SELECT n.nspname, t.typname, t.typtype,
                   (SELECT array_agg(quote_literal(e.enumlabel) ORDER BY e.enumsortorder)
                      FROM pg_enum e WHERE e.enumtypid = t.oid) AS labels,
                   format_type(t.typbasetype, t.typtypmod) AS base_type,
                   (SELECT string_agg(quote_ident(a.attname) || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
                      FROM pg_attribute a WHERE a.attrelid = t.typrelid AND a.attnum > 0 AND NOT a.attisdropped) AS attributes,
                   format_type(rg.rngsubtype, NULL) AS range_subtype
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            LEFT JOIN pg_class c ON c.oid = t.typrelid
            LEFT JOIN pg_range rg ON rg.rngtypid = t.oid
            WHERE {_USER_SCHEMAS} AND {_not_extension_member('pg_type', 't')}{in_scope}
              AND (t.typtype IN ('e', 'd', 'r') OR (t.typtype = 'c' AND c.relkind = 'c'))
            """,
            params,
        ):
            name = f"{_q(r['nspname'])}.{_q(r['typname'])}"
            if r["typtype"] == "e":
                ddl = f"CREATE TYPE {name} AS ENUM ({', '.join(r['labels'] or [])})"
            elif r["typtype"] == "d":
                ddl = f"CREATE DOMAIN {name} AS {r['base_type']}"
            elif r["typtype"] == "r":
                ddl = f"CREATE TYPE {name} AS RANGE (subtype = {r['range_subtype']})"
            else:
                ddl = f"CREATE TYPE {name} AS ({r['attributes'] or ''})"
            out.append(Statement(2, f"type {r['nspname']}.{r['typname']}", ddl))

        for r in conn.execute(
            f"""
            SELECT n.nspname, c.relname, format_type(s.seqtypid, NULL) AS seqtype
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_sequence s ON s.seqrelid = c.oid
            WHERE c.relkind = 'S' AND {_USER_SCHEMAS} AND {_not_extension_member('pg_class', 'c')}{in_scope}
            """,
            params,
        ):
            out.append(Statement(3, f"sequence {r['nspname']}.{r['relname']}",
                                 f"CREATE SEQUENCE {_q(r['nspname'])}.{_q(r['relname'])} AS {r['seqtype']}"))

        # Tables (incl. foreign and partitioned parents; partitions skipped), columns only.
        for r in conn.execute(
            f"""
            SELECT n.nspname, c.relname,
                   (SELECT string_agg(quote_ident(a.attname) || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
                      FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS columns
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'f') AND NOT c.relispartition
              AND {_USER_SCHEMAS} AND {_not_extension_member('pg_class', 'c')}{in_scope}
            """,
            params,
        ):
            out.append(Statement(4, f"table {r['nspname']}.{r['relname']}",
                                 f"CREATE TABLE {_q(r['nspname'])}.{_q(r['relname'])} ({r['columns'] or ''})"))

        # Unique keys and unique indexes: needed so ON CONFLICT (...) checks resolve.
        for r in conn.execute(
            f"""
            SELECT n.nspname, c.relname, pg_get_indexdef(i.indexrelid) AS ddl
            FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE i.indisunique AND c.relkind IN ('r', 'p') AND NOT c.relispartition
              AND {_USER_SCHEMAS} AND {_not_extension_member('pg_class', 'c')}{in_scope}
            """,
            params,
        ):
            out.append(Statement(5, f"unique index on {r['nspname']}.{r['relname']}", r["ddl"]))

        for r in conn.execute(
            f"""
            SELECT pg_get_functiondef(p.oid) AS ddl, n.nspname, p.proname
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE p.prokind IN ('f', 'p') AND {_USER_SCHEMAS} AND {_not_extension_member('pg_proc', 'p')}{in_scope}
            """,
            params,
        ):
            out.append(Statement(6, f"routine {r['nspname']}.{r['proname']}", r["ddl"]))

        for r in conn.execute(
            f"""
            SELECT n.nspname, c.relname, c.relkind, pg_get_viewdef(c.oid) AS def
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('v', 'm') AND {_USER_SCHEMAS} AND {_not_extension_member('pg_class', 'c')}{in_scope}
            """,
            params,
        ):
            name = f"{_q(r['nspname'])}.{_q(r['relname'])}"
            body = r["def"].rstrip().rstrip(";")
            ddl = (f"CREATE MATERIALIZED VIEW {name} AS {body} WITH NO DATA" if r["relkind"] == "m"
                   else f"CREATE VIEW {name} AS {body}")
            out.append(Statement(7, f"view {r['nspname']}.{r['relname']}", ddl))

        for r in conn.execute(
            f"""
            SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid) AS ddl
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal AND NOT c.relispartition AND {_USER_SCHEMAS}{in_scope}
            """,
            params,
        ):
            out.append(Statement(8, f"trigger {r['tgname']} on {r['nspname']}.{r['relname']}", r["ddl"]))
    return Structure(out, search_path)


_TEMP_TABLE = re.compile(
    r"\bCREATE\s+(?:GLOBAL\s+|LOCAL\s+)?TEMP(?:ORARY)?\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:pg_temp\.)?\"?([A-Za-z_][\w$]*)\"?",
    re.I,
)
_MISSING_RELATION = re.compile(r'relation "(?:pg_temp\.)?([^"]+)" does not exist', re.I)


def mark_runtime_temp_tables(catalog_objects) -> None:
    """plpgsql_check can't see temporary tables that code creates while it runs, so it
    reports them as missing. Flag those messages so they are reported as low-severity
    notes instead of errors."""
    temp_tables = {m.group(1).lower() for o in catalog_objects for m in _TEMP_TABLE.finditer(o.source or "")}
    for obj in catalog_objects:
        for r in obj.check_results:
            m = _MISSING_RELATION.search(r.get("message") or "")
            if m and (m.group(1).lower() in temp_tables or "pg_temp." in (r.get("message") or "")):
                r["runtime_temp_table"] = True


def build_copy(conn: psycopg.Connection, statements: list[Statement]) -> list[str]:
    """Create the statements, retrying failures until a pass makes no progress (handles
    dependencies such as views on views or types used by tables). Returns final errors."""
    conn.execute("SET check_function_bodies = off")
    pending = sorted(statements, key=lambda s: s.phase)
    errors: dict[str, str] = {}
    while pending:
        failed = []
        for stmt in pending:
            try:
                conn.execute(stmt.sql)
                errors.pop(stmt.label, None)
            except psycopg.Error as exc:
                errors[stmt.label] = str(exc).strip().splitlines()[0]
                failed.append(stmt)
        if len(failed) == len(pending):
            break
        pending = failed
    return [f"{label}: {message}" for label, message in errors.items()]


def _scratch_conn(dbname: str) -> psycopg.Connection:
    """Writable connection to the app's own scratch server (never a user database)."""
    s = get_settings()
    return psycopg.connect(
        host=s.scratch_db_host, port=s.scratch_db_port, user=s.scratch_db_user,
        password=s.scratch_db_password, dbname=dbname, autocommit=True,
        connect_timeout=s.connect_timeout_seconds, application_name="migration-studio-compile",
    )


def compile_in_scratch(
    target: Endpoint,
    schemas: Sequence[str] = (),
    progress: Callable[[float, str], None] = lambda *_: None,
) -> ScratchCompileResult:
    s = get_settings()
    started = time.monotonic()
    if not s.scratch_db_password:
        return ScratchCompileResult(ok=False, error="The scratch database is not configured (SCRATCH_DB_PASSWORD)")

    stages: dict[str, float] = {}
    progress(0.4, "Reading the PostgreSQL structure for plpgsql_check")
    mark = time.monotonic()
    try:
        structure = read_structure(target, schemas)
        statements = structure.statements
    except psycopg.Error as exc:
        return ScratchCompileResult(ok=False, error=f"Could not read the target catalog: {exc}".strip())
    stages["read structure"] = round(time.monotonic() - mark, 1)

    dbname = f"compile_{uuid.uuid4().hex[:12]}"
    try:
        with _scratch_conn(s.scratch_db_name) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    except psycopg.Error as exc:
        return ScratchCompileResult(ok=False, error=f"Could not create the scratch database: {exc}".strip())

    try:
        progress(0.45, f"Building a structure-only copy ({len(statements)} objects)")
        mark = time.monotonic()
        with _scratch_conn(dbname) as conn:
            path = search_path_sql(structure.search_path)
            conn.execute(sql.SQL("SET search_path = {}").format(path))
            restore_errors = build_copy(conn, statements)
            conn.execute("CREATE EXTENSION IF NOT EXISTS plpgsql_check")
            # New sessions (the checker below) must resolve names like the target does.
            conn.execute(sql.SQL("ALTER DATABASE {} SET search_path = {}").format(sql.Identifier(dbname), path))
        stages["build copy"] = round(time.monotonic() - mark, 1)

        scratch = Endpoint(kind=DbKind.POSTGRES, host=s.scratch_db_host, port=s.scratch_db_port,
                           database=dbname, username=s.scratch_db_user, password=s.scratch_db_password,
                           options={"sslmode": "disable"})
        progress(0.5, "Running plpgsql_check on the copy")
        mark = time.monotonic()
        catalog = read_catalog(scratch, schemas, run_plpgsql_check=True,
                               progress=lambda f, m: progress(0.5 + 0.2 * f, f"Schema copy: {m}"))
        stages["plpgsql_check"] = round(time.monotonic() - mark, 1)
        mark_runtime_temp_tables(catalog.objects)
        checks = {o.key: [{**r, "checked_in": "scratch copy"} for r in o.check_results]
                  for o in catalog.objects if o.check_results}
        return ScratchCompileResult(ok=True, checks=checks, restore_errors=restore_errors,
                                    duration_seconds=round(time.monotonic() - started, 1), stage_seconds=stages)
    finally:
        try:
            with _scratch_conn(s.scratch_db_name) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        except psycopg.Error:
            pass  # a leftover compile_* database on the scratch server is harmless
