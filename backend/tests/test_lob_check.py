"""LOB data check: SQL for both sides, pairing, and the comparison rules."""

import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import tuple_row

from app.config import get_settings
from app.lobs.check import _unpack, compare, oracle_count_sql, pair, pg_count_sql, pg_unit, read_pg_tables, summarize
from app.models import DbKind
from app.services import postgres
from app.services.endpoints import Endpoint


def test_oracle_sql_counts_lobs_without_reading_them():
    q = oracle_count_sql("SALES", "DOCS", [{"column": "BODY", "type": "BLOB"}, {"column": "NOTES", "type": "LONG"},
                                           {"column": "XML", "type": "XMLTYPE"}], sizes=True)
    # A LONG can only be tested in a WHERE clause, and a subquery can't sit next to aggregates (ORA-00937).
    assert q == ('SELECT a.c0, a.c1, a.c2, a.c3, a.c4, (SELECT COUNT(*) FROM "SALES"."DOCS" WHERE "NOTES" IS NOT NULL), a.c5 FROM '
                 '(SELECT COUNT(*) c0, COUNT(CASE WHEN "BODY" IS NOT NULL THEN 1 END) c1, COUNT(CASE WHEN DBMS_LOB.GETLENGTH("BODY") > 0 '
                 'THEN 1 END) c2, SUM(DBMS_LOB.GETLENGTH("BODY")) c3, MAX(DBMS_LOB.GETLENGTH("BODY")) c4, '
                 'COUNT(CASE WHEN "XML" IS NOT NULL THEN 1 END) c5 FROM "SALES"."DOCS") a')
    assert oracle_count_sql("SALES", "DOCS", [{"column": "BODY", "type": "BLOB"}], sizes=False) == \
        'SELECT COUNT(*), COUNT(CASE WHEN "BODY" IS NOT NULL THEN 1 END) FROM "SALES"."DOCS"'



def test_unpack():
    cols = [{"column": "A", "type": "BLOB", "unit": "bytes"}, {"column": "B", "type": "LONG", "unit": None}]
    rows, out = _unpack((10, 8, 7, 700, 300, 4), cols, lambda c: c["type"] == "BLOB")
    assert rows == 10
    assert out[0] | {} == {"column": "A", "type": "BLOB", "non_null": 8, "non_empty": 7, "total": 700, "max": 300, "unit": "bytes"}
    assert out[1]["non_null"] == 4 and out[1]["non_empty"] is None


def ora_table(name, rows, columns, schema="SALES", pg="sales.{}"):
    return {"schema": schema, "table": name, "num_rows": rows, "postgres_table": pg.format(name.lower()) if pg else None,
            "rows": rows, "error": None, "columns": columns}


def pg_table(name, rows, columns, schema="sales", **extra):
    return {"schema": schema, "table": name, "rows": rows, "error": None, "columns": columns, **extra}


def col(name, typ, nn, ne=None, total=None, mx=None, unit="bytes"):
    return {"column": name, "type": typ, "non_null": nn, "non_empty": ne, "total": total, "max": mx, "unit": unit}


def by_table(rows):
    return {r["table"]: r for r in rows}


