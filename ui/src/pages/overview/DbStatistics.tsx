import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { catalogApi, type Connection, type SnapshotStats, type StatsArea } from "../../api";
import { JobProgress } from "../../components/comparison";
import { EmptyState, ErrorBanner, KindTag, formatTime } from "../../components/ui";
import DifferenceReport from "./DifferenceReport";

const AREA_LABEL: Record<StatsArea, string> = {
  tables: "Tables", views: "Views", columns: "Columns", indexes: "Indexes", foreign_keys: "Foreign keys",
  procedures: "Procedures", functions: "Functions", trigger_functions: "Trigger functions", packages: "Packages",
  triggers: "Triggers", invalid_code: "Invalid code",
};

const fmt = (n: number | null | undefined) => (n === null || n === undefined ? "—" : n.toLocaleString());
const compact = (n: number) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n.toLocaleString());

interface Tile { area: StatsArea; note?: ReactNode; warn?: boolean; filter?: string }

function tilesFor(conn: Connection, s: SnapshotStats): Tile[] {
  const x = s.extras;
  const tiles: Tile[] = [
    { area: "tables", note: <>{x.partitioned_tables ? `${x.partitioned_tables} partitioned · ` : ""}{fmt(x.tables_without_primary_key)} without a PK · ≈{compact(x.rows_estimate)} rows</> },
    { area: "views" },
    { area: "columns", note: `${fmt(x.indexed_columns)} indexed` },
    { area: "indexes", note: `${fmt(x.unique_indexes)} unique` },
    { area: "foreign_keys", note: x.unindexed_foreign_keys ? `${fmt(x.unindexed_foreign_keys)} without an index` : "all indexed", warn: x.unindexed_foreign_keys > 0 },
    {
      area: "procedures",
      note: s.totals.packages ? `${fmt(s.totals.procedures - x.package_procedures)} standalone · ${fmt(x.package_procedures)} in ${fmt(s.totals.packages)} packages` : undefined,
    },
    {
      area: "functions",
      note: s.totals.packages ? `${fmt(s.totals.functions - x.package_functions)} standalone · ${fmt(x.package_functions)} in packages` : undefined,
    },
  ];
  if (conn.kind === "postgres" || s.totals.trigger_functions) tiles.push({ area: "trigger_functions" });
  tiles.push({ area: "triggers", note: x.disabled_triggers ? `${x.disabled_triggers} disabled` : undefined, warn: x.disabled_triggers > 0 });
  return tiles;
}

