"""Schema explorer end to end: snapshot a real PostgreSQL database, then browse it via the API."""

import uuid

import psycopg
import pytest
from psycopg import sql

from app.config import get_settings
from app.jobs import execute_job
from app.models import DbKind, Job, JobStatus
from app.services.endpoints import Endpoint
from tests.conftest import endpoint_payload

DDL = """
CREATE SCHEMA shop;
CREATE TABLE shop.customers (id int PRIMARY KEY, email text UNIQUE, name text NOT NULL);
COMMENT ON TABLE shop.customers IS 'People who order';
CREATE TABLE shop.orders (id int PRIMARY KEY, customer_id int REFERENCES shop.customers ON DELETE CASCADE,
                          placed date NOT NULL DEFAULT current_date) ;
CREATE TABLE shop.order_lines (order_id int, line_no int, amount numeric, PRIMARY KEY (order_id, line_no),
                               FOREIGN KEY (order_id) REFERENCES shop.orders (id));
CREATE TABLE shop.events (id bigint, at date NOT NULL, order_id int) PARTITION BY RANGE (at);
CREATE TABLE shop.events_2026 PARTITION OF shop.events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE shop.events_2027 PARTITION OF shop.events FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');
CREATE VIEW shop.big_orders AS SELECT o.id, c.name FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id;
CREATE PROCEDURE shop.close_order(p_id int) LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM order_lines WHERE order_id = p_id;
  UPDATE shop.orders SET placed = NULL WHERE id = p_id;
  EXECUTE 'INSERT INTO shop.events (id, at) VALUES (1, current_date)';
END $$;
CREATE FUNCTION shop.audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO shop.events (id, at, order_id) VALUES (1, current_date, NEW.id);
  RETURN NEW;
END $$;
CREATE TRIGGER orders_audit AFTER INSERT OR UPDATE ON shop.orders FOR EACH ROW EXECUTE FUNCTION shop.audit();
CREATE INDEX customers_name_lower ON shop.customers (lower(name)) INCLUDE (email);
CREATE INDEX events_order ON shop.events (order_id) WHERE order_id IS NOT NULL;
CREATE TRIGGER events_2026_only BEFORE DELETE ON shop.events_2026 FOR EACH ROW EXECUTE FUNCTION shop.audit();
"""


@pytest.fixture
def shop_db():
    s = get_settings()
    if not s.scratch_db_password:
        pytest.skip("scratch-pg is not configured")
    name = f"shop_{uuid.uuid4().hex[:8]}"
    admin = dict(host=s.scratch_db_host, port=s.scratch_db_port, user=s.scratch_db_user,
                 password=s.scratch_db_password, autocommit=True)
    with psycopg.connect(dbname=s.scratch_db_name, **admin) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    with psycopg.connect(dbname=name, **admin) as conn:
        conn.execute(DDL)
    yield Endpoint(kind=DbKind.POSTGRES, host=s.scratch_db_host, port=s.scratch_db_port, database=name,
                   username=s.scratch_db_user, password=s.scratch_db_password, options={"sslmode": "disable"})
    with psycopg.connect(dbname=s.scratch_db_name, **admin) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def snapshot(client, session_factory, connection_id, schemas=("shop",)):
    resp = client.post(f"/api/connections/{connection_id}/catalog-snapshots", json={"schemas": list(schemas)})
    assert resp.status_code == 202, resp.text
    snap = resp.json()
    execute_job(uuid.UUID(snap["job_id"]), session_factory)
    with session_factory() as s:
        job = s.get(Job, uuid.UUID(snap["job_id"]))
        assert job.status is JobStatus.SUCCEEDED, job.error
    return client.get(f"/api/catalog-snapshots/{snap['id']}").json()


def tables_by_name(client, snapshot_id, **params):
    page = client.get(f"/api/catalog-snapshots/{snapshot_id}/tables", params=params).json()
    return {t["name"]: t for t in page["items"]}


