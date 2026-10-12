"""Claude performance review of the code that uses a table (schema explorer)."""

import json
import re
import uuid

import psycopg
from psycopg import sql

from app.config import get_settings
from app.jobs import execute_job
from app.models import Job, JobStatus
from tests.conftest import endpoint_payload
from tests.test_catalog_explorer import shop_db, snapshot, tables_by_name  # noqa: F401  (fixture)
from tests.test_comparison_api import FakeClient as _FakeCreateClient
from tests.test_comparison_api import fake_response


class FakeClient(_FakeCreateClient):
    """The performance review streams its requests."""

    def __init__(self, responder):
        super().__init__(responder)
        self.beta.messages.stream = self._stream

    def _stream(self, **kwargs):
        response = self._create(**kwargs)

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                if isinstance(response, Exception):
                    raise response
                return response

        return Stream()


def setup(client, session_factory, shop_db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    conn = client.post("/api/connections", json=endpoint_payload(shop_db, "shop")).json()
    snap = snapshot(client, session_factory, conn["id"])
    return conn, snap, tables_by_name(client, snap["id"])


def answer(kwargs):
    """Flag every object in the request as medium priority and suggest one index."""
    task = kwargs["messages"][0]["content"][1]["text"]
    refs = re.findall(r"### ref=(C\d+):", task)
    return fake_response({
        "objects": [{"ref": r, "priority": "medium", "summary": "Row-by-row work.", "issues": [{
            "severity": "medium", "category": "row_by_row", "title": "Per-row DML", "explanation": "One statement per row.",
            "lines": [3], "suggestion": "Use one set-based statement.", "example": "UPDATE ...;",
        }]} for r in refs],
        "table_recommendations": [{"kind": "index", "title": "Index the FK", "explanation": "Parent deletes scan orders.",
                                   "sql": "CREATE INDEX ON shop.orders (customer_id);", "helps": refs}],
    })


def run(client, session_factory, table_id, **body):
    resp = client.post(f"/api/catalog-tables/{table_id}/perf-reviews", json=body)
    assert resp.status_code == 202, resp.text
    review = resp.json()
    execute_job(uuid.UUID(review["job_id"]), session_factory)
    with session_factory() as s:
        return s.get(Job, uuid.UUID(review["job_id"]))


def test_scopes_and_estimate(client, session_factory, shop_db, monkeypatch):
    _, _, tables = setup(client, session_factory, shop_db, monkeypatch)
    orders = tables["orders"]["id"]

    def estimate(scope):
        resp = client.post(f"/api/catalog-tables/{orders}/perf-reviews/estimate", json={"scope": scope})
        assert resp.status_code == 200, resp.text
        return resp.json()

    triggers, writers, everything = estimate("triggers"), estimate("writers"), estimate("all")
    # orders has one trigger (orders_audit, whose function audit is folded into it) and one
    # procedure that writes to it (close_order).
    assert triggers["objects"] == 1
    assert writers["objects"] == 2 and everything["objects"] == 2
    assert writers["credentials_configured"] and writers["estimated_cost_usd"] is not None
    assert writers["batches"] == 1


def test_review_sends_code_and_structure_and_stores_results(client, session_factory, shop_db, monkeypatch):
    _, _, tables = setup(client, session_factory, shop_db, monkeypatch)
    fake = FakeClient(answer)
    monkeypatch.setattr("app.catalog.perf_service.make_client", lambda: fake)

    job = run(client, session_factory, tables["orders"]["id"], scope="writers")
    assert job.status is JobStatus.SUCCEEDED, job.error

    sent = fake.calls[0]
    context, task = (b["text"] for b in sent["messages"][0]["content"])
    assert "## Table under review" in context and "shop.orders (table" in context
    assert "orders_pkey: (id) primary key" in context
    assert "index on the FK columns: NO" in context  # orders.customer_id
    assert "shop.order_lines" in context  # used by close_order
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "PostgreSQL" in sent["system"][0]["text"]
    assert "Trigger on shop.orders: AFTER INSERT OR UPDATE FOR EACH ROW" in task
    assert "Uses shop.orders: UPDATE" in task
    assert "INSERT INTO shop.events" in task  # the trigger carries its function's source
    assert task.count("### ref=") == 2

    state = client.get(f"/api/catalog-tables/{tables['orders']['id']}/perf-reviews").json()
    latest = state["latest"]
    assert latest["status"] == "succeeded" and latest["summary"]["priority"]["medium"] == 2
    assert {o["name"] for o in latest["results"]["objects"]} == {"shop.orders_audit", "shop.close_order"}
    assert all(o["current_code_id"] and not o["stale"] for o in latest["results"]["objects"])
    recs = latest["results"]["table_recommendations"]
    assert len(recs) == 1 and set(recs[0]["helps"]) == {"shop.orders_audit (trigger)", "shop.close_order (procedure)"}


def test_results_survive_a_refresh_and_go_stale_when_code_changes(client, session_factory, shop_db, monkeypatch):
    conn, _, tables = setup(client, session_factory, shop_db, monkeypatch)
    monkeypatch.setattr("app.catalog.perf_service.make_client", lambda: FakeClient(answer))
    assert run(client, session_factory, tables["orders"]["id"], scope="writers").status is JobStatus.SUCCEEDED

    s = get_settings()
    with psycopg.connect(host=s.scratch_db_host, port=s.scratch_db_port, user=s.scratch_db_user,
                         password=s.scratch_db_password, dbname=shop_db.database, autocommit=True) as pg:
        pg.execute(sql.SQL("CREATE OR REPLACE PROCEDURE shop.close_order(p_id int) LANGUAGE sql AS "
                           "$$ UPDATE shop.orders SET placed = NULL WHERE id = p_id $$"))
    snap = snapshot(client, session_factory, conn["id"])
    orders = tables_by_name(client, snap["id"])["orders"]["id"]
    objects = {o["name"]: o for o in client.get(f"/api/catalog-tables/{orders}/perf-reviews").json()["latest"]["results"]["objects"]}
    assert objects["shop.close_order"]["stale"] and not objects["shop.orders_audit"]["stale"]


def test_failures_are_reported(client, session_factory, shop_db, monkeypatch):
    _, _, tables = setup(client, session_factory, shop_db, monkeypatch)
    monkeypatch.setattr("app.catalog.perf_service.make_client",
                        lambda: FakeClient(lambda kw: fake_response(stop_reason="max_tokens")))
    job = run(client, session_factory, tables["orders"]["id"], scope="writers")
    assert job.status is JobStatus.FAILED and "max_tokens" in job.error
    latest = client.get(f"/api/catalog-tables/{tables['orders']['id']}/perf-reviews").json()["latest"]
    assert latest["status"] == "failed"


def test_unexpected_errors_fail_the_job_instead_of_vanishing(client, session_factory, shop_db, monkeypatch):
    _, _, tables = setup(client, session_factory, shop_db, monkeypatch)
    monkeypatch.setattr("app.catalog.perf_service.make_client",
                        lambda: FakeClient(lambda kw: ValueError("Streaming is required")))
    job = run(client, session_factory, tables["orders"]["id"], scope="writers")
    assert job.status is JobStatus.FAILED and "Streaming is required" in job.error


def test_requires_credentials_and_objects(client, session_factory, shop_db, monkeypatch):
    _, _, tables = setup(client, session_factory, shop_db, monkeypatch)
    # customers has no trigger and nothing writes to it.
    resp = client.post(f"/api/catalog-tables/{tables['customers']['id']}/perf-reviews", json={"scope": "writers"})
    assert resp.status_code == 422
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    resp = client.post(f"/api/catalog-tables/{tables['orders']['id']}/perf-reviews", json={"scope": "writers"})
    assert resp.status_code == 409 and "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_prompt_and_schema_are_well_formed():
    from app.catalog.perf_review import PERF_SCHEMA, system_prompt

    assert "Oracle" in system_prompt("Oracle 19c, PL/SQL")
    assert json.dumps(PERF_SCHEMA)  # serialisable for output_config