function ConnectionStats({ conn, selected, onPick }: {
  conn: Connection; selected: StatsArea | null; onPick: (area: StatsArea, filter?: string) => void;
}) {
  const queryClient = useQueryClient();
  const snapshots = useQuery({
    queryKey: ["catalog-snapshots", conn.id], queryFn: () => catalogApi.snapshots(conn.id),
    refetchInterval: (q) => (q.state.data?.some((s) => s.status === "queued" || s.status === "running") ? 3000 : false),
  });
  const snapshot = snapshots.data?.find((s) => s.status === "succeeded");
  const active = snapshots.data?.find((s) => s.status === "queued" || s.status === "running");
  const stats = useQuery({ queryKey: ["snapshot-stats", snapshot?.id], queryFn: () => catalogApi.stats(snapshot!.id), enabled: !!snapshot, staleTime: Infinity });
  const read = useMutation({
    mutationFn: () => catalogApi.start(conn.id, snapshot?.requested_schemas ?? []),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["catalog-snapshots", conn.id] }),
  });
  return (
    <section className={`card stats-db cmp-kind-${conn.kind}`}>
      <div className="stats-db-head">
        <h3>{conn.name}</h3>
        <KindTag kind={conn.kind} />
        <span className="muted small">{conn.host}/{conn.database}</span>
        <span className="stats-db-actions small">
          {snapshot && <span className="muted">read {formatTime(snapshot.finished_at)}</span>}
          <button className="btn btn-sm" onClick={() => read.mutate()} disabled={!!active || read.isPending}>
            {active ? "Reading…" : snapshot ? "Refresh" : "Read catalog"}
          </button>
        </span>
      </div>
      <ErrorBanner error={snapshots.error ?? stats.error ?? read.error} />
      {active?.job_id && (
        <JobProgress key={active.job_id} jobId={active.job_id} label="Reading the catalog"
          onDone={() => queryClient.invalidateQueries({ queryKey: ["catalog-snapshots", conn.id] })} />
      )}
      {snapshots.isSuccess && !snapshot && !active && (
        <p className="muted small">Not read yet. <strong>Read catalog</strong> counts its tables, code and indexes (read-only, seconds to a minute).</p>
      )}
      {stats.data && (
        <div className="stats-tiles">
          {tilesFor(conn, stats.data).map((t) => (
            <button key={t.area} className={`stats-tile ${selected === t.area ? "active" : ""}`} onClick={() => onPick(t.area)}
              title={`List the ${AREA_LABEL[t.area].toLowerCase()}`}>
              <span className="stat-label">{AREA_LABEL[t.area]}</span>
              <span className="stat-value">{fmt(stats.data!.totals[t.area])}</span>
              {t.note && <span className={`small ${t.warn ? "warn-text" : "muted"}`}>{t.note}</span>}
            </button>
          ))}
          {stats.data.extras.invalid_code > 0 && (
            <button className="stats-tile stats-tile-bad" onClick={() => onPick("invalid_code")} title="Objects that don't compile">
              <span className="stat-label">Invalid code</span>
              <span className="stat-value">{fmt(stats.data.extras.invalid_code)}</span>
              <span className="small error-text">don't compile in Oracle</span>
            </button>
          )}
        </div>
      )}
    </section>
  );
}

// --- drill-down -----------------------------------------------------------------------------------

function TableLink({ conn, id, label, tab }: { conn: string; id?: string | null; label: string; tab?: string }) {
  if (!id) return <span className="mono">{label}</span>;
  return <Link className="mono" to={`/explorer/${conn}?table=${id}${tab ? `&tab=${tab}` : ""}`}>{label}</Link>;
}

function CodeLink({ conn, id, label }: { conn: string; id: string; label: string }) {
  return <Link className="mono" to={`/explorer/${conn}?mode=code&code=${id}`}>{label}</Link>;
}

const yes = (v: boolean | null | undefined, good = true) =>
  v === null || v === undefined ? <span className="muted">—</span> : v === good ? <span className="sev-ok">yes</span> : <span className="warn-text">no</span>;

