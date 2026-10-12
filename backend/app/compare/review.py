"""Claude review of an Oracle/PostgreSQL pair.

Only source code, signatures and the automated check results are sent — never table data.
The Oracle listing goes first with a cache breakpoint, so the members of one package
(reviewed back to back) reuse the cached package body.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anthropic

from app.config import Settings, get_settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens (input, output), for the pre-run estimate only.
PRICING = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
CHARS_PER_TOKEN = 3.5
EST_OUTPUT_TOKENS = 4000  # verdict JSON plus adaptive thinking at high effort

CATEGORIES = [
    "null_handling", "exceptions", "joins", "row_limiting", "dates_times", "numeric", "strings",
    "transactions", "side_effects", "signature", "error_codes", "other",
]

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["equivalent", "differences_found", "needs_human_review"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "title": {"type": "string"},
                    "explanation": {"type": "string"},
                    "oracle_lines": {"type": "array", "items": {"type": "integer"}},
                    "postgres_lines": {"type": "array", "items": {"type": "integer"}},
                    "suggested_fix": {"type": "string"},
                },
                "required": ["severity", "category", "title", "explanation", "oracle_lines", "postgres_lines", "suggested_fix"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "confidence", "summary", "issues"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You review database code migrated from Oracle PL/SQL to PostgreSQL PL/pgSQL. For each pair you are given the Oracle original and the PostgreSQL version, and you decide whether the PostgreSQL version behaves the same for every input and database state the original handles.

The migration has already been deployed; the team needs to know where behaviour differs so they can fix it before relying on it. Report differences that can change results, errors, side effects or performance characteristics a caller would notice. Ignore formatting, naming style, comments and rewrites that are behaviourally identical.

Areas where Oracle-to-PostgreSQL conversions most often change behaviour:
- NULL and empty string: Oracle treats '' as NULL; PostgreSQL does not. LENGTH(''), concatenation with NULL, NVL/COALESCE on '' all differ.
- SELECT INTO: Oracle raises NO_DATA_FOUND / TOO_MANY_ROWS; PL/pgSQL only does with INTO STRICT. Handlers for those exceptions are dead without STRICT.
- Exception blocks: in PL/pgSQL an EXCEPTION clause rolls back the block's work (implicit savepoint); Oracle keeps statements that completed before the error. Error codes differ (SQLCODE vs SQLSTATE; RAISE_APPLICATION_ERROR numbers).
- Joins and row limiting: (+) outer joins dropped or inverted; ROWNUM applied before ORDER BY while LIMIT applies after.
- Dates and numbers: Oracle DATE includes a time of day; DATE ± n means days; date subtraction returns a number; now() is fixed per transaction while SYSDATE advances; NUMBER mapped to integer or float changes rounding; integer division.
- DECODE matches NULL to NULL; CASE x WHEN NULL never matches.
- Strings: SUBSTR with position 0 or negative positions, INSTR, TRIM/LPAD/RPAD edge cases, CHAR blank-padding, case-sensitive comparisons and collation-dependent ordering.
- Transactions and side effects: autonomous transactions, COMMIT inside routines, sequence usage, calls to other routines whose semantics changed, package variables emulated with settings (session scope, initial values, type conversions).
- Signatures: parameter order, modes, types and defaults; procedures that became functions.

Automated checks have already run; their results are included as hints. They can be wrong in both directions, so confirm each against the code rather than repeating it, and look for problems they missed.

Rules for your answer:
- Every issue must point to specific code: cite line numbers from the numbered listings (Oracle and PostgreSQL line numbers are independent).
- verdict "equivalent" only if you found no behavioural difference; "differences_found" if at least one issue could change behaviour; "needs_human_review" if correctness depends on code or data you cannot see (other routines, table definitions, settings) and you cannot decide.
- Use confidence to say how sure you are of the verdict as a whole.
- summary: two to four plain sentences a database developer can act on.
- suggested_fix: the concrete PostgreSQL change, or an empty string if there is none."""


class ReviewError(Exception):
    pass


@dataclass
class ReviewInput:
    pair_id: str
    oracle_label: str
    oracle_listing: str  # full object source, numbered by the caller's convention (from 1)
    oracle_member_range: tuple[int, int] | None
    oracle_signature: str | None
    oracle_findings: list[str]
    target_label: str
    target_kind: str
    target_listing: str
    signature_diffs: list[dict[str, Any]]
    static_findings: list[dict[str, Any]]
    oracle_truncated: bool = False


