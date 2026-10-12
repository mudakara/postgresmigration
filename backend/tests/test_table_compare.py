"""Side-by-side table comparison rules (app.catalog.table_compare)."""

import pytest

from app.catalog.table_compare import compare, compare_types

NAMES = ("Oracle", "PG")


@pytest.mark.parametrize("oracle, pg, status", [
    ("NUMBER(9)", "integer", "equivalent"),
    ("NUMBER(10)", "integer", "error"),  # 10 digits don't fit in integer
    ("NUMBER(12)", "bigint", "equivalent"),
    ("NUMBER(12,2)", "numeric(12,2)", "equivalent"),
    ("NUMBER(12,2)", "numeric(12,1)", "error"),
    ("NUMBER(12,2)", "integer", "error"),
    ("NUMBER", "numeric", "equivalent"),
    ("NUMBER", "bigint", "warning"),
    ("NUMBER(10)", "double precision", "warning"),
    ("VARCHAR2(1)", "character varying(1)", "equivalent"),  # the 2 in VARCHAR2 is not a length
    ("VARCHAR2(50 CHAR)", "character varying(50)", "equivalent"),
    ("VARCHAR2(50)", "character varying(30)", "error"),
    ("VARCHAR2(50)", "text", "equivalent"),
    ("CHAR(1)", "character(1)", "equivalent"),
    ("CHAR(1)", "boolean", "warning"),
    ("DATE", "timestamp(0) without time zone", "equivalent"),
    ("DATE", "date", "warning"),  # Oracle DATE has a time of day
    ("TIMESTAMP(6)", "timestamp without time zone", "equivalent"),
    ("TIMESTAMP(6) WITH TIME ZONE", "timestamp with time zone", "equivalent"),
    ("CLOB", "text", "equivalent"),
    ("BLOB", "bytea", "equivalent"),
    ("NUMBER(10)", "text", "error"),
])
def test_oracle_to_postgres_types(oracle, pg, status):
    assert compare_types(oracle, "oracle", pg, "postgres")[0] == status
    # Either side can be on the left.
    assert compare_types(pg, "postgres", oracle, "oracle")[0] == status


def test_same_dialect_types_compare_exactly():
    assert compare_types("integer", "postgres", "integer", "postgres")[0] == "same"
    assert compare_types("integer", "postgres", "bigint", "postgres")[0] == "error"


def table(**over):
    base = {
        "kind": "table", "partitions": 0, "row_estimate": 100, "comment": None, "columns": [],
        "primary_key": None, "unique_keys": [], "indexes": [], "references": [], "referenced_by": [],
        "read_by_views": [], "triggers": [], "used_by": [],
    }
    return {**base, **over}


def code(id_, name, kind="procedure", schema="APP"):
    return {"id": id_, "name": name, "kind": kind, "schema_name": schema, "signature": name, "extra": {}}


def usage(c, ops, subprograms=(), member_ops=None):
    return {"code": c, "operations": ops, "found_by": ["code"], "subprograms": list(subprograms), "lines": [1],
            "member_operations": member_ops or {}}


def rows(result, section):
    return [(r["status"], r["message"]) for r in result["sections"][section]]


