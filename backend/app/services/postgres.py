import psycopg

from app.config import get_settings
from app.services.endpoints import Endpoint, ProbeResult

# Extensions the comparison and compile-gate steps can make use of.
TRACKED_EXTENSIONS = ["plpgsql_check", "orafce", "pg_stat_statements"]


LOCK_TIMEOUT = "5s"


def connect(endpoint: Endpoint, **kwargs) -> psycopg.Connection:
    """Open a session in which every transaction is read-only: writes fail with
    "cannot execute ... in a read-only transaction". Set at startup, so it holds in
    autocommit mode and for every transaction on the connection.

    The session also never waits long for a lock: when another session holds or is
    waiting for an exclusive lock (DDL, VACUUM FULL...), a statement that needs that
    table fails after LOCK_TIMEOUT instead of queueing, where it could hold up the DDL."""
    return psycopg.connect(
        host=endpoint.host,
        port=endpoint.port,
        dbname=endpoint.database,
        user=endpoint.username,
        password=endpoint.password,
        sslmode=endpoint.options.get("sslmode") or "prefer",
        connect_timeout=get_settings().connect_timeout_seconds,
        application_name="migration-studio",
        options=f"-c default_transaction_read_only=on -c lock_timeout={LOCK_TIMEOUT}",
        **kwargs,
    )


def probe(endpoint: Endpoint) -> ProbeResult:
    with connect(endpoint, autocommit=True) as conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        major = conn.info.server_version // 10000
        rows = conn.execute(
            "SELECT name, installed_version FROM pg_available_extensions WHERE name = ANY(%s)",
            (TRACKED_EXTENSIONS,),
        ).fetchall()
        available = {name: installed for name, installed in rows}
        extensions = {
            name: {"available": name in available, "installed_version": available.get(name)}
            for name in TRACKED_EXTENSIONS
        }

    message = "Connected"
    target = get_settings().target_pg_version
    if major != target:
        message += f"; server is PostgreSQL {major}, the project targets {target}"
    return ProbeResult(
        ok=True,
        message=message,
        server_version=version,
        capabilities={"major_version": major, "extensions": extensions},
    )