def numbered(source: str, start: int = 1) -> str:
    lines = (source or "").rstrip("\n").split("\n")
    width = len(str(start + len(lines)))
    return "\n".join(f"{n:>{width}}| {text}" for n, text in enumerate(lines, start=start))


def _oracle_block(inp: ReviewInput) -> str:
    note = (" Only the member under review is shown because the package body is too large to include."
            if inp.oracle_truncated else "")
    return (f"Oracle source of {inp.oracle_label}.{note}\n"
            f"<oracle_source>\n{inp.oracle_listing}\n</oracle_source>")


def _task_block(inp: ReviewInput) -> str:
    parts = ["## Pair under review", f"Oracle: {inp.oracle_label}"]
    if inp.oracle_signature:
        parts.append(f"Oracle signature: {inp.oracle_signature}")
    if inp.oracle_member_range:
        a, b = inp.oracle_member_range
        parts.append(f"The Oracle implementation under review is lines {a}–{b} of the listing above; "
                     "the rest is context (package declarations, other members).")
    parts.append(f"PostgreSQL counterpart: {inp.target_label} ({inp.target_kind})")
    parts.append(f"<postgresql_source>\n{inp.target_listing}\n</postgresql_source>")

    parts.append("## Automated check results (hints to verify)")
    if inp.signature_diffs:
        parts += [f"- signature [{d['severity']}] {d['message']}" for d in inp.signature_diffs]
    for f in inp.static_findings:
        where = []
        if f.get("oracle_lines"):
            where.append(f"Oracle lines {f['oracle_lines']}")
        if f.get("postgres_lines"):
            where.append(f"PostgreSQL lines {f['postgres_lines']}")
        parts.append(f"- [{f['severity']}] {f['title']}: {f['detail']}" + (f" ({'; '.join(where)})" if where else ""))
    if inp.oracle_findings:
        parts.append("- Constructs flagged in the Oracle object: " + ", ".join(inp.oracle_findings))
    if not (inp.signature_diffs or inp.static_findings or inp.oracle_findings):
        parts.append("- none")
    parts.append("\nDoes the PostgreSQL version behave the same as the Oracle original? Answer in the required JSON format.")
    return "\n".join(parts)


def estimate(inputs: list[ReviewInput], model: str) -> dict[str, Any]:
    chars = sum(len(SYSTEM_PROMPT) + len(_oracle_block(i)) + len(_task_block(i)) for i in inputs)
    input_tokens = int(chars / CHARS_PER_TOKEN)
    output_tokens = EST_OUTPUT_TOKENS * len(inputs)
    price = PRICING.get(model)
    cost = round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 2) if price else None
    return {"pairs": len(inputs), "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "estimated_cost_usd": cost}


def review_pair(client: anthropic.Anthropic, inp: ReviewInput, settings: Settings) -> dict[str, Any]:
    response = client.beta.messages.create(
        model=settings.review_model,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={
            "effort": settings.review_effort,
            "format": {"type": "json_schema", "schema": REVIEW_SCHEMA},
        },
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _oracle_block(inp), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": _task_block(inp)},
            ],
        }],
    )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": response.usage.cache_read_input_tokens or 0,
        "cache_creation_input_tokens": response.usage.cache_creation_input_tokens or 0,
    }
    meta = {"model": response.model, "usage": usage, "reviewed_at": datetime.now(UTC).isoformat(),
            "request_id": getattr(response, "_request_id", None)}

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        return {
            "verdict": "needs_human_review", "confidence": "low", "issues": [],
            "summary": f"Claude declined to review this pair (refusal category: {category or 'unspecified'}). Review it manually.",
            **meta,
        }
    if response.stop_reason == "max_tokens":
        raise ReviewError("The review was cut off at max_tokens; rerun it for this pair")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ReviewError(f"No text in the response (stop reason {response.stop_reason})")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"Response was not valid JSON: {exc}") from exc
    return {**result, **meta}


def make_client() -> anthropic.Anthropic:
    # Retries 408/409/429/5xx and connection errors with backoff.
    workspace = get_settings().anthropic_workspace_id
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    return anthropic.Anthropic(max_retries=4, default_headers=headers)
