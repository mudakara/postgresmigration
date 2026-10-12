"""Compare an Oracle routine's parameter list with its PostgreSQL counterpart."""

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

Severity = Literal["high", "medium", "low"]


@dataclass
class SignatureDiff:
    code: str
    severity: Severity
    message: str
    position: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_PG_ALIASES = {
    "character varying": "varchar", "character": "char", "bpchar": "char",
    "timestamp without time zone": "timestamp", "timestamp with time zone": "timestamptz",
    "time without time zone": "time", "double precision": "float8", "real": "float4",
    "int": "integer", "int4": "integer", "int8": "bigint", "int2": "smallint", "decimal": "numeric",
}


def normalize_pg_type(pg_type: str) -> str:
    base = re.sub(r"\(.*?\)", "", pg_type or "").strip().lower()
    base = base.split(".")[-1].strip('"')
    is_array = base.endswith("[]")
    base = base.removesuffix("[]").strip()
    base = _PG_ALIASES.get(base, base)
    return base + ("[]" if is_array else "")


def _oracle_base(arg: dict[str, Any] | None, display: str | None = None) -> tuple[str, int | None, int | None]:
    text = (arg or {}).get("data_type") or display or ""
    shown = (arg or {}).get("type") or display or ""
    m = re.match(r"NUMBER\((\d+)(?:,(\d+))?\)", shown or "")
    precision = int(m.group(1)) if m else None
    scale = int(m.group(2) or 0) if m else None
    return text.upper().strip(), precision, scale


def type_compatibility(oracle: dict[str, Any] | str | None, pg_type: str) -> tuple[str, str | None]:
    """('ok' | 'risky' | 'incompatible' | 'unverified', note)."""
    arg = oracle if isinstance(oracle, dict) else None
    ora, precision, scale = _oracle_base(arg, oracle if isinstance(oracle, str) else None)
    pg = normalize_pg_type(pg_type)
    if not ora:
        return "unverified", None

    if ora in ("NUMBER", "FLOAT", "BINARY_DOUBLE", "BINARY_FLOAT", "DECIMAL", "NUMERIC"):
        if pg == "numeric":
            return "ok", None
        if pg in ("integer", "bigint", "smallint"):
            if scale == 0 and precision is not None and precision <= {"smallint": 4, "integer": 9, "bigint": 18}[pg]:
                return "ok", None
            return "risky", f"{ora} became {pg}: fractional values are rounded and large values overflow"
        if pg in ("float8", "float4"):
            return "risky", f"{ora} became {pg}: binary floating point changes rounding"
        return "incompatible", f"{ora} cannot map to {pg}"
    if ora in ("PLS_INTEGER", "BINARY_INTEGER", "INTEGER", "SMALLINT", "NATURAL", "POSITIVE", "SIMPLE_INTEGER"):
        return ("ok", None) if pg in ("integer", "bigint", "smallint", "numeric") else ("incompatible", f"{ora} cannot map to {pg}")
    if ora in ("VARCHAR2", "NVARCHAR2", "VARCHAR", "LONG", "CLOB", "NCLOB"):
        if pg in ("varchar", "text"):
            return "ok", None
        if pg == "char":
            return "risky", f"{ora} became char: values are blank-padded"
        return "incompatible", f"{ora} cannot map to {pg}"
    if ora in ("CHAR", "NCHAR"):
        if pg == "char":
            return "ok", None
        if pg in ("varchar", "text"):
            return "risky", f"{ora} became {pg}: trailing-blank comparison semantics differ"
        return "incompatible", f"{ora} cannot map to {pg}"
    if ora == "DATE":
        if pg == "timestamp":
            return "ok", None
        if pg == "date":
            return "risky", "Oracle DATE carries a time of day; PostgreSQL date drops it"
        if pg == "timestamptz":
            return "risky", "Oracle DATE has no time zone; timestamptz converts to the session time zone"
        return "incompatible", f"DATE cannot map to {pg}"
    if ora.startswith("TIMESTAMP"):
        if "TIME ZONE" in ora:
            return ("ok", None) if pg == "timestamptz" else ("risky", f"{ora} became {pg}")
        if pg == "timestamp":
            return "ok", None
        return ("risky", f"{ora} became {pg}") if pg in ("timestamptz", "date") else ("incompatible", f"{ora} cannot map to {pg}")
    if ora in ("BLOB", "RAW", "LONG RAW", "BFILE"):
        return ("ok", None) if pg == "bytea" else ("risky", f"{ora} became {pg}")
    if ora in ("PL/SQL BOOLEAN", "BOOLEAN"):
        return ("ok", None) if pg == "boolean" else ("incompatible", f"{ora} cannot map to {pg}")
    if ora == "REF CURSOR":
        return ("ok", None) if pg == "refcursor" else ("risky", f"REF CURSOR became {pg}: callers fetch differently")
    return "unverified", f"{ora} → {pg} needs manual verification"


