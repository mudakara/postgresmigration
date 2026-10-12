import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, compareApi, type Project } from "../../api";
import {
  JobProgress,
  METHOD_LABEL,
  REVIEW_STATUS_LABEL,
  RISK_LABEL,
  RISK_ORDER,
  ReviewLauncher,
  RiskChip,
  STATUS_LABEL,
  SignatureChip,
  VERDICT_LABEL,
  VerdictChip,
} from "../../components/comparison";
import { BarList, StatTile } from "../../components/inventory";
import { EmptyState, ErrorBanner, formatTime } from "../../components/ui";

const PAGE_SIZE = 50;
const FILTERS = ["status", "risk", "signature", "ai", "review", "kind", "q"] as const;

function TargetPicker({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const [choice, setChoice] = useState("");
  const save = useMutation({
    mutationFn: () => api.updateProject(project.id, { postgres_connection_id: choice }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["project", project.id] }),
  });
  const pg = (connections.data ?? []).filter((c) => c.kind === "postgres");
  return (
    <div className="card form">
      <h2>Which PostgreSQL database holds the migrated code?</h2>
      <p className="muted small">Scenario 2 compares every Oracle procedure, function, package member, view and trigger with its counterpart there.</p>
      {pg.length === 0 ? (
        <p>No PostgreSQL connections yet. <Link to="/connections">Add one</Link> (a read-only account is enough).</p>
      ) : (
        <div className="actions" style={{ justifyContent: "flex-start" }}>
          <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label="PostgreSQL connection">
            <option value="" disabled>Choose…</option>
            {pg.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <button className="btn btn-primary" disabled={!choice || save.isPending} onClick={() => save.mutate()}>Use this database</button>
        </div>
      )}
      <ErrorBanner error={save.error} />
    </div>
  );
}

export default function ComparisonTab({ project }: { project: Project }) {
  const projectId = project.id;
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState(params.get("q") ?? "");
  const [compareJob, setCompareJob] = useState<string | null>(null);
  const [reviewJob, setReviewJob] = useState<string | null>(null);
  const [showLauncher, setShowLauncher] = useState(false);
  const [showUnpaired, setShowUnpaired] = useState(false);

  const overview = useQuery({ queryKey: ["comparison", projectId], queryFn: () => compareApi.overview(projectId) });
  const page = Number(params.get("page") ?? "0");
  const apiParams = new URLSearchParams();
  for (const key of FILTERS) {
    const value = params.get(key);
    if (value) apiParams.set(key, value);
  }
  apiParams.set("sort", params.get("sort") ?? "risk");
  apiParams.set("limit", String(PAGE_SIZE));
  apiParams.set("offset", String(page * PAGE_SIZE));
  const pairs = useQuery({
    queryKey: ["pairs", projectId, apiParams.toString()],
    queryFn: () => compareApi.pairs(projectId, apiParams),
    placeholderData: keepPreviousData,
    enabled: !!overview.data?.last_successful_run,
  });
  const unpaired = useQuery({
    queryKey: ["unpaired", projectId],
    queryFn: () => compareApi.targets(projectId, { unpaired: "true", limit: "200" }),
    enabled: showUnpaired,
  });

  const refresh = useCallback(() => {
    for (const key of ["comparison", "pairs", "unpaired"]) queryClient.invalidateQueries({ queryKey: [key, projectId] });
  }, [queryClient, projectId]);

  const start = useMutation({
    mutationFn: () => compareApi.start(projectId),
    onSuccess: (run) => setCompareJob(run.job_id),
  });

  // Resume showing progress if a run is already going when the page opens.
  useEffect(() => {
    const last = overview.data?.last_run;
    if (last && (last.status === "queued" || last.status === "running") && last.job_id) setCompareJob(last.job_id);
  }, [overview.data?.last_run]);

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (!("page" in changes)) next.delete("page");
    next.set("tab", "comparison");
    setParams(next, { replace: true });
  };

  useEffect(() => {
    const handle = setTimeout(() => {
      if (search !== (params.get("q") ?? "")) update({ q: search });
    }, 300);
    return () => clearTimeout(handle);
  }, [search]);

  if (!project.postgres_connection_id) return <TargetPicker project={project} />;
  const o = overview.data;
  const run = o?.last_successful_run;
  const c = o?.counts;
  const running = !!compareJob;
  const total = pairs.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="report">
      <div className="toolbar">
        <div className="muted small">
          {run ? <>Last compared {formatTime(run.finished_at)} · PostgreSQL {run.target_version}</> : "Not compared yet."}
          {run && (run.summary.plpgsql_check
            ? ` · plpgsql_check ✓${run.summary.plpgsql_check === "scratch copy" ? " (on a local schema copy)" : ""}`
            : " · plpgsql_check not run")}
        </div>
        <div className="actions">
          <button className="btn" disabled={!run || !!reviewJob} onClick={() => setShowLauncher((v) => !v)}>Review with Claude…</button>
          <button className="btn btn-primary" disabled={running || start.isPending} onClick={() => start.mutate()}>
            {running ? "Comparing…" : run ? "Re-run comparison" : "Run comparison"}
          </button>
        </div>
      </div>

      <ErrorBanner error={overview.error ?? start.error} />
      {compareJob && (
        <JobProgress key={compareJob} jobId={compareJob} label="Comparing with PostgreSQL" onDone={() => { refresh(); setTimeout(() => setCompareJob(null), 0); }} />
      )}
      {reviewJob && <JobProgress key={reviewJob} jobId={reviewJob} label="Claude review" onDone={refresh} />}
      {showLauncher && (
        <section className="card">
          <h2>Review with Claude</h2>
          <p className="muted small">
            Claude reads each Oracle/PostgreSQL pair, with the automated findings as hints, and reports behavioural differences with
            line references. Only source code is sent.
          </p>
          <ReviewLauncher projectId={projectId} onStarted={(id) => { setReviewJob(id); setShowLauncher(false); }} onCancel={() => setShowLauncher(false)} />
        </section>
      )}
      {run?.warnings.map((w) => <div key={w} className="banner banner-warn">{w}</div>)}
      {o?.last_run?.status === "failed" && <div className="banner banner-error">Last comparison failed: {o.last_run.error}</div>}

      {!run && !running && (
        <EmptyState>
          Run the comparison to read the migrated PostgreSQL objects, pair each one with its Oracle original, and check signatures and
          known conversion pitfalls.
        </EmptyState>
      )}

      {run && c && (
        <>
          <section className="cards">
            <StatTile label="Oracle units" value={String(c.units)} sub={`${c.kind.routine ?? 0} routines · ${c.kind.view ?? 0} views · ${c.kind.trigger ?? 0} triggers`} />
            <StatTile label="Matched" value={String(c.status.matched ?? 0)} sub={`${c.units ? Math.round(((c.status.matched ?? 0) / c.units) * 100) : 0}% of units`} />
            <StatTile label="Missing in PostgreSQL" value={String(c.status.missing_in_target ?? 0)} sub="No counterpart found" tone={c.status.missing_in_target ? "bad" : undefined} />
            <StatTile label="High risk" value={String(c.risk.high ?? 0)} sub={`${c.risk.medium ?? 0} medium · ${c.signature.mismatch ?? 0} signature mismatches`} tone={c.risk.high ? "bad" : undefined} />
            <StatTile
              label="Claude reviewed"
              value={String(o.review.reviewed)}
              sub={`${c.ai_verdict.differences_found ?? 0} with differences${o.review.stale ? ` · ${o.review.stale} stale` : ""}`}
            />
          </section>

          <div className="two-col">
            <section className="card">
              <h2>Risk</h2>
              <p className="muted small">Highest severity among signature differences and static findings; missing counterparts count as high.</p>
              <BarList
                unit="units"
                data={RISK_ORDER.map((r) => ({ key: r, label: RISK_LABEL[r], value: c.risk[r] ?? 0 }))}
                onSelect={(risk) => update({ risk })}
              />
            </section>
            <section className="card">
              <h2>Naming convention detected</h2>
              <p className="muted small">How Oracle names were found in PostgreSQL. Low-confidence matches are worth confirming.</p>
              <div className="chips">
                {Object.entries(o.conventions).map(([key, v]) => (
                  <span key={key} className="chip">{v.label} <span className="muted">×{v.count}</span></span>
                ))}
              </div>
              <p className="small spaced">
                <button className="btn btn-sm btn-ghost" onClick={() => setShowUnpaired((v) => !v)}>
                  {showUnpaired ? "Hide" : "Show"} {c.unpaired_targets} PostgreSQL object{c.unpaired_targets === 1 ? "" : "s"} with no Oracle counterpart
                </button>
              </p>
              {showUnpaired && (
                <ul className="dep-list">
                  {(unpaired.data ?? []).map((t) => (
                    <li key={t.id} className="mono small">{t.display_name} <span className="muted">{t.kind}</span></li>
                  ))}
                </ul>
              )}
            </section>
          </div>

          <div className="filters card">
            <input type="search" placeholder="Search Oracle or PostgreSQL name…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search pairs" />
            <select value={params.get("status") ?? ""} onChange={(e) => update({ status: e.target.value })} aria-label="Status">
              <option value="">Any status</option>
              {Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <select value={params.get("risk") ?? ""} onChange={(e) => update({ risk: e.target.value })} aria-label="Risk">
              <option value="">Any risk</option>
              {RISK_ORDER.map((r) => <option key={r} value={r}>{RISK_LABEL[r]}</option>)}
            </select>
            <select value={params.get("signature") ?? ""} onChange={(e) => update({ signature: e.target.value })} aria-label="Signature">
              <option value="">Any signature</option>
              <option value="match">Match</option>
              <option value="compatible">Compatible</option>
              <option value="mismatch">Mismatch</option>
            </select>
            <select value={params.get("ai") ?? ""} onChange={(e) => update({ ai: e.target.value })} aria-label="Claude verdict">
              <option value="">Any Claude verdict</option>
              <option value="none">Not reviewed</option>
              {Object.entries(VERDICT_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              <option value="stale">Stale review</option>
            </select>
            <select value={params.get("review") ?? ""} onChange={(e) => update({ review: e.target.value })} aria-label="Team review">
              <option value="">Any team status</option>
              {Object.entries(REVIEW_STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <select value={params.get("sort") ?? "risk"} onChange={(e) => update({ sort: e.target.value })} aria-label="Sort">
              <option value="risk">Sort: risk</option>
              <option value="name">Sort: name</option>
              <option value="confidence">Sort: lowest match confidence</option>
            </select>
          </div>

          <ErrorBanner error={pairs.error} />
          {pairs.data?.total === 0 && <EmptyState>No pairs match these filters.</EmptyState>}
          {!!pairs.data?.items.length && (
            <div className="card table-card">
              <table className="pairs-table">
                <thead>
                  <tr>
                    <th>Oracle</th>
                    <th>PostgreSQL</th>
                    <th>Match</th>
                    <th>Signature</th>
                    <th className="num">Findings</th>
                    <th>Risk</th>
                    <th>Claude</th>
                    <th>Team</th>
                  </tr>
                </thead>
                <tbody>
                  {pairs.data.items.map((p) => (
                    <tr key={p.id}>
                      <td>
                        <Link to={`/projects/${projectId}/pairs/${p.id}`} className="obj-link">{p.oracle_name}</Link>
                        <div className="muted small">{p.unit_kind}</div>
                      </td>
                      <td className="mono small">
                        {p.target_name ?? <span className={p.status === "ignored" ? "muted" : "error-text"}>{STATUS_LABEL[p.status]}</span>}
                      </td>
                      <td className="small">
                        {p.match_method ? METHOD_LABEL[p.match_method] ?? p.match_method : "—"}
                        {p.match_confidence != null && p.match_method !== "manual" && (
                          <span className={p.match_confidence < 0.75 ? "warn-text" : "muted"}> {Math.round(p.match_confidence * 100)}%</span>
                        )}
                      </td>
                      <td><SignatureChip status={p.signature_status} /></td>
                      <td className="num">{p.finding_count || "—"}</td>
                      <td><RiskChip risk={p.risk} /></td>
                      <td><VerdictChip verdict={p.ai_verdict} stale={p.ai_stale} /></td>
                      <td className="small">{REVIEW_STATUS_LABEL[p.review_status] ?? p.review_status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="pager">
                <span className="muted small">{total.toLocaleString()} pair{total === 1 ? "" : "s"}</span>
                <div className="actions">
                  <button className="btn btn-sm" disabled={page === 0} onClick={() => update({ page: String(page - 1) })}>Previous</button>
                  <span className="small">Page {page + 1} of {pages}</span>
                  <button className="btn btn-sm" disabled={page + 1 >= pages} onClick={() => update({ page: String(page + 1) })}>Next</button>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

