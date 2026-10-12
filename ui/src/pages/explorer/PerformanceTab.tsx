import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useMemo, useRef, useState } from "react";

import {
  catalogApi,
  type CatalogTableDetail,
  type PerfObjectResult,
  type PerfPriority,
  type PerfRecommendation,
  type PerfScope,
} from "../../api";
import { JobProgress, RiskChip } from "../../components/comparison";
import { SeverityChip } from "../../components/inventory";
import SourceViewer, { buildMarks, type SourceViewerHandle } from "../../components/SourceViewer";
import { EmptyState, ErrorBanner, formatTime } from "../../components/ui";
import { WRITES } from "./shared";

const SCOPES: { value: Exclude<PerfScope, "selected">; label: string; hint: string }[] = [
  { value: "triggers", label: "Triggers on this table", hint: "Run on every write to the table." },
  { value: "writers", label: "Triggers + code that writes to it", hint: "Usually where the cost is." },
  { value: "all", label: "All code that uses it", hint: "Includes code that only reads it." },
];
const CATEGORY_LABEL: Record<string, string> = {
  indexing: "indexing", row_by_row: "row-by-row", query_shape: "query shape", n_plus_one: "N+1 queries",
  locking: "locking", trigger_overhead: "trigger overhead", dynamic_sql: "dynamic SQL",
  exception_handling: "exception handling", function_volatility: "function volatility", data_types: "data types",
  pagination: "pagination", temp_tables: "temp tables", other: "other",
};

function Recommendations({ items }: { items: PerfRecommendation[] }) {
  const [copied, setCopied] = useState<string | null>(null);
  if (!items.length) return null;
  return (
    <div className="perf-recs">
      <h3>Suggested changes to the tables ({items.length})</h3>
      {items.map((r) => (
        <div key={r.title + r.sql} className="perf-rec">
          <div className="finding-head">
            <span className="badge badge-info plain">{r.kind}</span>
            <strong>{r.title}</strong>
          </div>
          <p className="small">{r.explanation}</p>
          {r.sql && (
            <div className="perf-sql">
              <pre>{r.sql}</pre>
              <button className="btn btn-sm" onClick={() => { navigator.clipboard?.writeText(r.sql); setCopied(r.sql); }}>
                {copied === r.sql ? "Copied" : "Copy"}
              </button>
            </div>
          )}
          {r.helps.length > 0 && <div className="muted small">Helps: {r.helps.join(", ")}</div>}
        </div>
      ))}
      <p className="muted small">Test new indexes on non-prod first: each one adds work to every insert and update of the table.</p>
    </div>
  );
}

function ObjectResult({ o, onOpenCode }: { o: PerfObjectResult; onOpenCode: (id: string) => void }) {
  const codeId = o.current_code_id;
  const code = useQuery({ queryKey: ["catalog-code", codeId], queryFn: () => catalogApi.code(codeId!), enabled: !!codeId });
  const viewer = useRef<SourceViewerHandle>(null);
  const marks = useMemo(
    () => buildMarks(o.issues.map((i) => ({ lines: i.lines, severity: i.severity, title: i.title }))),
    [o.issues],
  );
  const jump = (line: number) => viewer.current?.scrollToLine(line);
  return (
    <div className="perf-object">
      {o.stale && (
        <div className="banner banner-warn small">
          {codeId ? "The source changed since this review: line numbers may no longer match. Re-review it to refresh."
            : "This object is no longer in the catalog."}
        </div>
      )}
      {o.issues.map((issue, i) => (
        <div key={i} className="finding">
          <div className="finding-head">
            <SeverityChip severity={issue.severity} />
            <span className="badge badge-neutral plain">{CATEGORY_LABEL[issue.category] ?? issue.category}</span>
            <strong>{issue.title}</strong>
          </div>
          <div className="small">{issue.explanation}</div>
          <div className="small"><strong>Suggestion:</strong> {issue.suggestion}</div>
          {issue.example && <pre className="perf-example">{issue.example}</pre>}
          {issue.lines.length > 0 && (
            <div className="line-chips small">
              <span className="muted">Lines:</span>
              {issue.lines.map((l) => <button key={l} className="chip chip-btn" onClick={() => jump(l)}>{l}</button>)}
            </div>
          )}
        </div>
      ))}
      {codeId && (
        <>
          <div className="toolbar">
            <span className="muted small">Source with the lines Claude points to</span>
            <button className="btn btn-sm btn-ghost" onClick={() => onOpenCode(codeId)}>Open in code view →</button>
          </div>
          <ErrorBanner error={code.error} />
          {code.data?.source && <SourceViewer ref={viewer} source={code.data.source} marks={marks} maxHeight="420px" />}
        </>
      )}
    </div>
  );
}

