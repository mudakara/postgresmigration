import time
from dataclasses import dataclass, field
from typing import Any

from app.models import Connection, DbKind
from app.security import get_cipher


@dataclass(frozen=True)
class Endpoint:
    """Decrypted connection details, held only in memory for the duration of a call."""

    kind: DbKind
    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_connection(cls, conn: Connection) -> "Endpoint":
        return cls(
            kind=conn.kind,
            host=conn.host,
            port=conn.port,
            database=conn.database,
            username=conn.username,
            password=get_cipher().decrypt(conn.password_encrypted),
            options=dict(conn.options or {}),
        )


def schema_scope(conn: Connection) -> list[str]:
    """The connection's schemas in scope (upper case for Oracle); empty means all."""
    names = [s for s in (conn.options or {}).get("schemas") or [] if s]
    return [s.upper() for s in names] if conn.kind is DbKind.ORACLE else names


def scoped_schemas(conn: Connection, requested: list[str] | None) -> list[str]:
    """Schemas a feature should read: the requested ones limited to the connection's
    scope, or the whole scope when nothing specific is requested."""
    scope = schema_scope(conn)
    wanted = [s.upper() if conn.kind is DbKind.ORACLE else s for s in requested or []]
    if not scope:
        return wanted
    if not wanted:
        return scope
    allowed = {s.lower() for s in scope}
    inside = [s for s in wanted if s.lower() in allowed]
    if not inside:
        raise ValueError(f"None of {', '.join(wanted)} is in the schema scope of connection {conn.name}: "
                         f"{', '.join(scope)}. Edit the connection to change its scope.")
    return inside


def use_user_grants(conn: Connection) -> bool:
    """Whether to read ALL_* instead of DBA_* views (Ora2Pg's USER_GRANTS): an explicit
    connection option wins, otherwise follow what the last connection test found."""
    explicit = (conn.options or {}).get("user_grants")
    if explicit is not None:
        return bool(explicit)
    return not (conn.capabilities or {}).get("dba_views", True)


@dataclass
class ProbeResult:
    ok: bool
    message: str
    server_version: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0


def probe(endpoint: Endpoint) -> ProbeResult:
    """Connect, read the server version and check the capabilities later steps rely on."""
    # Imported lazily so each driver is only loaded when it is needed.
    if endpoint.kind is DbKind.ORACLE:
        from app.services import oracle as driver
    else:
        from app.services import postgres as driver

    started = time.monotonic()
    try:
        result = driver.probe(endpoint)
    except Exception as exc:  # driver errors vary widely; report them, don't crash
        result = ProbeResult(ok=False, message=f"{type(exc).__name__}: {exc}"[:2000])
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result
