"""Choosing pairs to review, estimating cost, and running reviews in the worker."""

import os
import queue
import threading
import uuid
from collections import defaultdict
from typing import Any, Literal

import anthropic
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.compare.review import ReviewError, ReviewInput, estimate, make_client, numbered, review_pair
from app.compare.units import load_unit
from app.config import get_settings
from app.jobs.registry import JobContext
from app.models import ObjectPair, TargetObject

Scope = Literal["risky", "unreviewed", "all_matched", "selected"]


def credentials_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def select_pairs(session: Session, project_id: uuid.UUID, scope: Scope, pair_ids: list[uuid.UUID] | None) -> list[ObjectPair]:
    query = select(ObjectPair).where(ObjectPair.project_id == project_id, ObjectPair.status == "matched")
    not_reviewed = or_(ObjectPair.ai_review.is_(None), ObjectPair.ai_stale.is_(True))
    if scope == "selected":
        query = query.where(ObjectPair.id.in_(pair_ids or []))
    elif scope == "risky":
        query = query.where(ObjectPair.risk.in_(["high", "medium"]), not_reviewed)
    elif scope == "unreviewed":
        query = query.where(not_reviewed)
    # Same Oracle object back to back, so its cached listing is reused.
    return list(session.scalars(query.order_by(ObjectPair.source_object_id, ObjectPair.oracle_name)))


def build_input(session: Session, pair: ObjectPair) -> ReviewInput | None:
    unit = load_unit(session, pair.source_key)
    target = session.get(TargetObject, pair.target_object_id) if pair.target_object_id else None
    if unit is None or target is None:
        return None
    max_chars = get_settings().review_max_source_chars
    member_lines = unit.source.rstrip("\n").count("\n") + 1
    truncated = len(unit.object_source) > max_chars
    if truncated:
        listing = numbered(unit.source, unit.source_start_line)
        member_range = None
    else:
        listing = numbered(unit.object_source)
        member_range = (
            (unit.source_start_line, unit.source_start_line + member_lines - 1)
            if unit.source != unit.object_source else None
        )
    return ReviewInput(
        pair_id=str(pair.id),
        oracle_label=unit.display_name,
        oracle_listing=listing,
        oracle_member_range=member_range,
        oracle_signature=unit.signature,
        oracle_findings=[f["code"] for f in unit.object_findings],
        target_label=target.display_name,
        target_kind=target.kind,
        target_listing=numbered(target.source or ""),
        signature_diffs=pair.signature_diffs,
        static_findings=pair.static_findings,
        oracle_truncated=truncated,
    )


def estimate_for(session: Session, project_id: uuid.UUID, scope: Scope, pair_ids: list[uuid.UUID] | None) -> dict[str, Any]:
    pairs = select_pairs(session, project_id, scope, pair_ids)
    inputs = [i for i in (build_input(session, p) for p in pairs) if i is not None]
    return {**estimate(inputs, get_settings().review_model), "pair_ids": [i.pair_id for i in inputs]}


def run_reviews(ctx: JobContext, pair_ids: list[str]) -> dict[str, Any]:
    if not credentials_configured():
        raise ValueError("Set ANTHROPIC_API_KEY in .env and restart the worker to enable Claude reviews")
    settings = get_settings()
    session = ctx.session
    inputs: list[ReviewInput] = []
    for pid in pair_ids:
        pair = session.get(ObjectPair, uuid.UUID(pid))
        if pair is not None:
            built = build_input(session, pair)
            if built is not None:
                inputs.append(built)

    # One worker per Oracle listing: members of a package go sequentially (cache hits),
    # different objects in parallel.
    groups: dict[str, list[ReviewInput]] = defaultdict(list)
    for inp in inputs:
        groups[inp.oracle_listing].append(inp)
    work: queue.Queue = queue.Queue()
    for group in groups.values():
        work.put(group)
    results: queue.Queue = queue.Queue()
    stop = threading.Event()
    client = make_client()

    def worker():
        while not stop.is_set():
            try:
                group = work.get_nowait()
            except queue.Empty:
                return
            for inp in group:
                if stop.is_set():
                    return
                try:
                    results.put((inp, review_pair(client, inp, settings), None))
                except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                    stop.set()  # every other call would fail the same way
                    results.put((inp, None, exc))
                except (ReviewError, anthropic.APIError) as exc:
                    results.put((inp, None, exc))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, settings.review_concurrency))]
    for t in threads:
        t.start()

    done = failed = 0
    usage = defaultdict(int)
    fatal: Exception | None = None
    ctx.progress(0.01, f"Reviewing {len(inputs)} pair(s) with {settings.review_model}")
    while done + failed < len(inputs):
        try:
            inp, result, error = results.get(timeout=1)
        except queue.Empty:
            if not any(t.is_alive() for t in threads):
                break
            continue
        pair = session.get(ObjectPair, uuid.UUID(inp.pair_id))
        if error is None:
            pair.ai_review = result
            pair.ai_verdict = result["verdict"]
            pair.ai_stale = False
            for k, v in result.get("usage", {}).items():
                usage[k] += v
            done += 1
        else:
            failed += 1
            ctx.log(f"{inp.oracle_label}: {type(error).__name__}: {error}")
            if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                fatal = error
        ctx.progress((done + failed) / max(1, len(inputs)), f"Reviewed {done + failed}/{len(inputs)}")
    for t in threads:
        t.join(timeout=5)
    if fatal is not None:
        raise ValueError(f"Anthropic API rejected the credentials: {fatal}")
    return {"reviewed": done, "failed": failed, "model": settings.review_model, "usage": dict(usage)}
