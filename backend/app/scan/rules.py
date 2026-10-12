"""Rule catalogue and per-object analysis.

Four categories, each answering a different question:
- execution_safety: can Scenario 2 run this inside a rolled-back transaction? Rules with
  `unsafe=True` mark the object as unsafe to auto-execute.
- semantic_risk: constructs that convert cleanly but can silently behave differently.
- conversion: constructs that need manual rewriting effort.
- blocker: the object cannot be converted as-is.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from app.scan.plsql import MaskedSource, is_wrapped, mask, package_level_declarations

Category = Literal["execution_safety", "semantic_risk", "conversion", "blocker"]
Severity = Literal["high", "medium", "low"]

MAX_LINES_PER_FINDING = 50


@dataclass(frozen=True)
class Rule:
    code: str
    title: str
    category: Category
    severity: Severity
    weight: float  # effort contribution when Ora2Pg's cost isn't available
    advice: str
    pattern: re.Pattern | None = None
    on_strings: bool = False  # match against text with string literals intact
    applies_to: frozenset[str] | None = None
    unsafe: bool = False


def _re(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I | re.S)


RULES: list[Rule] = [
    # --- execution safety -------------------------------------------------------
    Rule("TXN_CONTROL", "Transaction control (COMMIT/ROLLBACK)", "execution_safety", "high", 3,
         "Commits inside the routine would persist test changes; PL/pgSQL only allows COMMIT in "
         "procedures called outside a transaction block.",
         _re(r"\b(COMMIT|ROLLBACK|SAVEPOINT)\b|\bSET\s+TRANSACTION\b"), unsafe=True),
    Rule("AUTONOMOUS_TXN", "Autonomous transaction", "execution_safety", "high", 5,
         "Work is committed independently of the caller and survives rollback. PostgreSQL has no "
         "equivalent; common workarounds are dblink or pg_background.",
         _re(r"\bPRAGMA\s+AUTONOMOUS_TRANSACTION\b"), unsafe=True),
    Rule("EXTERNAL_IO", "External I/O (UTL_FILE, UTL_HTTP, mail…)", "execution_safety", "high", 5,
         "Side effects outside the database cannot be rolled back; needs an extension or moving "
         "the logic into the application.",
         _re(r"\bUTL_(FILE|HTTP|SMTP|MAIL|TCP|INADDR|URL)\b"), unsafe=True),
    Rule("MESSAGING", "Pipes, alerts or queues", "execution_safety", "high", 5,
         "DBMS_PIPE / DBMS_ALERT / Advanced Queuing have no direct PostgreSQL equivalent "
         "(LISTEN/NOTIFY or an external queue).",
         _re(r"\bDBMS_(PIPE|ALERT|AQ|AQADM)\b"), unsafe=True),
    Rule("JOB_SCHEDULING", "Job scheduling", "execution_safety", "high", 4,
         "Creates or runs scheduler jobs; use pg_cron or an external scheduler.",
         _re(r"\bDBMS_(JOB|SCHEDULER)\b"), unsafe=True),
    Rule("DB_LINK", "Database link", "execution_safety", "high", 5,
         "Reads or writes another database; needs postgres_fdw/oracle_fdw and remote changes "
         "are not covered by the local rollback.",
         _re(r"[\w$#\"]@[A-Za-z][\w$#.]*"), unsafe=True),
    Rule("DYNAMIC_DDL", "DDL in dynamic SQL", "execution_safety", "high", 4,
         "DDL commits implicitly in Oracle, so the test transaction cannot be rolled back.",
         _re(r"\bEXECUTE\s+IMMEDIATE\s+'\s*(CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|RENAME|ANALYZE)\b"
             r"|\bDBMS_UTILITY\.EXEC_DDL_STATEMENT\b"),
         on_strings=True, unsafe=True),
    Rule("CALLS_UNSAFE", "Calls code that is unsafe to auto-execute", "execution_safety", "high", 0,
         "A routine this object depends on (directly or indirectly) commits, uses an autonomous "
         "transaction or has external side effects, so running this object has them too.",
         unsafe=True),
    Rule("SEQUENCE_NEXTVAL", "Sequence values consumed", "execution_safety", "low", 0.5,
         "Sequences are not rolled back and generated IDs differ between Oracle and PostgreSQL "
         "runs; compare results excluding them.",
         _re(r"\.\s*NEXTVAL\b")),
    Rule("SLEEP", "Deliberate waits", "execution_safety", "medium", 0.5,
         "DBMS_LOCK.SLEEP / DBMS_SESSION.SLEEP; use pg_sleep, and expect slow test runs.",
         _re(r"\bDBMS_(LOCK|SESSION)\.SLEEP\b")),

    # --- silent semantic differences -------------------------------------------
    Rule("OUTER_JOIN_PLUS", "Oracle (+) outer join", "semantic_risk", "high", 3,
         "Must become an explicit LEFT/RIGHT JOIN. A converter that drops (+) silently turns the "
         "outer join into an inner join and loses rows.",
         _re(r"\(\s*\+\s*\)")),
    Rule("SELECT_INTO", "SELECT … INTO", "semantic_risk", "medium", 1,
         "Oracle raises NO_DATA_FOUND / TOO_MANY_ROWS; PL/pgSQL only does with INTO STRICT, "
         "otherwise it silently assigns NULL or the first row.",
         _re(r"\bSELECT\b(?:(?!;).)*?(?<!COLLECT)\s+INTO\b")),
    Rule("NO_DATA_FOUND_HANDLER", "Handles NO_DATA_FOUND / TOO_MANY_ROWS", "semantic_risk", "high", 2,
         "These handlers never fire in PL/pgSQL unless the matching SELECT uses INTO STRICT.",
         _re(r"\bWHEN\s+(NO_DATA_FOUND|TOO_MANY_ROWS)\b")),
    Rule("EMPTY_STRING", "Empty string literal ''", "semantic_risk", "medium", 1,
         "Oracle treats '' as NULL; PostgreSQL does not, so comparisons, NVL and IS NULL checks "
         "can change outcome."),
    Rule("ROWNUM", "ROWNUM", "semantic_risk", "medium", 2,
         "ROWNUM is assigned before ORDER BY; LIMIT applies after. Direct translations can return "
         "different rows.",
         _re(r"\bROWNUM\b")),
    Rule("DECODE", "DECODE", "semantic_risk", "medium", 0.5,
         "DECODE treats NULL as equal to NULL; a plain CASE translation does not.",
         _re(r"\bDECODE\s*\(")),
    Rule("DATE_ARITHMETIC", "Oracle date arithmetic", "semantic_risk", "medium", 1,
         "DATE carries a time part, date ± number means days, and date − date returns a number; "
         "PostgreSQL returns intervals and has different truncation.",
         _re(r"\b(SYSDATE|SYSTIMESTAMP|CURRENT_DATE)\s*[-+]|\b(ADD_MONTHS|MONTHS_BETWEEN|LAST_DAY|NEXT_DAY)\s*\("
             r"|\bTRUNC\s*\(\s*(SYSDATE|SYSTIMESTAMP)")),
    Rule("SYSDATE", "SYSDATE / SYSTIMESTAMP", "semantic_risk", "low", 0.25,
         "SYSDATE advances during a transaction; now() in PostgreSQL is fixed at transaction start "
         "(use clock_timestamp() for Oracle-like behaviour).",
         _re(r"\b(SYSDATE|SYSTIMESTAMP)\b")),
    Rule("FORMAT_MODELS", "TO_CHAR / TO_DATE format models", "semantic_risk", "low", 0.25,
         "Format models and NLS defaults differ (e.g. RR, D, IW); verify output formats.",
         _re(r"\bTO_(DATE|CHAR|NUMBER|TIMESTAMP)\s*\(")),
    Rule("WHEN_OTHERS", "WHEN OTHERS handler", "semantic_risk", "low", 0.5,
         "Catch-all handlers hide behaviour differences; SQLCODE values differ in PostgreSQL.",
         _re(r"\bWHEN\s+OTHERS\b")),
    Rule("SQLCODE", "SQLCODE / SQLERRM", "semantic_risk", "low", 0.5,
         "Oracle error numbers become PostgreSQL SQLSTATEs; code that branches on them must change.",
         _re(r"\b(SQLCODE|SQLERRM)\b")),

    # --- conversion effort --------------------------------------------------------
    Rule("CONNECT_BY", "Hierarchical query (CONNECT BY)", "conversion", "high", 4,
         "Rewrite as WITH RECURSIVE; LEVEL, SYS_CONNECT_BY_PATH and NOCYCLE need care.",
         _re(r"\bCONNECT\s+BY\b")),
    Rule("PIPELINED", "Pipelined table function", "conversion", "high", 4,
         "Becomes a set-returning function (RETURN NEXT / RETURN QUERY).",
         _re(r"\bPIPELINED\b|\bPIPE\s+ROW\b")),
    Rule("GOTO", "GOTO", "conversion", "high", 3,
         "PL/pgSQL has no GOTO; the control flow must be restructured.",
         _re(r"\bGOTO\b")),
    Rule("PACKAGE_STATE", "Package-level variables", "conversion", "high", 3,
         "Package variables are per-session state in Oracle; PostgreSQL needs a convention such as "
         "custom GUCs or a session temp table."),
    Rule("COMPOUND_TRIGGER", "Compound trigger", "conversion", "high", 4,
         "Must be split into several PostgreSQL triggers.",
         _re(r"\bCOMPOUND\s+TRIGGER\b"), applies_to=frozenset({"TRIGGER"})),
    Rule("OBJECT_TYPE_METHODS", "Object type with methods", "conversion", "high", 4,
         "PostgreSQL composite types have no methods; members become standalone functions.",
         _re(r"\b(MEMBER|STATIC)\s+(FUNCTION|PROCEDURE)\b|\bCONSTRUCTOR\s+FUNCTION\b"),
         applies_to=frozenset({"TYPE", "TYPE BODY"})),
    Rule("BULK_COLLECT", "BULK COLLECT", "conversion", "medium", 2,
         "Becomes array_agg / set-based SQL; LIMIT clauses need a loop rewrite.",
         _re(r"\bBULK\s+COLLECT\b")),
    Rule("FORALL", "FORALL", "conversion", "medium", 2,
         "Becomes a set-based statement (often unnest of an array); SAVE EXCEPTIONS has no "
         "equivalent.",
         _re(r"\bFORALL\b")),
    Rule("COLLECTIONS", "PL/SQL collections", "conversion", "medium", 2,
         "Nested tables, VARRAYs and associative arrays map to PostgreSQL arrays or hstore/jsonb; "
         "methods like .EXTEND/.DELETE must be rewritten.",
         _re(r"\bTABLE\s+OF\b|\bVARRAY\b|\bVARYING\s+ARRAY\b|\bINDEX\s+BY\b")),
    Rule("REF_CURSOR", "REF CURSOR", "conversion", "medium", 2,
         "Becomes refcursor; callers that fetch from it must change too.",
         _re(r"\bREF\s+CURSOR\b|\bSYS_REFCURSOR\b")),
    Rule("DYNAMIC_SQL", "Dynamic SQL", "conversion", "medium", 2,
         "SQL text built at runtime is not converted by Ora2Pg; review every string.",
         _re(r"\bEXECUTE\s+IMMEDIATE\b|\bDBMS_SQL\b")),
    Rule("XML", "XML functions", "conversion", "medium", 2,
         "XMLTYPE and Oracle XML functions differ from PostgreSQL's xml support.",
         _re(r"\b(XMLTYPE|XMLELEMENT|XMLAGG|XMLFOREST|EXTRACTVALUE|XMLTABLE|XMLSEQUENCE)\b")),
    Rule("LOB", "DBMS_LOB", "conversion", "medium", 2,
         "LOB locators don't exist in PostgreSQL; use text/bytea functions.",
         _re(r"\bDBMS_LOB\b")),
    Rule("GTT_USAGE", "Uses a global temporary table", "conversion", "medium", 2,
         "PostgreSQL temporary tables are per-session objects that must be created before use."),
    Rule("MERGE", "MERGE", "conversion", "low", 1,
         "Supported since PostgreSQL 15, but error and RETURNING behaviour differ.",
         _re(r"\bMERGE\s+INTO\b")),
    Rule("TRIGGER_PREDICATES", "INSERTING / UPDATING / DELETING", "conversion", "low", 1,
         "Become TG_OP checks in a trigger function.",
         _re(r"\b(INSERTING|UPDATING|DELETING)\b"), applies_to=frozenset({"TRIGGER"})),
    Rule("CURSOR_ATTRIBUTES", "Cursor attributes (%ROWCOUNT, %FOUND…)", "conversion", "low", 0.5,
         "SQL%ROWCOUNT becomes GET DIAGNOSTICS; explicit cursor attributes become FOUND checks.",
         _re(r"%\s*(ROWCOUNT|FOUND|NOTFOUND|ISOPEN)\b")),
    Rule("DBMS_OUTPUT", "DBMS_OUTPUT", "conversion", "low", 0.25,
         "Becomes RAISE NOTICE (or orafce's dbms_output).",
         _re(r"\bDBMS_OUTPUT\b")),
    Rule("RAISE_APPLICATION_ERROR", "RAISE_APPLICATION_ERROR", "conversion", "low", 0.25,
         "Becomes RAISE EXCEPTION with a custom SQLSTATE; callers checking error numbers must change.",
         _re(r"\bRAISE_APPLICATION_ERROR\b")),

    # --- blockers ---------------------------------------------------------------
    Rule("WRAPPED", "Wrapped (obfuscated) source", "blocker", "high", 10,
         "The source is obfuscated and cannot be analysed or converted; obtain the original source."),
    Rule("INVALID_IN_ORACLE", "Invalid in Oracle", "blocker", "high", 0,
         "The object does not compile in Oracle either; confirm whether it is dead code before "
         "migrating it."),
]

RULES_BY_CODE = {rule.code: rule for rule in RULES}
UNSAFE_CODES = frozenset(r.code for r in RULES if r.unsafe)

# SYS packages that are either covered by a rule above or harmless to list.
UNREMARKABLE_SYS = {"STANDARD", "DBMS_STANDARD", "PLITBLM", "DUAL", "SYS_STUB_FOR_PURITY_ANALYSIS"}
_SYS_PREFIXES = ("DBMS_", "UTL_", "OWA", "HTP", "HTF", "APEX_", "CTX_", "SDO_", "ORD")


@dataclass
class Finding:
    code: str
    count: int
    lines: list[int]
    detail: str | None = None

    def as_dict(self) -> dict:
        rule = RULES_BY_CODE[self.code]
        return {
            "code": self.code,
            "title": rule.title,
            "category": rule.category,
            "severity": rule.severity,
            "count": self.count,
            "lines": self.lines,
            "detail": self.detail,
        }


@dataclass
class Analysis:
    findings: list[Finding] = field(default_factory=list)
    unsafe: bool = False
    wrapped: bool = False
    package_state: list[dict] = field(default_factory=list)
    heuristic_effort: float = 0.0

    @property
    def codes(self) -> list[str]:
        return [f.code for f in self.findings]


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda f: (
            ["blocker", "execution_safety", "semantic_risk", "conversion"].index(RULES_BY_CODE[f.code].category),
            ["high", "medium", "low"].index(RULES_BY_CODE[f.code].severity),
            f.code,
        ),
    )


def mark_calls_unsafe(analysis: Analysis, via: list[str]) -> None:
    """Flag an object that reaches unsafe code through its dependencies."""
    if not via or analysis.unsafe:
        return
    shown = ", ".join(via[:5]) + (f" and {len(via) - 5} more" if len(via) > 5 else "")
    analysis.findings = sort_findings(analysis.findings + [Finding("CALLS_UNSAFE", len(via), [], f"Depends on {shown}")])
    analysis.unsafe = True


def _lines(masked: MaskedSource, offsets: list[int]) -> list[int]:
    lines = sorted({masked.line_of(o) for o in offsets})
    return lines[:MAX_LINES_PER_FINDING]


def analyze(
    object_type: str,
    source: str | None,
    *,
    status: str = "VALID",
    has_db_link_dependency: bool = False,
    uses_gtt: bool = False,
) -> Analysis:
    found: dict[str, Finding] = {}

    def add(code: str, count: int = 1, lines: list[int] | None = None) -> None:
        found[code] = Finding(code, count, lines or [])

    if status != "VALID":
        add("INVALID_IN_ORACLE")

    wrapped = bool(source) and is_wrapped(source)
    package_state: list[dict] = []
    line_count = source.count("\n") + 1 if source else 0

    if wrapped:
        add("WRAPPED")
    elif source:
        masked = mask(source)
        for rule in RULES:
            if rule.pattern is None or (rule.applies_to and object_type not in rule.applies_to):
                continue
            text = masked.with_strings if rule.on_strings else masked.code
            offsets = [m.start() for m in rule.pattern.finditer(text)]
            if offsets:
                add(rule.code, len(offsets), _lines(masked, offsets))

        if masked.empty_string_offsets:
            add("EMPTY_STRING", len(masked.empty_string_offsets), _lines(masked, masked.empty_string_offsets))

        declarations = package_level_declarations(masked, object_type)
        package_state = [d.__dict__ for d in declarations]
        mutable = [d for d in declarations if d.kind == "variable"]
        if mutable:
            add("PACKAGE_STATE", len(mutable), sorted({d.line for d in mutable}))

    if has_db_link_dependency and "DB_LINK" not in found:
        add("DB_LINK")
    if uses_gtt:
        add("GTT_USAGE")

    findings = sort_findings(list(found.values()))
    effort = 1 + line_count / 100 + sum(
        RULES_BY_CODE[f.code].weight for f in findings if RULES_BY_CODE[f.code].category != "execution_safety"
    )
    return Analysis(
        findings=findings,
        unsafe=any(f.code in UNSAFE_CODES for f in findings),
        wrapped=wrapped,
        package_state=package_state,
        heuristic_effort=round(effort, 2),
    )


def sys_packages(referenced: list[tuple[str, str, str]]) -> list[str]:
    """Oracle-supplied packages an object calls, from (owner, name, type) dependencies."""
    names = {
        name
        for owner, name, ref_type in referenced
        if owner in ("SYS", "PUBLIC")
        and name not in UNREMARKABLE_SYS
        # Public synonyms also point at dictionary views, so only count package-like names.
        and (ref_type == "PACKAGE" or (ref_type == "SYNONYM" and name.startswith(_SYS_PREFIXES)))
    }
    return sorted(names)


Tier = Literal["simple", "moderate", "complex", "very_complex"]
TIER_LIMITS: list[tuple[float, Tier]] = [(5, "simple"), (15, "moderate"), (40, "complex")]


def complexity_tier(effort: float) -> Tier:
    for limit, tier in TIER_LIMITS:
        if effort <= limit:
            return tier
    return "very_complex"
