import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, compareApi, type PairSummary, type ReviewScope, type Risk } from "../api";
import { ErrorBanner } from "./ui";

export const RISK_LABEL: Record<Risk, string> = { high: "High", medium: "Medium", low: "Low", none: "None" };
export const RISK_ORDER: Risk[] = ["high", "medium", "low", "none"];

export const STATUS_LABEL: Record<PairSummary["status"], string> = {
  matched: "Matched",
  missing_in_target: "Missing in PostgreSQL",
  ignored: "Ignored",
};

export const VERDICT_LABEL: Record<string, string> = {
  equivalent: "Equivalent",
  differences_found: "Differences found",
  needs_human_review: "Needs human review",
};

export const REVIEW_STATUS_LABEL: Record<string, string> = {
  unreviewed: "Not reviewed",
  confirmed_ok: "Confirmed OK",
  needs_fix: "Needs fix",
  fixed: "Fixed",
  wont_fix: "Won't fix",
};

export const METHOD_LABEL: Record<string, string> = {
  schema_per_package: "package → schema",
  same_schema: "same schema",
  flattened: "pkg_member",
  public_schema: "public schema",
  other_schema: "other schema",
  manual: "manual",
};

export function RiskChip({ risk }: { risk: Risk }) {
  const icon = risk === "high" ? "▲" : risk === "medium" ? "◆" : risk === "low" ? "○" : "✓";
  return <span className={`sev sev-${risk === "none" ? "ok" : risk}`}><span aria-hidden>{icon}</span> {RISK_LABEL[risk]}</span>;
}

export function SignatureChip({ status }: { status: PairSummary["signature_status"] }) {
  if (!status) return <span className="muted small">—</span>;
  const cls = status === "match" ? "badge-ok" : status === "compatible" ? "badge-warn" : "badge-bad";
  const label = status === "match" ? "✓ Match" : status === "compatible" ? "◆ Compatible" : "✗ Mismatch";
  return <span className={`badge ${cls} plain`}>{label}</span>;
}

export function VerdictChip({ verdict, stale }: { verdict: PairSummary["ai_verdict"]; stale?: boolean }) {
  if (!verdict) return <span className="muted small">Not reviewed</span>;
  const cls = verdict === "equivalent" ? "badge-ok" : verdict === "differences_found" ? "badge-bad" : "badge-warn";
  const icon = verdict === "equivalent" ? "✓" : verdict === "differences_found" ? "✗" : "?";
  return (
    <span className={`badge ${cls} plain`} title={stale ? "Code changed since this review" : undefined}>
      {icon} {VERDICT_LABEL[verdict]}{stale ? " (stale)" : ""}
    </span>
  );
}

const SCOPES: { key: ReviewScope; label: string; help: string }[] = [
  { key: "risky", label: "High and medium risk, not yet reviewed", help: "Recommended first pass" },
  { key: "unreviewed", label: "Everything not yet reviewed", help: "Includes low-risk pairs" },
  { key: "all_matched", label: "All matched pairs", help: "Re-reviews pairs that already have a verdict" },
];

/** Choose what Claude reviews, see the estimated cost, and start the job. */
export function ReviewLauncher({
  projectId,
  pairIds,
  onStarted,
  onCancel,
}: {
  projectId: string;
  pairIds?: string[];
  onStarted: (jobId: string) => void;
  onCancel?: () => void;
}) {
  const [scope, setScope] = useState<ReviewScope>(pairIds ? "selected" : "risky");
  const estimate = useQuery({
    queryKey: ["review-estimate", projectId, scope, pairIds?.join(",")],
    queryFn: () => compareApi.estimate(projectId, scope, pairIds),
  });
  const start = useMutation({
    mutationFn: () => compareApi.startReviews(projectId, scope, pairIds),
    onSuccess: (job) => onStarted(job.id),
  });
  const e = estimate.data;

  return (
    <div className="review-launcher">
      {!pairIds && (
        <fieldset className="radio-group">
          <legend className="small muted">Review</legend>
          {SCOPES.map((s) => (
            <label key={s.key} className="checkbox inline">
              <input type="radio" name="scope" checked={scope === s.key} onChange={() => setScope(s.key)} />
              {s.label} <span className="muted small">— {s.help}</span>
            </label>
          ))}
        </fieldset>
      )}
      <ErrorBanner error={estimate.error ?? start.error} />
      {e && !e.credentials_configured && (
        <div className="banner banner-warn">
          Claude reviews need an Anthropic API key. Add <code>ANTHROPIC_API_KEY=…</code> to <code>.env</code> and restart the
          api and worker (<code>docker compose up -d api worker</code>). Only source code is sent — never table data.
        </div>
      )}
      {e && (
        <p className="small">
          {e.pairs === 0 ? "Nothing in this selection needs a review." : (
            <>
              <strong>{e.pairs}</strong> pair{e.pairs === 1 ? "" : "s"} with <span className="mono">{e.model}</span> ·
              about {Math.round(e.input_tokens / 1000).toLocaleString()}k input / {Math.round(e.output_tokens / 1000).toLocaleString()}k output tokens
              {e.estimated_cost_usd != null && <> · estimated <strong>${e.estimated_cost_usd.toFixed(2)}</strong></>}
              <span className="muted"> (rough; prompt caching usually lowers it)</span>
            </>
          )}
        </p>
      )}
      <div className="actions">
        {onCancel && <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>}
        <button
          className="btn btn-primary"
          disabled={!e || !e.credentials_configured || e.pairs === 0 || start.isPending}
          onClick={() => start.mutate()}
        >
          {start.isPending ? "Starting…" : "Start review"}
        </button>
      </div>
    </div>
  );
}

/** Polls a job and calls onDone once when it finishes. */
export function JobProgress({ jobId, label, onDone }: { jobId: string; label: string; onDone: () => void }) {
  const queryClient = useQueryClient();
  const job = useQuery({ queryKey: ["job", jobId], queryFn: () => api.job(jobId), refetchInterval: 1500 });
  const status = job.data?.status;
  useEffect(() => {
    if (status === "succeeded" || status === "failed") {
      onDone();
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  }, [status, onDone, queryClient]);
  if (status === "failed") return <div className="banner banner-error">{label} failed: {job.data?.error}</div>;
  if (status === "succeeded") return null;
  return (
    <div className="card scan-progress">
      <div className="scan-progress-head">
        <strong>{label}…</strong>
        <span className="muted small">{job.data?.progress_message ?? "Queued"}</span>
      </div>
      <div className="progress"><div style={{ width: `${Math.round((job.data?.progress ?? 0.02) * 100)}%` }} /></div>
    </div>
  );
}
