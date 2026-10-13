"""The connection's schema scope limits every feature (app.services.endpoints.scoped_schemas)."""

import pytest

from app.models import Connection, DbKind
from app.services.endpoints import schema_scope, scoped_schemas
from tests.conftest import endpoint_payload
from tests.test_catalog_explorer import shop_db, snapshot  # noqa: F401  (fixture)


def conn(kind, schemas):
    return Connection(name="c", kind=kind, host="h", port=1, database="d", username="u", password_encrypted="x",
                      options={"schemas": schemas})


def test_scope_rules():
    ora = conn(DbKind.ORACLE, ["sales", "BILLING"])
    assert schema_scope(ora) == ["SALES", "BILLING"]
    assert scoped_schemas(ora, []) == ["SALES", "BILLING"]  # nothing requested: the whole scope
    assert scoped_schemas(ora, ["billing", "OTHER"]) == ["BILLING"]  # requests are limited to the scope
    with pytest.raises(ValueError, match="scope"):
        scoped_schemas(ora, ["OTHER"])
    pg = conn(DbKind.POSTGRES, [])
    assert scoped_schemas(pg, []) == [] and scoped_schemas(pg, ["x"]) == ["x"]  # no scope: unchanged


def test_scope_is_saved_and_applied_to_catalog_reads(client, session_factory, shop_db):
    payload = endpoint_payload(shop_db, "scoped")
    payload["options"] = {**payload["options"], "schemas": ["shop"]}
    created = client.post("/api/connections", json=payload).json()
    assert created["options"]["schemas"] == ["shop"]
    snap = snapshot(client, session_factory, created["id"], schemas=())
    assert snap["requested_schemas"] == ["shop"] and snap["schemas"] == ["shop"]
    resp = client.post(f"/api/connections/{created['id']}/catalog-snapshots", json={"schemas": ["public"]})
    assert resp.status_code == 422 and "scope" in resp.json()["detail"]
    bad = client.patch(f"/api/connections/{created['id']}", json={"options": {"schemas": ["no spaces allowed"]}})
    assert bad.status_code == 422
