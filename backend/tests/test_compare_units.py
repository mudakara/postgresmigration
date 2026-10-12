import uuid

from app.compare.pairing import TargetRef, pair_units, signature_similarity
from app.compare.signatures import compare_signatures, normalize_pg_type, type_compatibility
from app.compare.static_checks import check_pair
from app.compare.units import OracleUnit, extract_member


def ora_arg(name, data_type="NUMBER", mode="IN", defaulted=False, type_=None):
    return {"name": name, "mode": mode, "data_type": data_type, "type": type_ or data_type, "defaulted": defaulted}


def pg_arg(name, type_="numeric", mode="IN"):
    return {"name": name, "mode": mode, "type": type_}


def unit(name, package=None, args=(), kind="routine", routine_kind="FUNCTION", owner="APP", metadata=None):
    return OracleUnit(
        key=f"routine:{name}:{package}", kind=kind, owner=owner, name=name, package=package,
        source_object_id=uuid.uuid4(), source_routine_id=None, display_name=name,
        routine_kind=routine_kind, arguments=list(args), metadata=metadata or {},
    )


def target(schema, name, args=(), kind="function", metadata=None):
    return TargetRef(uuid.uuid4(), schema, name, kind, list(args), metadata or {})


# --- pairing --------------------------------------------------------------------

def test_pairs_by_ora2pg_package_convention_and_same_schema():
    units = [unit("TOTAL", "PKG", [ora_arg("P_ID")]), unit("GET_NAME", None, [ora_arg("P_ID")])]
    t1, t2 = target("pkg", "total", [pg_arg("p_id")]), target("app", "get_name", [pg_arg("p_id")])
    results, conventions = pair_units(units, [t1, t2], {})
    assert [r.target_id for r in results] == [t1.id, t2.id]
    assert conventions == {"schema_per_package": 1, "same_schema": 1}


def test_flattened_package_names_are_found():
    results, conventions = pair_units([unit("TOTAL", "PKG")], [target("app", "pkg_total")], {})
    assert results[0].target_id is not None and conventions == {"flattened": 1}


def test_overloads_go_to_the_best_matching_signature():
    one = unit("F", None, [ora_arg("A")])
    one.key = "routine:1"
    two = unit("F", None, [ora_arg("A"), ora_arg("B")])
    two.key = "routine:2"
    t_two, t_one = target("app", "f", [pg_arg("a"), pg_arg("b")]), target("app", "f", [pg_arg("a")])
    results, _ = pair_units([one, two], [t_two, t_one], {})
    assert results[0].target_id == t_one.id and results[1].target_id == t_two.id


def test_a_target_is_never_claimed_twice_and_manual_matches_win():
    a, b = unit("F", "PKG"), unit("F", None)
    only = target("app", "f")
    results, _ = pair_units([a, b], [only], {})
    assert sum(r.target_id == only.id for r in results) == 1

    results, _ = pair_units([a, b], [only], {a.key: only.id})
    assert results[0].method == "manual" and results[1].target_id is None


def test_unmatched_units_report_missing():
    results, _ = pair_units([unit("NOWHERE")], [target("app", "other")], {})
    assert results[0].target_id is None


def test_triggers_prefer_the_same_table_and_skip_trigger_functions():
    trig = unit("TRG", kind="trigger", metadata={"trigger": {"table": "APP.ORDERS"}})
    wrong = target("app", "trg", kind="trigger", metadata={"table": "app.other"})
    right = target("app", "trg", kind="trigger", metadata={"table": "app.orders"})
    results, _ = pair_units([trig], [wrong, right], {})
    assert results[0].target_id == right.id

    fn = unit("TRG_FN")
    results, _ = pair_units([fn], [target("app", "trg_fn", metadata={"is_trigger_function": True})], {})
    assert results[0].target_id is None


def test_signature_similarity():
    assert signature_similarity([ora_arg("A")], [pg_arg("a")]) == 1.0
    assert signature_similarity([ora_arg("A"), ora_arg("B")], [pg_arg("b"), pg_arg("a")]) == 0.7
    assert signature_similarity([ora_arg("A")], [pg_arg("a"), pg_arg("b")]) < 0.3


# --- signatures -----------------------------------------------------------------

