import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { compareApi, inventoryApi, type PairDetail, type Severity } from "../api";
import {
  JobProgress,
  METHOD_LABEL,
  REVIEW_STATUS_LABEL,
  ReviewLauncher,
  RiskChip,
  SignatureChip,
  VerdictChip,
} from "../components/comparison";
import { SeverityChip } from "../components/inventory";
import SourceViewer, { buildMarks, type SourceViewerHandle } from "../components/SourceViewer";
import { ErrorBanner, formatTime } from "../components/ui";

type Side = "oracle" | "postgres";

function LineChips({ lines, side, onJump }: { lines: number[]; side: Side; onJump: (side: Side, line: number) => void }) {
  if (!lines.length) return null;
  return (
    <span className="line-chips">
      <span className="muted small">{side === "oracle" ? "Oracle" : "PG"}</span>
      {lines.map((l) => (
        <button key={l} className="chip chip-btn" onClick={() => onJump(side, l)}>L{l}</button>
      ))}
    </span>
  );
}

function SignatureTable({ pair }: { pair: PairDetail }) {
  const o = pair.oracle?.arguments ?? [];
  const t = (pair.target?.arguments ?? []).filter((a) => a.mode !== "TABLE");
  const rows = Math.max(o.length, t.length);
  const flagged = new Map(pair.signature_diffs.filter((d) => d.position).map((d) => [d.position!, d.severity]));
  if (!rows) return <p className="muted small">No parameters on either side.</p>;
  return (
    <table className="sig-table">
      <thead><tr><th>#</th><th>Oracle</th><th>PostgreSQL</th></tr></thead>
      <tbody>
        {Array.from({ length: rows }, (_, i) => {
          const sev = flagged.get(i + 1);
          return (
            <tr key={i} className={sev ? `flag-${sev}` : ""}>
              <td className="muted">{i + 1}</td>
              <td className="mono small">{o[i] ? `${o[i].name} ${o[i].mode} ${o[i].type}${o[i].defaulted ? " DEFAULT" : ""}` : "—"}</td>
              <td className="mono small">{t[i] ? `${t[i].name ?? "?"} ${t[i].mode} ${t[i].type}` : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function MatchEditor({ pair, projectId, onSaved }: { pair: PairDetail; projectId: string; onSaved: () => void }) {
  const [q, setQ] = useState(pair.oracle?.display_name.split(".").pop()?.replace(/ #\d+$/, "").toLowerCase() ?? "");
  const candidates = useQuery({
    queryKey: ["targets-search", projectId, q],
    queryFn: () => compareApi.targets(projectId, { q, limit: "20" }),
    enabled: q.length > 0,
  });
  const save = useMutation({ mutationFn: (body: Parameters<typeof compareApi.updatePair>[1]) => compareApi.updatePair(pair.id, body), onSuccess: onSaved });
  const list = [...pair.alternatives, ...(candidates.data ?? []).filter((c) => !pair.alternatives.some((a) => a.id === c.id))];

  return (
    <section className="card form">
      <h2>Change match</h2>
      <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search PostgreSQL objects…" aria-label="Search PostgreSQL objects" />
      <ul className="candidate-list">
        {list.filter((c) => c.id !== pair.target_object_id).map((c) => (
          <li key={c.id}>
            <span className="mono small">{c.display_name}</span> <span className="muted small">{c.kind}</span>
            <button className="btn btn-sm" disabled={save.isPending} onClick={() => save.mutate({ target_object_id: c.id })}>Use this</button>
          </li>
        ))}
        {q && candidates.isSuccess && list.length === 0 && <li className="muted small">No PostgreSQL objects match.</li>}
      </ul>
      <ErrorBanner error={save.error} />
      <div className="actions" style={{ justifyContent: "flex-start" }}>
        <button className="btn btn-sm" disabled={save.isPending} onClick={() => save.mutate({ ignore: true })} title="Not migrated on purpose; stop reporting it">
          No counterpart — ignore
        </button>
        {(pair.manual || pair.status === "ignored") && (
          <button className="btn btn-sm btn-ghost" disabled={save.isPending} onClick={() => save.mutate({ reset: true })}>
            Back to automatic matching
          </button>
        )}
      </div>
      {(pair.manual || pair.status === "ignored") && <p className="muted small">Automatic matching applies on the next comparison run.</p>}
    </section>
  );
}

export default function PairDetailPage() {
  const { projectId = "", pairId = "" } = useParams();
  const queryClient = useQueryClient();
  const pair = useQuery({ queryKey: ["pair", pairId], queryFn: () => compareApi.pair(pairId) });
  const project = useQuery({ queryKey: ["project", projectId], queryFn: () => inventoryApi.project(projectId) });
  const [editing, setEditing] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [reviewJob, setReviewJob] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ oracle: number | null; postgres: number | null }>({ oracle: null, postgres: null });
  const [note, setNote] = useState<string | null>(null);
  const oracleRef = useRef<SourceViewerHandle>(null);
  const pgRef = useRef<SourceViewerHandle>(null);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["pair", pairId] });
    queryClient.invalidateQueries({ queryKey: ["pairs", projectId] });
    queryClient.invalidateQueries({ queryKey: ["comparison", projectId] });
  };
  const saveReview = useMutation({
    mutationFn: (body: { review_status?: string; review_note?: string | null }) => compareApi.updatePair(pairId, body),
    onSuccess: () => { setNote(null); refresh(); },
  });

  const jump = (side: Side, line: number) => {
    setSelected((s) => ({ ...s, [side]: line }));
    (side === "oracle" ? oracleRef : pgRef).current?.scrollToLine(line);
  };

  if (pair.error) return <ErrorBanner error={pair.error} />;
  if (!pair.data) return <div className="card muted">Loading…</div>;
  const p = pair.data;
  const ai = p.ai_review;

  type Entry = { lines: number[]; severity: Severity; title: string };
  const oracleEntries: Entry[] = [
    ...p.static_findings.map((f) => ({ lines: f.oracle_lines, severity: f.severity, title: f.title })),
    ...(ai?.issues ?? []).map((i) => ({ lines: i.oracle_lines, severity: i.severity, title: `Claude: ${i.title}` })),
  ];
  const pgEntries: Entry[] = [
    ...p.static_findings.map((f) => ({ lines: f.postgres_lines, severity: f.severity, title: f.title })),
    ...(ai?.issues ?? []).map((i) => ({ lines: i.postgres_lines, severity: i.severity, title: `Claude: ${i.title}` })),
  ];

  return (
    <>
      <div className="breadcrumb small">
        <Link to="/projects">Projects</Link> › <Link to={`/projects/${projectId}`}>{project.data?.name ?? "…"}</Link> ›{" "}
        <Link to={`/projects/${projectId}?tab=comparison`}>Comparison</Link> › {p.oracle_name}
      </div>
      <header className="page-header">
        <div>
          <h1 className="pair-title">
            <span>{p.oracle_name}</span>
            <span className="muted" aria-label="compared with">⇄</span>
            <span className="mono">{p.target_name ?? (p.status === "ignored" ? "ignored" : "missing in PostgreSQL")}</span>
          </h1>
          <p className="object-meta">
            <RiskChip risk={p.risk} />
            <SignatureChip status={p.signature_status} />
            <VerdictChip verdict={p.ai_verdict} stale={p.ai_stale} />
            <span className="small muted">
              {p.match_method ? `matched by ${METHOD_LABEL[p.match_method] ?? p.match_method}` : "no match"}
              {p.match_confidence != null && p.match_method !== "manual" && ` (${Math.round(p.match_confidence * 100)}% confidence)`}
            </span>
            {p.oracle && <Link className="small" to={`/projects/${projectId}/objects/${p.oracle.source_object_id}`}>Oracle object in inventory →</Link>}
          </p>
        </div>
        <div className="actions">
          <button className="btn" onClick={() => setEditing((v) => !v)}>{editing ? "Close" : "Change match"}</button>
          {p.status === "matched" && (
            <button className="btn btn-primary" disabled={!!reviewJob} onClick={() => setReviewing((v) => !v)}>
              {ai ? "Re-review with Claude" : "Review with Claude"}
            </button>
          )}
        </div>
      </header>

      {editing && <MatchEditor pair={p} projectId={projectId} onSaved={() => { setEditing(false); refresh(); }} />}
      {reviewing && (
        <section className="card">
          <ReviewLauncher projectId={projectId} pairIds={[p.id]} onStarted={(id) => { setReviewJob(id); setReviewing(false); }} onCancel={() => setReviewing(false)} />
        </section>
      )}
      {reviewJob && <JobProgress key={reviewJob} jobId={reviewJob} label="Claude review" onDone={refresh} />}

      <div className="pair-cards">
          {p.oracle?.kind === "routine" && (
            <section className="card">
              <h2>Signature</h2>
              <SignatureTable pair={p} />
              {p.signature_diffs.map((d, i) => (
                <div key={i} className="finding">
                  <div className="finding-head"><SeverityChip severity={d.severity} /> <span className="small">{d.message}</span></div>
                </div>
              ))}
            </section>
          )}

          <section className="card">
            <h2>Static findings</h2>
            {p.status === "missing_in_target" && <p className="error-text small">No PostgreSQL counterpart was found. Use “Change match” if it exists under another name.</p>}
            {p.status === "matched" && p.static_findings.length === 0 && <p className="muted small">No known pitfalls detected.</p>}
            {p.static_findings.map((f, i) => (
              <div key={i} className="finding">
                <div className="finding-head"><SeverityChip severity={f.severity} /> <strong>{f.title}</strong></div>
                <div className="muted small">{f.detail}</div>
                <div className="line-row">
                  <LineChips lines={f.oracle_lines} side="oracle" onJump={jump} />
                  <LineChips lines={f.postgres_lines} side="postgres" onJump={jump} />
                </div>
              </div>
            ))}
          </section>

          <section className="card">
            <h2>Claude review</h2>
            {!ai && <p className="muted small">Not reviewed yet.</p>}
            {ai && (
              <>
                {p.ai_stale && <div className="banner banner-warn small">The code changed after this review; re-run it.</div>}
                <p><VerdictChip verdict={ai.verdict} /> <span className="muted small">confidence {ai.confidence}</span></p>
                <p className="small">{ai.summary}</p>
                {ai.issues.map((i, n) => (
                  <div key={n} className="finding">
                    <div className="finding-head"><SeverityChip severity={i.severity} /> <strong>{i.title}</strong> <span className="muted small">{i.category.replace(/_/g, " ")}</span></div>
                    <div className="small">{i.explanation}</div>
                    {i.suggested_fix && <div className="small fix"><strong>Fix:</strong> {i.suggested_fix}</div>}
                    <div className="line-row">
                      <LineChips lines={i.oracle_lines} side="oracle" onJump={jump} />
                      <LineChips lines={i.postgres_lines} side="postgres" onJump={jump} />
                    </div>
                  </div>
                ))}
                <p className="muted small">{ai.model} · {formatTime(ai.reviewed_at)}</p>
              </>
            )}
          </section>

          <section className="card form">
            <h2>Team review</h2>
            <select
              value={p.review_status}
              onChange={(e) => saveReview.mutate({ review_status: e.target.value })}
              aria-label="Team review status"
            >
              {Object.entries(REVIEW_STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <textarea rows={3} placeholder="Notes for the team…" value={note ?? p.review_note ?? ""} onChange={(e) => setNote(e.target.value)} />
            {note !== null && (
              <div className="actions">
                <button className="btn btn-sm btn-ghost" onClick={() => setNote(null)}>Cancel</button>
                <button className="btn btn-sm btn-primary" onClick={() => saveReview.mutate({ review_note: note || null })}>Save note</button>
              </div>
            )}
            <ErrorBanner error={saveReview.error} />
          </section>
      </div>

      <div className="side-by-side">
          <section className="card table-card">
            <div className="card-pad source-head">
              <h2>Oracle</h2>
              <span className="muted small">
                {p.oracle?.object_label}
                {p.oracle?.member_range && ` · member at lines ${p.oracle.member_range[0]}–${p.oracle.member_range[1]}`}
              </span>
            </div>
            {p.oracle ? (
              <SourceViewer
                ref={oracleRef}
                source={p.oracle.source}
                marks={buildMarks(oracleEntries)}
                range={p.oracle.member_range}
                selected={selected.oracle}
                onSelect={(l) => setSelected((s) => ({ ...s, oracle: l }))}
              />
            ) : <p className="muted card-pad">The Oracle object is no longer in the inventory.</p>}
          </section>
          <section className="card table-card">
            <div className="card-pad source-head">
              <h2>PostgreSQL</h2>
              <span className="muted small">{p.target ? `${p.target.kind}${p.target.language ? ` · ${p.target.language}` : ""}` : ""}</span>
            </div>
            {p.target?.source ? (
              <SourceViewer
                ref={pgRef}
                source={p.target.source}
                marks={buildMarks(pgEntries)}
                selected={selected.postgres}
                onSelect={(l) => setSelected((s) => ({ ...s, postgres: l }))}
              />
            ) : <p className="muted card-pad">No PostgreSQL counterpart.</p>}
          </section>
      </div>
    </>
  );
}
