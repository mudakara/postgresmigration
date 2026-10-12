import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  api,
  catalogApi,
  type CatalogTableSummary,
  type CompareOverview,
  type CompareSection,
  type Connection,
  type OverviewTable,
  type DiffRow,
  type DiffStatus,
  type TableComparison,
} from "../api";
import { JobProgress } from "../components/comparison";
import SourceViewer, { type LineMark } from "../components/SourceViewer";
import { EmptyState, ErrorBanner, KindTag, PageHeader, formatTime } from "../components/ui";

type Side = "left" | "right";
const OTHER: Record<Side, Side> = { left: "right", right: "left" };
const PARAM = { left: { conn: "l", table: "lt" }, right: { conn: "r", table: "rt" } } as const;

const STATUS_LABEL: Record<DiffStatus, string> = {
  same: "Same", equivalent: "Equivalent", info: "Info", warning: "Differs", error: "Problem",
};
const STATUS_ICON: Record<DiffStatus, string> = { same: "=", equivalent: "≈", info: "i", warning: "!", error: "✕" };

const tableKey = (t: { schema_name: string; name: string }) => `${t.schema_name}.${t.name}`;

/** The table on the other side with the same schema and name (ignoring case), else the
 * only table with that name in another schema. */
function findMatch(key: string, tables: CatalogTableSummary[]): { table: CatalogTableSummary; byNameOnly: boolean } | null {
  const [schema, ...rest] = key.split(".");
  const name = rest.join(".").toLowerCase();
  const exact = tables.find((t) => t.schema_name.toLowerCase() === schema.toLowerCase() && t.name.toLowerCase() === name);
  if (exact) return { table: exact, byNameOnly: false };
  const byName = tables.filter((t) => t.name.toLowerCase() === name);
  return byName.length === 1 ? { table: byName[0], byNameOnly: true } : null;
}

// --- table picker (searchable dropdown) ---------------------------------------------------

function TablePicker({ tables: all, value, onChange, disabled, only }: {
  tables: CatalogTableSummary[]; value: string | null; onChange: (key: string) => void; disabled?: boolean;
  only?: Map<string, OverviewTable> | null;  // restrict the list to these tables (and show their problem counts)
}) {
  const tables = useMemo(() => (only ? all.filter((t) => only.has(tableKey(t))) : all), [all, only]);
  const [text, setText] = useState(value ?? "");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const list = useRef<HTMLUListElement>(null);
  useEffect(() => setText(value ?? ""), [value]);

  const needle = text.trim().toLowerCase();
  const filtering = open && needle && needle !== (value ?? "").toLowerCase();
  const placeholder = value ?? `Search ${tables.length.toLocaleString()} tables (schema.table)…`;
  const matches = useMemo(
    () => (filtering ? tables.filter((t) => tableKey(t).toLowerCase().includes(needle)) : tables).slice(0, 300),
    [tables, needle, filtering],
  );
  useEffect(() => setActive(0), [needle]);
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const choose = (t: CatalogTableSummary) => {
    onChange(tableKey(t));
    setText(tableKey(t));
    setOpen(false);
  };

  return (
    <div className="picker">
      <input
        type="search" value={text} disabled={disabled} placeholder={placeholder}
        aria-label="Table" role="combobox" aria-expanded={open}
        onFocus={() => { setOpen(true); setText(""); }}  // start a fresh search; the current table stays as placeholder
        onBlur={() => setTimeout(() => { setOpen(false); setText(value ?? ""); }, 150)}
        onChange={(e) => { setText(e.target.value); setOpen(true); }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, matches.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
          else if (e.key === "Enter" && matches[active]) { e.preventDefault(); choose(matches[active]); }
          else if (e.key === "Escape") { setOpen(false); setText(value ?? ""); }
        }}
      />
      {open && (
        <ul className="picker-list" ref={list} role="listbox">
          {matches.length === 0 && <li className="muted small picker-empty">No table matches “{text}”</li>}
          {matches.map((t, i) => (
            <li key={t.id} data-i={i} role="option" aria-selected={i === active}
              className={`${i === active ? "active" : ""} ${tableKey(t) === value ? "chosen" : ""}`}
              onMouseDown={(e) => { e.preventDefault(); choose(t); }} onMouseEnter={() => setActive(i)}>
              <span className="mono"><span className="muted">{t.schema_name}.</span>{t.name}</span>
              <span className="picker-meta">
                {only?.get(tableKey(t)) && <ProblemCounts row={only.get(tableKey(t))!} />}
                <span className="muted small">{t.kind !== "table" ? t.kind : ""}</span>
              </span>
            </li>
          ))}
          {matches.length === 300 && <li className="muted small picker-empty">Showing the first 300: keep typing to narrow down</li>}
        </ul>
      )}
    </div>
  );
}

