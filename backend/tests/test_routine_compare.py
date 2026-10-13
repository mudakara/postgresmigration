"""Procedure/function comparison for a table pair (app.catalog.routine_compare)."""

import uuid

import psycopg

from app.catalog.routine_compare import (
    _signature_diffs, _table_diffs, align_by_name, parse_oracle_signature, parse_pg_arguments, parse_pg_signature,
)
from app.compare.signatures import compare_signatures
from tests.conftest import endpoint_payload
from tests.test_catalog_explorer import shop_db, snapshot, tables_by_name  # noqa: F401  (fixture)

NAMES = ("Oracle", "PG")


def test_parse_oracle_signature():
    sig = parse_oracle_signature("PKG.LOAD(P_ID IN NUMBER(10,2), P_NAME IN OUT VARCHAR2 DEFAULT, P_CUR OUT REF CURSOR) RETURN NUMBER")
    assert sig["name"] == "PKG.LOAD" and sig["kind"] == "FUNCTION" and sig["return_type"] == "NUMBER"
    assert [(a["name"], a["mode"], a["type"], a["defaulted"]) for a in sig["arguments"]] == [
        ("P_ID", "IN", "NUMBER(10,2)", False), ("P_NAME", "IN OUT", "VARCHAR2", True), ("P_CUR", "OUT", "REF CURSOR", False),
    ]
    assert parse_oracle_signature("P()")["kind"] == "PROCEDURE"


def test_parse_pg_arguments():
    args, defaults = parse_pg_arguments(
        "p_id integer, INOUT results refcursor, OUT n bigint, character varying, p_when timestamp without time zone DEFAULT now()")
    assert [(a["name"], a["mode"], a["type"]) for a in args] == [
        ("p_id", "IN", "integer"), ("results", "IN OUT", "refcursor"), ("n", "OUT", "bigint"),
        (None, "IN", "character varying"), ("p_when", "IN", "timestamp without time zone"),
    ]
    assert defaults == 1
    sig = parse_pg_signature({"signature": "f(p_id integer) RETURNS numeric", "extra": {}, "kind": "function"})
    assert sig["result"] == "numeric" and sig["n_defaults"] is None  # identity args carry no defaults


def test_parameters_are_compared_with_their_namesakes():
    ora = parse_oracle_signature("P(A IN NUMBER, B IN VARCHAR2, R OUT REF CURSOR)")
    pg, _ = parse_pg_arguments("a bigint, INOUT r refcursor, b text")
    aligned, moved = align_by_name(ora["arguments"], pg)
    assert [a["name"] for a in aligned] == ["a", "b", "r"] and moved == ["B", "R"]
    _, raw = compare_signatures("PROCEDURE", ora["arguments"], None, "procedure", aligned, None, 0)
    diffs = _signature_diffs(raw, ora, {"arguments": aligned, "n_defaults": None, "result": None}, "procedure", moved)
    by_title = {d["title"]: d for d in diffs}
    assert by_title["Parameters reordered"]["severity"] == "high" and "B, R" in by_title["Parameters reordered"]["detail"]
    # Ora2Pg's OUT -> INOUT for procedures, and NUMBER -> bigint parameters, are notes.
    assert by_title["Parameter mode changed"]["severity"] == "low"
    assert all(d["severity"] == "low" for d in diffs if d["title"] == "Parameter type changed")
    # Same names in the same order: nothing to align.
    assert align_by_name(ora["arguments"], parse_pg_arguments("a bigint, b text, INOUT r refcursor")[0])[1] == []


def test_table_usage_differences():
    diffs = _table_diffs({"app.orders": {"select", "update"}, "app.log": {"insert"}},
                         {"app.orders": {"select"}, "app.audit": {"insert"}}, NAMES)
    by_title = {d["title"]: d["severity"] for d in diffs}
    assert by_title == {"Also uses app.audit": "low", "No longer uses app.log": "high", "Uses app.orders differently": "high"}


def test_endpoint_compares_routines_of_a_table_pair(client, session_factory, shop_db):
    first_conn = client.post("/api/connections", json=endpoint_payload(shop_db, "a")).json()
    first = snapshot(client, session_factory, first_conn["id"])
    ep = shop_db
    with psycopg.connect(host=ep.host, port=ep.port, user=ep.username, password=ep.password,
                         dbname=ep.database, autocommit=True) as pg:
        pg.execute("""CREATE OR REPLACE PROCEDURE shop.close_order(p_id int) LANGUAGE plpgsql AS $$
                      BEGIN UPDATE shop.orders SET placed = NULL WHERE id = p_id; END $$""")
    second_conn = client.post("/api/connections", json=endpoint_payload(shop_db, "b")).json()
    second = snapshot(client, session_factory, second_conn["id"])
    left = tables_by_name(client, first["id"])["orders"]["id"]
    right = tables_by_name(client, second["id"])["orders"]["id"]

    result = client.get("/api/catalog-compare/routines", params={"left": left, "right": right}).json()
    rows = {r["left"]["name"]: r for r in result["routines"] if r["left"]}
    close = rows["close_order"]
    titles = {d["title"] for d in close["differences"]}
    assert "Source code differs" in titles
    assert "No longer uses shop.order_lines" in titles and "No longer uses shop.events" in titles
    assert close["status"] == "error"
    assert result["routines"][0]["status"] == "error"  # problems first
    assert uuid.UUID(close["right"]["code_id"])


def test_inventory_diff_counts_package_members_and_shared_package_schemas():
    from app.catalog.inventory_diff import _match, _routine_keys

    oracle = [
        {"name": "A.PKG.RUN", "kind": "procedure", "schema": "A", "package": "PKG", "member": "RUN"},
        {"name": "B.PKG.RUN", "kind": "procedure", "schema": "B", "package": "PKG", "member": "RUN"},  # same package name
        {"name": "A.SOLO", "kind": "function", "schema": "A", "package": None, "member": None, "plain": "SOLO"},
        {"name": "A.GONE", "kind": "procedure", "schema": "A", "package": None, "member": None, "plain": "GONE"},
    ]
    pg = [
        {"name": "pkg.run", "kind": "procedure", "schema": "pkg", "package": None, "member": None, "plain": "run"},
        {"name": "a.solo", "kind": "function", "schema": "a", "package": None, "member": None, "plain": "solo"},
        {"name": "a.extra", "kind": "function", "schema": "a", "package": None, "member": None, "plain": "extra"},
    ]
    pairs, only_left, only_right = _match(oracle, pg, _routine_keys, _routine_keys, exclusive=False)
    assert len(pairs) == 3  # both PKG.RUN members find pkg.run
    assert [r["name"] for r in only_left] == ["A.GONE"] and [r["name"] for r in only_right] == ["a.extra"]
