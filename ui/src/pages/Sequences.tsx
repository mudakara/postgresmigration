import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, sequenceApi, type SequenceCheckDetail, type SequenceResult, type SequenceStatus } from "../api";
import { JobProgress } from "../components/comparison";
import { EmptyState, ErrorBanner, PageHeader, formatTime } from "../components/ui";

const STATUS: Record<SequenceStatus, { label: string; badge: string; help: string }> = {
  behind: { label: "Behind the data", badge: "badge-bad", help: "The next insert will hit a duplicate key." },
  overflow: { label: "Too small", badge: "badge-bad", help: "The sequence's type or MAXVALUE can't hold the values it needs." },
  behind_oracle: { label: "Behind Oracle", badge: "badge-warn", help: "Below the value Oracle's sequence had reached: values already handed out can be issued again." },
  check_failed: { label: "Couldn't check", badge: "badge-warn", help: "max() of a linked column failed (timeout or privilege)." },
  no_privilege: { label: "No privilege", badge: "badge-warn", help: "This login can't read the sequence's value." },
  descending: { label: "Descending", badge: "badge-neutral", help: "Descending sequence: check it manually." },
  unlinked: { label: "Not linked", badge: "badge-neutral", help: "No column found that it fills; it may be used from application code." },
  ok: { label: "OK", badge: "badge-ok", help: "Ahead of the data." },
};
const ORDER: SequenceStatus[] = ["behind", "overflow", "behind_oracle", "check_failed", "no_privilege", "descending", "unlinked", "ok"];
const VIA: Record<string, string> = {
  identity: "identity column", serial: "serial column", default: "column default", trigger: "trigger", insert: "INSERT in",
};

function fmt(n: number | null | undefined) {
  return n === null || n === undefined ? "—" : n.toLocaleString();
}

