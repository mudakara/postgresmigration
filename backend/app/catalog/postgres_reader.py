"""Read tables, keys, relationships and code from a PostgreSQL database (read-only).

One bulk query per kind of object, so the number of round trips does not grow with the
size of the database. Partitions are folded into their parent table, and objects that
belong to extensions are skipped.
"""

import time
from collections.abc import Callable, Sequence

import psycopg
from psycopg.rows import dict_row

from app.catalog.model import Catalog, Code, Relation, Table
from app.services import postgres
from app.services.endpoints import Endpoint

_USER_SCHEMA = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)'"
_KIND = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "f": "foreign table"}
_FK_ACTION = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}


def _not_extension_member(catalog: str, alias: str) -> str:
    return (
        f"NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{catalog}'::regclass "
        f"AND d.objid = {alias}.oid AND d.deptype = 'e')"
    )


def _parse_search_path(value: str, user: str) -> list[str]:
    out = []
    for part in value.split(","):
        name = part.strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('""', '"')
        if name == "$user":
            name = user
        if name:
            out.append(name)
    return out


def _trigger_events(tgtype: int) -> tuple[str, str, str]:
    timing = "INSTEAD OF" if tgtype & 64 else "BEFORE" if tgtype & 2 else "AFTER"
    events = [e for bit, e in ((4, "INSERT"), (16, "UPDATE"), (8, "DELETE"), (32, "TRUNCATE")) if tgtype & bit]
    return timing, " OR ".join(events), "ROW" if tgtype & 1 else "STATEMENT"


