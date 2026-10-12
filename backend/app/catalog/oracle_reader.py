"""Read tables, keys, relationships and PL/SQL from the Oracle data dictionary (read-only).

Uses DBA_* views when the account can read them, ALL_* otherwise (what it can see).
Code-to-table links come from the dictionary's own dependency tracking, refined by
reading the source for how each table is used (see app.catalog.references).
"""

from collections import defaultdict
from collections.abc import Callable, Sequence

from app.catalog.model import Catalog, Code, Relation, Table, TableKey
from app.scan.extract import DictionaryReader
from app.services import oracle
from app.services.endpoints import Endpoint

_CODE_TYPES = ("PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY", "TRIGGER")
_TABLE_REF_TYPES = {"TABLE", "VIEW", "MATERIALIZED VIEW", "SYNONYM"}


class CatalogDictionary(DictionaryReader):
    def default_schemas(self) -> list[str]:
        """Every non-Oracle-maintained schema that owns a table, view or PL/SQL."""
        types, binds = self._in("t", ("TABLE", "VIEW", "PROCEDURE", "FUNCTION", "PACKAGE", "TRIGGER"))
        return [r[0] for r in self._query(
            f"SELECT u.username FROM {self.p}_USERS u WHERE u.oracle_maintained = 'N' "
            f"AND EXISTS (SELECT 1 FROM {self.p}_OBJECTS o WHERE o.owner = u.username AND o.object_type IN {types}) "
            f"ORDER BY u.username",
            binds,
        )]

    def tables(self, schemas: Sequence[str]) -> dict[TableKey, Table]:
        owners, binds = self._in("o", schemas)
        mviews = {(o, n) for o, n in self._query(f"SELECT owner, mview_name FROM {self.p}_MVIEWS WHERE owner IN {owners}", binds)}
        out: dict[TableKey, Table] = {}
        for owner, name, num_rows, temporary, partitioned in self._query(
            f"SELECT owner, table_name, num_rows, temporary, partitioned FROM {self.p}_TABLES "
            f"WHERE owner IN {owners} AND dropped = 'NO' AND nested = 'NO' AND secondary = 'N' "
            f"AND table_name NOT LIKE 'BIN$%' AND table_name NOT LIKE 'MLOG$%' AND table_name NOT LIKE 'RUPD$%'",
            binds,
        ):
            kind = ("materialized view" if (owner, name) in mviews else "temporary table" if temporary == "Y"
                    else "partitioned table" if partitioned == "YES" else "table")
            out[(owner, name)] = Table(schema=owner, name=name, kind=kind, row_estimate=num_rows)
        for owner, name in self._query(f"SELECT owner, view_name FROM {self.p}_VIEWS WHERE owner IN {owners}", binds):
            out[(owner, name)] = Table(schema=owner, name=name, kind="view")

        for owner, name, count in self._query(
            f"SELECT table_owner, table_name, COUNT(*) FROM {self.p}_TAB_PARTITIONS WHERE table_owner IN {owners} "
            f"GROUP BY table_owner, table_name",
            binds,
        ):
            if (owner, name) in out:
                out[(owner, name)].partitions = count
        for owner, name, comment in self._query(
            f"SELECT owner, table_name, comments FROM {self.p}_TAB_COMMENTS WHERE owner IN {owners} AND comments IS NOT NULL",
            binds,
        ):
            if (owner, name) in out:
                out[(owner, name)].comment = comment
        for owner, name, column, data_type, length, precision, scale, nullable, char_length, char_used in self._query(
            f"SELECT owner, table_name, column_name, data_type, data_length, data_precision, data_scale, nullable, "
            f"char_length, char_used FROM {self.p}_TAB_COLUMNS WHERE owner IN {owners} "
            f"ORDER BY owner, table_name, column_id",
            binds,
        ):
            table = out.get((owner, name))
            if table is not None:
                table.columns.append({
                    "name": column, "nullable": nullable == "Y",
                    "type": _column_type(data_type, length, precision, scale, char_length, char_used),
                })
        return out

    def constraints(self, schemas: Sequence[str], tables: dict[TableKey, Table]) -> list[Relation]:
        owners, binds = self._in("o", schemas)
        rows = self._query(
            f"SELECT owner, constraint_name, constraint_type, table_name, r_owner, r_constraint_name, delete_rule, "
            f"index_owner, index_name FROM {self.p}_CONSTRAINTS WHERE owner IN {owners} AND constraint_type IN ('P', 'U', 'R') "
            f"AND constraint_name NOT LIKE 'BIN$%'",
            binds,
        )
        # Foreign keys can point at keys in schemas outside the selection.
        outside = sorted({r[4] for r in rows if r[2] == "R" and r[4] and r[4] not in schemas})
        if outside:
            extra_owners, extra_binds = self._in("x", outside)
            rows += [r for r in self._query(
                f"SELECT owner, constraint_name, constraint_type, table_name, r_owner, r_constraint_name, delete_rule, "
                f"index_owner, index_name FROM {self.p}_CONSTRAINTS WHERE owner IN {extra_owners} "
                f"AND constraint_type IN ('P', 'U')",
                extra_binds,
            )]
        all_owners, all_binds = self._in("o", list(schemas) + outside)
        columns: dict[tuple[str, str], list[str]] = defaultdict(list)
        for owner, name, column in self._query(
            f"SELECT cc.owner, cc.constraint_name, cc.column_name FROM {self.p}_CONS_COLUMNS cc "
            f"JOIN {self.p}_CONSTRAINTS c ON c.owner = cc.owner AND c.constraint_name = cc.constraint_name "
            f"WHERE cc.owner IN {all_owners} AND c.constraint_type IN ('P', 'U', 'R') "
            f"ORDER BY cc.owner, cc.constraint_name, cc.position",
            all_binds,
        ):
            columns[(owner, name)].append(column)

        keys = {(owner, name): (owner, table) for owner, name, ctype, table, *_ in rows if ctype in ("P", "U")}
        relations = []
        for owner, name, ctype, table_name, r_owner, r_name, delete_rule, index_owner, index_name in rows:
            table = tables.get((owner, table_name))
            if table is None:
                continue
            cols = columns.get((owner, name), [])
            if ctype == "P":
                table.primary_key = {"name": name, "columns": cols, "index": (index_owner, index_name)}
            elif ctype == "U":
                table.unique_keys.append({"name": name, "columns": cols, "index": (index_owner, index_name)})
            elif (r_owner, r_name) in keys:
                relations.append(Relation(
                    kind="foreign_key", name=name, from_table=table.key, to_table=keys[(r_owner, r_name)],
                    from_columns=cols, to_columns=columns.get((r_owner, r_name), []),
                    details={"on_delete": delete_rule},
                ))
        return relations

    def indexes(self, schemas: Sequence[str], tables: dict[TableKey, Table]) -> None:
        """Attach each table's indexes (call after constraints(), which knows the key indexes)."""
        owners, binds = self._in("o", schemas)
        rows = self._query(
            f"SELECT owner, index_name, table_owner, table_name, index_type, uniqueness, status, partitioned "
            f"FROM {self.p}_INDEXES WHERE table_owner IN {owners} AND index_type NOT IN ('LOB', 'CLUSTER') "
            f"AND index_name NOT LIKE 'BIN$%'",
            binds,
        )
        columns: dict[tuple[str, str], list[tuple[int, str, str]]] = defaultdict(list)
        for owner, name, column, position, descend in self._query(
            f"SELECT index_owner, index_name, column_name, column_position, descend FROM {self.p}_IND_COLUMNS "
            f"WHERE table_owner IN {owners} ORDER BY index_owner, index_name, column_position",
            binds,
        ):
            columns[(owner, name)].append((position, column, descend))
        expressions: dict[tuple[str, str, int], str] = {}
        if any((r[4] or "").startswith("FUNCTION-BASED") for r in rows):
            for owner, name, position, expression in self._query(
                f"SELECT index_owner, index_name, column_position, column_expression FROM {self.p}_IND_EXPRESSIONS "
                f"WHERE table_owner IN {owners}",
                binds,
            ):
                expressions[(owner, name, position)] = _unquote(expression or "")

        for owner, name, table_owner, table_name, index_type, uniqueness, status, partitioned in rows:
            table = tables.get((table_owner, table_name))
            if table is None:
                continue
            keys = [
                text + (" DESC" if descend == "DESC" else "")
                for position, column, descend in columns.get((owner, name), [])
                for text in [expressions.get((owner, name, position), column)]
            ]
            pk = table.primary_key or {}
            table.indexes.append({
                "name": name if owner == table_owner else f"{owner}.{name}",
                "columns": keys, "include": [],
                "unique": uniqueness == "UNIQUE",
                "primary": tuple(pk.get("index") or ()) == (owner, name),
                "method": (index_type or "").lower(),
                "status": status, "partitioned": partitioned == "YES",
            })
        for table in tables.values():
            for key in [table.primary_key, *table.unique_keys]:
                if key:
                    key.pop("index", None)

    def synonyms(self, schemas: Sequence[str]) -> dict[TableKey, TableKey]:
        """Private and public synonyms that point at tables in the selected schemas."""
        targets, binds = self._in("o", schemas)
        rows = self._query(
            f"SELECT owner, synonym_name, table_owner, table_name FROM {self.p}_SYNONYMS "
            f"WHERE table_owner IN {targets} AND db_link IS NULL",
            binds,
        )
        return {(o, n): (to, tn) for o, n, to, tn in rows}


