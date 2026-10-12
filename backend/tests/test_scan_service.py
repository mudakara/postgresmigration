import json
import uuid

import pytest
from sqlalchemy import select

from app.models import Connection, DbKind, ObjectDependency, Project, Scan, SourceObject, SourceRoutine
from app.scan.cost_report import parse_report
from app.scan.service import analyze_extraction, persist
from tests import scan_fixtures as fx

REPORT = {
    "ora2pg version": 25.0, "total cost": 50.7, "human days cost": "2 person-day(s)", "migration level": "B-5",
    "objects": [
        {"object": "FUNCTION", "number": 1, "invalid": 0, "cost value": 4.0, "details": "get_name: 3. "},
        {"object": "PROCEDURE", "number": 2, "invalid": 0, "cost value": 7, "details": "log_message: 3.2. process: 3.4. "},
        {"object": "PACKAGE BODY", "number": 1, "invalid": 0, "cost value": 9,
         "details": "pkg.total: 4.1. pkg.total: 1. pkg.other: 3. "},
        {"object": "VIEW", "number": 1, "invalid": 0, "cost value": 1.0, "details": ""},
        {"object": "TABLE", "number": 3, "invalid": 0, "cost value": 1.0, "details": "orders has 3 rows. "},
    ],
}


def test_parse_report_costs_objects_and_package_members():
    report = parse_report("APP", "progress noise\n" + json.dumps(REPORT))
    assert report.ok and report.total_cost == 50.7
    assert report.human_days == 2.0 and report.migration_level == "B-5"
    assert report.object_costs[("FUNCTION", "get_name")] == 3
    assert report.object_costs[("PROCEDURE", "process")] == 3.4
    assert report.member_costs[("pkg", "total")] == pytest.approx(5.1)  # overloads summed
    assert report.object_costs[("PACKAGE BODY", "pkg")] == pytest.approx(8.1)
    assert ("TABLE", "orders") not in report.object_costs


def test_parse_report_rejects_non_json():
    with pytest.raises(ValueError):
        parse_report("APP", "FATAL: something")


def test_analysis_propagates_unsafety_through_calls_and_package_specs():
    extraction = fx.sample_extraction()
    extraction.objects.append(fx.obj("USES_PKG", "PROCEDURE", "CREATE OR REPLACE PROCEDURE uses_pkg IS BEGIN NULL; END;"))
    extraction.dependencies.append(fx.dep("USES_PKG", "PROCEDURE", "PKG", "PACKAGE"))

    analyzed = {a.extracted.key: a for a in analyze_extraction(extraction, {})}
    unsafe = {key[2] + " " + key[1] for key, a in analyzed.items() if a.analysis.unsafe}
    assert unsafe == {"LOG_MESSAGE PROCEDURE", "PROCESS PROCEDURE", "PKG PACKAGE BODY", "USES_PKG PROCEDURE"}
    # The spec itself is not flagged, only used to carry the body's unsafety to callers.
    assert not analyzed[("APP", "PACKAGE", "PKG")].analysis.unsafe
    calls = next(f for f in analyzed[("APP", "PROCEDURE", "PROCESS")].analysis.findings if f.code == "CALLS_UNSAFE")
    assert calls.detail == "Depends on APP.LOG_MESSAGE"


def test_analysis_uses_ora2pg_cost_when_available():
    costs = {"APP": parse_report("APP", json.dumps(REPORT))}
    analyzed = {a.extracted.key: a for a in analyze_extraction(fx.sample_extraction(), costs)}
    assert analyzed[("APP", "FUNCTION", "GET_NAME")].ora2pg_cost == 3
    assert analyzed[("APP", "VIEW", "V")].ora2pg_cost == 1.0  # omitted from details: base score
    spec = analyzed[("APP", "PACKAGE", "PKG")]
    assert spec.ora2pg_cost is None and spec.effort == spec.analysis.heuristic_effort


@pytest.fixture
def project(session_factory):
    with session_factory() as s:
        conn = Connection(name="ora", kind=DbKind.ORACLE, host="h", port=1521, database="d",
                          username="u", password_encrypted="x", options={}, capabilities={})
        s.add(conn)
        s.flush()
        p = Project(name="p", oracle_connection_id=conn.id, schemas=["APP"], settings={})
        s.add(p)
        s.commit()
        return p.id