function Columns({ s }: { s: SequenceResult }) {
  if (!s.columns.length) return <span className="muted small">—</span>;
  return (
    <div className="seq-columns">
      {s.columns.map((c) => (
        <div key={`${c.schema}.${c.table}.${c.column}`} className="small">
          <span className="mono">{c.schema}.{c.table}.{c.column}</span>{" "}
          <span className="muted">
            ({VIA[c.via]}{c.source ? ` ${c.source}` : ""}) max {c.error ? <span className="warn-text" title={c.error}>?</span> : fmt(c.max_value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function ScriptPanel({ check }: { check: SequenceCheckDetail }) {
  const [preview, setPreview] = useState<"needed" | "all" | null>(null);
  const script = useQuery({
    queryKey: ["sequence-script", check.id, preview],
    queryFn: () => sequenceApi.script(check.id, preview!),
    enabled: !!preview,
  });
  const [copied, setCopied] = useState(false);
  const counts = check.summary.status;
  const needed = (counts?.behind ?? 0) + (counts?.behind_oracle ?? 0);
  return (
    <section className="card seq-script">
      <h2>Fix script</h2>
      <p className="small">
        The app only reads your databases, so it gives you the fix as a SQL script to run with a login that owns the
        sequences. Each statement recomputes <span className="mono">max()</span> when it runs (rows added since this check
        are covered), includes Oracle's value when compared, and <strong>never moves a sequence backwards</strong>, so the
        script is safe to run again, for example after every data load.
      </p>
      <div className="toolbar">
        <div className="chips">
          <a className="btn btn-primary" href={sequenceApi.scriptUrl(check.id, "needed")} download>
            Download fix for {needed} sequence(s) behind
          </a>
          <a className="btn" href={sequenceApi.scriptUrl(check.id, "all")} download>
            Download re-sync of all {check.summary.linked ?? 0} linked
          </a>
        </div>
        <div className="chips">
          <button className={`chip chip-btn ${preview === "needed" ? "active" : ""}`} onClick={() => setPreview(preview === "needed" ? null : "needed")}>Preview fix</button>
          <button className={`chip chip-btn ${preview === "all" ? "active" : ""}`} onClick={() => setPreview(preview === "all" ? null : "all")}>Preview re-sync</button>
        </div>
      </div>
      <pre className="seq-run">psql -h &lt;host&gt; -p &lt;port&gt; -d &lt;database&gt; -U &lt;sequence owner&gt; -f fix_sequences.sql</pre>
      <ErrorBanner error={script.error} />
      {preview && script.data && (
        <div className="seq-preview">
          <button className="btn btn-sm" onClick={() => { navigator.clipboard?.writeText(script.data!); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>
            {copied ? "Copied" : "Copy"}
          </button>
          <pre>{script.data}</pre>
        </div>
      )}
      {(counts?.overflow ?? 0) > 0 && (
        <div className="banner banner-warn small">
          {counts!.overflow} sequence(s) can't hold the values they need: the script lists the ALTER SEQUENCE to run first (commented out).
        </div>
      )}
    </section>
  );
}

function Results({ check }: { check: SequenceCheckDetail }) {
  const [filter, setFilter] = useState<SequenceStatus | "attention" | "all">("attention");
  const [q, setQ] = useState("");
  const counts = check.summary.status ?? ({} as Record<SequenceStatus, number>);
  const attention: SequenceStatus[] = ["behind", "overflow", "behind_oracle", "check_failed", "no_privilege"];
  const needle = q.trim().toLowerCase();
  const rows = check.sequences.filter((s) =>
    (filter === "all" || (filter === "attention" ? attention.includes(s.status) : s.status === filter))
    && (!needle || `${s.schema}.${s.name}`.includes(needle) || s.columns.some((c) => `${c.schema}.${c.table}.${c.column}`.includes(needle))),
  );
  return (
    <section className="card table-card">
      <div className="filters">
        <div className="chips">
          <button className={`chip chip-btn ${filter === "attention" ? "active" : ""}`} onClick={() => setFilter("attention")}>
            Needs attention ({attention.reduce((n, s) => n + (counts[s] ?? 0), 0)})
          </button>
          {ORDER.filter((s) => counts[s]).map((s) => (
            <button key={s} className={`chip chip-btn ${filter === s ? "active" : ""}`} onClick={() => setFilter(s)} title={STATUS[s].help}>
              {STATUS[s].label} ({counts[s]})
            </button>
          ))}
          <button className={`chip chip-btn ${filter === "all" ? "active" : ""}`} onClick={() => setFilter("all")}>All ({check.sequences.length})</button>
        </div>
        <input type="search" placeholder="Search sequences, tables, columns…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {rows.length === 0 ? (
        <EmptyState>{filter === "attention" ? "No sequence needs attention." : "No sequences match."}</EmptyState>
      ) : (
        <div className="table-scroll">
          <table className="compact">
            <thead>
              <tr>
                <th>Sequence</th><th>Status</th><th className="num">Next value</th><th>Fills</th>
                {check.summary.oracle_compared && <th className="num">Oracle next</th>}
                <th className="num">Needs to be ≥</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={`${s.schema}.${s.name}`}>
                  <td className="mono">{s.schema}.{s.name}<div className="muted small">{s.data_type}{s.increment !== 1 ? `, by ${s.increment}` : ""}</div></td>
                  <td>
                    <span className={`badge plain ${STATUS[s.status].badge}`} title={STATUS[s.status].help}>{STATUS[s.status].label}</span>
                    {s.status !== "ok" && <div className="small seq-message">{s.message}</div>}
                  </td>
                  <td className="num mono">{fmt(s.next_value)}</td>
                  <td><Columns s={s} /></td>
                  {check.summary.oracle_compared && (
                    <td className="num mono" title={s.oracle ? `${s.oracle.owner}.${s.oracle.name} LAST_NUMBER` : "No matching Oracle sequence"}>{fmt(s.oracle?.last_number)}</td>
                  )}
                  <td className="num mono">{fmt(s.needed_next)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function SequencesPage() {
  const { connectionId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const pgConnections = connections.data?.filter((c) => c.kind === "postgres") ?? [];
  const oracleConnections = connections.data?.filter((c) => c.kind === "oracle") ?? [];
  const [oracleId, setOracleId] = useState("");
  const [schemas, setSchemas] = useState("");

  useEffect(() => {
    const pg = connections.data?.filter((c) => c.kind === "postgres") ?? [];
    if (!connectionId && pg.length === 1) navigate(`/sequences/${pg[0].id}`, { replace: true });
  }, [connectionId, connections.data, navigate]);

  const checks = useQuery({
    queryKey: ["sequence-checks", connectionId],
    queryFn: () => sequenceApi.checks(connectionId),
    enabled: !!connectionId,
    refetchInterval: (q) => (q.state.data?.some((c) => c.status === "queued" || c.status === "running") ? 4000 : false),
  });
  const active = checks.data?.find((c) => c.status === "queued" || c.status === "running");
  const latest = checks.data?.find((c) => c.status === "succeeded" || c.status === "failed");
  const detail = useQuery({
    queryKey: ["sequence-check", latest?.id],
    queryFn: () => sequenceApi.check(latest!.id),
    enabled: !!latest && latest.status === "succeeded",
  });
  useEffect(() => {
    if (latest) {
      setOracleId(latest.oracle_connection_id ?? "");
      setSchemas(latest.requested_schemas.join(", "));
    }
  }, [latest?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = useMutation({
    mutationFn: () => sequenceApi.start({
      postgres_connection_id: connectionId, oracle_connection_id: oracleId || null,
      schemas: schemas.split(/[\s,]+/).filter(Boolean),
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sequence-checks", connectionId] }),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["sequence-checks", connectionId] });

  return (
    <>
      <PageHeader
        title="Sequence check"
        subtitle="After loading data from Oracle, find PostgreSQL sequences that are behind the data, and get a script that fixes them."
        actions={
          <select className="explorer-conn" value={connectionId} aria-label="PostgreSQL connection"
            onChange={(e) => navigate(e.target.value ? `/sequences/${e.target.value}` : "/sequences")}>
            <option value="">Choose a PostgreSQL connection…</option>
            {pgConnections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        }
      />
      <ErrorBanner error={connections.error ?? checks.error ?? detail.error} />
      {!connectionId && (
        <EmptyState>
          {pgConnections.length === 0 ? <>Add a PostgreSQL connection on the <Link to="/connections">Connections</Link> page first.</> : "Choose a PostgreSQL connection."}
        </EmptyState>
      )}
      {connectionId && (
        <>
          <section className="card explorer-bar">
            <p className="small muted">
              For each sequence the check finds the columns it fills (identity and serial columns, column defaults, triggers
              that assign <span className="mono">NEW.col := nextval(…)</span>, and INSERT statements in procedures), reads the
              current <span className="mono">max()</span> of those columns and flags sequences whose next value isn't above
              it. Read-only: nothing is changed.
            </p>
            <div className="explorer-bar-row">
              <label className="explorer-schemas small">
                Schemas
                <input value={schemas} onChange={(e) => setSchemas(e.target.value)} placeholder="All schemas (or e.g. sales, billing)" disabled={!!active} />
              </label>
              <label className="explorer-schemas small">
                Also compare with Oracle (optional)
                <select value={oracleId} onChange={(e) => setOracleId(e.target.value)} disabled={!!active}>
                  <option value="">Don't compare</option>
                  {oracleConnections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </label>
              <button className="btn btn-primary" onClick={() => start.mutate()} disabled={!!active || start.isPending}>
                {active ? "Checking…" : latest ? "Check again" : "Check sequences"}
              </button>
            </div>
            <ErrorBanner error={start.error} />
            {active?.job_id && <JobProgress key={active.job_id} jobId={active.job_id} label="Checking sequences" onDone={refresh} />}
            {latest?.status === "failed" && !active && (
              <div className="banner banner-error">The last check failed: {latest.error} {latest.job_id && <Link to={`/jobs/${latest.job_id}`}>Log</Link>}</div>
            )}
            {detail.data && (
              <div className="explorer-stats small">
                <span><strong>{fmt(detail.data.summary.sequences)}</strong> sequences</span>
                <span><strong>{fmt(detail.data.summary.linked)}</strong> linked to a column</span>
                <span className={detail.data.summary.status?.behind ? "error-text" : ""}><strong>{fmt(detail.data.summary.status?.behind)}</strong> behind the data</span>
                {detail.data.summary.oracle_compared && (
                  <span><strong>{fmt(detail.data.summary.oracle_matched)}</strong> matched to an Oracle sequence</span>
                )}
                <span className="muted">checked {formatTime(detail.data.finished_at)} in {detail.data.schemas.length} schema(s)</span>
              </div>
            )}
            {detail.data && detail.data.warnings.length > 0 && (
              <div className="banner banner-warn small">{detail.data.warnings.map((w) => <div key={w}>{w}</div>)}</div>
            )}
          </section>
          {checks.isSuccess && !latest && !active && <EmptyState>No check yet: click <strong>Check sequences</strong>.</EmptyState>}
          {detail.data && <ScriptPanel check={detail.data} />}
          {detail.data && <Results check={detail.data} />}
        </>
      )}
    </>
  );
}
