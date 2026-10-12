import time

import pytest

from app.config import get_settings
from app.jobs import create_job, fail_interrupted_jobs
from app.models import Connection, DbKind, Job, JobStatus, Project, Scan, ScanStatus
from app.scan import cost_report
from app.services import ora2pg
from app.services.endpoints import Endpoint

ENDPOINT = Endpoint(kind=DbKind.ORACLE, host="h", port=1521, database="d", username="u", password="p")

HANGING = """#!/bin/sh
echo "Reporting Oracle Content..." >&2
echo "Looking at table definition..." >&2
sleep 600 &
sleep 600
"""

WORKING = """#!/bin/sh
echo "Looking at table definition..." >&2
sleep 1
echo '{"total cost": 7, "objects": []}'
"""


@pytest.fixture
def fake_ora2pg(tmp_path, monkeypatch):
    def install(body: str):
        script = tmp_path / "ora2pg"
        script.write_text(body)
        script.chmod(0o755)
        monkeypatch.setattr(get_settings(), "ora2pg_bin", str(script))
        monkeypatch.setattr(ora2pg, "PROGRESS_INTERVAL_SECONDS", 0.2)
    return install


def test_timeout_kills_the_whole_process_group_and_names_the_stage(fake_ora2pg, tmp_path):
    """A child holding the pipes open used to make the wait hang forever."""
    fake_ora2pg(HANGING)
    started = time.monotonic()
    with pytest.raises(ora2pg.Ora2PgError, match=r"timed out after 2s while: Looking at table definition"):
        ora2pg.run(ENDPOINT, "SHOW_REPORT", tmp_path, timeout=2)
    assert time.monotonic() - started < 10
    assert not (tmp_path / "ora2pg.conf").exists()  # config removed even on timeout


def test_progress_is_reported_and_stdout_kept_separate(fake_ora2pg, tmp_path):
    fake_ora2pg(WORKING)
    seen = []
    result = ora2pg.run(ENDPOINT, "SHOW_REPORT", tmp_path, timeout=30, on_progress=lambda line, t: seen.append(line))
    assert result.succeeded and result.stdout.strip().startswith('{"total cost"')
    assert "Looking at table definition..." in seen
    assert "Looking at table definition" in result.stderr


def test_report_timeout_becomes_a_failed_schema_not_an_exception(monkeypatch):
    def boom(*a, **k):
        raise ora2pg.Ora2PgError("ora2pg SHOW_REPORT timed out after 900s while: Looking at table definition...")

    monkeypatch.setattr(ora2pg, "run", boom)
    report = cost_report.run_schema_report(ENDPOINT, "SALES", user_grants=True)
    assert not report.ok and "timed out" in report.error


def test_interrupted_jobs_are_failed_on_worker_start(session_factory):
    with session_factory() as s:
        conn = Connection(name="o", kind=DbKind.ORACLE, host="h", port=1, database="d", username="u",
                          password_encrypted="x", options={}, capabilities={})
        s.add(conn)
        s.flush()
        project = Project(name="p", oracle_connection_id=conn.id, schemas=[], settings={})
        s.add(project)
        s.flush()
        running = create_job(s, "scan", params={}, project_id=project.id)
        running.status = JobStatus.RUNNING
        queued = create_job(s, "scan", params={}, project_id=project.id)
        scan = Scan(project_id=project.id, job_id=running.id, status=ScanStatus.RUNNING)
        s.add(scan)
        s.commit()
        ids = running.id, queued.id, scan.id

    assert fail_interrupted_jobs(session_factory) == 1
    with session_factory() as s:
        assert s.get(Job, ids[0]).status is JobStatus.FAILED and "Interrupted" in s.get(Job, ids[0]).error
        assert s.get(Job, ids[1]).status is JobStatus.QUEUED  # still in the broker; leave it
        assert s.get(Scan, ids[2]).status is ScanStatus.FAILED