function columnsFor(area: StatsArea, conn: string): { head: string[]; cells: (r: any) => ReactNode[] } {
  const t = (r: any, tab?: string) => <TableLink conn={conn} id={r.table_id} label={`${r.schema}.${r.table ?? r.name}`} tab={tab} />;
  switch (area) {
    case "tables":
    case "views":
      return {
        head: ["Name", "Kind", "Columns", "Indexes", "Rows (est.)", "Used by code", "Triggers"],
        cells: (r) => [t(r), r.kind, fmt(r.columns), fmt(r.indexes), fmt(r.rows), fmt(r.code), fmt(r.triggers)],
      };
    case "columns":
      return {
        head: ["Table", "Column", "Type", "Nullable", "Primary key", "Indexed"],
        cells: (r) => [t(r, "columns"), <span className="mono">{r.name}</span>, <span className="mono small">{r.type}</span>,
          r.nullable ? "yes" : <strong>NOT NULL</strong>, r.primary_key ? "PK" : "", yes(r.indexed)],
      };
    case "indexes":
      return {
        head: ["Table", "Index", "Columns", "Kind", "Type", "Status"],
        cells: (r) => [t(r, "indexes"), <span className="mono">{r.name}</span>,
          <span className="mono small">{r.columns.join(", ")}{r.include?.length ? ` INCLUDE (${r.include.join(", ")})` : ""}{r.predicate ? ` WHERE ${r.predicate}` : ""}</span>,
          r.primary ? "primary key" : r.unique ? "unique" : "", r.method ?? "",
          r.status && r.status !== "VALID" && r.status !== "N/A" ? <span className="badge badge-bad">{r.status}</span> : ""],
      };
    case "foreign_keys":
      return {
        head: ["Table", "Constraint", "Columns → references", "On delete", "Index on FK"],
        cells: (r) => [t(r, "relationships"), <span className="mono">{r.name}</span>,
          <span className="mono small">({r.columns.join(", ")}) → {r.references_id ? <TableLink conn={conn} id={r.references_id} label={r.references} /> : r.references} ({r.referenced_columns.join(", ")})</span>,
          r.on_delete ?? "—", yes(r.indexed)],
      };
    case "packages":
      return {
        head: ["Package", "Members", "Tables used", "Status"],
        cells: (r) => [<CodeLink conn={conn} id={r.code_id} label={`${r.schema}.${r.name}`} />, fmt(r.members), fmt(r.tables_used),
          r.status && r.status !== "VALID" ? <span className="badge badge-bad">{r.status}</span> : r.status ?? ""],
      };
    case "triggers":
      return {
        head: ["Trigger", "On table", "Fires", "Enabled", "Function"],
        cells: (r) => [<CodeLink conn={conn} id={r.code_id} label={`${r.schema}.${r.name}`} />,
          r.table ? <TableLink conn={conn} id={r.table_id} label={r.table} tab="triggers" /> : "—",
          <span className="small">{[r.timing, r.events, r.level && `FOR EACH ${r.level}`].filter(Boolean).join(" ")}</span>,
          r.enabled === false ? <span className="badge badge-warn">disabled</span> : "yes", <span className="mono small">{r.function ?? ""}</span>],
      };
    case "invalid_code":
      return {
        head: ["Name", "Kind", "Signature", "Status"],
        cells: (r) => [<CodeLink conn={conn} id={r.code_id} label={`${r.schema}.${r.name}`} />, r.kind,
          <span className="mono small">{r.signature}</span>, <span className="badge badge-bad">{r.status}</span>],
      };
    default:
      return {
        head: ["Name", "Where", "Signature", "Tables used", "Status"],
        cells: (r) => [<CodeLink conn={conn} id={r.code_id} label={`${r.schema}.${r.name}`} />,
          r.package ? <span className="small">package {r.package}</span> : <span className="muted small">standalone</span>,
          <span className="mono small">{r.signature}</span>,
          fmt(r.tables_used), r.status && r.status !== "VALID" ? <span className="badge badge-bad">{r.status}</span> : r.status ?? ""],
      };
  }
}

