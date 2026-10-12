"""Hand-built extractions so persistence and the API can be tested without Oracle."""

from datetime import datetime

from app.scan.extract import ExtractedDependency, ExtractedObject, ExtractedRoutine, Extraction

T0 = datetime(2026, 1, 1, 12, 0)

LOG_MESSAGE = """CREATE OR REPLACE PROCEDURE log_message (p IN VARCHAR2) IS
  PRAGMA AUTONOMOUS_TRANSACTION;
BEGIN
  INSERT INTO app_log VALUES (SYSDATE, p);
  COMMIT;
END;
"""

GET_NAME = """CREATE OR REPLACE FUNCTION get_name (p_id IN NUMBER) RETURN VARCHAR2 IS
  v VARCHAR2(100);
BEGIN
  SELECT name INTO v FROM customers WHERE id = p_id;
  RETURN v;
EXCEPTION WHEN NO_DATA_FOUND THEN RETURN NULL;
END;
"""

PROCESS = """CREATE OR REPLACE PROCEDURE process (p_id IN NUMBER, p_total OUT NUMBER) IS
BEGIN
  p_total := 0;
  log_message('x');
END;
"""

PKG_SPEC = """CREATE OR REPLACE PACKAGE pkg AS
  g_rate NUMBER := 0.2;
  FUNCTION total (p_id IN NUMBER) RETURN NUMBER;
END pkg;
"""

PKG_BODY = """CREATE OR REPLACE PACKAGE BODY pkg AS
  FUNCTION total (p_id IN NUMBER) RETURN NUMBER IS
  BEGIN
    log_message('total');
    RETURN 1;
  END;
END pkg;
"""

VIEW = "CREATE OR REPLACE VIEW APP.V AS\nSELECT c.id FROM customers c, orders o WHERE c.id = o.cid(+)"


def obj(name, object_type, source, status="VALID"):
    return ExtractedObject("APP", name, object_type, status, T0, T0, source, {})


def routine(name, kind, package=None, args=(), return_type=None):
    return ExtractedRoutine(
        owner="APP", package_name=package, name=name, overload="", subprogram_id=1, kind=kind,
        return_type=return_type, arguments=[
            {"position": i + 1, "name": a, "mode": "IN", "type": "NUMBER", "data_type": "NUMBER", "defaulted": False}
            for i, a in enumerate(args)
        ], attributes={},
    )


def dep(name, object_type, ref_name, ref_type, ref_owner="APP", link=None):
    return ExtractedDependency("APP", name, object_type, ref_owner, ref_name, ref_type, link)


def sample_extraction(**overrides) -> Extraction:
    objects = [
        obj("LOG_MESSAGE", "PROCEDURE", LOG_MESSAGE),
        obj("GET_NAME", "FUNCTION", GET_NAME),
        obj("PROCESS", "PROCEDURE", PROCESS),
        obj("PKG", "PACKAGE", PKG_SPEC),
        obj("PKG", "PACKAGE BODY", PKG_BODY),
        obj("V", "VIEW", VIEW),
    ]
    routines = [
        routine("LOG_MESSAGE", "PROCEDURE", args=["P"]),
        routine("GET_NAME", "FUNCTION", args=["P_ID"], return_type="VARCHAR2"),
        routine("PROCESS", "PROCEDURE", args=["P_ID", "P_TOTAL"]),
        routine("TOTAL", "FUNCTION", package="PKG", args=["P_ID"], return_type="NUMBER"),
    ]
    dependencies = [
        dep("PROCESS", "PROCEDURE", "LOG_MESSAGE", "PROCEDURE"),
        dep("PKG", "PACKAGE BODY", "PKG", "PACKAGE"),
        dep("PKG", "PACKAGE BODY", "LOG_MESSAGE", "PROCEDURE"),
        dep("GET_NAME", "FUNCTION", "CUSTOMERS", "TABLE"),
        dep("V", "VIEW", "CUSTOMERS", "TABLE"),
        dep("PROCESS", "PROCEDURE", "DBMS_OUTPUT", "SYNONYM", ref_owner="PUBLIC"),
    ]
    values = dict(
        oracle_version="19.21.0.0.0", dictionary="DBA", schemas=["APP"], missing_schemas=[],
        object_counts=[{"owner": "APP", "object_type": "TABLE", "count": 3, "invalid": 0}],
        objects=objects, routines=routines, dependencies=dependencies, temporary_tables=set(),
    )
    values.update(overrides)
    return Extraction(**values)
