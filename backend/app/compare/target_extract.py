"""Read routines, views and triggers from the migrated PostgreSQL database.

The session is read-only (see app.services.postgres.connect). Objects that belong to extensions (orafce's Oracle
compatibility functions, plpgsql_check itself...) are skipped: they are not migrated code.
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.services import postgres
from app.services.endpoints import Endpoint

_SYSTEM_SCHEMA_FILTER = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)'"
)
_MODE = {"i": "IN", "o": "OUT", "b": "IN OUT", "v": "VARIADIC", "t": "TABLE"}


def _not_extension_member(catalog: str, alias: str) -> str:
    return (
        f"NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{catalog}'::regclass "
        f"AND d.objid = {alias}.oid AND d.deptype = 'e')"
    )


@dataclass
class ExtractedTarget:
    oid: int
    schema: str
    name: str
    kind: str
    identity_args: str = ""
    result_type: str | None = None
    language: str | None = None
    arguments: list[dict[str, Any]] = field(default_factory=list)
    n_defaults: int = 0
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    check_results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.schema, self.kind, self.name, self.identity_args)

    @property
    def source_hash(self) -> str | None:
        return hashlib.sha256(self.source.encode()).hexdigest() if self.source is not None else None


@dataclass
class TargetCatalog:
    server_version: str
    objects: list[ExtractedTarget]
    plpgsql_check: bool
    extensions: dict[str, str]


def _schema_clause(schemas: Sequence[str]) -> tuple[str, list]:
    if not schemas:
        return "", []
    return " AND n.nspname = ANY(%s)", [list(schemas)]


def _body_start_line(source: str) -> int:
    """Line (1-based) where a function body starts in pg_get_functiondef output: the line
    containing the opening dollar quote. plpgsql line numbers count from there."""
    for i, line in enumerate(source.splitlines(), start=1):
        if re.search(r"\bAS\s+\$[A-Za-z_]*\$", line):
            return i
    return 1


def read_catalog(
    endpoint: Endpoint,
    schemas: Sequence[str] = (),
    *,
    run_plpgsql_check: bool = True,
    progress: Callable[[float, str], None] = lambda *_: None,
) -> TargetCatalog:
    with postgres.connect(endpoint, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET statement_timeout = '120s'")
        version = conn.execute("SHOW server_version").fetchone()["server_version"]
        extensions = {r["extname"]: r["extversion"] for r in conn.execute("SELECT extname, extversion FROM pg_extension")}
        clause, params = _schema_clause(schemas)

        progress(0.1, "Reading PostgreSQL functions and procedures")
        routines = conn.execute(
            f"""
            SELECT p.oid::bigint AS oid, n.nspname AS schema, p.proname AS name,
                   CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END AS kind,
                   pg_get_function_identity_arguments(p.oid) AS identity_args,
                   pg_get_function_result(p.oid) AS result_type,
                   l.lanname AS language, p.pronargdefaults AS n_defaults,
                   pg_get_functiondef(p.oid) AS source,
                   p.prorettype = 'trigger'::regtype AS is_trigger_function,
                   COALESCE((
                     SELECT json_agg(json_build_object(
                              'position', a.ord,
                              'name', p.proargnames[a.ord],
                              'mode', COALESCE(p.proargmodes[a.ord]::text, 'i'),
                              'type', format_type(a.typ, NULL)) ORDER BY a.ord)
                     FROM unnest(COALESCE(p.proallargtypes, p.proargtypes::oid[])) WITH ORDINALITY AS a(typ, ord)
                   ), '[]'::json) AS arguments
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            WHERE p.prokind IN ('f', 'p') AND {_SYSTEM_SCHEMA_FILTER} AND {_not_extension_member('pg_proc', 'p')}
            {clause}
            ORDER BY 2, 3
            """,
            params,
        ).fetchall()

        objects: list[ExtractedTarget] = []
        by_oid: dict[int, ExtractedTarget] = {}
        for r in routines:
            args = [
                {**a, "mode": _MODE.get(a["mode"], a["mode"]), "name": a["name"] or None}
                for a in (r["arguments"] or [])
            ]
            target = ExtractedTarget(
                oid=r["oid"], schema=r["schema"], name=r["name"], kind=r["kind"],
                identity_args=r["identity_args"], result_type=r["result_type"], language=r["language"],
                arguments=args, n_defaults=r["n_defaults"], source=r["source"],
                metadata={"is_trigger_function": r["is_trigger_function"], "body_start_line": _body_start_line(r["source"] or "")},
            )
            objects.append(target)
            by_oid[r["oid"]] = target

        progress(0.25, "Reading PostgreSQL views")
        for r in conn.execute(
            f"""
            SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name,
                   CASE c.relkind WHEN 'm' THEN 'materialized view' ELSE 'view' END AS kind,
                   pg_get_viewdef(c.oid, true) AS definition
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('v', 'm') AND {_SYSTEM_SCHEMA_FILTER} AND {_not_extension_member('pg_class', 'c')}
            {clause}
            ORDER BY 2, 3
            """,
            params,
        ):
            keyword = "MATERIALIZED VIEW" if r["kind"] == "materialized view" else "OR REPLACE VIEW"
            source = f"CREATE {keyword} {r['schema']}.{r['name']} AS\n{r['definition']}"
            objects.append(ExtractedTarget(oid=r["oid"], schema=r["schema"], name=r["name"], kind=r["kind"], source=source))

        progress(0.35, "Reading PostgreSQL triggers")
        for r in conn.execute(
            f"""
            SELECT t.oid::bigint AS oid, n.nspname AS schema, c.relname AS table_name, c.oid::bigint AS table_oid,
                   t.tgname AS name, pg_get_triggerdef(t.oid, true) AS definition, t.tgfoid::bigint AS function_oid,
                   fn.nspname || '.' || f.proname AS function_name
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_proc f ON f.oid = t.tgfoid
            JOIN pg_namespace fn ON fn.oid = f.pronamespace
            WHERE NOT t.tgisinternal AND {_SYSTEM_SCHEMA_FILTER}
            {clause}
            ORDER BY 2, 3, 5
            """,
            params,
        ):
            function = by_oid.get(r["function_oid"])
            source = r["definition"] + ";\n\n" + (function.source if function and function.source else "")
            objects.append(ExtractedTarget(
                oid=r["oid"], schema=r["schema"], name=r["name"], kind="trigger", source=source,
                identity_args=r["table_name"],  # trigger names are unique per table
                metadata={
                    "table": f"{r['schema']}.{r['table_name']}",
                    "table_oid": r["table_oid"],
                    "function": r["function_name"],
                    "function_oid": r["function_oid"],
                    "definition": r["definition"],
                    # Lines of the function inside the combined source.
                    "function_line_offset": r["definition"].count("\n") + 2,
                },
            ))

        has_check = "plpgsql_check" in extensions
        if has_check and run_plpgsql_check:
            _run_plpgsql_check(conn, objects, progress)
            for trigger in (o for o in objects if o.kind == "trigger"):
                function = by_oid.get(trigger.metadata["function_oid"])
                offset = trigger.metadata["function_line_offset"] - 1
                trigger.check_results = [
                    {**r, "line": r["line"] + offset if r.get("line") else None}
                    for r in (function.check_results if function else [])
                ]

    return TargetCatalog(server_version=version, objects=objects, plpgsql_check=has_check, extensions=extensions)


def _run_plpgsql_check(conn: psycopg.Connection, objects: list[ExtractedTarget], progress) -> None:
    """Static analysis only: plpgsql_check parses and type-checks, it doesn't run the code."""
    plpgsql = [o for o in objects if o.kind in ("function", "procedure") and o.language == "plpgsql"]
    trigger_tables = {
        o.metadata["function_oid"]: o.metadata["table_oid"] for o in objects if o.kind == "trigger"
    }
    for i, obj in enumerate(plpgsql):
        if i % 25 == 0:
            progress(0.4 + 0.3 * i / max(1, len(plpgsql)), f"plpgsql_check {i + 1}/{len(plpgsql)}")
        relid = trigger_tables.get(obj.oid, 0) if obj.metadata.get("is_trigger_function") else 0
        if obj.metadata.get("is_trigger_function") and not relid:
            continue  # trigger function not attached to any table: nothing to check against
        try:
            rows = conn.execute(
                "SELECT lineno, statement, sqlstate, message, detail, hint, level "
                "FROM plpgsql_check_function_tb(%s::oid, %s::oid)",
                (obj.oid, relid),
            ).fetchall()
        except psycopg.Error as exc:
            obj.check_results = [{"level": "check failed", "message": str(exc).strip()[:500]}]
            continue
        offset = obj.metadata.get("body_start_line", 1) - 1
        obj.check_results = [
            {
                "level": r["level"], "message": r["message"], "detail": r["detail"], "hint": r["hint"],
                "sqlstate": r["sqlstate"], "statement": r["statement"],
                "line": (r["lineno"] + offset) if r["lineno"] is not None else None,
            }
            for r in rows
            if r["level"] != "performance"
        ]