def test_comparison_rules():
    rows = by_table(compare(
        [ora_table("SAME", 10, [col("DOC", "BLOB", 9, 9, 900, 200)]),
         ora_table("FEWER_ROWS", 10, [col("DOC", "BLOB", 9, 9, 900, 200)]),
         ora_table("NULLED", 10, [col("DOC", "BLOB", 9, 9, 900, 200)]),
         ora_table("EMPTY_TO_NULL", 10, [col("DOC", "BLOB", 9, 7, 700, 200)]),
         ora_table("TRUNCATED", 10, [col("TXT", "CLOB", 5, 5, 5_000_000, 2_000_000, "chars")]),
         ora_table("NO_COLUMN", 3, [col("DOC", "BLOB", 3, 3, 30, 10)]),
         ora_table("NOT_MIGRATED", 4, [col("DOC", "BLOB", 4, 4, 40, 10)], pg=None),
         ora_table("LONGS", 4, [col("NOTES", "LONG", 4, unit=None)]),
         ora_table("NARROW", 2, [col("TXT", "CLOB", 2, 2, 20, 10, "chars")])],
        [pg_table("same", 10, [col("doc", "bytea", 9, 9, 900, 200)]),
         pg_table("fewer_rows", 8, [col("doc", "bytea", 7, 7, 700, 200)]),
         pg_table("nulled", 10, [col("doc", "bytea", 6, 6, 600, 200)]),
         pg_table("empty_to_null", 10, [col("doc", "bytea", 7, 7, 700, 200)]),
         pg_table("truncated", 10, [col("txt", "text", 5, 5, 4_000_000, 1_047_552, "chars")]),
         pg_table("no_column", 3, []),
         pg_table("longs", 4, [col("notes", "text", 3, unit=None)]),
         pg_table("narrow", 2, [col("txt", "character varying(4000)", 2, 2, 20, 10, "chars")]),
         pg_table("new_docs", 5, [col("doc", "bytea", 5, 5, 50, 10)], only_postgres=True)],
    ))
    assert rows["SAME"]["status"] == "ok"
    fewer = rows["FEWER_ROWS"]
    assert fewer["status"] == "error" and fewer["rows_missing"] == 2 and fewer["values_missing"] == 2
    assert "2 row(s) missing in PostgreSQL" in fewer["issues"][0]["message"]
    assert "(2 row(s) are missing)" in fewer["columns"][0]["issues"][0]["message"]
    assert rows["NULLED"]["status"] == "error" and rows["NULLED"]["values_missing"] == 3
    empty = rows["EMPTY_TO_NULL"]["columns"][0]["issues"]
    assert [i["severity"] for i in empty] == ["info"] and "EMPTY_BLOB" in empty[0]["message"]
    truncated = [i["message"] for i in rows["TRUNCATED"]["columns"][0]["issues"]]
    assert any("Largest value is 1,047,552 characters in PostgreSQL" in m for m in truncated)
    assert any("1,000,000 characters less data" in m and "cut short" in m for m in truncated)
    assert rows["NO_COLUMN"]["columns"][0]["status"] == "error" and rows["NO_COLUMN"]["values_missing"] == 3
    assert rows["NOT_MIGRATED"]["status"] == "error" and rows["NOT_MIGRATED"]["postgres"] is None
    assert rows["LONGS"]["status"] == "error" and "1 value(s) missing" in rows["LONGS"]["columns"][0]["issues"][0]["message"]
    assert rows["NARROW"]["status"] == "warning"  # CLOB into varchar(4000)
    assert rows["NEW_DOCS"]["status"] == "info" and rows["NEW_DOCS"]["oracle"] is None

    ordered = [r["status"] for r in compare(
        [ora_table("SAME", 10, [col("DOC", "BLOB", 9, 9, 900, 200)]), ora_table("NOT_MIGRATED", 4, [col("DOC", "BLOB", 4)], pg=None)],
        [pg_table("same", 10, [col("doc", "bytea", 9, 9, 900, 200)])])]
    assert ordered == ["error", "ok"]  # problems first

    s = summarize(list(rows.values()))
    assert s["oracle_tables"] == 9 and s["missing_tables"] == 1 and s["postgres_only_tables"] == 1
    assert s["rows_missing"] == 2 and s["truncated_columns"] == 1


def test_size_differences_without_truncation_and_longreadlen():
    rows = by_table(compare(
        [ora_table("EDITED", 5, [col("DOC", "BLOB", 5, 5, 1000, 300)]),
         ora_table("CAPPED", 5, [col("DOC", "BLOB", 5, unit="bytes")])],  # sizes not measured in Oracle
        [pg_table("edited", 5, [col("doc", "bytea", 5, 5, 995, 300)]),
         pg_table("capped", 5, [col("doc", "bytea", 5, 5, 3_000_000, 1_047_553)])],
    ))
    edited = rows["EDITED"]["columns"][0]["issues"]
    assert [i["severity"] for i in edited] == ["warning"] and "5 bytes less data" in edited[0]["message"]
    capped = rows["CAPPED"]["columns"][0]["issues"]
    assert capped[0]["kind"] == "truncated" and "LONGREADLEN" in capped[0]["message"]
    assert summarize(list(rows.values()))["truncated_columns"] == 1


