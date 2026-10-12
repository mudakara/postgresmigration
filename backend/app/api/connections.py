import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import jobs
from app.db import get_db
from app.models import Connection, DbKind, Project
from app.schemas import (
    ConnectionCreate,
    ConnectionRead,
    ConnectionTestRequest,
    ConnectionTestResult,
    ConnectionUpdate,
    EndpointFields,
    JobRead,
)
from app.security import get_cipher
from app.services.endpoints import Endpoint, probe

router = APIRouter(prefix="/connections", tags=["connections"])

# Changing any of these invalidates the last test result.
_ENDPOINT_FIELDS = ("host", "port", "database", "username", "password", "options")


def _get_or_404(db: Session, connection_id: uuid.UUID) -> Connection:
    conn = db.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return conn


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A connection with that name exists") from exc


@router.get("", response_model=list[ConnectionRead])
def list_connections(kind: DbKind | None = None, db: Session = Depends(get_db)):
    query = select(Connection).order_by(Connection.name)
    if kind is not None:
        query = query.where(Connection.kind == kind)
    return db.scalars(query).all()


@router.post("", response_model=ConnectionRead, status_code=status.HTTP_201_CREATED)
def create_connection(body: ConnectionCreate, db: Session = Depends(get_db)):
    conn = Connection(
        **body.model_dump(exclude={"password", "options"}),
        options=body.options.model_dump(exclude_none=True),
        password_encrypted=get_cipher().encrypt(body.password.get_secret_value()),
    )
    db.add(conn)
    _commit_or_conflict(db)
    return conn


@router.post("/test", response_model=ConnectionTestResult)
def test_unsaved_connection(body: ConnectionTestRequest):
    """Test connection details before saving them."""
    endpoint = Endpoint(
        **body.model_dump(exclude={"password", "options"}),
        password=body.password.get_secret_value(),
        options=body.options.model_dump(exclude_none=True),
    )
    return probe(endpoint).__dict__


@router.get("/{connection_id}", response_model=ConnectionRead)
def get_connection(connection_id: uuid.UUID, db: Session = Depends(get_db)):
    return _get_or_404(db, connection_id)


@router.patch("/{connection_id}", response_model=ConnectionRead)
def update_connection(
    connection_id: uuid.UUID, body: ConnectionUpdate, db: Session = Depends(get_db)
):
    conn = _get_or_404(db, connection_id)
    changes = body.model_dump(exclude_unset=True, exclude={"password", "options"})
    options = body.options.model_dump(exclude_none=True) if body.options else conn.options

    # Re-validate the merged result so e.g. Oracle-only options can't land on Postgres.
    merged = {
        "kind": conn.kind, "host": conn.host, "port": conn.port, "database": conn.database,
        "username": conn.username, **changes, "options": options,
    }
    merged.pop("name", None)
    try:
        EndpointFields.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(
            422, exc.errors(include_url=False, include_context=False, include_input=False)
        ) from exc

    for key, value in changes.items():
        setattr(conn, key, value)
    conn.options = options
    if body.password is not None:
        conn.password_encrypted = get_cipher().encrypt(body.password.get_secret_value())
    if any(f in body.model_fields_set for f in _ENDPOINT_FIELDS):
        conn.last_test_ok = None
        conn.last_test_message = "Connection details changed since the last test"
    _commit_or_conflict(db)
    return conn


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(connection_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = _get_or_404(db, connection_id)
    in_use = db.scalars(
        select(Project.name).where(
            (Project.oracle_connection_id == conn.id) | (Project.postgres_connection_id == conn.id)
        )
    ).all()
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Connection is used by project(s): {', '.join(in_use)}"
        )
    db.delete(conn)
    db.commit()


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
def test_connection(connection_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = _get_or_404(db, connection_id)
    result = probe(Endpoint.from_connection(conn))
    conn.last_tested_at = datetime.now(UTC)
    conn.last_test_ok = result.ok
    conn.last_test_message = result.message
    if result.ok:
        conn.server_version = result.server_version
        conn.capabilities = result.capabilities
    db.commit()
    return result.__dict__


@router.post(
    "/{connection_id}/ora2pg-check", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED
)
def start_ora2pg_check(connection_id: uuid.UUID, db: Session = Depends(get_db)):
    """Queue a job that runs `ora2pg -t SHOW_VERSION` against this Oracle connection."""
    conn = _get_or_404(db, connection_id)
    if conn.kind is not DbKind.ORACLE:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ora2Pg checks need an Oracle connection")
    job = jobs.create_job(db, "ora2pg_check", connection_id=conn.id)
    jobs.enqueue(job)
    return job