def _unquote(expression: str) -> str:
    """'"STATUS"' (how Oracle stores a DESC index column) -> 'STATUS'."""
    text = expression.strip()
    if len(text) > 2 and text[0] == text[-1] == '"' and '"' not in text[1:-1]:
        return text[1:-1]
    return text


def _column_type(data_type, length, precision, scale, char_length, char_used) -> str:
    if data_type in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        unit = " CHAR" if char_used == "C" and data_type in ("VARCHAR2", "CHAR") else ""
        return f"{data_type}({char_length or length}{unit})"
    if data_type == "NUMBER" and precision is not None:
        return f"NUMBER({precision},{scale})" if scale else f"NUMBER({precision})"
    if data_type == "RAW":
        return f"RAW({length})"
    return data_type


def read_catalog(
    endpoint: Endpoint, schemas: Sequence[str] = (), *, user_grants: bool,
    progress: Callable[[float, str], None] = lambda *_: None,
) -> Catalog:
    with oracle.connect(endpoint) as conn:
        version = conn.version
        reader = CatalogDictionary(conn, "ALL" if user_grants else "DBA")
        warnings: list[str] = []
        progress(0.08, "Resolving schemas")
        if schemas:
            present, missing = reader.resolve_schemas(schemas)
            warnings += [f"Schema not found or not visible to this account: {s}" for s in missing]
        else:
            present = reader.default_schemas()
        if not present:
            raise ValueError("No schemas to read" + (f": not found: {', '.join(schemas)}" if schemas else ""))

        progress(0.15, f"Reading tables and columns in {len(present)} schema(s)")
        tables = reader.tables(present)
        progress(0.35, "Reading keys and foreign keys")
        relations = reader.constraints(present, tables)
        progress(0.4, "Reading indexes")
        reader.indexes(present, tables)

        progress(0.45, "Reading PL/SQL source")
        objects = [o for o in reader.objects(present) if o.object_type in _CODE_TYPES]
        sources = reader.plsql_source(present)
        triggers = reader.trigger_metadata(present)
        routines = reader.routines(present)

        progress(0.6, "Reading dependencies")
        dependencies = reader.dependencies(present)
        synonyms: dict[TableKey, TableKey] = {}
        if reader.p == "DBA":
            synonyms = reader.synonyms(present)
        else:
            hidden_bodies = sum(
                1 for o in objects if o.object_type == "PACKAGE" and (o.owner, "PACKAGE BODY", o.name) not in sources
            )
            warnings.append(
                "This account reads the ALL_* dictionary views, so only tables and code it has privileges on are "
                "listed" + (f", and {hidden_bodies} package bodies are not visible (only their specs)" if hidden_bodies else "")
                + ". Granting SELECT ANY DICTIONARY lets the explorer read DBA_* views and see everything."
            )
            warnings.append(
                "Synonyms were not resolved: this account cannot read DBA_SYNONYMS (ALL_SYNONYMS is too slow to "
                "query in bulk). Code that reaches tables through synonyms is still linked by the dependency views."
            )

    refs: dict[tuple[str, str, str], set[TableKey]] = defaultdict(set)
    for d in dependencies:
        if d.ref_type not in _TABLE_REF_TYPES or d.ref_link:
            continue
        target = (d.ref_owner, d.ref_name)
        hops = 0
        while d.ref_type == "SYNONYM" and target in synonyms and hops < 10:  # synonyms of synonyms
            target, hops = synonyms[target], hops + 1
        if target in tables:
            obj_type = "PACKAGE" if d.object_type == "PACKAGE BODY" else d.object_type
            refs[(d.owner, obj_type, d.name)].add(target)
            if obj_type in ("VIEW", "MATERIALIZED VIEW") and (d.owner, d.name) in tables and target != (d.owner, d.name):
                relations.append(Relation(kind="view", name=None, from_table=(d.owner, d.name), to_table=target))

    standalone = {(r.owner, r.name): r for r in routines if not r.package_name}
    members: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in routines:
        if r.package_name:
            members[(r.owner, r.package_name)].append(r.signature)

    code: list[Code] = []
    by_key = {o.key: o for o in objects}
    for obj in objects:
        if obj.object_type == "PACKAGE BODY":
            continue
        key = (obj.owner, obj.object_type, obj.name)
        source = sources.get(key)
        entry = Code(schema=obj.owner, name=obj.name, kind=obj.object_type.lower(), status=obj.status,
                     language="PL/SQL", dictionary_refs=refs.get(key, set()))
        if obj.object_type == "PACKAGE":
            body = by_key.get((obj.owner, "PACKAGE BODY", obj.name))
            body_source = sources.get((obj.owner, "PACKAGE BODY", obj.name))
            if body_source:
                entry.package_body_from = len((source or "") + "\n/\n\n")
                source = (source or "") + "\n/\n\n" + body_source
            if body is not None and body.status != "VALID":
                entry.status = body.status
            entry.extra["members"] = members.get((obj.owner, obj.name), [])
            entry.signature = obj.name
        elif obj.object_type == "TRIGGER":
            meta = triggers.get(key, {})
            table = meta.get("table")
            if table and "." in table:
                owner, _, name = table.partition(".")
                entry.table = (owner, name) if (owner, name) in tables else None
            entry.signature = f"{obj.name} ON {table}" if table else obj.name
            entry.extra = {"timing": meta.get("timing"), "events": meta.get("events"), "enabled": meta.get("enabled"),
                           "base_object_type": meta.get("base_object_type")}
        else:
            routine = standalone.get((obj.owner, obj.name))
            entry.signature = routine.signature if routine else obj.name
        entry.source = source
        code.append(entry)

    return Catalog(
        server_version=version, dictionary=reader.p, schemas=present, fold="upper", search_path=[],
        tables=list(tables.values()), relations=relations, code=code, synonyms=synonyms, warnings=warnings,
    )