// --- one half: connection + table ---------------------------------------------------------

function SidePane({ side, connections, connId, tables, snapshot, value, notice, onConn, onTable, problems }: {
  side: Side; connections: Connection[]; connId: string; tables: CatalogTableSummary[] | undefined;
  snapshot: { id: string; finished_at: string | null } | null | undefined; value: string | null;
  notice: { tone: "error" | "warn"; text: string } | null;
  onConn: (id: string) => void; onTable: (key: string) => void;
  problems: Map<string, OverviewTable> | null;  // this side's tables with problems or differences
}) {
  const [onlyProblems, setOnlyProblems] = useState(false);
  const queryClient = useQueryClient();
  const conn = connections.find((c) => c.id === connId);
  const snapshots = useQuery({
    queryKey: ["catalog-snapshots", connId], queryFn: () => catalogApi.snapshots(connId), enabled: !!connId,
    refetchInterval: (q) => (q.state.data?.some((s) => s.status === "queued" || s.status === "running") ? 3000 : false),
  });
  const active = snapshots.data?.find((s) => s.status === "queued" || s.status === "running");
  const read = useMutation({
    mutationFn: () => catalogApi.start(connId, []),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["catalog-snapshots", connId] }),
  });
  return (
    <div className={`cmp-pane cmp-${side} ${conn ? `cmp-kind-${conn.kind}` : ""}`}>
      <div className="cmp-pane-head">
        <span className="cmp-side-label">{side === "left" ? "Left" : "Right"}</span>
        <select value={connId} onChange={(e) => onConn(e.target.value)} aria-label={`${side} connection`}>
          <option value="">Choose a connection…</option>
          {connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.kind === "oracle" ? "Oracle" : "PostgreSQL"})</option>)}
        </select>
        {conn && <KindTag kind={conn.kind} />}
      </div>
      {conn && (
        <div className="muted small">
          {conn.host}/{conn.database}
          {snapshot ? <> · catalog read {formatTime(snapshot.finished_at)} · <Link to={`/explorer/${connId}`}>explore</Link></> : null}
        </div>
      )}
      {connId && snapshots.isSuccess && !snapshot && !active && (
        <div className="banner banner-warn small">
          This connection's catalog hasn't been read yet.{" "}
          <button className="btn btn-sm" onClick={() => read.mutate()} disabled={read.isPending}>Read catalog</button>
        </div>
      )}
      {active?.job_id && (
        <JobProgress key={active.job_id} jobId={active.job_id} label="Reading the catalog"
          onDone={() => queryClient.invalidateQueries({ queryKey: ["catalog-snapshots", connId] })} />
      )}
      <ErrorBanner error={read.error} />
      {snapshot && tables && <TablePicker tables={tables} value={value} onChange={onTable} only={onlyProblems ? problems : null} />}
      {snapshot && tables && problems && (
        <label className="checkbox inline small">
          <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} />
          List only tables with problems or differences ({problems.size.toLocaleString()})
        </label>
      )}
      {snapshot && !tables && <div className="muted small">Loading tables…</div>}
      {notice && <div className={`banner small ${notice.tone === "error" ? "banner-error" : "banner-warn"}`}>{notice.text}</div>}
    </div>
  );
}

// --- comparison sections ------------------------------------------------------------------

interface SectionDef {
  key: CompareSection;
  title: string;
  headers: string[];
  cells: (item: any) => ReactNode[];
  expandable?: boolean;
}

const fmt = (n: number | null | undefined) => (n === null || n === undefined ? "—" : n.toLocaleString());
const list = (xs: string[] | undefined) => (xs ?? []).join(", ");

function triggerWhen(t: any) {
  const e = t.extra ?? {};
  return [e.timing, e.level && !String(e.timing ?? "").includes("EACH") ? `FOR EACH ${e.level}` : null, e.events].filter(Boolean).join(" ");
}