_MODE_EQUIV = {"IN": "IN", "OUT": "OUT", "IN OUT": "IN OUT", "INOUT": "IN OUT", "VARIADIC": "IN"}


def compare_signatures(
    oracle_kind: str,
    oracle_args: list[dict[str, Any]],
    oracle_return: str | None,
    target_kind: str,
    target_args: list[dict[str, Any]],
    target_result: str | None,
    target_n_defaults: int,
) -> tuple[str, list[SignatureDiff]]:
    diffs: list[SignatureDiff] = []
    pg_args = [a for a in target_args if a.get("mode") != "TABLE"]

    if oracle_kind == "FUNCTION" and target_kind == "procedure":
        diffs.append(SignatureDiff("KIND", "high", "Oracle function became a procedure: it can no longer be used in SQL expressions"))
    elif oracle_kind == "PROCEDURE" and target_kind == "function":
        diffs.append(SignatureDiff("KIND", "low", "Oracle procedure became a function: callers use SELECT/PERFORM instead of CALL"))

    if len(oracle_args) != len(pg_args):
        diffs.append(SignatureDiff(
            "ARG_COUNT", "high", f"Oracle has {len(oracle_args)} parameter(s), PostgreSQL has {len(pg_args)}"
        ))

    pg_names = [(a.get("name") or "").lower() for a in pg_args]
    for i, (o, p) in enumerate(zip(oracle_args, pg_args), start=1):
        o_name, p_name = (o.get("name") or "").lower(), (p.get("name") or "").lower()
        if o_name != p_name:
            if o_name and o_name in pg_names:
                diffs.append(SignatureDiff(
                    "ARG_ORDER", "high",
                    f"Parameter {i} is {o.get('name')} in Oracle but {p.get('name')} in PostgreSQL; "
                    f"{o.get('name')} moved to position {pg_names.index(o_name) + 1}. Positional callers pass values to the wrong parameter",
                    i,
                ))
            else:
                diffs.append(SignatureDiff(
                    "ARG_NAME", "low", f"Parameter {i} renamed {o.get('name')} → {p.get('name')} (breaks named-notation calls)", i,
                ))
        o_mode = _MODE_EQUIV.get((o.get("mode") or "IN").upper(), o.get("mode"))
        p_mode = _MODE_EQUIV.get((p.get("mode") or "IN").upper(), p.get("mode"))
        if o_mode != p_mode:
            diffs.append(SignatureDiff("ARG_MODE", "high", f"Parameter {o.get('name')} is {o_mode} in Oracle, {p_mode} in PostgreSQL", i))
        status, note = type_compatibility(o, p.get("type", ""))
        if status == "incompatible":
            diffs.append(SignatureDiff("ARG_TYPE", "high", f"{o.get('name')}: {note}", i))
        elif status == "risky":
            diffs.append(SignatureDiff("ARG_TYPE", "medium", f"{o.get('name')}: {note}", i))
        elif status == "unverified" and note:
            diffs.append(SignatureDiff("ARG_TYPE", "low", f"{o.get('name')}: {note}", i))

    # PostgreSQL defaults apply to the last N input parameters.
    pg_inputs = [i for i, a in enumerate(pg_args) if _MODE_EQUIV.get((a.get("mode") or "IN").upper()) in ("IN", "IN OUT")]
    defaulted_positions = set(pg_inputs[len(pg_inputs) - target_n_defaults:]) if target_n_defaults else set()
    for i, o in enumerate(oracle_args):
        if o.get("defaulted") and i < len(pg_args) and i not in defaulted_positions:
            diffs.append(SignatureDiff(
                "DEFAULT_MISSING", "medium", f"{o.get('name')} has a default in Oracle but not in PostgreSQL", i + 1,
            ))

    if oracle_kind == "FUNCTION" and oracle_return and target_result:
        has_out = any(_MODE_EQUIV.get((a.get("mode") or "").upper()) in ("OUT", "IN OUT") for a in pg_args)
        if not has_out and not target_result.lower().startswith(("setof", "table")):
            status, note = type_compatibility(oracle_return, target_result)
            if status in ("incompatible", "risky"):
                diffs.append(SignatureDiff(
                    "RETURN_TYPE", "high" if status == "incompatible" else "medium",
                    f"Return type: {note}",
                ))

    worst = {d.severity for d in diffs}
    status = "mismatch" if "high" in worst else "compatible" if "medium" in worst else "match"
    return status, diffs