def run_persist(session_factory, project_id, extraction, costs=None):
    with session_factory() as s:
        project = s.get(Project, project_id)
        scan = Scan(project_id=project_id, schemas=["APP"])
        s.add(scan)
        s.flush()
        summary = persist(s, project, scan, extraction, analyze_extraction(extraction, costs or {}), costs or {})
        s.commit()
        return scan.id, summary


def test_persist_creates_inventory_routines_and_resolved_dependencies(session_factory, project):
    costs = {"APP": parse_report("APP", json.dumps(REPORT))}
    _, summary = run_persist(session_factory, project, fx.sample_extraction(), costs)
    assert summary["objects"] == 6 and summary["added"] == 6 and summary["routines"] == 4
    assert summary["unsafe"] == 3

    with session_factory() as s:
        objs = {(o.object_type, o.name): o for o in s.scalars(select(SourceObject))}
        assert objs[("VIEW", "V")].finding_codes == ["OUTER_JOIN_PLUS"]
        assert objs[("PACKAGE", "PKG")].metadata_["package_state"][0]["name"] == "g_rate"
        assert objs[("PROCEDURE", "PROCESS")].sys_packages == ["DBMS_OUTPUT"]

        total = s.scalars(select(SourceRoutine).where(SourceRoutine.name == "TOTAL")).one()
        assert total.object_id == objs[("PACKAGE", "PKG")].id
        assert total.signature == "PKG.TOTAL(P_ID IN NUMBER) RETURN NUMBER"
        assert total.ora2pg_cost == pytest.approx(5.1)

        deps = s.scalars(select(ObjectDependency).where(ObjectDependency.object_id == objs[("PROCEDURE", "PROCESS")].id)).all()
        resolved = {d.referenced_name: d.referenced_object_id for d in deps}
        assert resolved["LOG_MESSAGE"] == objs[("PROCEDURE", "LOG_MESSAGE")].id
        assert resolved["DBMS_OUTPUT"] is None


def test_rescan_detects_changes_removals_and_restores_with_stable_ids(session_factory, project):
    first_scan, _ = run_persist(session_factory, project, fx.sample_extraction())
    with session_factory() as s:
        ids = {(o.object_type, o.name): o.id for o in s.scalars(select(SourceObject))}
        routine_id = s.scalars(select(SourceRoutine.id).where(SourceRoutine.name == "TOTAL")).one()

    changed = fx.sample_extraction()
    changed.objects = [o for o in changed.objects if o.name != "V"]
    changed.objects[1].source = fx.GET_NAME.replace("RETURN v;", "RETURN UPPER(v);")
    second_scan, summary = run_persist(session_factory, project, changed)
    assert (summary["added"], summary["changed"], summary["removed"]) == (0, 1, 1)

    with session_factory() as s:
        objs = {(o.object_type, o.name): o for o in s.scalars(select(SourceObject))}
        assert {k: o.id for k, o in objs.items()} == ids  # ids are stable across scans
        assert objs[("VIEW", "V")].removed_at is not None
        assert objs[("FUNCTION", "GET_NAME")].source_changed_scan_id == second_scan
        assert objs[("PROCEDURE", "PROCESS")].source_changed_scan_id == first_scan
        assert s.scalars(select(SourceRoutine.id).where(SourceRoutine.name == "TOTAL")).one() == routine_id

    _, summary = run_persist(session_factory, project, fx.sample_extraction())
    assert summary["restored"] == 1
    with session_factory() as s:
        assert s.scalars(select(SourceObject).where(SourceObject.name == "V")).one().removed_at is None


def test_routines_dropped_from_a_package_are_deleted(session_factory, project):
    run_persist(session_factory, project, fx.sample_extraction())
    smaller = fx.sample_extraction()
    smaller.routines = [r for r in smaller.routines if r.name != "TOTAL"]
    _, summary = run_persist(session_factory, project, smaller)
    assert summary["routines"] == 3
    with session_factory() as s:
        assert s.scalars(select(SourceRoutine).where(SourceRoutine.name == "TOTAL")).first() is None


def test_unscanned_schemas_are_left_untouched(session_factory, project):
    run_persist(session_factory, project, fx.sample_extraction())
    other = fx.sample_extraction(schemas=["OTHER"], objects=[], routines=[], dependencies=[])
    _, summary = run_persist(session_factory, project, other)
    assert summary["removed"] == 0


@pytest.fixture
def scanned_project(session_factory, project):
    costs = {"APP": parse_report("APP", json.dumps(REPORT))}
    run_persist(session_factory, project, fx.sample_extraction(), costs)
    return project


