"""End-to-end checks against a live Oracle. Skipped unless ORACLE_TEST_* is set; the
compose `oracle-sandbox` service provides one (see README)."""

import uuid
from dataclasses import replace

from app.jobs import create_job, execute_job
from app.models import Job, JobStatus
from app.services.endpoints import probe
from tests.conftest import endpoint_payload


def test_probe_reports_version_and_dictionary_access(oracle_endpoint):
    result = probe(oracle_endpoint)
    assert result.ok, result.message
    assert int(result.server_version.split(".")[0]) >= 19
    assert result.capabilities["dba_views"] is True


def test_probe_reports_bad_password(oracle_endpoint):
    result = probe(replace(oracle_endpoint, password="definitely-wrong"))
    assert not result.ok
    assert "ORA-01017" in result.message


def test_ora2pg_show_version_job(client, oracle_endpoint, session_factory):
    cid = client.post("/api/connections", json=endpoint_payload(oracle_endpoint, "sandbox")).json()["id"]
    assert client.post(f"/api/connections/{cid}/test").json()["ok"]

    with session_factory() as s:
        job_id = create_job(s, "ora2pg_check", connection_id=uuid.UUID(cid)).id
    execute_job(job_id, session_factory)

    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status is JobStatus.SUCCEEDED, job.error
        assert job.result["oracle_banner"].startswith("Oracle")
        assert job.result["ora2pg_version"].startswith("Ora2Pg v")
        # The password must not leak into the stored job log.
        assert oracle_endpoint.password not in job.log


def test_ora2pg_check_with_wrong_password_fails_cleanly(client, oracle_endpoint, session_factory):
    payload = endpoint_payload(oracle_endpoint, "sandbox-bad")
    payload["password"] = "definitely-wrong"
    cid = client.post("/api/connections", json=payload).json()["id"]

    with session_factory() as s:
        job_id = create_job(s, "ora2pg_check", connection_id=uuid.UUID(cid)).id
    execute_job(job_id, session_factory)

    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.status is JobStatus.FAILED
        assert "ORA-01017" in job.error


