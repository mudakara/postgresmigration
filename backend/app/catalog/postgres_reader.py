"""Read tables, keys, relationships and code from a PostgreSQL database (read-only).

One bulk query per kind of object, so the number of round trips does not grow with the
size of the database. Partitions are folded into their parent table, and objects that
belong to extensions are skipped.
"""

from collections.abc import Callable, Sequence

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


def read_catalog(
    endpoint: Endpoint, schemas: Sequence[str] = (), progress: Callable[[float, str], None] = lambda *_: None
) -> Catalog:
    with postgres.connect(endpoint, autocommit=True, row_factory=dict_row) as conn:
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
        for r in conn.execute(
            f"""
            SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name, c.relkind::text AS relkind,
                   CASE WHEN c.relkind = 'p'
                        THEN (SELECT sum(greatest(p.reltuples, 0))::bigint FROM pg_partition_tree(c.oid) t
                              JOIN pg_class p ON p.oid = t.relid WHERE t.isleaf)
                        WHEN c.relkind IN ('r', 'm') AND c.reltuples >= 0 THEN c.reltuples::bigint END AS row_estimate,
                   CASE WHEN c.relkind = 'p'
                        THEN (SELECT count(*) FROM pg_partition_tree(c.oid) t WHERE t.isleaf) ELSE 0 END AS partitions,
                   obj_description(c.oid, 'pg_class') AS comment,
                   COALESCE((
                     SELECT json_agg(json_build_object(
                              'name', a.attname, 'type', format_type(a.atttypid, a.atttypmod),
                              'nullable', NOT a.attnotnull, 'default', pg_get_expr(ad.adbin, ad.adrelid),
                              'comment', col_description(c.oid, a.attnum)) ORDER BY a.attnum)
                     FROM pg_attribute a LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                     WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                   ), '[]'::json) AS columns
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition AND {in_scope}
              AND {_not_extension_member('pg_class', 'c')}
            ORDER BY 2, 3
            """,
            params,
        ):
            tables[r["oid"]] = Table(
                schema=r["schema"], name=r["name"], kind=_KIND[r["relkind"]], columns=r["columns"],
                row_estimate=r["row_estimate"], partitions=r["partitions"], comment=r["comment"],
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
                   pg_get_expr(i.indpred, i.indrelid) AS predicate, pg_get_indexdef(i.indexrelid) AS definition,
                   ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
                         FROM generate_series(1, i.indnkeyatts) k ORDER BY k)::text[] AS columns,
                   ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
                         FROM generate_series(i.indnkeyatts + 1, i.indnatts) k ORDER BY k)::text[] AS include
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_am am ON am.oid = ic.relam
            WHERE NOT c.relispartition AND {in_scope}
            ORDER BY ic.relname
            """,
            params,
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
                source=r["source"], extra={"is_trigger_function": r["is_trigger_function"]},
                search_path=_parse_search_path(r["search_path"], user) if r["search_path"] else [],
            )
            code.append(fn)
            functions[r["oid"]] = fn

        progress(0.7, "Reading triggers")
        for r in conn.execute(
            f"""
            SELECT t.tgname AS name, t.tgtype::int AS tgtype, t.tgenabled::text AS enabled,
                   pg_get_triggerdef(t.oid, true) AS definition, t.tgfoid::bigint AS function_oid,
                   (CASE WHEN c.relispartition THEN pg_partition_root(c.oid) ELSE c.oid END)::bigint AS table_oid,
                   c.relispartition AS on_partition, c.relname AS fired_on
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal AND t.tgparentid = 0 AND {in_scope}
            ORDER BY c.relname, t.tgname
            """,
            params,
        ):
            table = tables.get(r["table_oid"])
            if table is None:
                continue
            fn = functions.get(r["function_oid"])
            timing, events, level = _trigger_events(r["tgtype"])
            code.append(Code(
                schema=table.schema, name=r["name"], kind="trigger", signature=f"{r['name']} ON {table.name}",
                language=fn.language if fn else None,
                source=r["definition"] + ";\n\n" + (fn.source if fn and fn.source else ""),
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
