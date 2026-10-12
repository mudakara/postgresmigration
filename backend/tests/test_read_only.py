"""Every session the app opens to a user database must be unable to write."""


import psycopg
import pytest
from sqlalchemy import make_url

from app.models import DbKind
from app.services import oracle, postgres
from app.services.endpoints import Endpoint
from tests.conftest import test_database_url as app_test_database_url

# --- Oracle: query-only guard on top of SET TRANSACTION READ ONLY -------------------


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM dual",
    "  select owner from dba_objects",
    "WITH x AS (SELECT 1 n FROM dual) SELECT n FROM x",
])
def test_queries_are_allowed(sql):
    oracle._check_query(sql)


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)", "UPDATE t SET a = 1", "DELETE FROM t", "MERGE INTO t USING s ON (1=1)",
    "CREATE TABLE t (x NUMBER)", "DROP TABLE t", "TRUNCATE TABLE t", "GRANT SELECT ON t TO x",
    "BEGIN log_message('x'); END;", "DECLARE x NUMBER; BEGIN NULL; END;", "CALL p()", "COMMIT",
    "ALTER SESSION SET CURRENT_SCHEMA = x", "SET TRANSACTION READ WRITE",
    "WITH FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END; SELECT f FROM dual", "",
])
def test_everything_else_is_refused(sql):
    with pytest.raises(oracle.ReadOnlyViolation):
        oracle._check_query(sql)


class FakeCursor:
    def __init__(self, log, fail=False):
        self.log, self.fail, self.arraysize = log, fail, 100

    def execute(self, sql, params=None, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.log.append(sql)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class FakeConnection:
    def __init__(self, fail_first=False):
        self.log, self.closed, self.version, self.fail_first = [], False, "19.21.0.0.0", fail_first

    def cursor(self):
        return FakeCursor(self.log, fail=self.fail_first and not self.log)

    def close(self):
        self.closed = True

    def rollback(self):
        self.log.append("<rollback>")


ENDPOINT = Endpoint(kind=DbKind.ORACLE, host="h", port=1521, database="d", username="u", password="p")


def test_connect_starts_a_read_only_transaction_and_guards_the_session(monkeypatch):
    fake = FakeConnection()
    monkeypatch.setattr(oracle.oracledb, "connect", lambda **kw: fake)
    conn = oracle.connect(ENDPOINT)
    assert fake.log == ["SET TRANSACTION READ ONLY"]
    assert isinstance(conn, oracle.ReadOnlyConnection) and conn.version == "19.21.0.0.0"

    with conn.cursor() as cur:
        cur.arraysize = 5000
        cur.execute("SELECT 1 FROM dual")
        with pytest.raises(oracle.ReadOnlyViolation):
            cur.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(oracle.ReadOnlyViolation):
            cur.callproc("p", [])
        with pytest.raises(oracle.ReadOnlyViolation):
            cur.executemany("SELECT 1 FROM dual", [])
    assert fake.log == ["SET TRANSACTION READ ONLY", "SELECT 1 FROM dual"]

    with pytest.raises(oracle.ReadOnlyViolation):
        conn.commit()
    with conn:
        pass
    assert fake.closed


def test_connect_closes_the_session_if_read_only_cannot_be_set(monkeypatch):
    fake = FakeConnection(fail_first=True)
    monkeypatch.setattr(oracle.oracledb, "connect", lambda **kw: fake)
    with pytest.raises(RuntimeError):
        oracle.connect(ENDPOINT)
    assert fake.closed


def test_live_oracle_session_refuses_writes(oracle_endpoint):
    with oracle.connect(oracle_endpoint) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM dual")
        with pytest.raises(oracle.ReadOnlyViolation):
            cur.execute("CREATE TABLE ro_probe (x NUMBER)")


# --- PostgreSQL: default_transaction_read_only for the whole session -----------------


@pytest.fixture
def test_db_endpoint() -> Endpoint:
    url = make_url(app_test_database_url())
    return Endpoint(kind=DbKind.POSTGRES, host=url.host, port=url.port or 5432, database=url.database,
                    username=url.username, password=url.password, options={"sslmode": "disable"})


@pytest.mark.parametrize("autocommit", [True, False])
@pytest.mark.parametrize("sql", [
    "CREATE TABLE ro_probe (x int)",
    "CREATE TEMP TABLE ro_probe_tmp (x int)",
    "CREATE FUNCTION ro_probe_f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "UPDATE connections SET name = name",
    "DELETE FROM jobs",
    "DO $$ BEGIN CREATE TABLE ro_probe_do (x int); END $$",
])
def test_postgres_sessions_refuse_writes(test_db_endpoint, autocommit, sql, engine):
    with postgres.connect(test_db_endpoint, autocommit=autocommit) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql)


def test_postgres_probe_and_catalog_reads_still_work(app_db_endpoint):
    from app.services.endpoints import probe

    assert probe(app_db_endpoint).ok
