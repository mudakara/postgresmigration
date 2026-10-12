import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Connection, DbKind, Project
from app.schemas import ProjectCreate, ProjectRead, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])


def _get_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


def _check_connection(db: Session, connection_id: uuid.UUID | None, kind: DbKind) -> None:
    if connection_id is None:
        return
    conn = db.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(422, f"Connection {connection_id} not found")
    if conn.kind is not kind:
        raise HTTPException(
            422,
            f"Connection '{conn.name}' is {conn.kind.value}, expected {kind.value}",
        )


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A project with that name exists") from exc


@router.get("", response_model=list[ProjectRead])
def list_projects(db: Session = Depends(get_db)):
    return db.scalars(select(Project).order_by(Project.name)).all()


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    _check_connection(db, body.oracle_connection_id, DbKind.ORACLE)
    _check_connection(db, body.postgres_connection_id, DbKind.POSTGRES)
    project = Project(**body.model_dump())
    db.add(project)
    _commit_or_conflict(db)
    return project


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: uuid.UUID, db: Session = Depends(get_db)):
    return _get_or_404(db, project_id)


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(project_id: uuid.UUID, body: ProjectUpdate, db: Session = Depends(get_db)):
    project = _get_or_404(db, project_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("oracle_connection_id", project.oracle_connection_id) is None:
        raise HTTPException(422, "A project needs an Oracle connection")
    if "oracle_connection_id" in changes:
        _check_connection(db, changes["oracle_connection_id"], DbKind.ORACLE)
    if "postgres_connection_id" in changes:
        _check_connection(db, changes["postgres_connection_id"], DbKind.POSTGRES)
    for key, value in changes.items():
        setattr(project, key, value)
    _commit_or_conflict(db)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: uuid.UUID, db: Session = Depends(get_db)):
    db.delete(_get_or_404(db, project_id))
    db.commit()