def test_type_compatibility_rules():
    assert type_compatibility(ora_arg("x", "NUMBER"), "numeric")[0] == "ok"
    assert type_compatibility(ora_arg("x", "NUMBER"), "integer")[0] == "risky"
    assert type_compatibility(ora_arg("x", "NUMBER", type_="NUMBER(9)"), "integer")[0] == "ok"
    assert type_compatibility(ora_arg("x", "DATE"), "timestamp(0) without time zone")[0] == "ok"
    assert type_compatibility(ora_arg("x", "DATE"), "date")[0] == "risky"
    assert type_compatibility(ora_arg("x", "VARCHAR2"), "character varying")[0] == "ok"
    assert type_compatibility(ora_arg("x", "VARCHAR2"), "bytea")[0] == "incompatible"
    assert normalize_pg_type("character varying(30)[]") == "varchar[]"


def test_swapped_parameters_are_a_mismatch():
    status, diffs = compare_signatures(
        "PROCEDURE", [ora_arg("P_CUSTOMER_ID"), ora_arg("P_PCT")], None,
        "procedure", [pg_arg("p_pct"), pg_arg("p_customer_id")], None, 0,
    )
    assert status == "mismatch" and {d.code for d in diffs} == {"ARG_ORDER"}


def test_missing_default_and_kind_change():
    status, diffs = compare_signatures(
        "PROCEDURE", [ora_arg("P_DAYS", defaulted=True)], None, "function", [pg_arg("p_days")], "void", 0,
    )
    assert status == "compatible"
    assert {d.code for d in diffs} == {"DEFAULT_MISSING", "KIND"}
    status, _ = compare_signatures(
        "PROCEDURE", [ora_arg("P_DAYS", defaulted=True)], None, "procedure", [pg_arg("p_days")], None, 1,
    )
    assert status == "match"


def test_out_parameters_and_return_types():
    status, diffs = compare_signatures(
        "PROCEDURE", [ora_arg("P_ID"), ora_arg("P_TOTAL", mode="OUT")], None,
        "function", [pg_arg("p_id"), pg_arg("p_total", mode="OUT")], "record", 0,
    )
    assert status == "match", diffs
    status, diffs = compare_signatures("FUNCTION", [], "VARCHAR2", "function", [], "bytea", 0)
    assert status == "mismatch" and diffs[0].code == "RETURN_TYPE"


# --- member extraction ------------------------------------------------------------

BODY = """CREATE OR REPLACE PACKAGE BODY pkg AS
  FUNCTION a (x NUMBER) RETURN NUMBER IS
    FUNCTION helper RETURN NUMBER IS BEGIN RETURN 1; END;
  BEGIN
    RETURN helper;
  END a;

  PROCEDURE b IS
  BEGIN
    NULL; -- FUNCTION a in a comment
  END b;
END pkg;
"""


def test_extract_member_spans_to_the_next_sibling():
    text, start = extract_member(BODY, "A")
    assert start == 2 and "helper" in text and "PROCEDURE b" not in text
    text, start = extract_member(BODY, "b")
    assert start == 8 and text.strip().endswith("END b;")
    assert extract_member(BODY, "missing") is None


# --- static checks ------------------------------------------------------------------

def codes(findings):
    return {f.code for f in findings}


def test_dead_no_data_found_handler_and_strict():
    ora = "FUNCTION f RETURN VARCHAR2 IS v VARCHAR2(10); BEGIN SELECT n INTO v FROM t; RETURN v;\nEXCEPTION WHEN NO_DATA_FOUND THEN RETURN NULL; END;"
    pg_bad = "BEGIN SELECT n INTO v FROM t; RETURN v; EXCEPTION WHEN no_data_found THEN RETURN NULL; END;"
    pg_ok = "BEGIN SELECT n INTO STRICT v FROM t; RETURN v; EXCEPTION WHEN no_data_found THEN RETURN NULL; END;"
    assert "DEAD_NO_DATA_FOUND_HANDLER" in codes(check_pair("routine", ora, 1, [], "function", pg_bad, []))
    assert codes(check_pair("routine", ora, 1, [], "function", pg_ok, [])) == set()


def test_select_from_dual_is_not_a_no_data_found_risk():
    ora = "BEGIN SELECT DECODE(p, 'A', 'x') INTO v FROM dual; END;"
    assert codes(check_pair("routine", ora, 1, [], "function", "BEGIN v := CASE p WHEN 'A' THEN 'x' END; END;", [])) == set()