def test_report_aggregates_inventory(client, scanned_project):
    report = client.get(f"/api/projects/{scanned_project}/report").json()
    assert report["totals"]["objects"] == 6
    assert report["totals"]["routines"] == {"standalone": 3, "package_members": 1, "procedures": 2, "functions": 2}
    assert report["totals"]["unsafe_to_execute"] == 3
    codes = {f["code"]: f["objects"] for f in report["findings"]}
    assert codes["CALLS_UNSAFE"] == 2 and codes["OUTER_JOIN_PLUS"] == 1
    assert report["findings"][0]["category"] == "execution_safety"
    assert sum(t["count"] for t in report["complexity"]) == 6
    assert report["sys_packages"] == [{"name": "DBMS_OUTPUT", "objects": 1}]


def test_object_list_filters_sorts_and_paginates(client, scanned_project):
    base = f"/api/projects/{scanned_project}/objects"
    assert client.get(base, params={"type": ["PACKAGE", "PACKAGE BODY"]}).json()["total"] == 2
    assert [o["name"] for o in client.get(base, params={"finding": "OUTER_JOIN_PLUS"}).json()["items"]] == ["V"]
    assert client.get(base, params={"unsafe": "true"}).json()["total"] == 3
    assert client.get(base, params={"category": "semantic_risk"}).json()["total"] >= 2
    assert client.get(base, params={"q": "log"}).json()["items"][0]["name"] == "LOG_MESSAGE"

    page = client.get(base, params={"sort": "effort", "order": "desc", "limit": 2}).json()
    assert page["total"] == 6 and len(page["items"]) == 2
    assert page["items"][0]["effort"] >= page["items"][1]["effort"]
    assert "source" not in page["items"][0]


def test_object_detail_includes_source_routines_and_both_dependency_directions(client, scanned_project):
    items = client.get(f"/api/projects/{scanned_project}/objects", params={"q": "LOG_MESSAGE"}).json()["items"]
    detail = client.get(f"/api/objects/{items[0]['id']}").json()
    assert detail["source"].startswith("CREATE OR REPLACE PROCEDURE log_message")
    assert detail["routines"][0]["signature"] == "LOG_MESSAGE(P IN NUMBER)"
    assert {u["name"] for u in detail["used_by"]} == {"PROCESS", "PKG"}
    assert {f["code"] for f in detail["findings"]} >= {"AUTONOMOUS_TXN", "TXN_CONTROL"}
    assert isinstance(detail["metadata"], dict)


def test_csv_export(client, scanned_project):
    resp = client.get(f"/api/projects/{scanned_project}/objects.csv")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("owner,object_type,name") and len(lines) == 7


def test_start_scan_queues_job_and_blocks_concurrent_scans(client, project, enqueued):
    resp = client.post(f"/api/projects/{project}/scans", json={"include_ora2pg_cost": False})
    assert resp.status_code == 202
    scan = resp.json()
    assert scan["status"] == "queued" and scan["include_ora2pg_cost"] is False
    assert enqueued[0].params == {"scan_id": scan["id"]}
    assert client.post(f"/api/projects/{project}/scans").status_code == 409
    assert [s["id"] for s in client.get(f"/api/projects/{project}/scans").json()] == [scan["id"]]


def test_unknown_project_is_404(client):
    assert client.get(f"/api/projects/{uuid.uuid4()}/report").status_code == 404
    assert client.post(f"/api/projects/{uuid.uuid4()}/scans").status_code == 404


def test_rescan_without_ora2pg_keeps_costs_for_unchanged_objects_only(session_factory, project):
    costs = {"APP": parse_report("APP", json.dumps(REPORT))}
    run_persist(session_factory, project, fx.sample_extraction(), costs)

    changed = fx.sample_extraction()
    changed.objects[1].source = fx.GET_NAME.replace("RETURN v;", "RETURN UPPER(v);")
    run_persist(session_factory, project, changed)  # no Ora2Pg this time

    with session_factory() as s:
        objs = {o.name: o for o in s.scalars(select(SourceObject).where(SourceObject.object_type != "PACKAGE"))}
        assert objs["PROCESS"].ora2pg_cost == 3.4 and objs["PROCESS"].effort == 3.4  # carried over
        assert objs["GET_NAME"].ora2pg_cost is None  # changed: old estimate is stale
        total = s.scalars(select(SourceRoutine).where(SourceRoutine.name == "TOTAL")).one()
        assert total.ora2pg_cost == pytest.approx(5.1)