def test_full_scan_of_sandbox_schema(client, oracle_endpoint, session_factory, enqueued):
    """Scans the MIGDEMO sample schema end to end, including the Ora2Pg cost report."""
    cid = client.post("/api/connections", json=endpoint_payload(oracle_endpoint, "scan-src")).json()["id"]
    assert client.post(f"/api/connections/{cid}/test").json()["ok"]
    pid = client.post("/api/projects", json={"name": "scan", "oracle_connection_id": cid, "schemas": ["MIGDEMO"]}).json()["id"]

    scan = client.post(f"/api/projects/{pid}/scans").json()
    execute_job(enqueued[-1].id, session_factory)

    scan = client.get(f"/api/scans/{scan['id']}").json()
    assert scan["status"] == "succeeded", scan["error"]
    assert scan["warnings"] == []
    assert scan["ora2pg_reports"][0]["ok"] and scan["ora2pg_reports"][0]["total_cost"] > 0

    objects = {
        (o["object_type"], o["name"]): o
        for o in client.get(f"/api/projects/{pid}/objects", params={"limit": 100}).json()["items"]
    }
    assert set(objects) == {
        ("FUNCTION", "GET_CUSTOMER_NAME"), ("FUNCTION", "FORMAT_AMOUNT"), ("PROCEDURE", "LOG_MESSAGE"), ("PROCEDURE", "PROCESS_ORDERS"),
        ("PROCEDURE", "ARCHIVE_OLD_ORDERS"), ("PACKAGE", "PKG_BILLING"), ("PACKAGE BODY", "PKG_BILLING"),
        ("TRIGGER", "TRG_ORDERS_AUDIT"), ("VIEW", "V_CUSTOMER_ORDERS"),
    }
    unsafe = {name for (t, name), o in objects.items() if o["unsafe_to_execute"]}
    assert unsafe == {"LOG_MESSAGE", "ARCHIVE_OLD_ORDERS", "PROCESS_ORDERS", "PKG_BILLING"}
    assert "OUTER_JOIN_PLUS" in objects[("VIEW", "V_CUSTOMER_ORDERS")]["finding_codes"]
    assert "NO_DATA_FOUND_HANDLER" in objects[("FUNCTION", "GET_CUSTOMER_NAME")]["finding_codes"]
    assert "PACKAGE_STATE" in objects[("PACKAGE", "PKG_BILLING")]["finding_codes"]
    assert objects[("PROCEDURE", "ARCHIVE_OLD_ORDERS")]["ora2pg_cost"] > objects[("FUNCTION", "GET_CUSTOMER_NAME")]["ora2pg_cost"]

    pkg = client.get(f"/api/objects/{objects[('PACKAGE', 'PKG_BILLING')]['id']}").json()
    assert {r["signature"] for r in pkg["routines"]} == {
        "PKG_BILLING.CALC_TOTAL(P_ORDER_ID IN NUMBER) RETURN NUMBER",
        "PKG_BILLING.APPLY_DISCOUNT(P_CUSTOMER_ID IN NUMBER, P_PCT IN NUMBER)",
        "PKG_BILLING.TIER_LABEL(P_TIER IN VARCHAR2) RETURN VARCHAR2",
    }
    trigger = client.get(f"/api/objects/{objects[('TRIGGER', 'TRG_ORDERS_AUDIT')]['id']}").json()
    assert trigger["metadata"]["trigger"]["table"] == "MIGDEMO.ORDERS"

    # A second scan updates in place.
    client.post(f"/api/projects/{pid}/scans", json={"include_ora2pg_cost": False})
    execute_job(enqueued[-1].id, session_factory)
    rescan = client.get(f"/api/projects/{pid}/scans").json()[0]
    assert rescan["status"] == "succeeded"
    assert (rescan["summary"]["added"], rescan["summary"]["changed"], rescan["summary"]["removed"]) == (0, 0, 0)


def test_scenario2_finds_the_planted_migration_bugs(client, oracle_endpoint, session_factory, enqueued):
    """Scan the Oracle sandbox, compare it with the PostgreSQL sandbox (see
    docker/postgres-sandbox), and check that every deliberately planted bug is reported."""
    import os

    import pytest

    if not os.environ.get("PG_TARGET_TEST_HOST"):
        pytest.skip("set PG_TARGET_TEST_HOST/DB/USER/PASSWORD to run the comparison test")
    ora = client.post("/api/connections", json=endpoint_payload(oracle_endpoint, "cmp-ora")).json()["id"]
    pg = client.post("/api/connections", json={
        "name": "cmp-pg", "kind": "postgres", "host": os.environ["PG_TARGET_TEST_HOST"], "port": 5432,
        "database": os.environ["PG_TARGET_TEST_DB"], "username": os.environ["PG_TARGET_TEST_USER"],
        "password": os.environ["PG_TARGET_TEST_PASSWORD"], "options": {"sslmode": "disable"},
    }).json()["id"]
    client.post(f"/api/connections/{ora}/test")
    pid = client.post("/api/projects", json={"name": "cmp", "oracle_connection_id": ora,
                                             "postgres_connection_id": pg, "schemas": ["MIGDEMO"]}).json()["id"]
    client.post(f"/api/projects/{pid}/scans", json={"include_ora2pg_cost": False})
    execute_job(enqueued[-1].id, session_factory)
    client.post(f"/api/projects/{pid}/comparisons")
    execute_job(enqueued[-1].id, session_factory)

    pairs = {p["oracle_name"]: p for p in client.get(f"/api/projects/{pid}/pairs", params={"limit": 100}).json()["items"]}

    def codes(name):
        detail = client.get(f"/api/pairs/{pairs[name]['id']}").json()
        return {f["code"] for f in detail["static_findings"]} | {d["code"] for d in detail["signature_diffs"]}

    assert pairs["MIGDEMO.FORMAT_AMOUNT"]["status"] == "missing_in_target"
    assert "OUTER_JOIN_LOST" in codes("MIGDEMO.V_CUSTOMER_ORDERS")
    assert "DEAD_NO_DATA_FOUND_HANDLER" in codes("MIGDEMO.GET_CUSTOMER_NAME")
    assert "AUTONOMOUS_TXN_LOST" in codes("MIGDEMO.LOG_MESSAGE")
    assert {"PLPGSQL_CHECK_ERROR", "ARG_TYPE"} <= codes("MIGDEMO.ARCHIVE_OLD_ORDERS")
    assert "ARG_ORDER" in codes("MIGDEMO.PKG_BILLING.APPLY_DISCOUNT")
    assert "EMPTY_STRING_IS_NULL" in codes("MIGDEMO.PKG_BILLING.TIER_LABEL")
    assert "CALLS_RISKY_ROUTINE" in codes("MIGDEMO.PROCESS_ORDERS")
    assert pairs["MIGDEMO.PKG_BILLING.CALC_TOTAL"]["risk"] == "none"
    assert pairs["MIGDEMO.TRG_ORDERS_AUDIT"]["status"] == "matched"
    unpaired = client.get(f"/api/projects/{pid}/target-objects", params={"unpaired": "true"}).json()
    assert [t["display_name"] for t in unpaired] == ["migdemo.legacy_cleanup()"]


