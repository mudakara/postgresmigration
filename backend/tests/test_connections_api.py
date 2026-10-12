from sqlalchemy import select

from app.models import Connection
from tests.conftest import endpoint_payload


def oracle_payload(name="ora-src", **overrides):
    payload = {
        "name": name, "kind": "oracle", "host": "ora.example", "port": 1521,
        "database": "ORCLPDB1", "username": "STUDIO_READER", "password": "pw",
    }
    return {**payload, **overrides}


def test_create_hides_and_encrypts_password(client, session_factory):
    resp = client.post("/api/connections", json=oracle_payload(password="Sup3r-secret"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "password" not in body and "password_encrypted" not in body

    with session_factory() as s:
        stored = s.scalars(select(Connection)).one()
    assert "Sup3r-secret" not in stored.password_encrypted


def test_duplicate_name_conflicts(client):
    assert client.post("/api/connections", json=oracle_payload()).status_code == 201
    assert client.post("/api/connections", json=oracle_payload()).status_code == 409


def test_rejects_values_unsafe_for_ora2pg_config(client):
    resp = client.post("/api/connections", json=oracle_payload(host="evil;sid=x"))
    assert resp.status_code == 422
    resp = client.post("/api/connections", json=oracle_payload(username="a\nORACLE_HOME /tmp"))
    assert resp.status_code == 422


def test_options_must_match_kind(client):
    resp = client.post("/api/connections", json=oracle_payload(options={"sslmode": "require"}))
    assert resp.status_code == 422


def test_update_revalidates_options_against_kind(client, app_db_endpoint):
    created = client.post("/api/connections", json=endpoint_payload(app_db_endpoint)).json()
    resp = client.patch(f"/api/connections/{created['id']}", json={"options": {"use_sid": True}})
    assert resp.status_code == 422


def test_update_password_keeps_other_fields_and_resets_test_status(client, app_db_endpoint):
    created = client.post("/api/connections", json=endpoint_payload(app_db_endpoint)).json()
    cid = created["id"]
    assert client.post(f"/api/connections/{cid}/test").json()["ok"] is True

    client.patch(f"/api/connections/{cid}", json={"password": "wrong-password"})
    after = client.get(f"/api/connections/{cid}").json()
    assert after["host"] == app_db_endpoint.host
    assert after["last_test_ok"] is None

    result = client.post(f"/api/connections/{cid}/test").json()
    assert result["ok"] is False
    assert "password authentication failed" in result["message"]


def test_probe_real_postgres_records_version_and_extensions(client, app_db_endpoint):
    cid = client.post("/api/connections", json=endpoint_payload(app_db_endpoint)).json()["id"]
    result = client.post(f"/api/connections/{cid}/test").json()
    assert result["ok"] is True, result
    assert result["server_version"].startswith("16")
    assert set(result["capabilities"]["extensions"]) == {"plpgsql_check", "orafce", "pg_stat_statements"}

    saved = client.get(f"/api/connections/{cid}").json()
    assert saved["last_test_ok"] is True
    assert saved["server_version"].startswith("16")


def test_unsaved_connection_test_reports_unreachable_host(client):
    payload = oracle_payload(host="127.0.0.1", port=1)
    payload.pop("name")
    result = client.post("/api/connections/test", json=payload).json()
    assert result["ok"] is False
    assert result["message"]


def test_delete_blocked_while_used_by_project(client, app_db_endpoint):
    ora = client.post("/api/connections", json=oracle_payload()).json()
    client.post("/api/projects", json={"name": "p", "oracle_connection_id": ora["id"]})
    assert client.delete(f"/api/connections/{ora['id']}").status_code == 409


def test_ora2pg_check_queues_job_for_oracle_only(client, enqueued, app_db_endpoint):
    ora = client.post("/api/connections", json=oracle_payload()).json()
    resp = client.post(f"/api/connections/{ora['id']}/ora2pg-check")
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert [str(j.id) for j in enqueued] == [resp.json()["id"]]

    pg = client.post("/api/connections", json=endpoint_payload(app_db_endpoint, "pg")).json()
    assert client.post(f"/api/connections/{pg['id']}/ora2pg-check").status_code == 409
