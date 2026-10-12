import json
import uuid
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from sqlalchemy import select

from app.compare import review as review_mod
from app.compare.target_extract import ExtractedTarget, TargetCatalog
from app.jobs import create_job, execute_job
from app.models import ComparisonRun, Connection, DbKind, Job, JobStatus, ObjectPair, Project, Scan, ScanStatus
from app.scan.service import analyze_extraction, persist
from tests import scan_fixtures as fx


def pg_fn(name, schema="app", args=(), kind="function", source="", result="void", language="plpgsql", **meta):
    return ExtractedTarget(
        oid=abs(hash((schema, name))) % 10**8, schema=schema, name=name, kind=kind,
        identity_args=", ".join(f"{a} numeric" for a in args), result_type=result, language=language,
        arguments=[{"position": i + 1, "name": a, "mode": "IN", "type": "numeric"} for i, a in enumerate(args)],
        source=source, metadata=meta,
    )


CATALOG = TargetCatalog(
    server_version="16.4",
    plpgsql_check=True,
    extensions={"plpgsql": "1.0", "plpgsql_check": "2.10"},
    objects=[
        pg_fn("log_message", args=["p"], kind="procedure",
              source="CREATE PROCEDURE app.log_message(p numeric)\nLANGUAGE plpgsql AS $$\nBEGIN\n  INSERT INTO app_log VALUES (now(), p);\nEND $$"),
        pg_fn("get_name", args=["p_id"], result="character varying",
              source="CREATE FUNCTION app.get_name(p_id numeric)\nAS $$\nBEGIN\n  SELECT name INTO v FROM customers WHERE id = p_id;\n  RETURN v;\nEXCEPTION WHEN no_data_found THEN RETURN NULL;\nEND $$"),
        pg_fn("process", args=["p_id", "p_total"],
              source="CREATE FUNCTION app.process(p_id numeric, p_total numeric)\nAS $$\nBEGIN\n  CALL app.log_message('x');\nEND $$"),
        pg_fn("total", schema="pkg", args=["p_id"], result="numeric",
              source="CREATE FUNCTION pkg.total(p_id numeric)\nAS $$\nBEGIN\n  CALL app.log_message('total');\n  RETURN 1;\nEND $$"),
        pg_fn("get_name_v2", args=["p_id"], source="CREATE FUNCTION app.get_name_v2(p_id numeric) AS $$ BEGIN RETURN 'x'; END $$"),
        ExtractedTarget(oid=99, schema="app", name="v", kind="view",
                        source="CREATE OR REPLACE VIEW app.v AS\nSELECT c.id FROM customers c, orders o\nWHERE c.id = o.cid"),
    ],
)


@pytest.fixture
def project(session_factory, monkeypatch):
    with session_factory() as s:
        ora = Connection(name="ora", kind=DbKind.ORACLE, host="h", port=1521, database="d", username="u",
                         password_encrypted="x", options={}, capabilities={})
        pg = Connection(name="pg", kind=DbKind.POSTGRES, host="h", port=5432, database="d", username="u",
                        password_encrypted="x", options={}, capabilities={})
        s.add_all([ora, pg])
        s.flush()
        p = Project(name="p", oracle_connection_id=ora.id, postgres_connection_id=pg.id, schemas=["APP"], settings={})
        s.add(p)
        s.flush()
        scan = Scan(project_id=p.id, schemas=["APP"], status=ScanStatus.SUCCEEDED)
        s.add(scan)
        s.flush()
        extraction = fx.sample_extraction()
        persist(s, p, scan, extraction, analyze_extraction(extraction, {}), {})
        s.commit()
        project_id = p.id
    monkeypatch.setattr("app.compare.service.read_catalog", lambda *a, **k: CATALOG)
    monkeypatch.setattr("app.compare.service.Endpoint.from_connection", lambda conn: None)
    return project_id


def run_compare(session_factory, project_id):
    with session_factory() as s:
        run = ComparisonRun(project_id=project_id)
        s.add(run)
        s.flush()
        job = create_job(s, "compare", params={"run_id": str(run.id)}, project_id=project_id)
        job_id, run_id = job.id, run.id
    execute_job(job_id, session_factory)
    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status is JobStatus.SUCCEEDED, job.error
        return s.get(ComparisonRun, run_id)