export default function PerformanceTab({ t, onOpenCode }: { t: CatalogTableDetail; onOpenCode: (id: string) => void }) {
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<Exclude<PerfScope, "selected">>("writers");
  const [onlyAttention, setOnlyAttention] = useState(true);
  const [open, setOpen] = useState<string | null>(null);
  const state = useQuery({
    queryKey: ["perf-review", t.id],
    queryFn: () => catalogApi.perfState(t.id),
    refetchInterval: (q) => (q.state.data?.active ? 4000 : false),
  });
  const estimate = useMutation({ mutationFn: (s: PerfScope) => catalogApi.perfEstimate(t.id, s) });
  const start = useMutation({
    mutationFn: ({ s, ids }: { s: PerfScope; ids?: string[] }) => catalogApi.startPerf(t.id, s, ids),
    onSuccess: () => { estimate.reset(); queryClient.invalidateQueries({ queryKey: ["perf-review", t.id] }); },
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["perf-review", t.id] });

  // Rough counts for the scope labels; the estimate gives the exact number.
  const writers = t.used_by.filter((u) => u.operations.some((o) => WRITES.has(o))).length;
  const counts = { triggers: t.triggers.length, writers: t.triggers.length + writers, all: t.triggers.length + t.used_by.length };

  if (state.error) return <ErrorBanner error={state.error} />;
  if (!state.data) return <div className="muted">Loading…</div>;
  const { latest, active, credentials_configured: hasKey, model } = state.data;
  const objects = latest?.results.objects ?? [];
  const shown = objects.filter((o) => !onlyAttention || o.priority !== "none");
  const stale = objects.filter((o) => o.stale && o.current_code_id).map((o) => o.current_code_id!);
  const priority = latest?.summary.priority;
  const e = estimate.data;

  return (
    <div className="tab-body">
      <p className="small muted">
        Claude ({model}) reads the procedures, functions and triggers that use this table, together with the structure of the
        tables they touch (columns, keys, indexes, foreign keys) and the optimizer's row-count estimates, and says which ones
        need performance work and how to fix them. <strong>No table data is sent.</strong>
      </p>
      {!hasKey && <div className="banner banner-warn small">Set ANTHROPIC_API_KEY in .env and restart the worker to enable Claude reviews.</div>}

      {!active && (
        <div className="card perf-launch">
          <fieldset className="radio-group">
            {SCOPES.map((s) => (
              <label key={s.value} className="checkbox inline small">
                <input type="radio" name={`perf-scope-${t.id}`} checked={scope === s.value}
                  onChange={() => { setScope(s.value); estimate.reset(); }} />
                {s.label} (~{counts[s.value]}) <span className="muted">{s.hint}</span>
              </label>
            ))}
          </fieldset>
          <div className="toolbar">
            <button className="btn" onClick={() => estimate.mutate(scope)} disabled={estimate.isPending || counts[scope] === 0}>
              {estimate.isPending ? "Estimating…" : "Estimate cost"}
            </button>
            {e && (
              <span className="small">
                {e.objects} object(s) in {e.batches} request(s) · ~{Math.round(e.input_tokens / 1000)}k input / ~{Math.round(e.output_tokens / 1000)}k output tokens
                {e.estimated_cost_usd !== null && <> · <strong>≈ ${e.estimated_cost_usd.toFixed(2)}</strong></>}
              </span>
            )}
            {e && (
              <button className="btn btn-primary" disabled={!hasKey || e.objects === 0 || e.objects > e.max_objects || start.isPending}
                onClick={() => start.mutate({ s: scope })}>
                Run performance review
              </button>
            )}
          </div>
          {e && e.objects > e.max_objects && (
            <div className="banner banner-warn small">At most {e.max_objects} objects per review: choose a narrower scope.</div>
          )}
          {counts[scope] === 0 && <p className="muted small">Nothing in this scope for this table.</p>}
          <ErrorBanner error={estimate.error ?? start.error} />
        </div>
      )}

      {active?.job_id && <JobProgress key={active.job_id} jobId={active.job_id} label="Claude performance review" onDone={refresh} />}

      {!latest && !active && <EmptyState>No performance review of this table yet.</EmptyState>}
      {latest?.status === "failed" && <div className="banner banner-error small">The last review failed: {latest.error}</div>}

      {latest && latest.status === "succeeded" && (
        <>
          <div className="perf-summary small">
            <span>Reviewed {formatTime(latest.finished_at)} · {latest.summary.reviewed} object(s) · {latest.model}</span>
            {priority && (["high", "medium", "low", "none"] as PerfPriority[]).map((p) => (
              <span key={p} className="perf-count"><RiskChip risk={p} /> {priority[p] ?? 0}</span>
            ))}
            {(latest.summary.failed ?? 0) > 0 && (
              <span className="warn-text" title={(latest.results.failed ?? []).map((f) => `${f.label}: ${f.error}`).join("\n")}>
                {latest.summary.failed} not reviewed
              </span>
            )}
            {stale.length > 0 && !active && (
              <button className="btn btn-sm" disabled={!hasKey || start.isPending} onClick={() => start.mutate({ s: "selected", ids: stale })}>
                Re-review {stale.length} changed object(s)
              </button>
            )}
          </div>

          <Recommendations items={latest.results.table_recommendations ?? []} />

          <div className="toolbar">
            <h3>Procedures, functions and triggers</h3>
            <label className="checkbox inline small">
              <input type="checkbox" checked={onlyAttention} onChange={(ev) => setOnlyAttention(ev.target.checked)} />
              Only those that need work ({objects.filter((o) => o.priority !== "none").length} of {objects.length})
            </label>
          </div>
          {shown.length === 0 ? (
            <EmptyState>Claude found nothing that needs performance work in the reviewed objects.</EmptyState>
          ) : (
            <div className="table-scroll">
              <table className="compact">
                <thead><tr><th>Priority</th><th>Object</th><th>Assessment</th><th className="num">Issues</th></tr></thead>
                <tbody>
                  {shown.map((o) => (
                    <Fragment key={o.label}>
                      <tr className={`clickable ${open === o.label ? "selected" : ""}`} onClick={() => setOpen(open === o.label ? null : o.label)}>
                        <td><RiskChip risk={o.priority} /></td>
                        <td>
                          <span className="mono">{o.name}</span> <span className="tag tag-code">{o.kind}</span>
                          {o.stale && <div className="warn-text small">changed since review</div>}
                        </td>
                        <td className="small">{o.summary}</td>
                        <td className="num">{o.issues.length}</td>
                      </tr>
                      {open === o.label && (
                        <tr className="expanded"><td colSpan={4}><ObjectResult o={o} onOpenCode={onOpenCode} /></td></tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted small">
            Claude can't see execution plans or data distribution: confirm each change with EXPLAIN (ANALYZE, BUFFERS) on non-prod before applying it.
          </p>
        </>
      )}
    </div>
  );
}
