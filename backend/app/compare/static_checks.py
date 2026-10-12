"""Static checks that compare an Oracle unit's code with its PostgreSQL counterpart.

Each check looks for a specific, known way a conversion goes wrong. They are deliberately
narrow so findings are actionable; anything subtler is left to the Claude review, which
receives these results as hints.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.scan.plsql import MaskedSource, mask

_F = re.I | re.S


@dataclass
class StaticFinding:
    code: str
    severity: str
    title: str
    detail: str
    oracle_lines: list[int] = field(default_factory=list)
    postgres_lines: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lines(m: MaskedSource, pattern: str, text: str | None = None, offset: int = 0) -> list[int]:
    source = m.code if text is None else text
    return sorted({m.line_of(x.start()) + offset for x in re.finditer(pattern, source, _F)})[:20]


def _count(m: MaskedSource, pattern: str, on_strings: bool = False) -> int:
    return len(re.findall(pattern, m.with_strings if on_strings else m.code, _F))


_SELECT_INTO = r"\bSELECT\b(?:(?!;).)*?(?<!COLLECT)\s+INTO\b"
_INTO_STRICT = r"\bINTO\s+STRICT\b"
_OUTER_JOIN = r"\b(LEFT|RIGHT|FULL)(\s+OUTER)?\s+JOIN\b"
_ROW_LIMIT = r"\bLIMIT\b|\bFETCH\s+(FIRST|NEXT)\b|\brow_number\s*\(|\bROWNUM\b"

# Oracle-only syntax that should not survive conversion. (pattern, title, high unless orafce)
_ORACLE_LEFTOVERS = [
    (r"\(\s*\+\s*\)", "Oracle (+) join syntax", False),
    (r"\bROWNUM\b", "ROWNUM", False),
    (r"\bCONNECT\s+BY\b", "CONNECT BY", False),
    (r"\.\s*NEXTVAL\b|\.\s*CURRVAL\b", "sequence.NEXTVAL", False),
    (r"\bEXECUTE\s+IMMEDIATE\b", "EXECUTE IMMEDIATE", False),
    (r"\bPRAGMA\b", "PRAGMA", False),
    (r"%\s*(ROWCOUNT|FOUND|NOTFOUND|ISOPEN)\b", "cursor attribute (%ROWCOUNT…)", False),
    (r"\bRAISE_APPLICATION_ERROR\b", "RAISE_APPLICATION_ERROR", False),
    (r"\bSYSDATE\b", "SYSDATE", True),
    (r"\bNVL2?\s*\(", "NVL", True),
    (r"\bDECODE\s*\(", "DECODE", True),
    (r"\bFROM\s+DUAL\b", "FROM DUAL", True),
    (r"\bDBMS_OUTPUT\b", "DBMS_OUTPUT", True),
]


def _simple_case_when_null(pg: MaskedSource) -> list[int]:
    """`CASE x WHEN NULL THEN` never matches (NULL = NULL is unknown)."""
    lines = []
    for m in re.finditer(r"\bWHEN\s+NULL\s+THEN\b", pg.code, _F):
        before = pg.code[: m.start()]
        case = list(re.finditer(r"\bCASE\b", before, re.I))
        if case and not re.match(r"\s*WHEN\b", before[case[-1].end():], re.I):
            lines.append(pg.line_of(m.start()))
    return lines


def _varchar_params(arguments: list[dict[str, Any]]) -> list[str]:
    return [
        a["name"] for a in arguments
        if a.get("name") and (a.get("data_type") or "").upper() in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "VARCHAR", "CLOB")
        and (a.get("mode") or "IN") in ("IN", "IN OUT")
    ]


def check_pair(
    unit_kind: str,
    oracle_source: str,
    oracle_start_line: int,
    oracle_arguments: list[dict[str, Any]],
    target_kind: str,
    target_source: str,
    target_check_results: list[dict[str, Any]],
    *,
    orafce: bool = False,
    oracle_trigger: dict[str, Any] | None = None,
    target_trigger_definition: str | None = None,
) -> list[StaticFinding]:
    ora = mask(oracle_source or "")
    pg = mask(target_source or "")
    off = oracle_start_line - 1
    out: list[StaticFinding] = []

    def add(code, severity, title, detail, oracle_lines=(), postgres_lines=()):
        out.append(StaticFinding(code, severity, title, detail, list(oracle_lines), list(postgres_lines)))

    # 1. NO_DATA_FOUND / TOO_MANY_ROWS semantics. SELECT ... INTO ... FROM DUAL always
    # returns exactly one row, so it can't raise either exception.
    ora_selects = sum(1 for m in re.finditer(_SELECT_INTO + r"(?:(?!;).)*", ora.code, _F)
                      if not re.search(r"\bFROM\s+DUAL\b", m.group(0), re.I))
    pg_strict = _count(pg, _INTO_STRICT)
    handler = _lines(ora, r"\bWHEN\s+(NO_DATA_FOUND|TOO_MANY_ROWS)\b", offset=off)
    if ora_selects and handler and not pg_strict:
        add("DEAD_NO_DATA_FOUND_HANDLER", "high", "NO_DATA_FOUND handler can never fire",
            "Oracle's SELECT INTO raises NO_DATA_FOUND/TOO_MANY_ROWS and the handler runs. PostgreSQL's "
            "SELECT INTO without STRICT silently assigns NULL (or the first row), so the handler is dead code "
            "and multi-row results are no longer an error.",
            handler, _lines(pg, r"\bWHEN\s+(no_data_found|too_many_rows)\b|" + _SELECT_INTO))
    elif ora_selects > pg_strict:
        add("SELECT_INTO_NOT_STRICT", "medium", "SELECT INTO without STRICT",
            f"Oracle has {ora_selects} single-row SELECT INTO; PostgreSQL uses INTO STRICT {pg_strict} time(s). "
            "Without STRICT, 'no rows' gives NULL instead of an error and extra rows are ignored.",
            _lines(ora, _SELECT_INTO, offset=off), _lines(pg, _SELECT_INTO))

    # 2. Outer joins.
    plus = _lines(ora, r"\(\s*\+\s*\)", offset=off)
    if plus and not _count(pg, _OUTER_JOIN) and not _count(pg, r"\(\s*\+\s*\)"):
        add("OUTER_JOIN_LOST", "high", "Outer join became an inner join",
            "Oracle uses (+) outer-join syntax but the PostgreSQL code has no LEFT/RIGHT/FULL JOIN: rows "
            "without a match on the optional side are now dropped.",
            plus, _lines(pg, r"\bWHERE\b"))

    # 3. ROWNUM.
    rownum = _lines(ora, r"\bROWNUM\b", offset=off)
    if rownum and not _count(pg, _ROW_LIMIT):
        add("ROW_LIMIT_LOST", "medium", "ROWNUM limit not found",
            "Oracle limits rows with ROWNUM but the PostgreSQL code has no LIMIT, FETCH FIRST or row_number().",
            rownum)

    # 4. '' is NULL in Oracle.
    empty_cmp = _lines(pg, r"(=|<>|!=)\s*''(?!')", text=pg.with_strings)
    if empty_cmp:
        add("EMPTY_STRING_COMPARISON", "medium", "Comparison with ''",
            "In Oracle '' is NULL, so a comparison with '' is never true; in PostgreSQL it matches empty strings.",
            [], empty_cmp)
    for param in _varchar_params(oracle_arguments):
        p = re.escape(param)
        ora_null = _lines(ora, rf"\b{p}\s+IS\s+(NOT\s+)?NULL\b", offset=off)
        if not ora_null:
            continue
        pg_handles_empty = _count(pg, rf"nullif\s*\(\s*{p}\b|\b{p}\s*(=|<>)\s*''|length\s*\(\s*{p}\s*\)|coalesce\s*\(\s*{p}\s*,\s*''\s*\)", on_strings=True)
        if not pg_handles_empty:
            add("EMPTY_STRING_IS_NULL", "medium", f"'' input for {param} is handled differently",
                f"Oracle treats an empty string as NULL, so {param} = '' takes the IS NULL branch. PostgreSQL "
                f"checks only for NULL, so '' now falls through to the other branch.",
                ora_null, _lines(pg, rf"\b{p}\s+IS\s+(NOT\s+)?NULL\b"))

    # 5. CASE x WHEN NULL.
    when_null = _simple_case_when_null(pg)
    if when_null:
        add("CASE_WHEN_NULL", "high", "CASE … WHEN NULL never matches",
            "A simple CASE compares with =, and NULL = NULL is not true. Oracle's DECODE matched NULLs; "
            "use CASE WHEN x IS NULL instead.", _lines(ora, r"\bDECODE\s*\(", offset=off), when_null)

    # 6. Transactions.
    autonomous = _lines(ora, r"\bPRAGMA\s+AUTONOMOUS_TRANSACTION\b", offset=off)
    if autonomous and not _count(pg, r"\bdblink\b|\bpg_background\b"):
        add("AUTONOMOUS_TXN_LOST", "high", "Autonomous transaction lost",
            "Oracle commits this work independently of the caller (e.g. error logs survive a rollback). The "
            "PostgreSQL version runs in the caller's transaction, so its changes roll back with it.",
            autonomous, _lines(pg, r"\bINSERT\b|\bUPDATE\b|\bDELETE\b"))
    pg_commit = _lines(pg, r"\b(COMMIT|ROLLBACK)\b")
    if pg_commit and target_kind == "function":
        add("COMMIT_IN_FUNCTION", "high", "COMMIT inside a function",
            "PostgreSQL functions cannot commit; this raises an error at run time. Use a procedure called "
            "outside a transaction block.", _lines(ora, r"\b(COMMIT|ROLLBACK)\b", offset=off), pg_commit)

    # 7. Oracle syntax left behind.
    for pattern, title, orafce_provides in _ORACLE_LEFTOVERS:
        lines = _lines(pg, pattern)
        if lines:
            severity = "low" if (orafce_provides and orafce) else "high"
            detail = ("Provided by the orafce extension, so it runs, but behaviour may differ subtly."
                      if severity == "low" else "Oracle-only syntax remains in the PostgreSQL code; it will fail or misbehave.")
            add("ORACLE_SYNTAX_REMAINS", severity, f"Oracle syntax remains: {title}", detail, [], lines)

    # 8. Errors and time.
    if _count(ora, r"\bRAISE_APPLICATION_ERROR\b") and _count(pg, r"\bRAISE\s+EXCEPTION\b") and not _count(pg, r"\bERRCODE\b"):
        add("ERROR_CODE_LOST", "low", "Custom error numbers not preserved",
            "Oracle raises -20xxx error numbers; the PostgreSQL RAISE has no ERRCODE, so callers that check "
            "the error code see P0001 instead.",
            _lines(ora, r"\bRAISE_APPLICATION_ERROR\b", offset=off), _lines(pg, r"\bRAISE\s+EXCEPTION\b"))
    if _count(ora, r"\b(SYSDATE|SYSTIMESTAMP)\b") and _count(pg, r"\bnow\s*\(\s*\)|\bcurrent_timestamp\b|\blocaltimestamp\b|\bcurrent_date\b"):
        add("NOW_IS_TRANSACTION_TIME", "low", "Time is fixed per transaction",
            "SYSDATE advances during a transaction; now()/current_timestamp/localtimestamp in PostgreSQL return "
            "the transaction start time. Use clock_timestamp() where elapsed time matters.",
            _lines(ora, r"\b(SYSDATE|SYSTIMESTAMP)\b", offset=off),
            _lines(pg, r"\bnow\s*\(\s*\)|\bcurrent_timestamp\b|\blocaltimestamp\b|\bcurrent_date\b"))
    date_math = _lines(pg, r"\b(now\s*\(\s*\)|current_timestamp|localtimestamp(\s*\(\s*\d\s*\))?|clock_timestamp\s*\(\s*\))\s*[-+]\s*(?!interval\b)[\w(]")
    if date_math:
        add("TIMESTAMP_PLUS_NUMBER", "medium", "Timestamp ± number",
            "Oracle DATE ± n means n days. In PostgreSQL a timestamp needs an interval (n * interval '1 day'); "
            "timestamp ± integer is an error.",
            _lines(ora, r"\b(SYSDATE|SYSTIMESTAMP)\s*[-+]", offset=off), date_math)

    # 9. Triggers: timing and events.
    if unit_kind == "trigger" and oracle_trigger and target_trigger_definition:
        o_timing = (oracle_trigger.get("timing") or "").upper()
        pg_def = target_trigger_definition.upper()
        for word in ("BEFORE", "AFTER", "INSTEAD OF"):
            if word in o_timing and word not in pg_def:
                add("TRIGGER_TIMING", "high", "Trigger timing differs", f"Oracle fires {o_timing}; PostgreSQL: {target_trigger_definition}")
        if ("EACH ROW" in o_timing) != ("FOR EACH ROW" in pg_def):
            add("TRIGGER_LEVEL", "high", "Row vs statement trigger", f"Oracle fires {o_timing}; PostgreSQL: {target_trigger_definition}")
        o_events = set(re.findall(r"INSERT|UPDATE|DELETE", (oracle_trigger.get("events") or "").upper()))
        pg_events = set(re.findall(r"\b(INSERT|UPDATE|DELETE|TRUNCATE)\b", pg_def.split(" ON ")[0]))
        if o_events and o_events != pg_events:
            add("TRIGGER_EVENTS", "high", "Trigger events differ",
                f"Oracle fires on {', '.join(sorted(o_events))}; PostgreSQL on {', '.join(sorted(pg_events)) or 'nothing'}")

    # 10. plpgsql_check in the target.
    for r in target_check_results:
        level = r.get("level") or ""
        if r.get("runtime_temp_table"):
            add("PLPGSQL_CHECK_TEMP_TABLE", "low", "plpgsql_check: " + (r.get("message") or ""),
                "This looks like a temporary table the code creates while it runs, which static checking can't "
                "see. Verify it is created before this statement runs.", [], [r["line"]] if r.get("line") else [])
        elif level == "error" or level == "check failed":
            where = " (checked on a structure-only copy of the schema)" if r.get("checked_in") else ""
            add("PLPGSQL_CHECK_ERROR", "high", "plpgsql_check: " + (r.get("message") or "error"),
                ("; ".join(x for x in (r.get("detail"), r.get("hint")) if x)
                 or "Static analysis of the PostgreSQL code reports an error; it will fail when run.") + where,
                [], [r["line"]] if r.get("line") else [])
        elif level == "warning":  # "warning extra" (unused variables etc.) is too noisy to report
            add("PLPGSQL_CHECK_WARNING", "low", "plpgsql_check: " + (r.get("message") or "warning"),
                r.get("detail") or r.get("hint") or "", [], [r["line"]] if r.get("line") else [])

    return out
