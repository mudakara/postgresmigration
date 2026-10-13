import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";

import { api, sequenceStatsApi, type SchemaSeqStats, type SequenceStatsDetail } from "../api";
import { JobProgress } from "../components/comparison";
import { EmptyState, ErrorBanner, PageHeader, formatTime } from "../components/ui";

const fmt = (n: number | null | undefined) => (n === null || n === undefined ? "—" : n.toLocaleString());
const STATUS = {
  error: { label: "Problem", cls: "diff-error", icon: "✕" },
  warning: { label: "Check", cls: "diff-warning", icon: "!" },
  info: { label: "Note", cls: "diff-info", icon: "i" },
  ok: { label: "OK", cls: "diff-same", icon: "✓" },
} as const;
type Status = keyof typeof STATUS;

function Badge({ status }: { status: Status }) {
  const s = STATUS[status];
  return <span className={`diff-badge ${s.cls}`}>{s.icon} {s.label}</span>;
}

function csv(rows: (string | number | null | undefined)[][]) {
  return rows.map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
}

function download(name: string, rows: (string | number | null | undefined)[][]) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv(rows)], { type: "text/csv" }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

// --- per schema -------------------------------------------------------------------------------------

function SchemaTable({ data, selected, onSelect }: { data: SequenceStatsDetail; selected: string; onSelect: (s: string) => void }) {
  const o = data.result.oracle?.schemas ?? [];
  const p = data.result.postgres?.schemas ?? [];
  const names = [...new Set([...o, ...p].map((s) => s.schema.toLowerCase()))].sort();
  const find = (list: SchemaSeqStats[], n: string) => list.find((s) => s.schema.toLowerCase() === n);
  const cells = (s?: SchemaSeqStats) => s ? (
    <>
      <td className="num">{fmt(s.sequences)}</td>
      <td className="num">{fmt(s.own)}</td>
      <td className="num">{s.other_schema ? <strong className="warn-text">⚠ {s.other_schema}</strong> : <span className="muted">0</span>}</td>
      <td className="num muted">{fmt(s.code_only)}</td>
      <td className="num muted">{fmt(s.unlinked)}</td>
    </>
  ) : <td colSpan={5} className="cmp-absent">—</td>;
  const both = !!data.result.oracle && !!data.result.postgres;
  return (
    <div className="table-scroll">
      <table className="compact seq-stats-schemas">
        <thead>
          <tr>
            <th rowSpan={2}>Schema</th>
            {data.result.oracle && <th colSpan={5} className="cmp-half-head cmp-left">Oracle</th>}
            {data.result.postgres && <th colSpan={5} className="cmp-half-head cmp-right">PostgreSQL</th>}
          </tr>
          <tr>
            {[...(data.result.oracle ? [0] : []), ...(data.result.postgres ? [1] : [])].map((i) => (
              <Fragment key={i}>
                <th className="num">Sequences</th><th className="num" title="Fill tables of their own schema">Own tables</th>
                <th className="num" title="Fill tables of, or are used by code in, another schema">Other schema</th>
                <th className="num" title="Used by code, but no column found">Code only</th><th className="num" title="Not used anywhere">Unused</th>
              </Fragment>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map((n) => (
            <tr key={n} className={`clickable ${selected === n ? "selected" : ""}`} onClick={() => onSelect(selected === n ? "" : n)}>
              <td className="mono"><strong>{n.toUpperCase()}</strong></td>
              {data.result.oracle && cells(find(o, n))}
              {data.result.postgres && cells(find(p, n))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small card-pad">
        Click a schema to list its sequences. <strong>Other schema</strong> counts sequences that fill tables of another schema
        (for example a sequence in SALES used for a BILLING table){both ? "; both databases are counted the same way" : ""}.
      </p>
    </div>
  );
}

function SchemaSequences({ data, schema }: { data: SequenceStatsDetail; schema: string }) {
  const sides = ([["Oracle", data.result.oracle], ["PostgreSQL", data.result.postgres]] as const).filter(([, d]) => d);
  return (
    <div className="seq-schema-lists">
      {sides.map(([label, d]) => {
        const rows = d!.sequences.filter((s) => s.schema.toLowerCase() === schema);
        return (
          <div key={label} className="min0">
            <h3>{label}: {rows.length} sequence(s) in {schema.toUpperCase()}</h3>
            <div className="table-scroll seq-list">
              <table className="compact">
                <thead><tr><th>Sequence</th><th className="num">{label === "Oracle" ? "Last number" : "Next value"}</th><th>Fills</th><th>Belongs to</th></tr></thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={s.name} className={s.ownership === "other_schema" ? "diff-row diff-row-warning" : ""}>
                      <td className="mono">{s.name}</td>
                      <td className="num mono">{fmt(label === "Oracle" ? s.last_number : s.next_value)}</td>
                      <td className="small">
                        {(s.links ?? []).length ? s.links.map((l: any) => <div key={`${l.schema}.${l.table}.${l.column}`} className="mono">{l.schema}.{l.table}.{l.column} <span className="muted">({l.via})</span></div>)
                          : (s.used_by ?? []).length ? <span className="muted">used by {s.used_by.slice(0, 3).join(", ")}</span> : <span className="muted">—</span>}
                      </td>
                      <td className="small">
                        {s.ownership === "other_schema" ? <strong className="warn-text">⚠ {s.other_schemas.join(", ")}</strong>
                          : s.ownership === "own" ? "own schema" : s.ownership === "code_only" ? <span className="muted">code only</span> : <span className="muted">unused</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// --- table by table ---------------------------------------------------------------------------------

function TableComparison({ data, schema }: { data: SequenceStatsDetail; schema: string }) {
  const [filter, setFilter] = useState<"problems" | "attention" | "all">("problems");
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const all = data.result.tables;
  const rows = all.filter((r) => (!schema || r.schema === schema)
    && (filter === "all" || (filter === "problems" ? r.status === "error" : r.status === "error" || r.status === "warning"))
    && (!needle || `${r.schema}.${r.table}.${r.column} ${r.oracle?.sequence ?? ""} ${r.postgres?.sequence ?? ""}`.toLowerCase().includes(needle)));
  const n = (s: string) => all.filter((r) => (!schema || r.schema === schema) && r.status === s).length;
  const exportCsv = () => download(`sequences_by_table${schema ? "_" + schema : ""}.csv`, [
    ["Table", "Column", "Status", "Oracle sequence", "Oracle last number", "Oracle max", "PostgreSQL sequence", "PostgreSQL next value", "PostgreSQL max", "Issues"],
    ...rows.map((r) => [`${r.schema}.${r.table}`, r.column, r.status, r.oracle?.sequence, r.oracle?.last_number, r.oracle?.max,
      r.postgres?.sequence, r.postgres?.next_value, r.postgres?.max, r.issues.map((i: any) => i.message).join(" | ")]),
  ]);
  return (
    <section className="card table-card">
      <div className="stats-drill-head">
        <h2>Table by table: Oracle vs PostgreSQL</h2>
        <span className="muted small">Each column a sequence fills, with the sequence values and the data's MAX() on both sides.</span>
      </div>
      <div className="filters">
        <div className="chips">
          <button className={`chip chip-btn ${filter === "problems" ? "active" : ""}`} onClick={() => setFilter("problems")}>Problems ({n("error")})</button>
          <button className={`chip chip-btn ${filter === "attention" ? "active" : ""}`} onClick={() => setFilter("attention")}>Problems + checks ({n("error") + n("warning")})</button>
          <button className={`chip chip-btn ${filter === "all" ? "active" : ""}`} onClick={() => setFilter("all")}>All ({all.filter((r) => !schema || r.schema === schema).length})</button>
        </div>
        <input type="search" placeholder="Filter by table, column or sequence…" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn btn-sm" onClick={exportCsv} disabled={!rows.length}>Download CSV</button>
        <a className="btn btn-sm btn-primary" href={sequenceStatsApi.fixUrl(data.id)} download>Download fix script</a>
      </div>
      {schema && <p className="small card-pad">Schema <strong>{schema.toUpperCase()}</strong> only (clear it in the schema table above).</p>}
      {rows.length === 0 ? <EmptyState>Nothing in this filter.</EmptyState> : (
        <div className="table-scroll seq-table-list">
          <table className="compact cmp-table">
            <thead>
              <tr>
                <th rowSpan={2}>Table.column</th>
                <th colSpan={3} className="cmp-half-head cmp-left">Oracle</th>
                <th rowSpan={2} className="cmp-status-col">Status</th>
                <th colSpan={3} className="cmp-half-head cmp-right">PostgreSQL</th>
              </tr>
              <tr>
                <th className="cmp-left">Sequence</th><th className="num cmp-left">Last number</th><th className="num cmp-left">Max in data</th>
                <th className="cmp-right">Sequence</th><th className="num cmp-right">Next value</th><th className="num cmp-right">Max in data</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 1500).map((r) => (
                <Fragment key={`${r.schema}.${r.table}.${r.column}`}>
                  <tr className={`diff-row diff-row-${r.status === "ok" ? "same" : r.status}`}>
                    <td className="mono">{r.schema}.{r.table}.<strong>{r.column}</strong></td>
                    {r.oracle ? <>
                      <td className="mono small cmp-left">{r.oracle.sequence}<div className="muted">{r.oracle.via}</div></td>
                      <td className="num mono cmp-left">{fmt(r.oracle.last_number)}</td>
                      <td className="num mono cmp-left">{r.oracle.max_error ? <span className="warn-text" title={r.oracle.max_error}>?</span> : fmt(r.oracle.max)}</td>
                    </> : <td colSpan={3} className="cmp-left cmp-absent">— no sequence —</td>}
                    <td className="cmp-status-col"><Badge status={r.status} /></td>
                    {r.postgres ? <>
                      <td className="mono small cmp-right">{r.postgres.sequence}<div className="muted">{r.postgres.via}</div></td>
                      <td className="num mono cmp-right">{fmt(r.postgres.next_value)}</td>
                      <td className="num mono cmp-right">{r.postgres.max_error ? <span className="warn-text" title={r.postgres.max_error}>?</span> : fmt(r.postgres.max)}</td>
                    </> : <td colSpan={3} className="cmp-right cmp-absent">— no sequence —</td>}
                  </tr>
                  {r.issues.length > 0 && (
                    <tr className={`diff-msg diff-row-${r.status === "ok" ? "same" : r.status}`}>
                      <td colSpan={8} className="small">{r.issues.map((i: any, k: number) => <div key={k}>{i.message}</div>)}</td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
          {rows.length > 1500 && <p className="muted small card-pad">Showing 1,500 of {rows.length.toLocaleString()}: download the CSV for all.</p>}
        </div>
      )}
    </section>
  );
}

// --- page ---------------------------------------------------------------------------------------------

export default function SequenceStatsPage() {
  const queryClient = useQueryClient();
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const oracles = connections.data?.filter((c) => c.kind === "oracle") ?? [];
  const pgs = connections.data?.filter((c) => c.kind === "postgres") ?? [];
  const [ora, setOra] = useState<string | null>(null);
  const [pg, setPg] = useState<string | null>(null);
  const oracleId = ora ?? oracles[0]?.id ?? "";
  const pgId = pg ?? pgs[0]?.id ?? "";
  const [schema, setSchema] = useState("");

  const runs = useQuery({
    queryKey: ["sequence-stats"], queryFn: sequenceStatsApi.list,
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "queued" || r.status === "running") ? 4000 : false),
  });
  const sameChoice = (r: { oracle_connection_id: string | null; postgres_connection_id: string | null }) =>
    (r.oracle_connection_id ?? "") === oracleId && (r.postgres_connection_id ?? "") === pgId;
  const active = runs.data?.find((r) => (r.status === "queued" || r.status === "running") && sameChoice(r));
  const latest = runs.data?.find((r) => r.status === "succeeded" && sameChoice(r));
  const failed = runs.data?.find((r) => sameChoice(r) && r.status !== "queued" && r.status !== "running");
  const detail = useQuery({ queryKey: ["sequence-stats", latest?.id], queryFn: () => sequenceStatsApi.get(latest!.id), enabled: !!latest, staleTime: Infinity });
  const start = useMutation({
    mutationFn: () => sequenceStatsApi.start(oracleId || null, pgId || null),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sequence-stats"] }),
  });
  const d = detail.data;
  const s = d?.summary;
  // Count from the (recomputed) table list, so the header matches the list below.
  const count = (status: string) => (d?.result.tables ?? []).filter((t) => t.status === status).length;

  return (
    <>
      <PageHeader title="Sequence statistics"
        subtitle="Every schema's sequences in Oracle and PostgreSQL: which tables they fill, sequences that serve another schema, and a table-by-table check of the migrated values." />
      <ErrorBanner error={connections.error ?? runs.error ?? detail.error ?? start.error} />
      <section className="card explorer-bar">
        <div className="explorer-bar-row">
          <label className="explorer-schemas small">Oracle
            <select value={oracleId} onChange={(e) => setOra(e.target.value)} disabled={!!active}>
              <option value="">(none)</option>
              {oracles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
          <label className="explorer-schemas small">PostgreSQL
            <select value={pgId} onChange={(e) => setPg(e.target.value)} disabled={!!active}>
              <option value="">(none)</option>
              {pgs.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
          <button className="btn btn-primary" onClick={() => start.mutate()} disabled={!!active || start.isPending || (!oracleId && !pgId)}>
            {active ? "Analysing…" : latest ? "Analyse again" : "Analyse sequences"}
          </button>
        </div>
        <p className="muted small">
          Uses each connection's schema scope. Read-only: besides the dictionaries it reads only MAX() of the columns sequences fill.
          With both databases chosen, the values are compared table by table.
        </p>
        {active?.job_id && <JobProgress key={active.job_id} jobId={active.job_id} label="Analysing sequences" onDone={() => queryClient.invalidateQueries({ queryKey: ["sequence-stats"] })} />}
        {failed?.status === "failed" && !active && <div className="banner banner-error">The last analysis failed: {failed.error} {failed.job_id && <Link to={`/jobs/${failed.job_id}`}>Log</Link>}</div>}
        {s && (
          <div className="explorer-stats small">
            {d!.result.oracle && <span><strong>{fmt(s.oracle_sequences)}</strong> Oracle sequences</span>}
            {d!.result.postgres && <span><strong>{fmt(s.postgres_sequences)}</strong> PostgreSQL sequences</span>}
            <span className={s.cross_schema ? "warn-text" : ""}><strong>{fmt(s.cross_schema)}</strong> serving another schema</span>
            {!!s.tables_compared && <>
              <span><strong>{fmt(s.tables_compared)}</strong> table columns compared</span>
              <span className="error-text"><strong>{fmt(count("error"))}</strong> problems</span>
              <span className="warn-text"><strong>{fmt(count("warning"))}</strong> to check</span>
            </>}
            <span className="muted">analysed {formatTime(d!.finished_at)}</span>
          </div>
        )}
        {d && d.warnings.length > 0 && <div className="banner banner-warn small">{d.warnings.map((w) => <div key={w}>{w}</div>)}</div>}
      </section>
      {runs.isSuccess && !latest && !active && <EmptyState>Choose the databases and click <strong>Analyse sequences</strong>.</EmptyState>}
      {d && (
        <>
          <section className="card table-card">
            <div className="stats-drill-head"><h2>By schema</h2></div>
            <SchemaTable data={d} selected={schema} onSelect={setSchema} />
            {schema && <SchemaSequences data={d} schema={schema} />}
          </section>
          {d.result.cross_schema.length > 0 && (
            <section className="card table-card">
              <div className="stats-drill-head">
                <h2>Sequences serving another schema ({d.result.cross_schema.length})</h2>
                <button className="btn btn-sm" onClick={() => download("sequences_serving_another_schema.csv", [
                  ["Database", "Sequence", "Serves", "Tables", "Used by"],
                  ...d.result.cross_schema.map((c) => [c.side, c.sequence, c.other_schemas.join(" "), c.tables.join(" "), c.used_by.join(" ")]),
                ])}>Download CSV</button>
              </div>
              <div className="table-scroll seq-list">
                <table className="compact">
                  <thead><tr><th>Database</th><th>Sequence</th><th>Serves schema</th><th>Fills</th><th>Used by</th></tr></thead>
                  <tbody>
                    {d.result.cross_schema.filter((c) => !schema || c.schema.toLowerCase() === schema).map((c) => (
                      <tr key={`${c.side}.${c.sequence}`} className="diff-row diff-row-warning">
                        <td>{c.side === "oracle" ? "Oracle" : "PostgreSQL"}</td>
                        <td className="mono">{c.sequence}</td>
                        <td><strong className="warn-text">⚠ {c.other_schemas.join(", ")}</strong></td>
                        <td className="mono small">{c.tables.join(", ") || "—"}</td>
                        <td className="small muted">{c.used_by.join(", ") || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
          {d.result.tables.length > 0 && <TableComparison data={d} schema={schema} />}
          {d.result.unlinked.length > 0 && (
            <section className="card table-card">
              <div className="stats-drill-head">
                <h2>Oracle sequences not linked to a column ({d.result.unlinked.length})</h2>
                <span className="muted small">No column was found for these (they may be used by application code); compared with PostgreSQL by name.</span>
              </div>
              <div className="table-scroll seq-list">
                <table className="compact">
                  <thead><tr><th>Oracle sequence</th><th className="num">Last number</th><th>PostgreSQL</th><th className="num">Next value</th><th>Status</th></tr></thead>
                  <tbody>
                    {d.result.unlinked.filter((u) => !schema || u.schema.toLowerCase() === schema).map((u) => (
                      <tr key={u.sequence} className={`diff-row diff-row-${u.status === "ok" ? "same" : u.status}`}>
                        <td className="mono">{u.sequence}{u.used_by.length > 0 && <div className="muted small">used by {u.used_by.slice(0, 2).join(", ")}</div>}</td>
                        <td className="num mono">{fmt(u.last_number)}</td>
                        <td className="mono">{u.postgres ?? <span className="muted">—</span>}</td>
                        <td className="num mono">{fmt(u.next_value)}</td>
                        <td className="small"><Badge status={u.status} /> {u.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </>
      )}
    </>
  );
}
