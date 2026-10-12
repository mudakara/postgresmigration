"""How code is linked to the tables it uses (app.catalog.references / link_code)."""

from app.catalog.model import Catalog, Code, Table
from app.catalog.references import Resolver, find_usages
from app.catalog.service import has_supporting_index, link_code

PG_TABLES = [("app", "orders"), ("app", "order_lines"), ("app", "customers"), ("audit", "log"), ("app", "status")]


def pg_usages(source: str, schema: str = "app", search_path=("public",)):
    resolver = Resolver(PG_TABLES, fold=str.lower, search_path=search_path)
    return {k: (sorted(u.operations), sorted(u.found_by)) for k, u in find_usages(source, resolver.bind(schema)).items()}


def test_operations_are_classified():
    found = pg_usages("""
        CREATE FUNCTION app.f() RETURNS void LANGUAGE plpgsql AS $function$
        BEGIN
          INSERT INTO audit.log (msg) SELECT o.id FROM orders o JOIN order_lines l ON l.order_id = o.id;
          UPDATE Customers SET name = 'x' FROM app.status s WHERE s.id = 1;
          DELETE FROM order_lines WHERE id IN (SELECT id FROM orders FOR UPDATE);
        END $function$""")
    assert found == {
        ("audit", "log"): (["insert"], ["code"]),
        ("app", "orders"): (["select"], ["code"]),
        ("app", "order_lines"): (["delete", "select"], ["code"]),
        ("app", "customers"): (["update"], ["code"]),
        ("app", "status"): (["select"], ["code"]),
    }


def test_comma_joins_aliases_and_rowtype():
    found = pg_usages("""
        DECLARE r orders%ROWTYPE;
        BEGIN
          SELECT * INTO r FROM orders o, app.customers AS c, order_lines WHERE o.id = c.id;
        END""")
    assert found[("app", "orders")][0] == ["select", "type"]
    assert ("app", "customers") in found and ("app", "order_lines") in found


def test_non_table_uses_of_keywords_are_ignored():
    found = pg_usages("""
        -- SELECT * FROM orders  (a comment)
        SELECT extract(year FROM status), x IS DISTINCT FROM customers, trim(both ' ' FROM order_lines)
        FROM generate_series(1, 3);
        INSERT INTO t VALUES (1) ON CONFLICT (id) DO UPDATE SET status = 1;
        CREATE TRIGGER t AFTER INSERT OR UPDATE OR DELETE ON orders FOR EACH ROW EXECUTE FUNCTION f();""")
    assert found == {}


def test_dynamic_sql_inside_strings_is_found():
    found = pg_usages("""
        BEGIN
          EXECUTE 'TRUNCATE TABLE app.orders';
          EXECUTE 'DELETE FROM ' || 'audit.log WHERE id = $1' USING 1;
          RAISE NOTICE 'nothing about customers here';
        END""")
    assert found == {("app", "orders"): (["truncate"], ["dynamic_sql"]), ("audit", "log"): (["delete"], ["dynamic_sql"])}


def test_unqualified_names_follow_the_code_schema_then_search_path():
    tables = [("a", "t"), ("b", "t"), ("b", "only_in_b")]
    resolver = Resolver(tables, fold=str.lower, search_path=["b"])
    assert set(find_usages("SELECT 1 FROM t", resolver.bind("a"))) == {("a", "t")}
    assert set(find_usages("SELECT 1 FROM t", resolver.bind("c"))) == {("b", "t")}
    ambiguous = Resolver(tables, fold=str.lower)
    assert set(find_usages("SELECT 1 FROM t, only_in_b", ambiguous.bind("c"))) == {("b", "only_in_b")}
    # Quoted names keep their case.
    mixed = Resolver([("a", "Mixed")], fold=str.lower)
    assert set(find_usages('SELECT 1 FROM "Mixed"', mixed.bind("a"))) == {("a", "Mixed")}
    assert set(find_usages("SELECT 1 FROM mixed", mixed.bind("a"))) == set()