def test_outer_join_lost_and_kept():
    ora = "SELECT * FROM a, b WHERE a.id = b.id(+)"
    assert "OUTER_JOIN_LOST" in codes(check_pair("view", ora, 1, [], "view", "SELECT * FROM a, b WHERE a.id = b.id", []))
    assert codes(check_pair("view", ora, 1, [], "view", "SELECT * FROM a LEFT JOIN b ON a.id = b.id", [])) == set()


def test_empty_string_param_semantics():
    args = [ora_arg("P_TIER", "VARCHAR2")]
    ora = "BEGIN IF p_tier IS NULL THEN RETURN 'U'; END IF; END;"
    assert "EMPTY_STRING_IS_NULL" in codes(check_pair("routine", ora, 1, args, "function", "BEGIN IF p_tier IS NULL THEN RETURN 'U'; END IF; END;", []))
    handled = "BEGIN IF nullif(p_tier, '') IS NULL THEN RETURN 'U'; END IF; END;"
    assert "EMPTY_STRING_IS_NULL" not in codes(check_pair("routine", ora, 1, args, "function", handled, []))


def test_case_when_null_and_autonomous_and_commit_in_function():
    found = check_pair(
        "routine", "PRAGMA AUTONOMOUS_TRANSACTION; BEGIN COMMIT; END;", 1, [], "function",
        "BEGIN INSERT INTO log VALUES (1); COMMIT; RETURN CASE x WHEN NULL THEN 1 ELSE 2 END; END;", [],
    )
    assert {"AUTONOMOUS_TXN_LOST", "COMMIT_IN_FUNCTION", "CASE_WHEN_NULL"} <= codes(found)
    searched = check_pair("routine", "BEGIN NULL; END;", 1, [], "function", "BEGIN RETURN CASE WHEN x IS NULL THEN 1 END; END;", [])
    assert "CASE_WHEN_NULL" not in codes(searched)


def test_oracle_leftovers_depend_on_orafce():
    pg = "BEGIN v := NVL(a, 0); SELECT * FROM t WHERE ROWNUM < 2; END;"
    by_code = {f.title: f.severity for f in check_pair("routine", "BEGIN NULL; END;", 1, [], "function", pg, [], orafce=True)}
    assert by_code["Oracle syntax remains: NVL"] == "low"
    assert by_code["Oracle syntax remains: ROWNUM"] == "high"


def test_oracle_line_offsets_apply_to_package_members():
    found = check_pair("routine", "\nBEGIN SELECT a INTO b FROM t; END;", 40, [], "function", "BEGIN SELECT a INTO b FROM t; END;", [])
    assert found[0].oracle_lines == [41]


def test_plpgsql_check_results_become_findings():
    found = check_pair("routine", "BEGIN NULL; END;", 1, [], "function", "x", [
        {"level": "error", "message": "operator does not exist", "line": 7},
        {"level": "warning", "message": "target type is different type than source type", "line": 3},
        {"level": "warning extra", "message": "unused variable", "line": 4},  # too noisy: dropped
    ])
    assert [(f.code, f.severity, f.postgres_lines) for f in found] == [
        ("PLPGSQL_CHECK_ERROR", "high", [7]), ("PLPGSQL_CHECK_WARNING", "low", [3]),
    ]


def test_trigger_timing_and_events():
    meta = {"timing": "BEFORE EACH ROW", "events": "INSERT OR UPDATE"}
    ok = check_pair("trigger", "", 1, [], "trigger", "", [], oracle_trigger=meta,
                    target_trigger_definition="CREATE TRIGGER t BEFORE INSERT OR UPDATE OF status ON s.o FOR EACH ROW EXECUTE FUNCTION f()")
    assert codes(ok) == set()
    bad = check_pair("trigger", "", 1, [], "trigger", "", [], oracle_trigger=meta,
                     target_trigger_definition="CREATE TRIGGER t AFTER INSERT ON s.o FOR EACH STATEMENT EXECUTE FUNCTION f()")
    assert {"TRIGGER_TIMING", "TRIGGER_LEVEL", "TRIGGER_EVENTS"} <= codes(bad)