def test_snapshot_lists_tables_relationships_and_code(client, session_factory, shop_db):
    conn = client.post("/api/connections", json=endpoint_payload(shop_db, "shop")).json()
    snap = snapshot(client, session_factory, conn["id"])

    assert snap["status"] == "succeeded" and snap["schemas"] == ["shop"]
    assert snap["summary"]["tables"] == 4 and snap["summary"]["views"] == 1
    assert snap["summary"]["foreign_keys"] == 2
    assert snap["summary"]["code"] == {"procedure": 1, "function": 1, "trigger": 2}

    tables = tables_by_name(client, snap["id"])
    assert "events_2026" not in tables  # partitions fold into their parent
    assert tables["events"]["kind"] == "partitioned table" and tables["events"]["partitions"] == 2
    assert tables_by_name(client, snap["id"], q="ORDER")
    assert set(tables_by_name(client, snap["id"], kind="view")) == {"big_orders"}

    orders = client.get(f"/api/catalog-tables/{tables['orders']['id']}").json()
    assert orders["primary_key"]["columns"] == ["id"]
    assert [c["name"] for c in orders["columns"]] == ["id", "customer_id", "placed"]
    ref = orders["references"][0]
    assert ref["other_label"] == "shop.customers" and ref["columns"] == ["customer_id"]
    assert ref["other_columns"] == ["id"] and ref["details"]["on_delete"] == "CASCADE"
    assert ref["other_table_id"] == tables["customers"]["id"]
    incoming = orders["referenced_by"][0]
    assert incoming["other_label"] == "shop.order_lines" and incoming["columns"] == ["id"]
    assert [v["other_label"] for v in orders["read_by_views"]] == ["shop.big_orders"]

    used = {u["code"]["name"]: u for u in orders["used_by"]}
    assert used["close_order"]["operations"] == ["update"]
    assert [t["name"] for t in orders["triggers"]] == ["orders_audit"]
    trigger = orders["triggers"][0]
    assert trigger["extra"]["timing"] == "AFTER" and trigger["extra"]["events"] == "INSERT OR UPDATE"

    events = client.get(f"/api/catalog-tables/{tables['events']['id']}").json()
    by_code = {u["code"]["name"]: u for u in events["used_by"]}
    assert by_code["close_order"]["found_by"] == ["dynamic_sql"]
    assert by_code["audit"]["operations"] == ["insert"]
    # A trigger created directly on a partition is listed on the partitioned table.
    assert [t["extra"]["partition"] for t in events["triggers"]] == ["events_2026"]

    view = client.get(f"/api/catalog-tables/{tables['big_orders']['id']}").json()
    assert {r["other_label"] for r in view["reads_from"]} == {"shop.orders", "shop.customers"}

    proc = client.get(f"/api/catalog-code/{used['close_order']['code']['id']}").json()
    assert "DELETE FROM order_lines" in proc["source"]
    assert {u["table"]["name"]: u["operations"] for u in proc["uses"]} == {
        "events": ["insert"], "order_lines": ["delete"], "orders": ["update"],
    }


def test_new_snapshot_replaces_the_previous_one(client, session_factory, shop_db):
    conn = client.post("/api/connections", json=endpoint_payload(shop_db, "shop")).json()
    first = snapshot(client, session_factory, conn["id"])
    second = snapshot(client, session_factory, conn["id"])
    listed = client.get(f"/api/connections/{conn['id']}/catalog-snapshots").json()
    assert [s["id"] for s in listed] == [second["id"]]
    assert client.get(f"/api/catalog-snapshots/{first['id']}").status_code == 404


def test_missing_schema_fails_the_snapshot_clearly(client, session_factory, shop_db):
    conn = client.post("/api/connections", json=endpoint_payload(shop_db, "shop")).json()
    resp = client.post(f"/api/connections/{conn['id']}/catalog-snapshots", json={"schemas": ["nope"]})
    execute_job(uuid.UUID(resp.json()["job_id"]), session_factory)
    snap = client.get(f"/api/catalog-snapshots/{resp.json()['id']}").json()
    assert snap["status"] == "failed" and "not found: nope" in snap["error"]


def test_bad_schema_names_are_rejected(client, app_db_endpoint):
    conn = client.post("/api/connections", json=endpoint_payload(app_db_endpoint, "pg")).json()
    resp = client.post(f"/api/connections/{conn['id']}/catalog-snapshots", json={"schemas": ["a b"]})
    assert resp.status_code == 422