def test_full_comparison():
    oracle = table(
        columns=[{"name": "ID", "type": "NUMBER(10)", "nullable": False},
                 {"name": "AMOUNT", "type": "NUMBER(12,2)", "nullable": True},
                 {"name": "LEGACY", "type": "VARCHAR2(10)", "nullable": True}],
        primary_key={"name": "PK_T", "columns": ["ID"]},
        unique_keys=[{"name": "UQ_T", "columns": ["AMOUNT"]}],
        indexes=[{"name": "PK_T", "columns": ["ID"], "unique": True, "primary": True},
                 {"name": "IX_AMOUNT", "columns": ["AMOUNT"], "unique": False}],
        references=[{"name": "FK_C", "columns": ["ID"], "other_label": "APP.CUST", "other_columns": ["ID"],
                     "details": {"on_delete": "CASCADE"}}],
        triggers=[{"name": "TRG_T_BI", "extra": {"timing": "BEFORE EACH ROW", "events": "INSERT", "enabled": True}},
                  {"name": "TRG_AUDIT", "extra": {"timing": "AFTER EACH ROW", "events": "INSERT OR UPDATE", "enabled": True}}],
        used_by=[usage(code("o1", "PKG", "package"), ["select", "delete"], ["load", "purge"],
                       {"load": ["select"], "purge": ["delete"]}),
                 usage(code("o2", "GONE"), ["select"])],
    )
    pg = table(
        columns=[{"name": "id", "type": "integer", "nullable": False},
                 {"name": "amount", "type": "numeric(12,2)", "nullable": False},
                 {"name": "extra", "type": "text", "nullable": True}],
        primary_key={"name": "pk_t", "columns": ["id"]},
        indexes=[{"name": "pk_t", "columns": ["id"], "unique": True, "primary": True}],
        references=[{"name": "fk_c", "columns": ["id"], "other_label": "app.cust", "other_columns": ["id"],
                     "details": {"on_delete": "NO ACTION"}}],
        triggers=[{"name": "trg_audit", "extra": {"timing": "AFTER", "level": "ROW", "events": "INSERT", "enabled": True}}],
        used_by=[usage(code("p1", "load", "function", "pkg"), ["select"]),
                 usage(code("p2", "purge", "function", "pkg"), ["select"])],
    )
    result = compare(oracle, pg, left_dialect="oracle", right_dialect="postgres", names=NAMES,
                     right_code_names={"gone", "app.gone"})

    cols = {r["left"]["name"] if r["left"] else r["right"]["name"]: r for r in result["sections"]["columns"]}
    assert cols["ID"]["status"] == "error" and "overflow" in cols["ID"]["message"]
    assert cols["AMOUNT"]["status"] == "warning" and "NOT NULL on PG only" in cols["AMOUNT"]["message"]
    assert cols["LEGACY"]["status"] == "error" and cols["LEGACY"]["right"] is None
    assert cols["extra"]["status"] == "warning" and cols["extra"]["left"] is None

    # Names match ignoring case (Oracle PK_T is PostgreSQL pk_t).
    assert rows(result, "keys") == [("error", "Unique key missing in PG"), ("same", "")]  # problems first
    idx = rows(result, "indexes")
    assert idx == [("warning", idx[0][1]), ("same", "")]  # IX_AMOUNT missing (a performance risk) sorts first
    assert rows(result, "foreign_keys") == [("warning", "ON DELETE CASCADE vs NO ACTION")]

    trg = {r["left"]["name"]: r for r in result["sections"]["triggers"]}
    assert trg["TRG_T_BI"]["status"] == "error"  # its logic is missing
    assert trg["TRG_AUDIT"]["status"] == "error" and "Events" in trg["TRG_AUDIT"]["message"]

    code_rows = {r["left"]["name"]: r for r in result["sections"]["code"] if r["left"]}
    assert code_rows["PKG.load"]["status"] == "same"  # matched to pkg.load, both only SELECT
    assert code_rows["PKG.purge"]["status"] == "error" and "no longer DELETE" in code_rows["PKG.purge"]["message"]
    assert code_rows["GONE"]["status"] == "error" and "doesn't use this table" in code_rows["GONE"]["message"]
    assert result["counts"]["error"] >= 7


def test_package_level_operations_are_not_compared_per_member():
    oracle = table(used_by=[usage(code("o1", "PKG", "package"), ["select", "delete"], ["load"])])  # old snapshot: no member ops
    pg = table(used_by=[usage(code("p1", "load", "function", "pkg"), ["select"])])
    result = compare(oracle, pg, left_dialect="oracle", right_dialect="postgres", names=NAMES, right_code_names=set())
    assert result["sections"]["code"][0]["status"] == "same"
    assert "Refresh the catalog" in result["sections"]["code"][0]["message"]