const SECTIONS: SectionDef[] = [
  {
    key: "table", title: "Table", headers: ["Value"],
    cells: (v) => [typeof v === "number" ? fmt(v) : v ?? "—"],
  },
  {
    key: "columns", title: "Columns and data types", headers: ["#", "Column", "Type", "Nullable"],
    cells: (c) => [c.position, <span className="mono">{c.name}</span>, <span className="mono">{c.type}</span>, c.nullable ? "yes" : <strong>NOT NULL</strong>],
  },
  {
    key: "keys", title: "Primary and unique keys", headers: ["Kind", "Name", "Columns"],
    cells: (k) => [k.kind, <span className="mono">{k.name}</span>, <span className="mono">{list(k.columns)}</span>],
  },
  {
    key: "indexes", title: "Indexes", headers: ["Index", "Columns", "Type"],
    cells: (ix) => [
      <span className="mono">{ix.name}</span>,
      <span className="mono">{list(ix.columns)}{ix.include?.length ? ` INCLUDE (${list(ix.include)})` : ""}{ix.predicate ? ` WHERE ${ix.predicate}` : ""}</span>,
      [ix.primary ? "primary" : ix.unique ? "unique" : "", ix.method].filter(Boolean).join(", "),
    ],
  },
  {
    key: "foreign_keys", title: "Foreign keys (this table references)", headers: ["Constraint", "Columns → references", "On delete"],
    cells: (r) => [<span className="mono">{r.name}</span>, <span className="mono">({list(r.columns)}) → {r.other_label} ({list(r.other_columns)})</span>, r.details?.on_delete ?? "—"],
  },
  {
    key: "referenced_by", title: "Foreign keys pointing at this table", headers: ["From table", "Their columns", "Constraint"],
    cells: (r) => [<span className="mono">{r.other_label}</span>, <span className="mono">{list(r.other_columns)}</span>, <span className="mono">{r.name}</span>],
  },
  {
    key: "triggers", title: "Triggers", headers: ["Trigger", "Fires", "Enabled"], expandable: true,
    cells: (t) => [<span className="mono">{t.name}</span>, triggerWhen(t), t.extra?.enabled === false ? "disabled" : "yes"],
  },
  {
    key: "code", title: "Procedures, functions and packages using this table", headers: ["Routine", "Kind", "Operations"], expandable: true,
    cells: (c) => [
      <span className="mono"><span className="muted">{c.schema}.</span>{c.name}</span>,
      c.package ? "package member" : c.kind,
      <span className="mono">{(c.operations ?? []).map((o: string) => o.toUpperCase()).join(", ") || "—"}</span>,
    ],
  },
  {
    key: "views", title: "Views reading this table", headers: ["View"],
    cells: (v) => [<span className="mono">{v.other_label}</span>],
  },
];

function Badge({ status }: { status: DiffStatus }) {
  return <span className={`diff-badge diff-${status}`} title={STATUS_LABEL[status]}>{STATUS_ICON[status]} {STATUS_LABEL[status]}</span>;
}

function codeId(item: any): string | null {
  return item?.code_id ?? item?.id ?? null;
}

function SourcePair({ row }: { row: DiffRow }) {
  return (
    <div className="cmp-sources">
      {(["left", "right"] as Side[]).map((side) => <SourceSide key={side} item={row[side]} />)}
    </div>
  );
}

function SourceSide({ item }: { item: any }) {
  const id = codeId(item);
  const code = useQuery({ queryKey: ["catalog-code", id], queryFn: () => catalogApi.code(id!), enabled: !!id });
  const marks = useMemo(() => {
    const m = new Map<number, LineMark>();
    for (const l of item?.lines ?? []) m.set(l, { severity: "medium", titles: ["Uses this table"] });
    return m;
  }, [item]);
  if (!item) return <div className="cmp-source-missing muted small">Not present on this side</div>;
  if (code.error) return <ErrorBanner error={code.error} />;
  if (!code.data) return <div className="muted small">Loading source…</div>;
  return code.data.source
    ? <SourceViewer source={code.data.source} marks={marks} maxHeight="420px" />
    : <div className="muted small">Source not visible to this account</div>;
}