def pairs_by_name(client, project_id):
    return {p["oracle_name"]: p for p in client.get(f"/api/projects/{project_id}/pairs", params={"limit": 100}).json()["items"]}


def test_comparison_pairs_checks_and_summarises(client, session_factory, project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    run = run_compare(session_factory, project)
    assert run.summary["matched"] == 5 and run.summary["missing_in_target"] == 0
    assert run.summary["conventions"] == {"same_schema": 4, "schema_per_package": 1}
    assert run.summary["unpaired_targets"] == 1

    pairs = pairs_by_name(client, project)
    assert pairs["APP.V"]["risk"] == "high"
    assert pairs["APP.LOG_MESSAGE"]["risk"] == "high"
    assert pairs["APP.GET_NAME"]["target_name"] == "app.get_name(p_id numeric)"

    detail = client.get(f"/api/pairs/{pairs['APP.PKG.TOTAL']['id']}").json()
    assert detail["oracle"]["member_range"] == [2, 6]
    assert detail["oracle"]["object_label"] == "APP.PKG (PACKAGE BODY)"
    codes = {f["code"] for f in detail["static_findings"]}
    assert "CALLS_RISKY_ROUTINE" in codes  # TOTAL calls LOG_MESSAGE, which lost its autonomous transaction

    process = client.get(f"/api/pairs/{pairs['APP.PROCESS']['id']}").json()
    assert {f["code"] for f in process["static_findings"]} >= {"CALLS_RISKY_ROUTINE"}

    overview = client.get(f"/api/projects/{project}/comparison").json()
    assert overview["counts"]["status"] == {"matched": 5}
    assert overview["counts"]["unpaired_targets"] == 1
    assert overview["review"]["credentials_configured"] is False
    assert overview["conventions"]["schema_per_package"]["count"] == 1

    unpaired = client.get(f"/api/projects/{project}/target-objects", params={"unpaired": "true"}).json()
    assert [t["display_name"] for t in unpaired] == ["app.get_name_v2(p_id numeric)"]

    high = client.get(f"/api/projects/{project}/pairs", params={"risk": "high"}).json()
    assert high["total"] >= 2


def test_manual_match_survives_reruns_and_reset_restores_auto(client, session_factory, project):
    run_compare(session_factory, project)
    pairs = pairs_by_name(client, project)
    v2 = client.get(f"/api/projects/{project}/target-objects", params={"q": "get_name_v2"}).json()[0]

    resp = client.patch(f"/api/pairs/{pairs['APP.GET_NAME']['id']}", json={"target_object_id": v2["id"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["manual"] and body["match_method"] == "manual" and body["target_object_id"] == v2["id"]

    run_compare(session_factory, project)
    again = client.get(f"/api/pairs/{pairs['APP.GET_NAME']['id']}").json()
    assert again["target_object_id"] == v2["id"] and again["manual"]

    client.patch(f"/api/pairs/{pairs['APP.GET_NAME']['id']}", json={"reset": True})
    run_compare(session_factory, project)
    restored = client.get(f"/api/pairs/{pairs['APP.GET_NAME']['id']}").json()
    assert restored["target_name"] == "app.get_name(p_id numeric)" and not restored["manual"]


def test_ignore_and_review_status(client, session_factory, project):
    run_compare(session_factory, project)
    pid = pairs_by_name(client, project)["APP.V"]["id"]
    ignored = client.patch(f"/api/pairs/{pid}", json={"ignore": True}).json()
    assert ignored["status"] == "ignored" and ignored["risk"] == "none"
    run_compare(session_factory, project)
    assert client.get(f"/api/pairs/{pid}").json()["status"] == "ignored"

    reviewed = client.patch(f"/api/pairs/{pid}", json={"review_status": "wont_fix", "review_note": "view is unused"}).json()
    assert reviewed["review_status"] == "wont_fix" and reviewed["review_note"] == "view is unused"
    assert client.patch(f"/api/pairs/{pid}", json={"ignore": True, "reset": True}).status_code == 422


def test_comparison_requires_a_target_connection(client, session_factory, project):
    with session_factory() as s:
        s.get(Project, project).postgres_connection_id = None
        s.commit()
    assert client.post(f"/api/projects/{project}/comparisons").status_code == 422


# --- Claude reviews -------------------------------------------------------------

def fake_response(payload=None, stop_reason="end_turn", category=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(payload or {}))],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=900, cache_creation_input_tokens=0),
        model="claude-opus-5-5",
        _request_id="req_test",
    )


VERDICT = {
    "verdict": "differences_found", "confidence": "high", "summary": "The outer join was dropped.",
    "issues": [{"severity": "high", "category": "joins", "title": "Inner join", "explanation": "Rows are lost.",
                "oracle_lines": [4], "postgres_lines": [3], "suggested_fix": "Use LEFT JOIN."}],
}


class FakeClient:
    def __init__(self, responder):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._responder = responder

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder(kwargs)


def test_estimate_and_start_without_credentials(client, session_factory, project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    run_compare(session_factory, project)
    est = client.post(f"/api/projects/{project}/reviews/estimate", json={"scope": "all_matched"}).json()
    assert est["pairs"] == 5 and est["credentials_configured"] is False
    assert est["estimated_cost_usd"] > 0 and est["model"] == "claude-opus-5-5"
    assert client.post(f"/api/projects/{project}/reviews", json={"scope": "all_matched"}).status_code == 422


def test_review_job_sends_cached_structured_request_and_stores_verdicts(client, session_factory, project, monkeypatch, enqueued):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = FakeClient(lambda kwargs: fake_response(VERDICT))
    monkeypatch.setattr("app.compare.review_service.make_client", lambda: fake)
    run_compare(session_factory, project)

    resp = client.post(f"/api/projects/{project}/reviews", json={"scope": "risky"})
    assert resp.status_code == 202, resp.text
    execute_job(uuid.UUID(resp.json()["id"]), session_factory)

    with session_factory() as s:
        job = s.get(Job, uuid.UUID(resp.json()["id"]))
        assert job.status is JobStatus.SUCCEEDED, job.error
        assert job.result["reviewed"] == len(fake.calls) > 0
        reviewed = s.scalars(select(ObjectPair).where(ObjectPair.ai_verdict.is_not(None))).all()
        assert all(p.ai_verdict == "differences_found" and not p.ai_stale for p in reviewed)
        assert reviewed[0].ai_review["usage"]["cache_read_input_tokens"] == 900

    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["schema"] == review_mod.REVIEW_SCHEMA
    oracle_block, task_block = call["messages"][0]["content"]
    assert oracle_block["cache_control"] == {"type": "ephemeral"} and "<oracle_source>" in oracle_block["text"]
    assert "<postgresql_source>" in task_block["text"] and "Automated check results" in task_block["text"]
    assert "cache_control" not in task_block


def test_refusal_becomes_needs_human_review(session_factory, project):
    inp = review_mod.ReviewInput(
        pair_id=str(uuid.uuid4()), oracle_label="APP.V", oracle_listing="1| x", oracle_member_range=None,
        oracle_signature=None, oracle_findings=[], target_label="app.v", target_kind="view",
        target_listing="1| y", signature_diffs=[], static_findings=[],
    )
    from app.config import get_settings
    fake = FakeClient(lambda kwargs: fake_response(stop_reason="refusal", category="cyber"))
    result = review_mod.review_pair(fake, inp, get_settings())
    assert result["verdict"] == "needs_human_review" and "cyber" in result["summary"]

    fake = FakeClient(lambda kwargs: fake_response(stop_reason="max_tokens"))
    with pytest.raises(review_mod.ReviewError):
        review_mod.review_pair(fake, inp, get_settings())


def test_bad_credentials_fail_the_job(client, session_factory, project, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad")
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def deny(kwargs):
        raise anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=request), body=None)

    monkeypatch.setattr("app.compare.review_service.make_client", lambda: FakeClient(deny))
    run_compare(session_factory, project)
    with session_factory() as s:
        ids = [str(p.id) for p in s.scalars(select(ObjectPair).where(ObjectPair.status == "matched"))]
        job_id = create_job(s, "ai_review", params={"pair_ids": ids}, project_id=project).id
    execute_job(job_id, session_factory)
    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status is JobStatus.FAILED and "credentials" in job.error


def test_workspace_header_is_sent_only_when_configured(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    get_settings.cache_clear()
    try:
        assert "anthropic-workspace-id" not in review_mod.make_client().default_headers
        monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
        get_settings.cache_clear()
        assert review_mod.make_client().default_headers["anthropic-workspace-id"] == "wrkspc_test"
    finally:
        monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
        get_settings.cache_clear()
