"""Sequence statistics: ownership per schema and the table-by-table Oracle/PostgreSQL comparison."""

from app.sequences.parse import find_oracle_uses
from app.sequences.stats import _ownership, compare_tables, default_sequence, unlinked_by_name


def test_oracle_trigger_and_insert_uses():
    uses = find_oracle_uses("""BEGIN
      :NEW.ID := SALES.ORDERS_SEQ.NEXTVAL;
      SELECT AUDIT_SEQ.NEXTVAL INTO :NEW.AUDIT_ID FROM DUAL;
      INSERT INTO BILLING.LOG (MSG, LOG_ID) VALUES ('x', LOG_SEQ.NEXTVAL);
    END;""")
    assert {(u.sequence, u.column, u.table, u.via) for u in uses} == {
        (("SALES", "ORDERS_SEQ"), "ID", None, "trigger"),
        (("AUDIT_SEQ",), "AUDIT_ID", None, "trigger"),
        (("LOG_SEQ",), "LOG_ID", ("BILLING", "LOG"), "insert"),
    }


def test_ownership():
    assert _ownership("SALES", {"SALES"}, set()) == ("own", [])
    assert _ownership("SALES", {"SALES", "BILLING"}, set()) == ("other_schema", ["BILLING"])  # an SALES sequence fills a BILLING table
    assert _ownership("SALES", set(), {"BILLING"}) == ("other_schema", ["BILLING"])  # only used from BILLING code
    assert _ownership("SALES", set(), {"SALES"}) == ("code_only", [])
    assert _ownership("SALES", set(), set()) == ("unlinked", [])


def ora(name, last, links, schema="SALES"):
    return {"schema": schema, "name": name, "last_number": last, "used_by": [],
            "links": [{"schema": s, "table": t, "column": c, "max_value": m, "error": None, "via": "trigger", "source": "SALES.TRG"}
                      for s, t, c, m in links]}


def pg(name, next_value, links, schema="sales"):
    return {"schema": schema, "name": name, "next_value": next_value, "status": "ok", "increment": 1, "max_value": 2**63 - 1,
            "links": [{"schema": s, "table": t, "column": c, "max_value": m, "error": None, "via": "default", "source": None}
                      for s, t, c, m in links]}


def test_table_by_table_comparison():
    rows = compare_tables(
        [ora("ORDERS_SEQ", 500, [("SALES", "ORDERS", "ID", 480)]),
         ora("ITEMS_SEQ", 100, [("SALES", "ITEMS", "ID", 90)]),
         ora("GONE_SEQ", 10, [("SALES", "NOPG", "ID", 9)]),
         ora("LOAD_SEQ", 70, [("SALES", "LOAD", "ID", 60)]),
         ora("ISEQ$$_123", 31, [("SALES", "IDENT", "ID", 30)])],
        [pg("orders_seq", 300, [("sales", "orders", "id", 480)]),  # behind data and Oracle
         pg("items_id_seq", 101, [("sales", "items", "id", 90)]),  # fine, different name
         pg("load_seq", 75, [("sales", "load", "id", 40)]),  # fewer rows in PostgreSQL
         pg("ident_id_seq", 31, [("sales", "ident", "id", 30)])],  # Oracle identity: its generated name is expected to differ
    )
    by = {r["table"]: r for r in rows}
    assert by["orders"]["status"] == "error"
    assert {i["severity"] for i in by["orders"]["issues"]} == {"error", "warning"}
    assert by["items"]["status"] == "info" and "Different sequence names" in by["items"]["issues"][0]["message"]
    assert by["nopg"]["status"] == "error" and "No sequence fills this column" in by["nopg"]["issues"][0]["message"]
    assert by["load"]["status"] == "warning" and "may not all be migrated" in by["load"]["issues"][0]["message"]
    assert by["ident"]["status"] == "ok"
    assert rows[0]["status"] == "error"


def test_columns_only_postgres_fills_are_compared_with_the_oracle_sequence_of_that_name():
    rows = compare_tables([ora("APP_SEQ", 900, [])], [pg("app_seq", 500, [("sales", "app", "id", 400)])])
    assert rows[0]["oracle"]["sequence"] == "SALES.APP_SEQ" and rows[0]["status"] == "warning"
    assert any("below Oracle" in i["message"] for i in rows[0]["issues"])


def test_unlinked_sequences_compared_by_name():
    out = unlinked_by_name([ora("FREE_SEQ", 50, []), ora("LOST_SEQ", 5, [])], [pg("free_seq", 20, [])])
    by = {r["sequence"]: r for r in out}
    assert by["SALES.FREE_SEQ"]["status"] == "warning" and "below Oracle" in by["SALES.FREE_SEQ"]["message"]
    assert by["SALES.LOST_SEQ"]["message"] == "No PostgreSQL sequence of that name."


def test_oracle_column_default_sequences():
    assert default_sequence('"BILLING"."ORDERS_SEQ"."NEXTVAL"', "SALES") == ("BILLING", "ORDERS_SEQ")
    assert default_sequence("orders_seq.nextval ", "SALES") == ("SALES", "ORDERS_SEQ")
    assert default_sequence("SYSDATE", "SALES") is None and default_sequence(None, "SALES") is None
