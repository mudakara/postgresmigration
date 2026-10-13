import re
import threading
from typing import Any

import oracledb

from app.config import get_settings
from app.services.endpoints import Endpoint, ProbeResult

_client_lock = threading.Lock()
_client_initialised = False


def _ensure_client_mode() -> None:
    global _client_initialised
    settings = get_settings()
    if settings.oracle_client_mode != "thick" or _client_initialised:
        return
    with _client_lock:
        if not _client_initialised:
            oracledb.init_oracle_client(lib_dir=str(settings.oracle_home))
            _client_initialised = True


def make_dsn(endpoint: Endpoint) -> str:
    if endpoint.options.get("use_sid"):
        return oracledb.makedsn(endpoint.host, endpoint.port, sid=endpoint.database)
    return oracledb.makedsn(endpoint.host, endpoint.port, service_name=endpoint.database)


class ReadOnlyViolation(Exception):
    """A statement other than a query was about to be sent to Oracle."""


# Queries only. A WITH clause may define PL/SQL functions (12c+), which could do anything.
_QUERY = re.compile(r"^\s*(SELECT|WITH)\b", re.I)
_WITH_PLSQL = re.compile(r"^\s*WITH\s+(FUNCTION|PROCEDURE)\b", re.I)


def _check_query(sql: str) -> None:
    if not _QUERY.match(sql) or _WITH_PLSQL.match(sql):
        first = sql.strip().split(None, 1)[0] if sql.strip() else "(empty)"
        raise ReadOnlyViolation(f"Refusing to send a non-query statement to Oracle ({first}…)")


class ReadOnlyCursor:
    """A cursor that only executes queries."""

    def __init__(self, cursor: oracledb.Cursor):
        object.__setattr__(self, "_cursor", cursor)

    def execute(self, statement: str, parameters: Any = None, **kwargs):
        _check_query(statement)
        return self._cursor.execute(statement, parameters, **kwargs)

    def _refuse(self, *args, **kwargs):
        raise ReadOnlyViolation("Only queries may be executed on Oracle connections")

    executemany = callproc = callfunc = _refuse

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __setattr__(self, name, value):  # arraysize, prefetchrows...
        setattr(self._cursor, name, value)

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cursor.close()


class ReadOnlyConnection:
    """An Oracle session the app cannot write through.

    Two layers, because Oracle 19c has no read-only session mode:
    1. The session starts with SET TRANSACTION READ ONLY, so DML (INSERT, UPDATE, DELETE,
       MERGE, SELECT ... FOR UPDATE) fails with ORA-01456.
    2. Only SELECT/WITH statements are sent at all. This matters because DDL commits
       implicitly (ending the read-only transaction), PL/SQL blocks can run autonomous
       transactions, and a COMMIT would make the session read-write again.
    Use a database account without write privileges as well; that is the only guarantee
    that covers every tool, not just this app.
    """

    def __init__(self, conn: oracledb.Connection):
        self._conn = conn

    @property
    def version(self) -> str:
        return self._conn.version

    def cursor(self) -> ReadOnlyCursor:
        return ReadOnlyCursor(self._conn.cursor())

    @property
    def call_timeout(self) -> int:
        """Milliseconds a single database round trip may take (0: no limit)."""
        return self._conn.call_timeout

    @call_timeout.setter
    def call_timeout(self, ms: int) -> None:
        self._conn.call_timeout = ms

    def commit(self):
        raise ReadOnlyViolation("Oracle connections are read-only; commit is not allowed")

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._conn.close()  # closing without commit rolls back


def connect(endpoint: Endpoint) -> ReadOnlyConnection:
    _ensure_client_mode()
    conn = oracledb.connect(
        user=endpoint.username,
        password=endpoint.password,
        dsn=make_dsn(endpoint),
        tcp_connect_timeout=get_settings().connect_timeout_seconds,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
    except BaseException:
        conn.close()
        raise
    return ReadOnlyConnection(conn)


def _can_query(cursor: ReadOnlyCursor, sql: str) -> bool:
    try:
        cursor.execute(sql)
        cursor.fetchone()
        return True
    except oracledb.DatabaseError:
        return False


def probe(endpoint: Endpoint) -> ProbeResult:
    with connect(endpoint) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT SYS_CONTEXT('USERENV', 'CON_NAME'), SYS_CONTEXT('USERENV', 'SESSION_USER') "
            "FROM dual"
        )
        container, session_user = cur.fetchone()
        capabilities = {
            "container": container,
            "session_user": session_user,
            # Without DBA_* access Ora2Pg must fall back to ALL_* views (USER_GRANTS 1),
            # which only show objects this account has been granted access to.
            "dba_views": _can_query(cur, "SELECT 1 FROM dba_objects WHERE ROWNUM = 1"),
            # Needed later for the license-free performance comparison.
            "v_sql": _can_query(cur, "SELECT 1 FROM v$sql WHERE ROWNUM = 1"),
        }
        major = int(conn.version.split(".")[0])
        message = "Connected"
        if major < 12:
            message += f"; Oracle {conn.version} is older than the supported 12.1"
        if not capabilities["dba_views"]:
            message += "; no DBA_* view access, scans will only see objects granted to this user"
        return ProbeResult(
            ok=True, message=message, server_version=conn.version, capabilities=capabilities
        )
