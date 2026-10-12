from app.scan.plsql import is_wrapped, mask, package_level_declarations
from app.scan.rules import analyze, complexity_tier, sys_packages


def codes(analysis):
    return set(analysis.codes)


# --- masking ------------------------------------------------------------------

def test_mask_preserves_offsets_and_lines():
    src = "BEGIN -- COMMIT here\n  x := 'it''s (+)';\n  /* ROLLBACK\n */ COMMIT;\nEND;"
    m = mask(src)
    assert len(m.code) == len(src) == len(m.with_strings)
    assert m.code.count("\n") == src.count("\n")
    assert "COMMIT here" not in m.code and "ROLLBACK" not in m.code
    assert "(+)" not in m.code and "it''s (+)" in m.with_strings
    assert m.line_of(src.index("COMMIT;")) == 4


def test_mask_handles_q_quotes_and_quoted_identifiers():
    src = """x := q'[it's COMMIT]'; y := Q'{a}b}'; "Weird--Name" := 1; z := nq'!x!';"""
    m = mask(src)
    assert "COMMIT" not in m.code
    assert '"Weird--Name"' in m.code  # not treated as a comment
    assert "z := nq'" in m.code


def test_empty_string_literals_are_detected_but_escaped_quotes_are_not():
    m = mask("IF a = '' THEN b := 'it''s'; END IF; c := '''';")
    assert len(m.empty_string_offsets) == 1


def test_wrapped_detection():
    assert is_wrapped("CREATE OR REPLACE PACKAGE BODY billing wrapped \na000000\n1\n")
    assert is_wrapped("PROCEDURE p wrapped\nabcd")
    assert not is_wrapped("PROCEDURE wrapped_thing IS BEGIN NULL; END;")


# --- package state --------------------------------------------------------------

SPEC = """PACKAGE pkg_billing AS
  g_tax_rate NUMBER := 0.2;  -- state
  c_max CONSTANT PLS_INTEGER := 10;
  e_bad EXCEPTION;
  TYPE t_rec IS RECORD (a NUMBER, b VARCHAR2(10));
  CURSOR c_all IS SELECT * FROM t;
  FUNCTION calc_total (p_order_id IN NUMBER) RETURN NUMBER;
END pkg_billing;
"""

BODY = """PACKAGE BODY pkg_billing AS
  g_cache VARCHAR2(100);
  FUNCTION calc_total (p_order_id IN NUMBER) RETURN NUMBER IS
    v_local NUMBER;
  BEGIN
    RETURN 1;
  END;
END pkg_billing;
"""


def test_package_declarations_in_spec():
    decls = package_level_declarations(mask(SPEC), "PACKAGE")
    assert [(d.name, d.kind, d.line) for d in decls] == [
        ("g_tax_rate", "variable", 2),
        ("c_max", "constant", 3),
    ]


def test_package_body_globals_stop_at_first_subprogram():
    decls = package_level_declarations(mask(BODY), "PACKAGE BODY")
    assert [d.name for d in decls] == ["g_cache"]  # v_local is a local, not package state


def test_constants_alone_are_not_package_state_findings():
    spec = "PACKAGE k AS\n  c_one CONSTANT NUMBER := 1;\nEND k;\n"
    assert "PACKAGE_STATE" not in codes(analyze("PACKAGE", spec))
    assert "PACKAGE_STATE" in codes(analyze("PACKAGE", SPEC))


# --- rules --------------------------------------------------------------------

PROC = """PROCEDURE archive (p_days IN NUMBER) IS
  PRAGMA AUTONOMOUS_TRANSACTION;
  v_name VARCHAR2(10);
BEGIN
  -- COMMIT is mentioned in a comment only on this line
  SELECT name INTO v_name FROM customers c, orders o WHERE c.id = o.cid(+) AND ROWNUM = 1;
  IF v_name = '' THEN NULL; END IF;
  EXECUTE IMMEDIATE 'TRUNCATE TABLE staging';
  COMMIT;
EXCEPTION
  WHEN NO_DATA_FOUND THEN NULL;
END;
"""


def test_rules_fire_with_correct_lines():
    a = analyze("PROCEDURE", PROC)
    by_code = {f.code: f for f in a.findings}
    assert by_code["TXN_CONTROL"].lines == [9]  # not line 5's comment
    assert by_code["OUTER_JOIN_PLUS"].lines == [6]
    assert by_code["ROWNUM"].lines == [6]
    assert by_code["SELECT_INTO"].lines == [6]
    assert by_code["EMPTY_STRING"].lines == [7]
    assert by_code["DYNAMIC_DDL"].lines == [8]
    assert {"AUTONOMOUS_TXN", "NO_DATA_FOUND_HANDLER", "DYNAMIC_SQL"} <= codes(a)
    assert a.unsafe


def test_findings_are_ordered_by_category_then_severity():
    a = analyze("PROCEDURE", PROC)
    assert a.findings[0].code in {"AUTONOMOUS_TXN", "DYNAMIC_DDL", "TXN_CONTROL"}


def test_bulk_collect_is_not_a_single_row_select_into():
    a = analyze("PROCEDURE", "PROCEDURE p IS BEGIN SELECT id BULK COLLECT INTO v FROM t; END;")
    assert "BULK_COLLECT" in codes(a) and "SELECT_INTO" not in codes(a)


def test_strings_do_not_trigger_rules():
    a = analyze("PROCEDURE", "PROCEDURE p IS BEGIN log('COMMIT (+) ROWNUM CONNECT BY'); END;")
    assert codes(a) == set()
    assert not a.unsafe


def test_trigger_only_rules_respect_object_type():
    body = "BEGIN IF INSERTING THEN NULL; END IF; END;"
    assert "TRIGGER_PREDICATES" in codes(analyze("TRIGGER", "TRIGGER t BEFORE INSERT ON x FOR EACH ROW " + body))
    assert "TRIGGER_PREDICATES" not in codes(analyze("PROCEDURE", "PROCEDURE p IS " + body))


def test_db_link_from_text_or_dependency():
    assert "DB_LINK" in codes(analyze("PROCEDURE", "PROCEDURE p IS BEGIN DELETE FROM t@remote; END;"))
    a = analyze("VIEW", "CREATE VIEW v AS SELECT 1 FROM dual", has_db_link_dependency=True)
    assert "DB_LINK" in codes(a) and a.unsafe


def test_wrapped_and_invalid_are_blockers_and_skip_text_rules():
    a = analyze("PACKAGE BODY", "PACKAGE BODY p wrapped\na000000\nCOMMIT\n", status="INVALID")
    assert codes(a) == {"WRAPPED", "INVALID_IN_ORACLE"}
    assert a.wrapped


def test_effort_heuristic_and_tiers():
    simple = analyze("FUNCTION", "FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END;")
    assert complexity_tier(simple.heuristic_effort) == "simple"
    assert complexity_tier(16) == "complex" and complexity_tier(41) == "very_complex"


def test_sys_packages_ignore_dictionary_synonyms():
    refs = [
        ("SYS", "DBMS_OUTPUT", "PACKAGE"), ("PUBLIC", "DBMS_OUTPUT", "SYNONYM"),
        ("PUBLIC", "USER_TABLES", "SYNONYM"), ("SYS", "STANDARD", "PACKAGE"),
        ("PUBLIC", "UTL_FILE", "SYNONYM"), ("MIGDEMO", "ORDERS", "TABLE"),
    ]
    assert sys_packages(refs) == ["DBMS_OUTPUT", "UTL_FILE"]
