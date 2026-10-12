"""Claude performance review of the code that uses one table.

What is sent: the source code, the structure of the tables it touches (columns, keys,
indexes, foreign keys) and the optimizer's row-count estimates. Never table data.
The structure block is shared by every batch of one review and goes first with a cache
breakpoint, so later batches read it from the cache.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anthropic

from app.compare.review import CHARS_PER_TOKEN, FALLBACK_BETA, PRICING, ReviewError
from app.config import Settings

EST_OUTPUT_TOKENS_PER_OBJECT = 2500  # findings JSON plus adaptive thinking

CATEGORIES = [
    "indexing", "row_by_row", "query_shape", "n_plus_one", "locking", "trigger_overhead", "dynamic_sql",
    "exception_handling", "function_volatility", "data_types", "pagination", "temp_tables", "other",
]
RECOMMENDATION_KINDS = ["index", "statistics", "partitioning", "schema", "other"]

PERF_SCHEMA = {
    "type": "object",
    "properties": {
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low", "none"]},
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
                                "lines": {"type": "array", "items": {"type": "integer"}},
                                "suggestion": {"type": "string"},
                                "example": {"type": "string"},
                            },
                            "required": ["severity", "category", "title", "explanation", "lines", "suggestion", "example"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["ref", "priority", "summary", "issues"],
                "additionalProperties": False,
            },
        },
        "table_recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": RECOMMENDATION_KINDS},
                    "title": {"type": "string"},
                    "explanation": {"type": "string"},
                    "sql": {"type": "string"},
                    "helps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "title", "explanation", "sql", "helps"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["objects", "table_recommendations"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a database performance engineer reviewing stored code ({dialect}) for one table and the procedures, functions, packages and triggers that use it. The team wants to know which of these objects are likely to be slow or to hurt the rest of the workload, and exactly what to change.

You are given the structure of the relevant tables (columns, primary and unique keys, indexes, foreign keys, and the optimizer's row-count estimates) and the numbered source of each object. You cannot see execution plans, data distribution or call frequency, so reason from the code and the structure, and say what to confirm with EXPLAIN (ANALYZE, BUFFERS) or the database's statistics when it matters.

Look for, among others:
- Row-by-row processing: cursor or FOR loops that run a query or DML per row where one set-based statement would do; repeated lookups of the same value (N+1).
- Predicates that can't use an index: functions or casts on indexed columns (UPPER(col), col::text, TRUNC(date_col), NVL/COALESCE(col, ...) = x), implicit type conversions between the column and the parameter, leading-wildcard LIKE, OR across different columns, NOT IN with nullable subqueries.
- Filters, joins and sorts on columns that no index supports, given the indexes listed (don't propose an index that already exists or is covered by one whose leading columns match). Foreign keys without a supporting index on large child tables.
- Query shape: SELECT * where few columns are needed; COUNT(*) used only to test existence (use EXISTS); DISTINCT or UNION hiding a bad join; correlated subqueries that could be joins; OFFSET pagination over large tables; unbounded queries on large tables.
- Locking and contention: SELECT ... FOR UPDATE on wide ranges, long transactions, updates of hot rows, triggers that update other tables per row, unindexed foreign keys causing table scans or locks on parent changes.
- Trigger overhead: row-level triggers doing queries or DML for every row of bulk operations; work that could be statement-level or moved out of the trigger.
- Dynamic SQL: string-built statements without bind parameters (no plan reuse, injection risk), EXECUTE inside loops.
- PL/pgSQL specifics (when the code is PostgreSQL): an EXCEPTION block inside a loop starts a subtransaction per iteration; functions declared VOLATILE (the default) that could be STABLE or IMMUTABLE, preventing index use or inlining when called in WHERE clauses; RAISE NOTICE in hot loops; temporary tables created on every call; numeric used where integer/bigint would do in loop counters and joins.
- PL/SQL specifics (when the code is Oracle): missing BULK COLLECT / FORALL, commits inside loops, context switches from calling PL/SQL functions inside SQL on large tables.

Rules for your answer:
- Return one entry in "objects" for every object you were given, using its ref exactly. Refs are only for these fields: in summaries, explanations and suggestions, name objects by their schema-qualified names.
- priority: "high" when the object likely costs a lot at the table sizes shown (e.g. per-row loops or non-sargable filters on large tables, a trigger that multiplies the work of every write); "medium" for real but bounded costs; "low" for minor improvements; "none" when you found nothing worth changing. Don't inflate: most objects are usually "none" or "low".
- Every issue must cite line numbers from that object's numbered listing.
- suggestion: what to change, concretely. example: a short rewritten statement, loop, declaration or DDL showing the fix (in the same dialect), or an empty string if a sentence suffices.
- summary: one or two plain sentences; for priority "none", say briefly why it is fine.
- table_recommendations: changes to the tables themselves (mostly indexes) that would help several objects, as runnable DDL in "sql", with the refs they help in "helps". Prefer one composite index that serves several queries over many single-column ones, and mention the write cost of new indexes on heavily written tables. Leave the list empty if nothing is warranted.
- Ignore formatting, naming and style. Don't report correctness bugs unless they also cost performance."""