function Section({ def, rows, names, onlyDiffs, counts }: {
  def: SectionDef; rows: DiffRow[]; names: [string, string]; onlyDiffs: boolean; counts: Record<DiffStatus, number>;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const diffs = counts.warning + counts.error + counts.info;
  const shown = rows.map((r, i) => ({ r, i })).filter(({ r }) => !onlyDiffs || !["same", "equivalent"].includes(r.status));
  const n = def.headers.length;
  const labelCol = def.key === "table";
  return (
    <details className="card table-card cmp-section" open={diffs > 0 || def.key === "columns"}>
      <summary className="cmp-section-head">
        <strong>{def.title}</strong>
        <span className="muted small">{rows.length} item(s)</span>
        {counts.error > 0 && <span className="diff-badge diff-error">✕ {counts.error}</span>}
        {counts.warning > 0 && <span className="diff-badge diff-warning">! {counts.warning}</span>}
        {counts.info > 0 && <span className="diff-badge diff-info">i {counts.info}</span>}
        {diffs === 0 && rows.length > 0 && <span className="diff-badge diff-same">all match</span>}
      </summary>
      {rows.length === 0 ? (
        <p className="muted small card-pad">Nothing on either side.</p>
      ) : shown.length === 0 ? (
        <p className="muted small card-pad">No differences.</p>
      ) : (
        <div className="table-scroll">
          <table className="compact cmp-table">
            <thead>
              <tr>
                {labelCol && <th rowSpan={2}>Property</th>}
                <th colSpan={n} className="cmp-half-head cmp-left">{names[0]}</th>
                <th rowSpan={2} className="cmp-status-col">Status</th>
                <th colSpan={n} className="cmp-half-head cmp-right">{names[1]}</th>
              </tr>
              <tr>
                {def.headers.map((h) => <th key={`l${h}`} className="cmp-left">{h}</th>)}
                {def.headers.map((h) => <th key={`r${h}`} className="cmp-right">{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {shown.map(({ r, i }) => {
                const expandable = def.expandable && (codeId(r.left) || codeId(r.right));
                return (
                  <Fragment key={i}>
                    <tr className={`diff-row diff-row-${r.status} ${expandable ? "clickable" : ""} ${open === i ? "selected" : ""}`}
                      onClick={expandable ? () => setOpen(open === i ? null : i) : undefined}>
                      {labelCol && <td className="small"><strong>{r.label}</strong></td>}
                      {r.left !== null && r.left !== undefined
                        ? def.cells(r.left).map((c, j) => <td key={`l${j}`} className="cmp-left">{c}</td>)
                        : <td colSpan={n} className="cmp-left cmp-absent">— missing —</td>}
                      <td className="cmp-status-col"><Badge status={r.status} /></td>
                      {r.right !== null && r.right !== undefined
                        ? def.cells(r.right).map((c, j) => <td key={`r${j}`} className="cmp-right">{c}</td>)
                        : <td colSpan={n} className="cmp-right cmp-absent">— missing —</td>}
                    </tr>
                    {r.message && (
                      <tr className={`diff-msg diff-row-${r.status}`}>
                        <td colSpan={n * 2 + 1 + (labelCol ? 1 : 0)} className="small">
                          {r.message}{expandable ? <span className="muted"> · click the row to see both sources</span> : null}
                        </td>
                      </tr>
                    )}
                    {open === i && (
                      <tr className="expanded"><td colSpan={n * 2 + 1 + (labelCol ? 1 : 0)}><SourcePair row={r} /></td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}

function Comparison({ data }: { data: TableComparison }) {
  const [onlyDiffs, setOnlyDiffs] = useState(false);
  const c = data.counts;
  return (
    <>
      <section className="card cmp-summary">
        <div className="cmp-summary-tables">
          <span className="mono"><strong>{data.left.table.schema_name}.{data.left.table.name}</strong></span>
          <span className="muted">vs</span>
          <span className="mono"><strong>{data.right.table.schema_name}.{data.right.table.name}</strong></span>
        </div>
        <div className="chips">
          <span className="diff-badge diff-error">✕ {c.error} problem(s)</span>
          <span className="diff-badge diff-warning">! {c.warning} difference(s)</span>
          <span className="diff-badge diff-info">i {c.info} note(s)</span>
          <span className="diff-badge diff-same">= {c.same + c.equivalent} matching</span>
        </div>
        <label className="checkbox inline small">
          <input type="checkbox" checked={onlyDiffs} onChange={(e) => setOnlyDiffs(e.target.checked)} />
          Show only differences
        </label>
        <p className="muted small cmp-legend">
          <Badge status="error" /> likely to break or lose data ·
          <Badge status="warning" /> different, may be intended ·
          <Badge status="info" /> worth knowing ·
          <Badge status="equivalent" /> same meaning (e.g. NUMBER(12) → bigint) ·
          <Badge status="same" /> identical.
          Compared from each connection's last catalog read; refresh a catalog to pick up recent changes.
        </p>
      </section>
      {SECTIONS.map((def) => (
        <Section key={def.key} def={def} rows={data.sections[def.key]} names={data.names}
          onlyDiffs={onlyDiffs} counts={data.section_counts[def.key]} />
      ))}
    </>
  );
}

// --- every table at once ----------------------------------------------------------------------

function ProblemCounts({ row }: { row: OverviewTable }) {
  if (row.missing) return <span className="diff-badge diff-error">missing</span>;
  return (
    <>
      {row.counts.error > 0 && <span className="diff-badge diff-error">✕ {row.counts.error}</span>}
      {row.counts.warning > 0 && <span className="diff-badge diff-warning">! {row.counts.warning}</span>}
    </>
  );
}

type OverviewFilter = "problems" | "differences" | "only_left" | "only_right" | "clean" | "all";
const AREA_LABEL: Record<string, string> = {
  table: "table", columns: "columns", keys: "keys", indexes: "indexes", foreign_keys: "foreign keys",
  referenced_by: "incoming FKs", triggers: "triggers", code: "routines", views: "views",
};

function matchesFilter(r: OverviewTable, f: OverviewFilter) {
  switch (f) {
    case "problems": return !!r.left && !!r.right && r.status === "error";
    case "differences": return r.status === "warning";
    case "only_left": return !!r.left && !r.right;
    case "only_right": return !r.left && !!r.right;
    case "clean": return r.status === "same" || r.status === "info";
    default: return true;
  }
}

function ProblemTables({ data, selected, onPick }: {
  data: CompareOverview; selected: { left: string | null; right: string | null };
  onPick: (row: OverviewTable) => void;
}) {
  const [filter, setFilter] = useState<OverviewFilter>("problems");
  const [area, setArea] = useState("");
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(200);
  const s = data.summary;
  const [L, R] = data.names;
  const needle = q.trim().toLowerCase();
  const rows = data.tables.filter((r) =>
    matchesFilter(r, filter)
    && (!area || (r.sections as Record<string, unknown>)[area])
    && (!needle || `${r.left?.key ?? ""} ${r.right?.key ?? ""}`.toLowerCase().includes(needle)));
  const chips: [OverviewFilter, string, number][] = [
    ["problems", "With problems", s.with_problems],
    ["differences", "With differences", s.with_differences],
    ["only_left", `Only in ${L}`, s.only_left],
    ["only_right", `Only in ${R}`, s.only_right],
    ["clean", "Matching", s.clean],
    ["all", "All", s.pairs],
  ];
  return (
    <details className="card table-card cmp-overview" open>
      <summary className="cmp-section-head">
        <strong>All tables</strong>
        <span className="muted small">{s.matched.toLocaleString()} matched pairs compared</span>
        <span className="diff-badge diff-error">✕ {s.with_problems} with problems</span>
        <span className="diff-badge diff-warning">! {s.with_differences} with differences</span>
        <span className="diff-badge diff-same">= {s.clean} matching</span>
      </summary>
      <div className="filters">
        <div className="chips">
          {chips.map(([key, label, n]) => (
            <button key={key} className={`chip chip-btn ${filter === key ? "active" : ""}`} onClick={() => { setFilter(key); setLimit(200); }}>
              {label} ({n.toLocaleString()})
            </button>
          ))}
        </div>
        <select value={area} onChange={(e) => setArea(e.target.value)} aria-label="Area" disabled={filter.startsWith("only") || filter === "clean"}>
          <option value="">Any area</option>
          {Object.entries(AREA_LABEL).map(([k, label]) => <option key={k} value={k}>Issues in {label}</option>)}
        </select>
        <input type="search" placeholder="Filter by schema or table…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {rows.length === 0 ? (
        <p className="muted small card-pad">No tables in this filter.</p>
      ) : (
        <div className="table-scroll cmp-overview-list">
          <table className="compact">
            <thead>
              <tr><th>{L}</th><th>{R}</th><th>Found</th><th>Where</th><th>First problem</th></tr>
            </thead>
            <tbody>
              {rows.slice(0, limit).map((r) => {
                const active = (r.left?.key ?? null) === selected.left && (r.right?.key ?? null) === selected.right;
                return (
                  <tr key={`${r.left?.id}-${r.right?.id}`} className={`clickable diff-row diff-row-${r.status} ${active ? "selected" : ""}`}
                    onClick={() => onPick(r)} title="Compare this table side by side">
                    <td className="mono">{r.left?.key ?? <span className="muted">—</span>}</td>
                    <td className="mono">{r.right?.key ?? <span className="muted">—</span>}</td>
                    <td className="cmp-found"><ProblemCounts row={r} /></td>
                    <td className="small">
                      {Object.entries(r.sections).map(([k, c]) => (
                        <span key={k} className={`cmp-area ${c?.error ? "cmp-area-error" : "cmp-area-warning"}`}>
                          {AREA_LABEL[k] ?? k} {(c?.error ?? 0) + (c?.warning ?? 0)}
                        </span>
                      ))}
                    </td>
                    <td className="small cmp-headline">{r.headline}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {rows.length > limit && (
            <div className="pager">
              <span className="muted small">Showing {limit} of {rows.length.toLocaleString()}</span>
              <button className="btn btn-sm" onClick={() => setLimit(limit + 300)}>Show more</button>
            </div>
          )}
        </div>
      )}
    </details>
  );
}

// --- page -------------------------------------------------------------------------------------

function useSide(connId: string) {
  const snapshots = useQuery({ queryKey: ["catalog-snapshots", connId], queryFn: () => catalogApi.snapshots(connId), enabled: !!connId });
  const snapshot = snapshots.data ? snapshots.data.find((s) => s.status === "succeeded") ?? null : undefined;
  const tables = useQuery({
    queryKey: ["catalog-tables", snapshot?.id, "all"],
    queryFn: () => catalogApi.tables(snapshot!.id, new URLSearchParams({ limit: "5000" })),
    enabled: !!snapshot,
    staleTime: 60_000,
  });
  return { snapshot, tables: tables.data?.items, error: snapshots.error ?? tables.error };
}

export default function TableComparePage() {
  const [params, setParams] = useSearchParams();
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const [notices, setNotices] = useState<Record<Side, { tone: "error" | "warn"; text: string } | null>>({ left: null, right: null });

  const conn = { left: params.get(PARAM.left.conn) ?? "", right: params.get(PARAM.right.conn) ?? "" };
  const chosen = { left: params.get(PARAM.left.table), right: params.get(PARAM.right.table) };
  const sides = { left: useSide(conn.left), right: useSide(conn.right) };

  // Default: Oracle on the left, PostgreSQL on the right.
  useEffect(() => {
    const list = connections.data;
    if (!list || conn.left || conn.right) return;
    const next = new URLSearchParams(params);
    const ora = list.find((c) => c.kind === "oracle");
    const pg = list.find((c) => c.kind === "postgres");
    const left = ora ?? list[0];
    const right = pg && pg.id !== left?.id ? pg : list.find((c) => c.id !== left?.id);
    if (left) next.set(PARAM.left.conn, left.id);
    if (right) next.set(PARAM.right.conn, right.id);
    setParams(next, { replace: true });
  }, [connections.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const nameOf = (side: Side) => connections.data?.find((c) => c.id === conn[side])?.name ?? "the other connection";

  /** Pick a table on one side and the matching table on the other. */
  const pickTable = (side: Side, key: string) => {
    const other = OTHER[side];
    const next = new URLSearchParams(params);
    next.set(PARAM[side].table, key);
    const otherTables = sides[other].tables;
    const fresh: Record<Side, { tone: "error" | "warn"; text: string } | null> = { left: null, right: null };
    if (otherTables) {
      const match = findMatch(key, otherTables);
      if (match) {
        next.set(PARAM[other].table, tableKey(match.table));
        if (match.byNameOnly) {
          fresh[other] = { tone: "warn", text: `No ${key} in ${nameOf(other)}; matched ${tableKey(match.table)}, the only table with that name. Check it is the right one.` };
        }
      } else {
        next.delete(PARAM[other].table);
        fresh[other] = { tone: "error", text: `No table ${key} in ${nameOf(other)} (searched ignoring upper/lower case). It may not have been migrated, or has another name: pick it manually.` };
      }
    }
    setNotices(fresh);
    setParams(next);
  };

  const pickConn = (side: Side, id: string) => {
    const next = new URLSearchParams(params);
    if (id) next.set(PARAM[side].conn, id);
    else next.delete(PARAM[side].conn);
    next.delete(PARAM[side].table);
    setNotices((n) => ({ ...n, [side]: null }));
    setParams(next);
  };

  // A table chosen before the other side's tables loaded: match it now.
  useEffect(() => {
    for (const side of ["left", "right"] as Side[]) {
      const other = OTHER[side];
      if (chosen[side] && !chosen[other] && sides[other].tables && !notices[other]) {
        pickTable(side, chosen[side]!);
        return;
      }
    }
  }, [sides.left.tables, sides.right.tables]); // eslint-disable-line react-hooks/exhaustive-deps

  const ids = {
    left: chosen.left ? sides.left.tables?.find((t) => tableKey(t) === chosen.left)?.id : undefined,
    right: chosen.right ? sides.right.tables?.find((t) => tableKey(t) === chosen.right)?.id : undefined,
  };
  const comparison = useQuery({
    queryKey: ["table-compare", ids.left, ids.right],
    queryFn: () => catalogApi.compare(ids.left!, ids.right!),
    enabled: !!ids.left && !!ids.right,
  });
  const snaps = { left: sides.left.snapshot?.id, right: sides.right.snapshot?.id };
  const overview = useQuery({
    queryKey: ["table-compare-overview", snaps.left, snaps.right],
    queryFn: () => catalogApi.compareOverview(snaps.left!, snaps.right!),
    enabled: !!snaps.left && !!snaps.right,
    staleTime: Infinity,  // snapshots never change; a refresh makes a new one
  });
  const problems = useMemo(() => {
    if (!overview.data) return { left: null, right: null };
    const out = { left: new Map<string, OverviewTable>(), right: new Map<string, OverviewTable>() };
    for (const r of overview.data.tables) {
      if ((r.status !== "error" && r.status !== "warning") || !r.left || !r.right) continue;  // matched pairs only
      if (r.left) out.left.set(r.left.key, r);
      if (r.right) out.right.set(r.right.key, r);
    }
    return out;
  }, [overview.data]);
  const resultRef = useRef<HTMLDivElement>(null);

  /** Choose a pair straight from the all-tables list (no name matching needed). */
  const pickPair = (row: OverviewTable) => {
    if (!row.left || !row.right) {
      const side: Side = row.left ? "left" : "right";
      pickTable(side, (row.left ?? row.right)!.key);
    } else {
      const next = new URLSearchParams(params);
      next.set(PARAM.left.table, row.left.key);
      next.set(PARAM.right.table, row.right.key);
      setNotices({ left: null, right: null });
      setParams(next);
    }
    setTimeout(() => resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 300);
  };

  return (
    <>
      <PageHeader
        title="Table compare"
        subtitle="Pick a table on either side; the same table is selected on the other. Columns, data types, keys, indexes, foreign keys, triggers and the code that uses it are compared side by side, with differences highlighted."
      />
      <ErrorBanner error={connections.error ?? sides.left.error ?? sides.right.error} />
      {connections.data && connections.data.length < 2 && (
        <EmptyState>Add at least two connections on the <Link to="/connections">Connections</Link> page.</EmptyState>
      )}
      {connections.data && (
        <div className="cmp-panes">
          {(["left", "right"] as Side[]).map((side) => (
            <SidePane key={side} side={side} connections={connections.data} connId={conn[side]}
              tables={sides[side].tables} snapshot={sides[side].snapshot} value={chosen[side]} notice={notices[side]}
              onConn={(id) => pickConn(side, id)} onTable={(key) => pickTable(side, key)} problems={problems[side]} />
          ))}
        </div>
      )}
      <ErrorBanner error={overview.error} />
      {overview.isFetching && !overview.data && <div className="card muted small">Comparing every table…</div>}
      {overview.data && <ProblemTables data={overview.data} selected={chosen} onPick={pickPair} />}
      <div ref={resultRef} />
      <ErrorBanner error={comparison.error} />
      {comparison.isFetching && !comparison.data && <div className="card muted">Comparing…</div>}
      {!ids.left || !ids.right ? (
        connections.data && connections.data.length >= 2 && <EmptyState>Choose a table on either side to compare.</EmptyState>
      ) : comparison.data ? (
        <Comparison data={comparison.data} />
      ) : null}
    </>
  );
}