def test_indexes_and_unindexed_foreign_keys(client, session_factory, shop_db):
    conn = client.post("/api/connections", json=endpoint_payload(shop_db, "shop")).json()
    snap = snapshot(client, session_factory, conn["id"])
    assert snap["summary"]["unindexed_foreign_keys"] == 1

    tables = tables_by_name(client, snap["id"])
    customers = client.get(f"/api/catalog-tables/{tables['customers']['id']}").json()
    by_name = {ix["name"]: ix for ix in customers["indexes"]}
    assert by_name["customers_pkey"]["primary"] and by_name["customers_pkey"]["columns"] == ["id"]
    assert by_name["customers_email_key"]["unique"] and not by_name["customers_email_key"]["primary"]
    assert by_name["customers_name_lower"]["columns"] == ["lower(name)"]
    assert by_name["customers_name_lower"]["include"] == ["email"]

    # orders.customer_id has no index; order_lines.order_id leads its primary key.
    orders = client.get(f"/api/catalog-tables/{tables['orders']['id']}").json()
    assert orders["references"][0]["details"]["indexed"] is False
    assert orders["unindexed_fk_count"] == 1
    lines = client.get(f"/api/catalog-tables/{tables['order_lines']['id']}").json()
    assert lines["references"][0]["details"]["indexed"] is True
    assert set(tables_by_name(client, snap["id"], indexes="unindexed_fk")) == {"orders"}
    assert set(tables_by_name(client, snap["id"], indexes="none")) == set()

    events = client.get(f"/api/catalog-tables/{tables['events']['id']}").json()
    assert events["indexes"][0]["predicate"] == "(order_id IS NOT NULL)"


def test_code_search_and_filters(client, session_factory, shop_db):
    conn = client.post("/api/connections", json=endpoint_payload(shop_db, "shop")).json()
    snap = snapshot(client, session_factory, conn["id"])

    def names(**params):
        page = client.get(f"/api/catalog-snapshots/{snap['id']}/code", params=params).json()
        return sorted(f"{c['kind']}:{c['name']}" for c in page["items"])

    assert names() == ["function:audit", "procedure:close_order", "trigger:events_2026_only", "trigger:orders_audit"]
    assert names(kind="trigger") == ["trigger:events_2026_only", "trigger:orders_audit"]
    assert names(kind="trigger_function") == ["function:audit"]
    assert names(kind="function") == []
    assert names(q="CLOSE") == ["procedure:close_order"]
    assert names(q="order_lines") == []
    assert names(q="order_lines", in_source=True) == ["procedure:close_order"]
    assert names(access="writes") == ["function:audit", "procedure:close_order"]
    # PostgreSQL trigger objects carry no table links of their own (their function does).
    assert names(access="no_tables") == ["trigger:events_2026_only", "trigger:orders_audit"]

    trigger = next(c for c in client.get(f"/api/catalog-snapshots/{snap['id']}/code", params={"q": "orders_audit"}).json()["items"])
    detail = client.get(f"/api/catalog-code/{trigger['id']}").json()
    assert detail["table"]["name"] == "orders" and detail["snapshot_id"] == snap["id"]


def test_bulk_details_match_the_single_table_view(client, session_factory, shop_db):
    """The all-tables comparison must see exactly what the side-by-side page sees."""
    from app.catalog.bulk import snapshot_details

    conn = client.post("/api/connections", json=endpoint_payload(shop_db, "shop")).json()
    snap = snapshot(client, session_factory, conn["id"])
    with session_factory() as s:
        bulk = snapshot_details(s, uuid.UUID(snap["id"]))
    assert len(bulk) == 5
    for table_id, detail in bulk.items():
        assert detail == client.get(f"/api/catalog-tables/{table_id}").json(), detail["name"]


def test_overview_compares_every_table(client, session_factory, shop_db):
    conn = client.post("/api/connections", json=endpoint_payload(shop_db, "shop")).json()
    first = snapshot(client, session_factory, conn["id"])
    ep = shop_db
    with psycopg.connect(host=ep.host, port=ep.port, user=ep.username, password=ep.password,
                         dbname=ep.database, autocommit=True) as pg:
        pg.execute("ALTER TABLE shop.customers ALTER COLUMN email TYPE varchar(20)")
        pg.execute("CREATE TABLE shop.only_here (id int)")
    # A second connection to the same database, so both snapshots survive.
    other = client.post("/api/connections", json=endpoint_payload(shop_db, "shop2")).json()
    second = snapshot(client, session_factory, other["id"])

    result = client.get("/api/catalog-compare/overview",
                        params={"left_snapshot": first["id"], "right_snapshot": second["id"]}).json()
    rows = {(r["left"] or r["right"])["key"]: r for r in result["tables"]}
    assert rows["shop.customers"]["status"] == "error" and rows["shop.customers"]["sections"]["columns"] == {"error": 1}
    assert "columns:" in rows["shop.customers"]["headline"]
    assert rows["shop.only_here"]["left"] is None and rows["shop.only_here"]["missing"] == "shop"
    assert rows["shop.orders"]["status"] in ("same", "info")
    assert result["summary"]["only_right"] == 1 and result["summary"]["with_problems"] == 1
    assert result["tables"][0]["status"] == "error"  # problems first