function Drill({ conn, area, params, setParams }: {
  conn: Connection; area: StatsArea; params: URLSearchParams; setParams: (p: URLSearchParams) => void;
}) {
  const snapshots = useQuery({ queryKey: ["catalog-snapshots", conn.id], queryFn: () => catalogApi.snapshots(conn.id) });
  const snapshot = snapshots.data?.find((s) => s.status === "succeeded");
  const stats = useQuery({ queryKey: ["snapshot-stats", snapshot?.id], queryFn: () => catalogApi.stats(snapshot!.id), enabled: !!snapshot, staleTime: Infinity });
  const schema = params.get("schema") ?? "";
  const filter = params.get("f") ?? "";
  const [q, setQ] = useState(params.get("q") ?? "");
  const [debounced, setDebounced] = useState(q);
  const [limit, setLimit] = useState(200);
  useEffect(() => { const t = setTimeout(() => setDebounced(q), 250); return () => clearTimeout(t); }, [q]);
  useEffect(() => setLimit(200), [area, schema, filter, debounced]);

  const query = new URLSearchParams({ limit: String(limit) });
  if (schema) query.set("schema", schema);
  if (filter) query.set("filter", filter);
  if (debounced) query.set("q", debounced);
  const items = useQuery({
    queryKey: ["snapshot-stats-items", snapshot?.id, area, query.toString()],
    queryFn: () => catalogApi.statsItems(snapshot!.id, area, query),
    enabled: !!snapshot, placeholderData: (prev) => prev,
  });
  const set = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    setParams(next);
  };
  const schemas = (stats.data?.by_schema ?? []).filter((s) => s[area] > 0).sort((a, b) => b[area] - a[area]);
  const cols = columnsFor(area, conn.id);
  const data = items.data;

  return (
    <section className="card table-card stats-drill">
      <div className="stats-drill-head">
        <h2>{AREA_LABEL[area]} <span className="muted">in {conn.name}</span></h2>
        <span className="muted small">{data ? `${data.total.toLocaleString()} ${data.total === 1 ? "item" : "items"}` : "Loading…"}</span>
        <button className="btn btn-sm btn-ghost" onClick={() => { const n = new URLSearchParams(); setParams(n); }}>Close ✕</button>
      </div>
      {schemas.length > 1 && (
        <div className="stats-schemas">
          <button className={`chip chip-btn ${!schema ? "active" : ""}`} onClick={() => set("schema", null)}>All schemas</button>
          {schemas.slice(0, 12).map((s) => (
            <button key={s.schema} className={`chip chip-btn ${schema === s.schema ? "active" : ""}`} onClick={() => set("schema", s.schema)}>
              {s.schema} ({s[area].toLocaleString()})
            </button>
          ))}
          {schemas.length > 12 && (
            <select value={schemas.slice(0, 12).some((s) => s.schema === schema) ? "" : schema} onChange={(e) => set("schema", e.target.value || null)} aria-label="Other schemas">
              <option value="">{schemas.length - 12} more schemas…</option>
              {schemas.slice(12).map((s) => <option key={s.schema} value={s.schema}>{s.schema} ({s[area]})</option>)}
            </select>
          )}
        </div>
      )}
      <div className="filters">
        {data && Object.keys(data.filters).length > 0 && (
          <div className="chips">
            <button className={`chip chip-btn ${!filter ? "active" : ""}`} onClick={() => set("f", null)}>All</button>
            {Object.entries(data.filters).map(([k, label]) => (
              <button key={k} className={`chip chip-btn ${filter === k ? "active" : ""}`} onClick={() => set("f", k)}>{label}</button>
            ))}
          </div>
        )}
        <input type="search" placeholder="Search by schema, table or name…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorBanner error={items.error} />
      {data && data.items.length === 0 ? (
        <EmptyState>Nothing matches.</EmptyState>
      ) : data ? (
        <div className="table-scroll stats-drill-list">
          <table className="compact">
            <thead><tr>{cols.head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
            <tbody>
              {data.items.map((r, i) => <tr key={i}>{cols.cells(r).map((c, j) => <td key={j}>{c}</td>)}</tr>)}
            </tbody>
          </table>
          {data.total > data.items.length && (
            <div className="pager">
              <span className="muted small">Showing {data.items.length.toLocaleString()} of {data.total.toLocaleString()}</span>
              <button className="btn btn-sm" onClick={() => setLimit(Math.min(limit + 500, 2000))} disabled={limit >= 2000}>
                {limit >= 2000 ? "Narrow the search to see more" : "Show more"}
              </button>
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}

export default function DbStatistics({ connections }: { connections: Connection[] }) {
  const [params, setParams] = useSearchParams();
  const db = params.get("db");
  const area = params.get("area") as StatsArea | null;
  const drillRef = useRef<HTMLDivElement>(null);
  const pick = (connId: string, a: StatsArea, filter?: string) => {
    const next = new URLSearchParams({ db: connId, area: a });
    if (filter) next.set("f", filter);
    setParams(next);
    setTimeout(() => drillRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
  };
  const conn = connections.find((c) => c.id === db);

  if (!connections.length) return null;
  return (
    <>
      <div className="stats-head">
        <h2>Database statistics</h2>
        <span className="muted small">From each connection's last catalog read. Click a number to list those items.</span>
      </div>
      <div className="stats-grid">
        {connections.map((c) => (
          <ConnectionStats key={c.id} conn={c} selected={c.id === db ? area : null} onPick={(a, f) => pick(c.id, a, f)} />
        ))}
      </div>
      <div ref={drillRef} />
      {conn && area && <Drill key={`${conn.id}-${area}`} conn={conn} area={area} params={params} setParams={setParams} />}
      <DifferenceReport connections={connections} />
    </>
  );
}
