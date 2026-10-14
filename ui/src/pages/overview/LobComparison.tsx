import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";

import { lobApi, type Connection, type LobCheckDetail, type LobMeasure, type LobStatus, type LobTableRow } from "../../api";
import { JobProgress } from "../../components/comparison";
import { EmptyState, ErrorBanner, formatTime } from "../../components/ui";

const STATUS = {
  error: { label: "Problem", cls: "diff-error", icon: "✕" },
  warning: { label: "Check", cls: "diff-warning", icon: "!" },
  info: { label: "Note", cls: "diff-info", icon: "i" },
  ok: { label: "Same", cls: "diff-same", icon: "✓" },
} as const;
const LIMITS = [[300, "5 min"], [900, "15 min"], [3600, "1 hour"]] as const;

const fmt = (n: number | null | undefined) => (n === null || n === undefined ? "—" : n.toLocaleString());

const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

function size(n: number | null | undefined, unit: LobMeasure["unit"]) {
  if (n === null || n === undefined) return "—";
  if (unit !== "bytes") return `${n >= 10_000_000 ? compact.format(n) : n.toLocaleString()} ch`;
  for (const [label, factor] of [["GB", 2 ** 30], ["MB", 2 ** 20], ["KB", 2 ** 10]] as const) {
    if (n >= factor) return `${(n / factor).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${label}`;
  }
  return `${n.toLocaleString()} B`;
}

function Badge({ status }: { status: LobStatus }) {
  const s = STATUS[status];
  return <span className={`diff-badge ${s.cls}`}>{s.icon} {s.label}</span>;
}

