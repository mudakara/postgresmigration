"""plpgsql_check for targets without it, via a structure-only copy on scratch-pg."""

import uuid

import psycopg
import pytest
from psycopg import sql

from app.compare import scratch_compile
from app.config import get_settings
from app.models import DbKind
from app.services.endpoints import Endpoint

TARGET_DDL = """
CREATE SCHEMA app;
CREATE TABLE app.orders (order_id numeric PRIMARY KEY, order_date timestamp, amount numeric);
CREATE FUNCTION app.good(p_id numeric) RETURNS numeric LANGUAGE plpgsql AS $$
BEGIN
  RETURN (SELECT amount FROM app.orders WHERE order_id = p_id);
END $$;
CREATE FUNCTION app.broken(p_days integer) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM app.orders WHERE order_date < localtimestamp - p_days;
  RETURN n;
END $$;
INSERT INTO app.orders VALUES (1, now(), 10);
CREATE TABLE app.events (id bigint, at date NOT NULL) PARTITION BY RANGE (at);
CREATE TABLE app.events_2026 PARTITION OF app.events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE app."Events Odd""Name" PARTITION OF app.events FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');
CREATE TYPE app.status AS ENUM ('NEW', 'SHIPPED');
CREATE TABLE app.shipments (id int PRIMARY KEY, state app.status, label text);
CREATE VIEW app.z_base AS SELECT id, state FROM app.shipments;
CREATE VIEW app.a_on_top AS SELECT id FROM app.z_base WHERE state = 'NEW';
CREATE FUNCTION app.upsert(p_id int) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO app.shipments (id, state) VALUES (p_id, 'NEW')
  ON CONFLICT (id) DO UPDATE SET state = 'SHIPPED';
  PERFORM count(*) FROM app.a_on_top;
END $$;
CREATE FUNCTION app.trg_label() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.no_such_column := 'x';
  RETURN NEW;
END $$;
CREATE TRIGGER trg_label BEFORE INSERT ON app.shipments FOR EACH ROW EXECUTE FUNCTION app.trg_label();
CREATE FUNCTION app.count_events() RETURNS bigint LANGUAGE plpgsql AS $$
BEGIN
  RETURN (SELECT count(*) FROM app.events);
END $$;
"""


@pytest.fixture
def target_without_plpgsql_check():
    """A throwaway "target" database on the scratch server, created from template0 so it
    certainly lacks plpgsql_check."""
    s = get_settings()
    if not s.scratch_db_password:
        pytest.skip("scratch-pg is not configured")
    name = f"target_{uuid.uuid4().hex[:8]}"
    admin = dict(host=s.scratch_db_host, port=s.scratch_db_port, user=s.scratch_db_user,
                 password=s.scratch_db_password, autocommit=True)
    with psycopg.connect(dbname=s.scratch_db_name, **admin) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    with psycopg.connect(dbname=name, **admin) as conn:
        conn.execute(TARGET_DDL)
    yield Endpoint(kind=DbKind.POSTGRES, host=s.scratch_db_host, port=s.scratch_db_port, database=name,
                   username=s.scratch_db_user, password=s.scratch_db_password, options={"sslmode": "disable"})
    with psycopg.connect(dbname=s.scratch_db_name, **admin) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def scratch_databases() -> set[str]:
    s = get_settings()
    with psycopg.connect(host=s.scratch_db_host, port=s.scratch_db_port, user=s.scratch_db_user,
                         password=s.scratch_db_password, dbname=s.scratch_db_name) as conn:
        return {r[0] for r in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'compile\\_%'")}


def test_broken_code_is_found_and_the_copy_is_dropped(target_without_plpgsql_check):
    before = scratch_databases()
    result = scratch_compile.compile_in_scratch(target_without_plpgsql_check, ["app"])

    assert result.ok, result.error
    broken = result.checks[("app", "function", "broken", "p_days integer")]
    assert any(r["level"] == "error" and "operator does not exist" in r["message"] for r in broken)
    assert broken[0]["checked_in"] == "scratch copy"
    assert ("app", "function", "good", "p_id numeric") not in result.checks
    # Partitions are skipped (even with awkward names) but the parent is copied, so code
    # that queries the partitioned table still checks cleanly.
    assert ("app", "function", "count_events", "") not in result.checks
    assert result.restore_errors == []
    # Dependency order is resolved (enum before table, view on view), ON CONFLICT finds the
    # primary key, and trigger code is checked against its table.
    assert ("app", "function", "upsert", "p_id integer") not in result.checks
    trigger = result.checks[("app", "trigger", "trg_label", "shipments")]
    assert any("no_such_column" in (r["message"] or "") for r in trigger)
    assert scratch_databases() == before  # temporary database removed

    # The target itself was only read: still no plpgsql_check, data untouched.
    ep = target_without_plpgsql_check
    with psycopg.connect(host=ep.host, port=ep.port, user=ep.username, password=ep.password, dbname=ep.database) as conn:
        assert conn.execute("SELECT count(*) FROM pg_extension WHERE extname = 'plpgsql_check'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM app.orders").fetchone()[0] == 1
        # Line numbers refer to the target's own definition, as shown in the pair view.
        definition = conn.execute("SELECT pg_get_functiondef('app.broken(integer)'::regprocedure)").fetchone()[0]
        assert "localtimestamp - p_days" in definition.splitlines()[broken[0]["line"] - 1]


def test_unreachable_target_reports_a_clear_error():
    ep = Endpoint(kind=DbKind.POSTGRES, host="127.0.0.1", port=1, database="x", username="u", password="p")
    result = scratch_compile.compile_in_scratch(ep)
    assert not result.ok and "Could not read the target catalog" in result.error


def test_search_path_is_rebuilt_from_identifiers():
    with psycopg.connect(host=get_settings().scratch_db_host, user=get_settings().scratch_db_user,
                         password=get_settings().scratch_db_password, dbname=get_settings().scratch_db_name) as conn:
        rendered = scratch_compile.search_path_sql('"$user", public, sales, "Odd, ""Name"""').as_string(conn)
    assert rendered == '"$user", "public", "sales", "Odd, ""Name"""'


def test_runtime_temp_tables_are_flagged(target_without_plpgsql_check):
    s = get_settings()
    ep = target_without_plpgsql_check
    with psycopg.connect(host=ep.host, user=ep.username, password=ep.password, dbname=ep.database, autocommit=True) as conn:
        conn.execute("""
            CREATE FUNCTION app.fill() RETURNS void LANGUAGE plpgsql AS $$
            BEGIN CREATE TEMP TABLE IF NOT EXISTS work_rows (x int); END $$;
            CREATE FUNCTION app.use_rows() RETURNS bigint LANGUAGE plpgsql AS $$
            BEGIN RETURN (SELECT count(*) FROM work_rows); END $$;
        """)
    result = scratch_compile.compile_in_scratch(ep, ["app"])
    flagged = result.checks[("app", "function", "use_rows", "")]
    assert all(r.get("runtime_temp_table") for r in flagged if "work_rows" in (r["message"] or ""))
