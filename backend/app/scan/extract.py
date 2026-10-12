"""Read the PL/SQL inventory out of the Oracle data dictionary.

All queries are bulk (one per dictionary view, filtered by owner), so the number of
round trips does not grow with the number of objects.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import oracledb

PLSQL_TYPES = ("PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY", "TRIGGER", "TYPE", "TYPE BODY")
VIEW_TYPES = ("VIEW", "MATERIALIZED VIEW")
INVENTORY_TYPES = PLSQL_TYPES + VIEW_TYPES

_SIMPLE_IDENT = re.compile(r"^[A-Z][A-Z0-9_$#]*$")


def ident(name: str) -> str:
    return name if _SIMPLE_IDENT.match(name) else '"' + name.replace('"', '""') + '"'


@dataclass
class ExtractedObject:
    owner: str
    name: str
    object_type: str
    status: str
    created: datetime | None
    last_ddl: datetime | None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.owner, self.object_type, self.name)


@dataclass
class ExtractedRoutine:
    owner: str
    package_name: str | None
    name: str
    overload: str  # "" when not overloaded
    subprogram_id: int | None
    kind: str  # PROCEDURE | FUNCTION
    return_type: str | None
    arguments: list[dict[str, Any]]
    attributes: dict[str, Any]

    @property
    def parent_key(self) -> tuple[str, str, str]:
        if self.package_name:
            return (self.owner, "PACKAGE", self.package_name)
        return (self.owner, self.kind, self.name)

    @property
    def signature(self) -> str:
        args = ", ".join(
            f"{a['name'] or '?'} {a['mode']} {a['type']}" + (" DEFAULT" if a.get("defaulted") else "")
            for a in self.arguments
        )
        qualified = f"{self.package_name}.{self.name}" if self.package_name else self.name
        returns = f" RETURN {self.return_type}" if self.kind == "FUNCTION" else ""
        return f"{qualified}({args}){returns}"


@dataclass
class ExtractedDependency:
    owner: str
    name: str
    object_type: str
    ref_owner: str
    ref_name: str
    ref_type: str
    ref_link: str | None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.owner, self.object_type, self.name)


@dataclass
class Extraction:
    oracle_version: str
    dictionary: str  # "DBA" or "ALL"
    schemas: list[str]
    missing_schemas: list[str]
    object_counts: list[dict[str, Any]]  # every object type, for the report header
    objects: list[ExtractedObject]
    routines: list[ExtractedRoutine]
    dependencies: list[ExtractedDependency]
    temporary_tables: set[tuple[str, str]]


Progress = Callable[[float, str], None]


class DictionaryReader:
    def __init__(self, conn, dictionary: str = "DBA"):  # app.services.oracle.ReadOnlyConnection
        if dictionary not in ("DBA", "ALL"):
            raise ValueError(dictionary)
        self.conn = conn
        self.p = dictionary

    def _query(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.arraysize = 5000
            cur.prefetchrows = 5001
            cur.execute(sql, params or {})
            return cur.fetchall()

    @staticmethod
    def _in(prefix: str, values: Sequence[str]) -> tuple[str, dict[str, str]]:
        """`IN (:p0, :p1...)` with binds; schema lists are capped well under Oracle's 1000."""
        binds = {f"{prefix}{i}": v for i, v in enumerate(values)}
        return "(" + ", ".join(f":{k}" for k in binds) + ")", binds

    def resolve_schemas(self, requested: Sequence[str]) -> tuple[list[str], list[str]]:
        if requested:
            clause, binds = self._in("s", list(requested))
            found = {r[0] for r in self._query(f"SELECT username FROM {self.p}_USERS WHERE username IN {clause}", binds)}
            return [s for s in requested if s in found], [s for s in requested if s not in found]
        # No explicit list: every non-Oracle-maintained schema that owns PL/SQL or views.
        types_clause, binds = self._in("t", INVENTORY_TYPES)
        rows = self._query(
            f"SELECT u.username FROM {self.p}_USERS u WHERE u.oracle_maintained = 'N' "
            f"AND EXISTS (SELECT 1 FROM {self.p}_OBJECTS o WHERE o.owner = u.username "
            f"AND o.object_type IN {types_clause}) ORDER BY u.username",
            binds,
        )
        return [r[0] for r in rows], []

    def object_counts(self, schemas: Sequence[str]) -> list[dict[str, Any]]:
        owners, binds = self._in("o", schemas)
        rows = self._query(
            f"SELECT owner, object_type, COUNT(*), SUM(CASE WHEN status <> 'VALID' THEN 1 ELSE 0 END) "
            f"FROM {self.p}_OBJECTS WHERE owner IN {owners} AND object_name NOT LIKE 'BIN$%' "
            f"GROUP BY owner, object_type ORDER BY owner, object_type",
            binds,
        )
        return [{"owner": o, "object_type": t, "count": c, "invalid": i} for o, t, c, i in rows]

    def objects(self, schemas: Sequence[str]) -> list[ExtractedObject]:
        owners, binds = self._in("o", schemas)
        types, tbinds = self._in("t", INVENTORY_TYPES)
        rows = self._query(
            f"SELECT owner, object_name, object_type, status, created, last_ddl_time FROM {self.p}_OBJECTS "
            f"WHERE owner IN {owners} AND object_type IN {types} AND generated = 'N' "
            f"AND object_name NOT LIKE 'BIN$%' ORDER BY owner, object_type, object_name",
            {**binds, **tbinds},
        )
        return [ExtractedObject(*row) for row in rows]

    def plsql_source(self, schemas: Sequence[str]) -> dict[tuple[str, str, str], str]:
        owners, binds = self._in("o", schemas)
        types, tbinds = self._in("t", PLSQL_TYPES)
        rows = self._query(
            f"SELECT owner, type, name, text FROM {self.p}_SOURCE WHERE owner IN {owners} "
            f"AND type IN {types} ORDER BY owner, type, name, line",
            {**binds, **tbinds},
        )
        chunks: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for owner, obj_type, name, text in rows:
            chunks[(owner, obj_type, name)].append(text or "")
        return {key: "CREATE OR REPLACE " + "".join(parts) for key, parts in chunks.items()}

    def view_source(self, schemas: Sequence[str]) -> dict[tuple[str, str, str], str]:
        owners, binds = self._in("o", schemas)
        result = {}
        for owner, name, text in self._query(
            f"SELECT owner, view_name, text FROM {self.p}_VIEWS WHERE owner IN {owners}", binds
        ):
            result[(owner, "VIEW", name)] = f"CREATE OR REPLACE VIEW {ident(owner)}.{ident(name)} AS\n{text or ''}"
        for owner, name, query in self._query(
            f"SELECT owner, mview_name, query FROM {self.p}_MVIEWS WHERE owner IN {owners}", binds
        ):
            result[(owner, "MATERIALIZED VIEW", name)] = (
                f"CREATE MATERIALIZED VIEW {ident(owner)}.{ident(name)} AS\n{query or ''}"
            )
        return result

    def trigger_metadata(self, schemas: Sequence[str]) -> dict[tuple[str, str, str], dict[str, Any]]:
        owners, binds = self._in("o", schemas)
        rows = self._query(
            f"SELECT owner, trigger_name, table_owner, table_name, base_object_type, trigger_type, "
            f"triggering_event, status FROM {self.p}_TRIGGERS WHERE owner IN {owners}",
            binds,
        )
        return {
            (owner, "TRIGGER", name): {
                "table": f"{tbl_owner}.{tbl}" if tbl else None,
                "base_object_type": base.strip() if base else None,
                "timing": trig_type,
                "events": event,
                "enabled": status == "ENABLED",
            }
            for owner, name, tbl_owner, tbl, base, trig_type, event, status in rows
        }

    def routines(self, schemas: Sequence[str]) -> list[ExtractedRoutine]:
        owners, binds = self._in("o", schemas)
        procs = self._query(
            f"SELECT owner, object_name, procedure_name, object_type, overload, subprogram_id, "
            f"pipelined, deterministic, authid FROM {self.p}_PROCEDURES WHERE owner IN {owners} "
            f"AND object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE') "
            f"AND NOT (object_type = 'PACKAGE' AND procedure_name IS NULL)",
            binds,
        )
        arg_rows = self._query(
            f"SELECT owner, package_name, object_name, overload, argument_name, position, in_out, "
            f"data_type, type_owner, type_name, type_subname, pls_type, defaulted, data_precision, "
            f"data_scale FROM {self.p}_ARGUMENTS WHERE owner IN {owners} AND data_level = 0 "
            f"ORDER BY owner, package_name, object_name, overload, sequence",
            binds,
        )
        args_by_key: dict[tuple, list[tuple]] = defaultdict(list)
        for row in arg_rows:
            owner, package, name, overload = row[:4]
            args_by_key[(owner, package, name, overload or "")].append(row)

        routines = []
        for owner, obj_name, proc_name, obj_type, overload, sub_id, pipelined, deterministic, authid in procs:
            package = obj_name if obj_type == "PACKAGE" else None
            name = proc_name if package else obj_name
            arguments, return_type = [], None
            for row in args_by_key.get((owner, package, name, overload or ""), []):
                (_, _, _, _, arg_name, position, in_out, data_type, t_owner, t_name, t_sub,
                 pls_type, defaulted, precision, scale) = row
                if position == 0 and arg_name is None:
                    return_type = _display_type(data_type, t_owner, t_name, t_sub, pls_type, precision, scale)
                    continue
                if arg_name is None and data_type is None:
                    continue  # placeholder row for a routine without parameters
                arguments.append({
                    "position": position,
                    "name": arg_name,
                    "mode": (in_out or "IN").replace("/", " "),
                    "type": _display_type(data_type, t_owner, t_name, t_sub, pls_type, precision, scale),
                    "data_type": data_type,
                    "defaulted": defaulted == "Y",
                })
            kind = "FUNCTION" if (obj_type == "FUNCTION" or return_type is not None) else "PROCEDURE"
            routines.append(ExtractedRoutine(
                owner=owner, package_name=package, name=name, overload=overload or "",
                subprogram_id=sub_id, kind=kind, return_type=return_type, arguments=arguments,
                attributes={
                    "pipelined": pipelined == "YES",
                    "deterministic": deterministic == "YES",
                    "authid": authid,
                },
            ))
        return routines

    def dependencies(self, schemas: Sequence[str]) -> list[ExtractedDependency]:
        owners, binds = self._in("o", schemas)
        types, tbinds = self._in("t", INVENTORY_TYPES)
        rows = self._query(
            f"SELECT owner, name, type, referenced_owner, referenced_name, referenced_type, "
            f"referenced_link_name FROM {self.p}_DEPENDENCIES WHERE owner IN {owners} AND type IN {types}",
            {**binds, **tbinds},
        )
        return [ExtractedDependency(*row) for row in rows]

    def temporary_tables(self, schemas: Sequence[str]) -> set[tuple[str, str]]:
        owners, binds = self._in("o", schemas)
        rows = self._query(
            f"SELECT owner, table_name FROM {self.p}_TABLES WHERE owner IN {owners} AND temporary = 'Y'", binds
        )
        return {(o, t) for o, t in rows}

    def extract(self, requested_schemas: Sequence[str], progress: Progress) -> Extraction:
        progress(0.08, "Resolving schemas")
        schemas, missing = self.resolve_schemas(requested_schemas)
        if not schemas:
            raise ValueError(
                "No schemas to scan: " + (f"not found: {', '.join(missing)}" if missing else "no visible schema owns PL/SQL or views")
            )

        progress(0.12, f"Listing objects in {len(schemas)} schema(s)")
        counts = self.object_counts(schemas)
        objects = self.objects(schemas)

        progress(0.2, f"Reading source of {len(objects)} objects")
        sources = {**self.plsql_source(schemas), **self.view_source(schemas)}
        triggers = self.trigger_metadata(schemas)
        for obj in objects:
            obj.source = sources.get(obj.key)
            if obj.key in triggers:
                obj.metadata["trigger"] = triggers[obj.key]

        progress(0.35, "Reading routine signatures")
        routines = self.routines(schemas)

        progress(0.45, "Reading dependencies")
        dependencies = self.dependencies(schemas)
        temp_tables = self.temporary_tables(schemas)

        return Extraction(
            oracle_version=self.conn.version,
            dictionary=self.p,
            schemas=schemas,
            missing_schemas=missing,
            object_counts=counts,
            objects=objects,
            routines=routines,
            dependencies=dependencies,
            temporary_tables=temp_tables,
        )


def _display_type(data_type, type_owner, type_name, type_subname, pls_type, precision, scale) -> str:
    if type_name:
        parts = [p for p in (type_name, type_subname) if p]
        if type_owner and type_owner not in ("SYS", "PUBLIC"):
            parts.insert(0, type_owner)
        return ".".join(parts)
    base = pls_type or data_type or "?"
    if base == "NUMBER" and precision is not None:
        return f"NUMBER({precision},{scale})" if scale else f"NUMBER({precision})"
    return base