function download(name: string, rows: (string | number | null | undefined)[][]) {
  const text = rows.map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

/** A PostgreSQL number, highlighted when it is below (missing data) or above Oracle's. */
function Diff({ pg, ora, children }: { pg: number | null | undefined; ora: number | null | undefined; children: React.ReactNode }) {
  const cls = pg === null || pg === undefined || ora === null || ora === undefined || pg === ora ? "" : pg < ora ? "error-text" : "warn-text";
  return cls ? <strong className={cls}>{children}</strong> : <>{children}</>;
}

function MeasureCells({ m, other, side, sized }: { m: LobMeasure | null; other: LobMeasure | null; side: "left" | "right"; sized: boolean }) {
  const cls = `num mono cmp-${side}`;
  if (!m) return <td colSpan={sized ? 5 : 2} className={`cmp-${side} cmp-absent`}>— no column —</td>;
  const compare = side === "right" && other;
  const cell = (pg: number | null | undefined, ora: number | null | undefined, text: string) => {
    const exact = pg === null || pg === undefined ? undefined : `${pg.toLocaleString()} ${m.unit === "bytes" ? "bytes" : m.unit === "chars" ? "characters" : ""}`;
    return <span title={exact}>{compare ? <Diff pg={pg} ora={ora}>{text}</Diff> : text}</span>;
  };
  return (
    <>
      <td className={`mono small cmp-${side}`}>{m.type}</td>
      <td className={cls}>{cell(m.non_null, other?.non_null, fmt(m.non_null))}</td>
      {sized && <>
        <td className={cls}>{cell(m.non_empty, other?.non_empty, fmt(m.non_empty))}</td>
        <td className={cls}>{cell(m.total, other?.total, size(m.total, m.unit))}</td>
        <td className={cls}>{cell(m.max, other?.max, size(m.max, m.unit))}</td>
      </>}
    </>
  );
}

function RowsCell({ side, rows, error, other }: { side: "left" | "right"; rows: number | null | undefined; error?: string | null; other?: number | null }) {
  return (
    <td className={`num mono cmp-${side}`}>
      {error ? <span className="warn-text" title={error}>not counted</span>
        : side === "right" ? <Diff pg={rows} ora={other}>{fmt(rows)}</Diff> : fmt(rows)}
    </td>
  );
}

function TableRows({ t, sized }: { t: LobTableRow; sized: boolean }) {
  const span = sized ? 5 : 2;
  const rowCls = (s: LobStatus) => `diff-row diff-row-${s === "ok" ? "same" : s}`;
  return (
    <>
      <tr className={`${rowCls(t.status)} lob-table-row`}>
        <td className="mono lob-name"><strong>{t.schema}.{t.table}</strong>
          {t.postgres_table && t.postgres_table.toLowerCase() !== `${t.schema}.${t.table}`.toLowerCase() &&
            <div className="muted small">→ {t.postgres_table}</div>}
        </td>
        {t.oracle ? <>
          <td className="cmp-left small muted">table</td>
          <RowsCell side="left" rows={t.oracle.rows} error={t.oracle.error} />
          <td colSpan={span - 1} className="cmp-left" />
        </> : <td colSpan={span + 1} className="cmp-left cmp-absent">— not in Oracle —</td>}
        <td className="cmp-status-col"><Badge status={t.status} /></td>
        {t.postgres ? <>
          <td className="cmp-right small muted">table</td>
          <RowsCell side="right" rows={t.postgres.rows} error={t.postgres.error} other={t.oracle?.error ? null : t.oracle?.rows} />
          <td colSpan={span - 1} className="cmp-right" />
        </> : <td colSpan={span + 1} className="cmp-right cmp-absent">— table not in PostgreSQL —</td>}
      </tr>
      {t.issues.length > 0 && (
        <tr className={`diff-msg ${rowCls(t.status)}`}><td colSpan={2 * span + 4} className="small">{t.issues.map((i, k) => <div key={k}>{i.message}</div>)}</td></tr>
      )}
      {t.columns.map((c) => (
        <Fragment key={c.column}>
          <tr className={rowCls(c.status)}>
            <td className="mono lob-name lob-col-name">↳ {c.column}</td>
            {c.oracle ? <MeasureCells m={c.oracle} other={null} side="left" sized={sized} /> : <td colSpan={span} className="cmp-left cmp-absent">—</td>}
            <td className="cmp-status-col">{c.status !== "ok" && <Badge status={c.status} />}</td>
            {t.postgres ? <MeasureCells m={c.postgres} other={c.oracle} side="right" sized={sized} /> : <td colSpan={span} className="cmp-right cmp-absent">—</td>}
          </tr>
          {c.issues.length > 0 && (
            <tr className={`diff-msg ${rowCls(c.status)}`}><td colSpan={2 * span + 4} className="small lob-col-msg">{c.issues.map((i, k) => <div key={k}>{i.message}</div>)}</td></tr>
          )}
        </Fragment>
      ))}
    </>
  );
}

function Results({ data }: { data: LobCheckDetail }) {
  const [filter, setFilter] = useState<"problems" | "attention" | "all">("attention");
  const [schema, setSchema] = useState("");
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const sized = data.sizes;
  const all = data.tables;
  const schemas = [...new Set(all.map((t) => t.schema))].sort();
  const inSchema = all.filter((t) => !schema || t.schema === schema);
  const n = (...s: LobStatus[]) => inSchema.filter((t) => s.includes(t.status)).length;
  const rows = inSchema.filter((t) =>
    (filter === "all" || (filter === "problems" ? t.status === "error" : t.status === "error" || t.status === "warning"))
    && (!needle || `${t.schema}.${t.table} ${t.postgres_table ?? ""} ${t.columns.map((c) => c.column).join(" ")}`.toLowerCase().includes(needle)));

  const exportCsv = () => download(`lob_check${schema ? "_" + schema : ""}.csv`, [
    ["Table", "Column", "Status", "Oracle type", "Oracle rows", "Oracle values", "Oracle with data", "Oracle total length", "Oracle largest",
      "PostgreSQL table", "PostgreSQL type", "PostgreSQL rows", "PostgreSQL values", "PostgreSQL with data", "PostgreSQL total length",
      "PostgreSQL largest", "Unit", "Issues"],
    ...rows.flatMap((t) => t.columns.map((c) => [
      `${t.schema}.${t.table}`, c.column, c.status, c.oracle?.type, t.oracle?.rows, c.oracle?.non_null, c.oracle?.non_empty, c.oracle?.total,
      c.oracle?.max, t.postgres_table, c.postgres?.type, t.postgres?.rows, c.postgres?.non_null, c.postgres?.non_empty, c.postgres?.total,
      c.postgres?.max, c.oracle?.unit ?? c.postgres?.unit, [...t.issues, ...c.issues].map((i) => i.message).join(" | "),
    ])),
  ]);

  return (
    <>
      <div className="filters">
        <div className="chips">
          <button className={`chip chip-btn ${filter === "problems" ? "active" : ""}`} onClick={() => setFilter("problems")}>Problems ({n("error")})</button>
          <button className={`chip chip-btn ${filter === "attention" ? "active" : ""}`} onClick={() => setFilter("attention")}>Problems + checks ({n("error", "warning")})</button>
          <button className={`chip chip-btn ${filter === "all" ? "active" : ""}`} onClick={() => setFilter("all")}>All ({inSchema.length})</button>
        </div>
        {schemas.length > 1 && (
          <select value={schema} onChange={(e) => setSchema(e.target.value)} aria-label="Schema">
            <option value="">All schemas</option>
            {schemas.map((s) => <option key={s} value={s}>{s} ({all.filter((t) => t.schema === s).length})</option>)}
          </select>
        )}
        <input type="search" placeholder="Filter by table or column…" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn btn-sm" onClick={exportCsv} disabled={!rows.length}>Download CSV</button>
      </div>
      {rows.length === 0 ? <EmptyState>{filter === "all" ? "Nothing matches." : "No differences in this filter."}</EmptyState> : (
        <div className="table-scroll seq-table-list">
          <table className="compact cmp-table lob-table">
            <thead>
              <tr>
                <th rowSpan={2}>Table / LOB column</th>
                <th colSpan={sized ? 5 : 2} className="cmp-half-head cmp-left">Oracle</th>
                <th rowSpan={2} className="cmp-status-col">Status</th>
                <th colSpan={sized ? 5 : 2} className="cmp-half-head cmp-right">PostgreSQL</th>
              </tr>
              <tr>
                {["left", "right"].map((side) => (
                  <Fragment key={side}>
                    <th className={`cmp-${side}`}>Type</th>
                    <th className={`num cmp-${side}`} title="Rows for the table; values that are not NULL for a column">Rows / values</th>
                    {sized && <>
                      <th className={`num cmp-${side}`} title="Values that are not empty (length > 0)">With data</th>
                      <th className={`num cmp-${side}`} title="Total length: bytes for binary, characters (ch) for text">Total</th>
                      <th className={`num cmp-${side}`}>Largest</th>
                    </>}
                  </Fragment>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 600).map((t) => <TableRows key={`${t.schema}.${t.table}`} t={t} sized={sized} />)}
            </tbody>
          </table>
          {rows.length > 600 && <p className="muted small card-pad">Showing 600 of {rows.length.toLocaleString()} tables: download the CSV for all.</p>}
        </div>
      )}
    </>
  );
}

export default function LobComparison({ connections }: { connections: Connection[] }) {
  const queryClient = useQueryClient();
  const oracles = connections.filter((c) => c.kind === "oracle");
  const pgs = connections.filter((c) => c.kind === "postgres");
  const [ora, setOra] = useState<string | null>(null);
  const [pg, setPg] = useState<string | null>(null);
  const [sizes, setSizes] = useState(true);
  const [limit, setLimit] = useState(300);
  const oracleId = ora ?? oracles[0]?.id ?? "";
  const pgId = pg ?? pgs[0]?.id ?? "";

  const runs = useQuery({
    queryKey: ["lob-checks"], queryFn: lobApi.list,
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "queued" || r.status === "running") ? 4000 : false),
  });
  const same = (r: { oracle_connection_id: string; postgres_connection_id: string }) =>
    r.oracle_connection_id === oracleId && r.postgres_connection_id === pgId;
  const active = runs.data?.find((r) => (r.status === "queued" || r.status === "running") && same(r));
  const latest = runs.data?.find((r) => r.status === "succeeded" && same(r));
  const last = runs.data?.find((r) => same(r) && r.status !== "queued" && r.status !== "running");
  const detail = useQuery({ queryKey: ["lob-checks", latest?.id], queryFn: () => lobApi.get(latest!.id), enabled: !!latest, staleTime: Infinity });
  const start = useMutation({
    mutationFn: () => lobApi.start(oracleId, pgId, sizes, limit),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["lob-checks"] }),
  });
  const d = detail.data;
  const s = d?.summary;

  if (!oracles.length || !pgs.length) return null;
  return (
    <>
      <div className="stats-head">
        <h2>LOB data check</h2>
        <span className="muted small">
          Tables with BLOB, CLOB, NCLOB, BFILE, LONG, LONG RAW or XMLTYPE columns, counted in both databases to find LOB data that didn't migrate.
        </span>
      </div>
      <section className="card table-card">
        <div className="explorer-bar-row card-pad">
          <label className="explorer-schemas small">Oracle
            <select value={oracleId} onChange={(e) => setOra(e.target.value)} disabled={!!active}>
              {oracles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
          <label className="explorer-schemas small">PostgreSQL
            <select value={pgId} onChange={(e) => setPg(e.target.value)} disabled={!!active}>
              {pgs.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
          <label className="small" title="Total and largest length of the values: finds truncated LOBs, but reads every LOB's length">
            <input type="checkbox" checked={sizes} onChange={(e) => setSizes(e.target.checked)} disabled={!!active} /> Measure sizes
          </label>
          <label className="explorer-schemas small">Limit per table
            <select value={limit} onChange={(e) => setLimit(Number(e.target.value))} disabled={!!active}>
              {LIMITS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select>
          </label>
          <button className="btn btn-primary" onClick={() => start.mutate()} disabled={!!active || start.isPending || !oracleId || !pgId}>
            {active ? "Counting…" : latest ? "Check again" : "Check LOB data"}
          </button>
        </div>
        <p className="muted small card-pad">
          Uses each connection's schema scope. Read-only: it counts rows and LOB values and reads their lengths, never their contents.
          Every table is read in full, so on large tables this takes a while. Data that changed after the migration also shows as a difference.
        </p>
        <div className="card-pad">
          <ErrorBanner error={runs.error ?? detail.error ?? start.error} />
          {active?.job_id && <JobProgress key={active.job_id} jobId={active.job_id} label="LOB data check"
            onDone={() => queryClient.invalidateQueries({ queryKey: ["lob-checks"] })} />}
          {last?.status === "failed" && !active && (
            <div className="banner banner-error">The last check failed: {last.error} {last.job_id && <Link to={`/jobs/${last.job_id}`}>Log</Link>}</div>
          )}
        </div>
        {s && (
          <div className="explorer-stats small card-pad">
            <span><strong>{fmt(s.oracle_tables)}</strong> Oracle tables with LOBs ({fmt(s.oracle_columns)} columns)</span>
            <span><strong>{fmt(s.matched_tables)}</strong> found in PostgreSQL</span>
            <span className={s.missing_tables ? "error-text" : ""}><strong>{fmt(s.missing_tables)}</strong> missing in PostgreSQL</span>
            <span className={s.rows_missing ? "error-text" : ""}><strong>{fmt(s.rows_missing)}</strong> rows missing</span>
            <span className={s.values_missing ? "error-text" : ""}><strong>{fmt(s.values_missing)}</strong> LOB values missing</span>
            {d!.sizes && <span className={s.truncated_columns ? "error-text" : ""}><strong>{fmt(s.truncated_columns)}</strong> columns truncated</span>}
            {!!s.not_counted && <span className="warn-text"><strong>{fmt(s.not_counted)}</strong> tables not counted</span>}
            {!!s.postgres_only_tables && <span className="muted">{fmt(s.postgres_only_tables)} binary tables only in PostgreSQL</span>}
            <span className="muted">checked {formatTime(d!.finished_at)}</span>
          </div>
        )}
        {d && d.warnings.length > 0 && <div className="banner banner-warn small">{d.warnings.map((w) => <div key={w}>{w}</div>)}</div>}
        {runs.isSuccess && !latest && !active && <EmptyState>Choose the databases and click <strong>Check LOB data</strong>.</EmptyState>}
        {d && <Results data={d} />}
      </section>
    </>
  );
}