def test_failed_size_measurement_is_counted_again_without_sizes():
    import threading

    from app.lobs.check import _measure

    calls, opened = [], []

    def run_one(conn, task, full):
        calls.append((task["table"], full))
        if full and task["table"] == "BIG":
            raise RuntimeError("DPY-4011: the database or network closed the connection")
        task["rows"] = 1

    tasks = [{"table": "BIG", "error": None, "sizes_error": None}, {"table": "SMALL", "error": None, "sizes_error": None}]
    _measure(tasks, lambda: opened.append(1) or object(), run_one, [0], threading.Lock(), workers=1, sizes=True)
    assert calls == [("BIG", True), ("BIG", False), ("SMALL", True)]
    assert len(opened) == 2  # a new session after the failure
    assert tasks[0]["error"] is None and tasks[0]["sizes_error"].startswith("DPY-4011") and tasks[0]["rows"] == 1


def test_uncounted_tables_are_flagged_not_compared():
    o = ora_table("SLOW", None, [{"column": "DOC", "type": "BLOB", "unit": "bytes"}])
    o["error"] = "DPY-4024: call timeout of 300000 ms exceeded"
    row = compare([o], [pg_table("slow", 10, [col("doc", "bytea", 9, 9, 900, 200)])])[0]
    assert row["status"] == "warning" and "could not be counted" in row["issues"][0]["message"]
    assert row["columns"][0]["issues"][0]["message"] == "Not counted."
    assert summarize([row])["not_counted"] == 1


def test_pairing():
    pg = {("sales", "docs"): {"columns": {"doc": {"base": "bytea"}}},
          ("archive", "files"): {"columns": {"f": {"base": "bytea"}}},
          ("sales", "guids"): {"columns": {"id": {"base": "bytea"}}},  # RAW(16) in Oracle: not a LOB table
          ("sales", "new_blobs"): {"columns": {"b": {"base": "bytea"}}},
          ("sales", "plain"): {"columns": {"t": {"base": "text"}}}}
    pairs, pg_only = pair([{"schema": "SALES", "table": "DOCS"}, {"schema": "SALES", "table": "FILES"},
                           {"schema": "SALES", "table": "GONE"}], pg, {("SALES", "GUIDS")})
    assert pairs == {("SALES", "DOCS"): ("sales", "docs"), ("SALES", "FILES"): ("archive", "files"), ("SALES", "GONE"): None}
    assert pg_only == [("sales", "new_blobs")]


DDL = """
CREATE SCHEMA app;
CREATE TABLE app.docs (id int PRIMARY KEY, body bytea, notes text, ref oid);
INSERT INTO app.docs VALUES (1, '\\x0102', 'abc', NULL), (2, '', NULL, 1), (3, NULL, 'déjà', NULL);
"""


@pytest.fixture
def scratch_db():
    s = get_settings()
    if not s.scratch_db_password:
        pytest.skip("scratch-pg is not configured")
    name = f"lob_{uuid.uuid4().hex[:8]}"
    admin = dict(host=s.scratch_db_host, port=s.scratch_db_port, user=s.scratch_db_user, password=s.scratch_db_password, autocommit=True)
    with psycopg.connect(dbname=s.scratch_db_name, **admin) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    with psycopg.connect(dbname=name, **admin) as conn:
        conn.execute(DDL)
    yield Endpoint(kind=DbKind.POSTGRES, host=s.scratch_db_host, port=s.scratch_db_port, database=name,
                   username=s.scratch_db_user, password=s.scratch_db_password, options={"sslmode": "disable"})
    with psycopg.connect(dbname=s.scratch_db_name, **admin) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_postgres_counts(scratch_db):
    with postgres.connect(scratch_db, autocommit=True, row_factory=tuple_row) as conn:
        tables = read_pg_tables(conn, ["app"])
        cols = tables[("app", "docs")]["columns"]
        assert [pg_unit(cols[c]) for c in ("body", "notes", "ref", "id")] == ["bytes", "chars", None, None]
        wanted = [{"column": c, "type": cols[c]["type"], "unit": pg_unit(cols[c])} for c in ("body", "notes", "ref")]
        row = conn.execute(pg_count_sql("app", "docs", wanted, sizes=True)).fetchone()
    rows, measured = _unpack(row, wanted, lambda c: bool(c["unit"]))
    assert rows == 3
    body, notes, ref = measured
    assert (body["non_null"], body["non_empty"], body["total"], body["max"]) == (2, 1, 2, 2)
    assert (notes["non_null"], notes["non_empty"], notes["total"], notes["max"]) == (2, 2, 7, 4)  # characters, not bytes
    assert ref["non_null"] == 1 and ref["total"] is None