def test_oracle_links_combine_dictionary_and_source():
    spec = "CREATE OR REPLACE PACKAGE pkg AS PROCEDURE load; FUNCTION total RETURN NUMBER; END pkg;"
    body = """CREATE OR REPLACE PACKAGE BODY pkg AS
  PROCEDURE load IS
  BEGIN
    MERGE INTO orders o USING staging s ON (o.id = s.id)
      WHEN MATCHED THEN UPDATE SET o.amount = s.amount;
    DELETE customers WHERE id = 0;
    EXECUTE IMMEDIATE 'DELETE FROM app_log';
  END load;
  FUNCTION total RETURN NUMBER IS
    n NUMBER;
    v_cursor orders%ROWTYPE;
  BEGIN
    SELECT COUNT(*) INTO n FROM orders;
    RETURN n;
  END total;
END pkg;"""
    tables = [Table("APP", n, "table") for n in ("ORDERS", "STAGING", "CUSTOMERS", "APP_LOG", "UNUSED")]
    package = Code(
        "APP", "PKG", "package", source=spec + "\n/\n\n" + body, package_body_from=len(spec + "\n/\n\n"),
        # The dictionary only knows static references; CUSTOMERS is reached through a
        # synonym in real life, here it is simply missing to show it is then dropped.
        dictionary_refs={("APP", "ORDERS"), ("APP", "STAGING"), ("APP", "UNUSED")},
    )
    catalog = Catalog("19c", "ALL", ["APP"], "upper", [], tables, [], [package])
    links = {link.table[1]: link for link in link_code(catalog)[0]}

    assert links["ORDERS"].operations == ["select", "merge", "type"]
    assert links["ORDERS"].found_by == ["code", "dictionary"]
    assert links["ORDERS"].subprograms == ["load", "total"]
    assert links["STAGING"].operations == ["select"] and links["STAGING"].subprograms == ["load"]
    assert links["APP_LOG"].found_by == ["dynamic_sql"] and links["APP_LOG"].operations == ["delete"]
    assert "CUSTOMERS" not in links  # a static match Oracle does not confirm
    assert links["UNUSED"].found_by == ["dictionary"] and links["UNUSED"].operations == []


def test_supporting_index_must_lead_with_the_foreign_key_columns():
    indexes = [{"columns": ['"Order Id"', "line_no"]}, {"columns": ["b", "a", "c"]}, {"columns": ["z"], "predicate": "z > 0"}]
    assert has_supporting_index(["Order Id"], indexes)
    assert has_supporting_index(["a", "b"], indexes)  # order within the prefix doesn't matter
    assert not has_supporting_index(["line_no"], indexes)  # second column only
    assert not has_supporting_index(["z"], indexes)  # partial index
    assert has_supporting_index(["STATUS"], [{"columns": ["STATUS DESC"]}])


def test_synonyms_only_resolve_to_tables_and_views():
    tables = [("APP", "ORDERS"), ("HR", "STAFF")]
    synonyms = {
        ("PUBLIC", "INVOICE"): ("FTS", "INVOICE"),  # a sequence: not a table
        ("PUBLIC", "ORD"): ("APP", "ORD_SYN"),  # a synonym of a synonym
        ("APP", "ORD_SYN"): ("APP", "ORDERS"),
        ("APP", "STAFF"): ("HR", "STAFF"),
        ("APP", "LOOP"): ("APP", "LOOP"),
    }
    resolver = Resolver(tables, fold=str.upper, synonyms=synonyms)
    resolve = resolver.bind("APP")
    assert set(find_usages("SELECT 1 FROM invoice", resolve)) == set()
    assert set(find_usages("SELECT 1 FROM ord", resolve)) == {("APP", "ORDERS")}
    assert set(find_usages("SELECT 1 FROM staff", resolve)) == {("HR", "STAFF")}
    assert set(find_usages("SELECT 1 FROM public.invoice, loop", resolve)) == set()

    # End to end: link_code must never produce a link to something that isn't in the catalog.
    code = Code("APP", "P", "procedure", source="BEGIN SELECT invoice.nextval INTO n FROM dual; "
                                               "INSERT INTO invoice_log SELECT * FROM invoice; END;",
                dictionary_refs=set())
    catalog = Catalog("19c", "DBA", ["APP"], "upper", [], [Table(o, n, "table") for o, n in tables], [], [code],
                      synonyms=synonyms)
    assert link_code(catalog) == [[]]
