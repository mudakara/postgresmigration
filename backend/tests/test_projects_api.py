from tests.conftest import endpoint_payload


def make_connections(client, pg_endpoint):
    ora = client.post("/api/connections", json={
        "name": "ora", "kind": "oracle", "host": "ora.example", "port": 1521,
        "database": "ORCLPDB1", "username": "R", "password": "pw",
    }).json()
    pg = client.post("/api/connections", json=endpoint_payload(pg_endpoint, "pg")).json()
    return ora, pg


def test_create_and_update_project(client, app_db_endpoint):
    ora, pg = make_connections(client, app_db_endpoint)
    resp = client.post("/api/projects", json={
        "name": "billing", "oracle_connection_id": ora["id"],
        "postgres_connection_id": pg["id"], "schemas": ["BILLING", "C##SHARED"],
    })
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    resp = client.patch(f"/api/projects/{pid}", json={"schemas": ["BILLING"]})
    assert resp.json()["schemas"] == ["BILLING"]


def test_connection_kinds_are_enforced(client, app_db_endpoint):
    ora, pg = make_connections(client, app_db_endpoint)
    resp = client.post("/api/projects", json={"name": "x", "oracle_connection_id": pg["id"]})
    assert resp.status_code == 422
    resp = client.post("/api/projects", json={
        "name": "x", "oracle_connection_id": ora["id"], "postgres_connection_id": ora["id"],
    })
    assert resp.status_code == 422


def test_oracle_connection_cannot_be_cleared(client, app_db_endpoint):
    ora, _ = make_connections(client, app_db_endpoint)
    pid = client.post("/api/projects", json={"name": "x", "oracle_connection_id": ora["id"]}).json()["id"]
    assert client.patch(f"/api/projects/{pid}", json={"oracle_connection_id": None}).status_code == 422


def test_schema_names_are_validated(client, app_db_endpoint):
    ora, _ = make_connections(client, app_db_endpoint)
    resp = client.post("/api/projects", json={
        "name": "x", "oracle_connection_id": ora["id"], "schemas": ["bad name"],
    })
    assert resp.status_code == 422


def test_target_schemas_are_validated(client, app_db_endpoint):
    ora, pg = make_connections(client, app_db_endpoint)
    ok = client.post("/api/projects", json={
        "name": "t", "oracle_connection_id": ora["id"], "settings": {"target_schemas": ["sales", "billing"]},
    })
    assert ok.status_code == 201 and ok.json()["settings"]["target_schemas"] == ["sales", "billing"]
    for bad in (["sa*"], ["a b"], "sales"):
        resp = client.patch(f"/api/projects/{ok.json()['id']}", json={"settings": {"target_schemas": bad}})
        assert resp.status_code == 422, bad
