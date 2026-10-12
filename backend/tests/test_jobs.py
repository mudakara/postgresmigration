import pytest

from app.jobs import create_job, execute_job
from app.jobs import registry
from app.models import Job, JobStatus


@pytest.fixture
def temp_handlers(monkeypatch):
    handlers = {}
    monkeypatch.setattr(registry, "_HANDLERS", handlers)
    return handlers


def test_successful_job_records_result_log_and_timestamps(session_factory, temp_handlers):
    def handler(ctx):
        ctx.log("step one")
        ctx.progress(0.5, "halfway")
        return {"answer": 42}

    temp_handlers["demo"] = handler
    with session_factory() as s:
        job_id = create_job(s, "demo", params={"x": 1}).id

    execute_job(job_id, session_factory)

    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status is JobStatus.SUCCEEDED
        assert job.result == {"answer": 42}
        assert "step one" in job.log
        assert job.progress == 1.0
        assert job.progress_message is None
        assert job.started_at <= job.finished_at


def test_failing_job_keeps_partial_log_and_records_error(session_factory, temp_handlers):
    def handler(ctx):
        ctx.log("before failure")
        ctx.progress(0.3, "converting")
        raise RuntimeError("boom")

    temp_handlers["demo"] = handler
    with session_factory() as s:
        job_id = create_job(s, "demo").id

    execute_job(job_id, session_factory)

    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status is JobStatus.FAILED
        assert job.error == "RuntimeError: boom"
        assert "before failure" in job.log and "Traceback" in job.log
        assert job.progress_message == "converting"


def test_job_is_not_run_twice(session_factory, temp_handlers):
    calls = []
    temp_handlers["demo"] = lambda ctx: calls.append(1)
    with session_factory() as s:
        job_id = create_job(s, "demo").id

    execute_job(job_id, session_factory)
    execute_job(job_id, session_factory)  # e.g. a redelivered broker message
    assert calls == [1]


def test_unknown_job_type_is_rejected(session_factory):
    with session_factory() as s, pytest.raises(ValueError):
        create_job(s, "does-not-exist")


def test_jobs_api_lists_and_filters(client, session_factory):
    with session_factory() as s:
        job = create_job(s, "ora2pg_check")
    listed = client.get("/api/jobs", params={"type": "ora2pg_check"}).json()
    assert [j["id"] for j in listed] == [str(job.id)]
    assert client.get("/api/jobs", params={"status": "failed"}).json() == []
    assert client.get(f"/api/jobs/{job.id}").json()["log"] is None