# Most deparse functions (pg_get_expr, pg_get_indexdef, pg_get_triggerdef, pg_get_viewdef...)
# take a lock on the table, so they can't run on a table another session holds exclusively
# (ALTER TABLE, VACUUM FULL, partition maintenance...). Those tables are found first and
# their definitions skipped; everything else about them is read from the plain catalogs.
_LOCKED = """
    SELECT DISTINCT l.relation::bigint AS oid FROM pg_locks l
    WHERE l.locktype = 'relation' AND l.mode = 'AccessExclusiveLock' AND l.pid <> pg_backend_pid()
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""
ATTEMPTS = 3


def read_catalog(
    endpoint: Endpoint, schemas: Sequence[str] = (), progress: Callable[[float, str], None] = lambda *_: None
) -> Catalog:
    with postgres.connect(endpoint, autocommit=True, row_factory=dict_row) as conn:
        for attempt in range(1, ATTEMPTS + 1):
            locked = [r["oid"] for r in conn.execute(_LOCKED)]
            try:
                return _read(conn, schemas, progress, locked)
            except psycopg.errors.LockNotAvailable:
                if attempt == ATTEMPTS:
                    raise ValueError(
                        "Tables kept being locked by other sessions while reading (DDL or maintenance in progress "
                        "on the database). Try again when it has finished."
                    ) from None
                progress(0.1, f"A table was locked by another session; retrying ({attempt}/{ATTEMPTS - 1})")
                time.sleep(2)
    raise AssertionError("unreachable")


def _read(conn, schemas: Sequence[str], progress, locked: list[int]) -> Catalog:
    conn.execute("SET statement_timeout = '300s'")
    version = conn.execute("SHOW server_version").fetchone()["server_version"]
    user = conn.execute("SELECT current_user AS u").fetchone()["u"]
    search_path = _parse_search_path(conn.execute("SHOW search_path").fetchone()["search_path"], user)

    clause, params = ("", []) if not schemas else (" AND n.nspname = ANY(%s)", [list(schemas)])
    present = [r["nspname"] for r in conn.execute(
        f"SELECT n.nspname FROM pg_namespace n WHERE {_USER_SCHEMA} AND {_not_extension_member('pg_namespace', 'n')}"
        f"{clause} ORDER BY 1", params,
    )]
    warnings = [f"Schema not found: {s}" for s in schemas if s not in present]
    if not present:
        raise ValueError("No schemas to read" + (f": not found: {', '.join(schemas)}" if schemas else ""))
    params = [present]
    in_scope = "n.nspname = ANY(%s)"

    progress(0.15, "Reading tables and columns")
    tables: dict[int, Table] = {}
    # Partitions are counted from pg_inherits rather than pg_partition_tree(), which
    # locks every partition and so waits behind (and can hold up) DDL on them.
    for r in conn.execute(
        f"""
        WITH RECURSIVE tree AS (
            SELECT i.inhparent AS root, i.inhrelid AS rel
            FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relkind = 'p' AND NOT p.relispartition
          UNION ALL
            SELECT t.root, i.inhrelid FROM tree t JOIN pg_inherits i ON i.inhparent = t.rel
        ), leaves AS (
            SELECT t.root, count(*) AS partitions, sum(greatest(c.reltuples, 0))::bigint AS rows
            FROM tree t JOIN pg_class c ON c.oid = t.rel WHERE c.relkind <> 'p' GROUP BY t.root
        )
        SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name, c.relkind::text AS relkind,
               c.oid = ANY(%s::oid[]) AS locked,
               CASE WHEN c.relkind = 'p' THEN leaves.rows
                    WHEN c.relkind IN ('r', 'm') AND c.reltuples >= 0 THEN c.reltuples::bigint END AS row_estimate,
               COALESCE(leaves.partitions, 0) AS partitions,
               obj_description(c.oid, 'pg_class') AS comment,
               COALESCE((
                 SELECT json_agg(json_build_object(
                          'name', a.attname, 'type', format_type(a.atttypid, a.atttypmod),
                          'nullable', NOT a.attnotnull,
                          'default', CASE WHEN c.oid = ANY(%s::oid[]) THEN NULL ELSE pg_get_expr(ad.adbin, ad.adrelid) END,
                          'comment', col_description(c.oid, a.attnum)) ORDER BY a.attnum)
                 FROM pg_attribute a LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                 WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
               ), '[]'::json) AS columns
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN leaves ON leaves.root = c.oid
        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition AND {in_scope}
          AND {_not_extension_member('pg_class', 'c')}
        ORDER BY 2, 3
        """,
        [locked, locked, *params],
    ):
        tables[r["oid"]] = Table(
            schema=r["schema"], name=r["name"], kind=_KIND[r["relkind"]], columns=r["columns"],
            row_estimate=r["row_estimate"], partitions=r["partitions"], comment=r["comment"],
            extra={"locked": True} if r["locked"] else {},
        )
    busy = sorted(f"{t.schema}.{t.name}" for t in tables.values() if t.extra.get("locked"))
    if busy:
        warnings.append(
            f"{len(busy)} table(s) were locked by another session while reading (DDL or maintenance in progress): "
            f"{', '.join(busy[:10])}{'…' if len(busy) > 10 else ''}. Their column defaults and full index and trigger "
            "definitions were not read; refresh the catalog later to get them."
        )

    progress(0.35, "Reading keys and foreign keys")
    relations: list[Relation] = []
    for r in conn.execute(
        f"""
        SELECT con.conname AS name, con.contype::text AS contype, con.conrelid::bigint AS table_oid,
               con.confrelid::bigint AS ref_oid, rn.nspname AS ref_schema, rc.relname AS ref_name,
               con.confdeltype::text AS on_delete, con.confupdtype::text AS on_update,
               ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(num, ord)
                     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.num ORDER BY k.ord)::text[] AS columns,
               ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(num, ord)
                     JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.num ORDER BY k.ord)::text[] AS ref_columns
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_class rc ON rc.oid = con.confrelid
        LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace
        WHERE con.contype IN ('p', 'u', 'f') AND con.conparentid = 0 AND NOT c.relispartition AND {in_scope}
        ORDER BY con.conname
        """,
        params,
    ):
        table = tables.get(r["table_oid"])
        if table is None:
            continue
        if r["contype"] == "p":
            table.primary_key = {"name": r["name"], "columns": r["columns"]}
        elif r["contype"] == "u":
            table.unique_keys.append({"name": r["name"], "columns": r["columns"]})
        else:
            relations.append(Relation(
                kind="foreign_key", name=r["name"], from_table=table.key,
                to_table=(r["ref_schema"], r["ref_name"]), from_columns=r["columns"], to_columns=r["ref_columns"],
                details={"on_delete": _FK_ACTION.get(r["on_delete"]), "on_update": _FK_ACTION.get(r["on_update"])},
            ))

    progress(0.4, "Reading indexes")
    for r in conn.execute(
        f"""
        SELECT i.indrelid::bigint AS table_oid, ic.relname AS name, i.indisunique AS unique,
               i.indisprimary AS primary, am.amname AS method, i.indisvalid AS valid,
               CASE WHEN lk.locked THEN NULL ELSE pg_get_expr(i.indpred, i.indrelid) END AS predicate,
               CASE WHEN lk.locked THEN NULL ELSE pg_get_indexdef(i.indexrelid) END AS definition,
               CASE WHEN lk.locked
                    THEN ARRAY(SELECT COALESCE(a.attname::text, '(expression)')
                               FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(num, ord)
                               LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.num
                               WHERE k.ord <= i.indnkeyatts ORDER BY k.ord)
                    ELSE ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
                               FROM generate_series(1, i.indnkeyatts) k ORDER BY k) END::text[] AS columns,
               CASE WHEN lk.locked
                    THEN ARRAY(SELECT a.attname::text
                               FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(num, ord)
                               JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.num
                               WHERE k.ord > i.indnkeyatts ORDER BY k.ord)
                    ELSE ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
                               FROM generate_series(i.indnkeyatts + 1, i.indnatts) k ORDER BY k) END::text[] AS include
        FROM pg_index i
        CROSS JOIN LATERAL (SELECT i.indrelid = ANY(%s::oid[]) OR i.indexrelid = ANY(%s::oid[]) AS locked) lk
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_am am ON am.oid = ic.relam
        WHERE NOT c.relispartition AND {in_scope}
        ORDER BY ic.relname
        """,
        [locked, locked, *params],
    ):
        table = tables.get(r["table_oid"])
        if table is not None:
            table.indexes.append({
                "name": r["name"], "columns": r["columns"], "include": r["include"], "unique": r["unique"],
                "primary": r["primary"], "method": r["method"], "predicate": r["predicate"],
                "status": None if r["valid"] else "INVALID", "definition": r["definition"],
            })

    progress(0.45, "Reading view dependencies")
    for r in conn.execute(
        f"""
        SELECT DISTINCT v.oid::bigint AS view_oid, tn.nspname AS ref_schema, t.relname AS ref_name
        FROM pg_rewrite rw
        JOIN pg_class v ON v.oid = rw.ev_class
        JOIN pg_namespace n ON n.oid = v.relnamespace
        JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = rw.oid
                        AND d.refclassid = 'pg_class'::regclass
        JOIN pg_class t ON t.oid = d.refobjid AND t.oid <> v.oid AND t.relkind IN ('r', 'p', 'v', 'm', 'f')
        JOIN pg_namespace tn ON tn.oid = t.relnamespace
        WHERE v.relkind IN ('v', 'm') AND {in_scope}
        """,
        params,
    ):
        view = tables.get(r["view_oid"])
        if view is not None:
            relations.append(Relation(kind="view", name=None, from_table=view.key, to_table=(r["ref_schema"], r["ref_name"])))

    progress(0.55, "Reading functions and procedures")
    code: list[Code] = []
    functions: dict[int, Code] = {}
    for r in conn.execute(
        f"""
        SELECT p.oid::bigint AS oid, n.nspname AS schema, p.proname AS name,
               CASE WHEN p.prokind = 'p' THEN 'procedure' ELSE 'function' END AS kind,
               pg_get_function_identity_arguments(p.oid) AS args, pg_get_function_result(p.oid) AS result,
               pg_get_function_arguments(p.oid) AS full_args,
               l.lanname AS language, p.prorettype = 'trigger'::regtype AS is_trigger_function,
               (SELECT substr(cfg, 13) FROM unnest(p.proconfig) cfg WHERE cfg LIKE 'search_path=%%') AS search_path,
               CASE WHEN l.lanname IN ('internal', 'c') THEN NULL ELSE pg_get_functiondef(p.oid) END AS source
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
        WHERE p.prokind IN ('f', 'p') AND {in_scope} AND {_not_extension_member('pg_proc', 'p')}
        ORDER BY 2, 3, 5
        """,
        params,
    ):
        signature = f"{r['name']}({r['args']})" + (f" RETURNS {r['result']}" if r["result"] else "")
        fn = Code(
            schema=r["schema"], name=r["name"], kind=r["kind"], signature=signature, language=r["language"],
            source=r["source"], extra={"is_trigger_function": r["is_trigger_function"], "arguments": r["full_args"]},
            search_path=_parse_search_path(r["search_path"], user) if r["search_path"] else [],
        )
        code.append(fn)
        functions[r["oid"]] = fn

    progress(0.7, "Reading triggers")
    for r in conn.execute(
        f"""
        SELECT t.tgname AS name, t.tgtype::int AS tgtype, t.tgenabled::text AS enabled,
               CASE WHEN t.tgrelid = ANY(%s::oid[]) THEN NULL ELSE pg_get_triggerdef(t.oid, true) END AS definition,
               t.tgfoid::bigint AS function_oid,
               (CASE WHEN c.relispartition THEN pg_partition_root(c.oid) ELSE c.oid END)::bigint AS table_oid,
               c.relispartition AS on_partition, c.relname AS fired_on
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal AND t.tgparentid = 0 AND {in_scope}
        ORDER BY c.relname, t.tgname
        """,
        [locked, *params],
    ):
        table = tables.get(r["table_oid"])
        if table is None:
            continue
        fn = functions.get(r["function_oid"])
        timing, events, level = _trigger_events(r["tgtype"])
        code.append(Code(
            schema=table.schema, name=r["name"], kind="trigger", signature=f"{r['name']} ON {table.name}",
            language=fn.language if fn else None,
            source=(r["definition"] + ";" if r["definition"] else
                    f"-- Definition of trigger {r['name']} not read: {table.schema}.{table.name} was locked by "
                    "another session.") + "\n\n" + (fn.source if fn and fn.source else ""),
            table=table.key, scan_source=False,
            extra={
                "timing": timing, "events": events, "level": level, "enabled": r["enabled"] != "D",
                "function": f"{fn.schema}.{fn.signature}" if fn else None,
                "function_key": list(fn.key) if fn else None,
                "partition": r["fired_on"] if r["on_partition"] else None,
            },
        ))

    return Catalog(
        server_version=version, dictionary=None, schemas=present, fold="lower", search_path=search_path,
        tables=list(tables.values()), relations=relations, code=code, warnings=warnings,
    )