def test_schema_explorer_snapshot_of_sandbox(client, oracle_endpoint, session_factory, enqueued):
    cid = client.post("/api/connections", json=endpoint_payload(oracle_endpoint, "sandbox-explorer")).json()["id"]
    snap = client.post(f"/api/connections/{cid}/catalog-snapshots", json={"schemas": ["migdemo"]}).json()
    assert snap["requested_schemas"] == ["MIGDEMO"]
    execute_job(uuid.UUID(snap["job_id"]), session_factory)
    snap = client.get(f"/api/catalog-snapshots/{snap['id']}").json()
    assert snap["status"] == "succeeded", snap["error"]
    assert snap["dictionary"] == "DBA"

    tables = {t["name"]: t for t in client.get(f"/api/catalog-snapshots/{snap['id']}/tables").json()["items"]}
    orders = client.get(f"/api/catalog-tables/{tables['ORDERS']['id']}").json()
    assert [r["other_label"] for r in orders["references"]] == ["MIGDEMO.CUSTOMERS"]
    assert "MIGDEMO.V_CUSTOMER_ORDERS" in [v["other_label"] for v in orders["read_by_views"]]
    used = {u["code"]["name"]: u for u in orders["used_by"]}
    assert "delete" in used["ARCHIVE_OLD_ORDERS"]["operations"]
    assert "dictionary" in used["ARCHIVE_OLD_ORDERS"]["found_by"]
    assert "update" in used["PKG_BILLING"]["operations"]
    assert used["PKG_BILLING"]["subprograms"]
    assert [t["name"] for t in orders["triggers"]] == ["TRG_ORDERS_AUDIT"]
    pk = next(ix for ix in orders["indexes"] if ix["primary"])
    assert pk["columns"] == ["ORDER_ID"] and pk["unique"]
    # ORDERS.CUSTOMER_ID has a foreign key but no index.
    assert orders["references"][0]["details"]["indexed"] is False
    assert orders["unindexed_fk_count"] == 1

    code = client.get(f"/api/catalog-snapshots/{snap['id']}/code", params={"kind": "trigger"}).json()["items"]
    assert [c["name"] for c in code] == ["TRG_ORDERS_AUDIT"]
    found = client.get(f"/api/catalog-snapshots/{snap['id']}/code",
                       params={"q": "orders_archive", "in_source": "true"}).json()["items"]
    assert [c["name"] for c in found] == ["ARCHIVE_OLD_ORDERS"]
