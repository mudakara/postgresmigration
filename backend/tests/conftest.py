import argparse
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import DbKind
from app.services.endpoints import Endpoint

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(get_settings().database_url).set(database="studio_test")
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def engine():
    url = test_database_url()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    # Exercise the migrations in both directions on every run.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE jobs, projects, connections CASCADE"))


@pytest.fixture
def enqueued(monkeypatch):
    """Capture jobs the API would send to Celery instead of needing a worker."""
    jobs = []
    monkeypatch.setattr("app.jobs.enqueue", jobs.append)
    return jobs


@pytest.fixture
def client(session_factory, enqueued):
    def override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def app_db_endpoint() -> Endpoint:
    """The application's own Postgres, used as a real target for connection tests."""
    url = make_url(get_settings().database_url)
    return Endpoint(
        kind=DbKind.POSTGRES,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
        username=url.username,
        password=url.password,
        options={"sslmode": "disable"},
    )


@pytest.fixture
def oracle_endpoint() -> Endpoint:
    if not os.environ.get("ORACLE_TEST_HOST"):
        pytest.skip("set ORACLE_TEST_HOST/PORT/SERVICE/USER/PASSWORD to run Oracle tests")
    return Endpoint(
        kind=DbKind.ORACLE,
        host=os.environ["ORACLE_TEST_HOST"],
        port=int(os.environ.get("ORACLE_TEST_PORT", "1521")),
        database=os.environ["ORACLE_TEST_SERVICE"],
        username=os.environ["ORACLE_TEST_USER"],
        password=os.environ["ORACLE_TEST_PASSWORD"],
    )


def endpoint_payload(endpoint: Endpoint, name: str = "conn") -> dict:
    return {
        "name": name,
        "kind": endpoint.kind.value,
        "host": endpoint.host,
        "port": endpoint.port,
        "database": endpoint.database,
        "username": endpoint.username,
        "password": endpoint.password,
        "options": endpoint.options,
    }