@dataclass
class PerfObject:
    ref: str
    code_id: str
    label: str  # schema.name (kind)
    signature: str | None
    listing: str  # numbered source (possibly only the relevant package members)
    focus: str  # how it uses the table under review
    other_tables: list[str] = field(default_factory=list)
    note: str = ""

    def block(self) -> str:
        head = [f"### ref={self.ref}: {self.label}"]
        if self.signature:
            head.append(f"Signature: {self.signature}")
        head.append(self.focus)
        if self.other_tables:
            head.append("Other tables it uses: " + "; ".join(self.other_tables))
        if self.note:
            head.append(self.note)
        return "\n".join(head) + f"\n<source>\n{self.listing}\n</source>"


def system_prompt(dialect: str) -> str:
    return SYSTEM_PROMPT.replace("{dialect}", dialect)


def task_block(table_label: str, objects: list[PerfObject]) -> str:
    return (
        f"## Objects to review ({len(objects)}), all of which use {table_label}\n\n"
        + "\n\n".join(o.block() for o in objects)
        + "\n\nReview each object for performance and answer in the required JSON format."
    )


def estimate(context: str, batches: list[list[PerfObject]], dialect: str, table_label: str, model: str) -> dict[str, Any]:
    chars = sum(len(system_prompt(dialect)) + len(context) + len(task_block(table_label, b)) for b in batches)
    input_tokens = int(chars / CHARS_PER_TOKEN)
    output_tokens = EST_OUTPUT_TOKENS_PER_OBJECT * sum(len(b) for b in batches)
    price = PRICING.get(model)
    cost = round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 2) if price else None
    return {"objects": sum(len(b) for b in batches), "batches": len(batches), "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "estimated_cost_usd": cost}


def review_batch(
    client: anthropic.Anthropic, settings: Settings, *, dialect: str, context: str, table_label: str,
    objects: list[PerfObject],
) -> dict[str, Any]:
    # Streamed: the SDK refuses non-streaming requests that may run past 10 minutes,
    # which a 32k-token answer with thinking can.
    with client.beta.messages.stream(
        model=settings.review_model,
        max_tokens=32000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": settings.review_effort, "format": {"type": "json_schema", "schema": PERF_SCHEMA}},
        system=[{"type": "text", "text": system_prompt(dialect), "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": task_block(table_label, objects)},
            ],
        }],
    ) as stream:
        response = stream.get_final_message()
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": response.usage.cache_read_input_tokens or 0,
        "cache_creation_input_tokens": response.usage.cache_creation_input_tokens or 0,
    }
    meta = {"model": response.model, "usage": usage, "reviewed_at": datetime.now(UTC).isoformat()}
    if response.stop_reason == "refusal":
        return {"objects": [], "table_recommendations": [], "refused": True, **meta}
    if response.stop_reason == "max_tokens":
        raise ReviewError("The review was cut off at max_tokens; review fewer objects at a time")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ReviewError(f"No text in the response (stop reason {response.stop_reason})")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"Response was not valid JSON: {exc}") from exc
    return {**result, **meta}
