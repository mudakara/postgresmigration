import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  api,
  catalogApi,
  type CatalogIndex,
  type CatalogRelation,
  type CatalogSnapshot,
  type CatalogTableDetail,
  type CatalogTableSummary,
} from "../api";
import { JobProgress } from "../components/comparison";
import RelationshipDiagram from "../components/RelationshipDiagram";
import { EmptyState, ErrorBanner, KindTag, PageHeader, formatTime } from "../components/ui";
import { CodeDetail, CodeList } from "./explorer/CodeBrowser";
import PerformanceTab from "./explorer/PerformanceTab";
import { CodeKind, CodePanel, FoundBy, Operations, WRITES, setParam } from "./explorer/shared";

type OpenCode = (id: string) => void;

// --- table detail tabs --------------------------------------------------------------

function RelationTable({ rows, outgoing, onSelect }: { rows: CatalogRelation[]; outgoing: boolean; onSelect: (id: string) => void }) {
  return (
    <div className="table-scroll"><table className="compact">
      <thead>
        <tr>
          <th>{outgoing ? "References" : "Referenced by"}</th>
          <th>{outgoing ? "Columns here" : "Their columns"}</th>
          <th>{outgoing ? "Their columns" : "Columns here"}</th>
          <th>Constraint</th>
          <th>On delete</th>
          <th title="Whether an index starts with the foreign key columns (on the child table)">Index on FK</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td className="mono">{r.other_table_id ? <a href="#" onClick={(e) => { e.preventDefault(); onSelect(r.other_table_id!); }}>{r.other_label}</a> : <span title="Not in this snapshot">{r.other_label}</span>}</td>
            <td className="mono small">{(outgoing ? r.columns : r.other_columns).join(", ")}</td>
            <td className="mono small">{(outgoing ? r.other_columns : r.columns).join(", ")}</td>
            <td className="mono small muted">{r.name}</td>
            <td className="small">{r.details.on_delete ?? "—"}</td>
            <td className="small"><FkIndex indexed={r.details.indexed} /></td>
          </tr>
        ))}
      </tbody>
    </table></div>
  );
}

function FkIndex({ indexed }: { indexed: boolean | undefined }) {
  if (indexed === undefined) return <span className="muted" title="Refresh the catalog to check">—</span>;
  return indexed ? <span className="sev-ok">✓ indexed</span> : (
    <span className="warn-text" title="No index starts with these columns: deleting or updating a parent row scans the whole child table (and Oracle locks it)">
      ⚠ missing
    </span>
  );
}

function RelationshipsTab({ t, onSelect }: { t: CatalogTableDetail; onSelect: (id: string) => void }) {
  const views = [...t.read_by_views, ...t.reads_from];
  return (
    <div className="tab-body">
      <RelationshipDiagram tableId={t.id} label={`${t.schema_name}.${t.name}`} references={t.references} referencedBy={t.referenced_by} onSelect={onSelect} />
      <p className="muted small">Arrows point from the child table to the table it references. Hover a box for the foreign key columns; click it to open that table.</p>
      {t.references.length > 0 && <RelationTable rows={t.references} outgoing onSelect={onSelect} />}
      {t.referenced_by.length > 0 && <RelationTable rows={t.referenced_by} outgoing={false} onSelect={onSelect} />}
      {views.length > 0 && (
        <div className="view-links">
          {t.reads_from.length > 0 && (
            <div className="line-chips small">
              <span className="muted">This view reads:</span>
              {t.reads_from.map((r) => <TableChip key={r.id} r={r} onSelect={onSelect} />)}
            </div>
          )}
          {t.read_by_views.length > 0 && (
            <div className="line-chips small">
              <span className="muted">Views that read this {t.kind.includes("view") ? "view" : "table"}:</span>
              {t.read_by_views.map((r) => <TableChip key={r.id} r={r} onSelect={onSelect} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TableChip({ r, onSelect }: { r: CatalogRelation; onSelect: (id: string) => void }) {
  return r.other_table_id ? (
    <button className="chip chip-btn" onClick={() => onSelect(r.other_table_id!)}>{r.other_label}</button>
  ) : (
    <span className="chip mono" title="Not in this snapshot">{r.other_label}</span>
  );
}

/** Index key text -> column name ("Mixed" -> Mixed, STATUS DESC -> STATUS). */
function plainColumn(key: string) {
  const text = key.trim().replace(/\s+DESC$/i, "");
  return text.length > 1 && text.startsWith('"') && text.endsWith('"') ? text.slice(1, -1).replace(/""/g, '"') : text;
}

/** Whether an index expression such as lower(name) refers to column `name`. */
function mentions(expression: string, column: string) {
  const escaped = column.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^\\w$#])"?${escaped}"?([^\\w$#]|$)`, "i").test(expression);
}

/** For each column, the indexes it is part of: as key column n of m, INCLUDE column or inside an expression. */
function indexesByColumn(t: CatalogTableDetail) {
  const out = new Map<string, { index: CatalogIndex; role: string }[]>();
  const add = (column: string, index: CatalogIndex, role: string) => out.set(column, [...(out.get(column) ?? []), { index, role }]);
  const names = t.columns.map((c) => c.name);
  for (const ix of t.indexes) {
    ix.columns.forEach((key, i) => {
      const plain = plainColumn(key);
      if (names.includes(plain)) add(plain, ix, `${i + 1}/${ix.columns.length}`);
      else for (const n of names) if (mentions(key, n)) add(n, ix, "expression");
    });
    for (const key of ix.include ?? []) {
      const plain = plainColumn(key);
      if (names.includes(plain)) add(plain, ix, "include");
    }
  }
  return out;
}

function ColumnsTab({ t }: { t: CatalogTableDetail }) {
  const [onlyIndexed, setOnlyIndexed] = useState(false);
  const indexed = indexesByColumn(t);
  const pk = new Set(t.primary_key?.columns ?? []);
  const fk = new Map<string, string>();
  for (const r of t.references) r.columns.forEach((c, i) => fk.set(c, `${r.other_label}.${r.other_columns[i] ?? "?"}`));
  const uq = new Set(t.unique_keys.flatMap((u) => u.columns));
  const hasDefaults = t.columns.some((c) => c.default);
  const hasComments = t.columns.some((c) => c.comment);
  const shown = t.columns.map((c, i) => ({ c, i })).filter(({ c }) => !onlyIndexed || indexed.has(c.name));
  return (
    <div className="tab-body">
    <label className="checkbox inline small">
      <input type="checkbox" checked={onlyIndexed} onChange={(e) => setOnlyIndexed(e.target.checked)} />
      Only indexed columns ({t.columns.filter((c) => indexed.has(c.name)).length} of {t.columns.length})
    </label>
    <div className="table-scroll"><table className="compact">
      <thead>
        <tr>
          <th className="num">#</th><th>Column</th><th>Type</th><th>Null</th><th>Indexed in</th>
          {hasDefaults && <th>Default</th>}
          {hasComments && <th>Comment</th>}
        </tr>
      </thead>
      <tbody>
        {shown.map(({ c, i }) => (
          <tr key={c.name}>
            <td className="num muted small">{i + 1}</td>
            <td>
              <span className="mono">{c.name}</span>{" "}
              {pk.has(c.name) && <span className="key key-pk" title={t.primary_key?.name}>PK</span>}
              {fk.has(c.name) && <span className="key key-fk" title={`References ${fk.get(c.name)}`}>FK</span>}
              {uq.has(c.name) && <span className="key" title="Part of a unique key">UQ</span>}
            </td>
            <td className="mono small">{c.type}</td>
            <td className="small">{c.nullable ? "yes" : <strong>NOT NULL</strong>}</td>
            <td className="small">
              {(indexed.get(c.name) ?? []).map(({ index, role }) => (
                <div key={index.name + role} className="mono" title={index.definition ?? index.columns.join(", ")}>
                  {index.name} <span className="muted">({role === "include" ? "INCLUDE" : role === "expression" ? "in expression" : `col ${role}`}{index.unique ? ", unique" : ""})</span>
                </div>
              ))}
              {!indexed.has(c.name) && <span className="muted">—</span>}
            </td>
            {hasDefaults && <td className="mono small truncate" title={c.default ?? ""}>{c.default ?? ""}</td>}
            {hasComments && <td className="small">{c.comment ?? ""}</td>}
          </tr>
        ))}
      </tbody>
    </table></div>
    </div>
  );
}

function IndexesTab({ t, onSelect }: { t: CatalogTableDetail; onSelect: (id: string) => void }) {
  const unindexed = t.references.filter((r) => r.details.indexed === false);
  return (
    <div className="tab-body">
      {unindexed.length > 0 && (
        <div className="banner banner-warn small">
          <strong>{unindexed.length} foreign key(s) without a supporting index.</strong> Deleting or updating a row in the
          parent table then scans this whole table (and Oracle takes a table lock on it). Consider an index on:
          <ul className="dep-list mono">
            {unindexed.map((r) => (
              <li key={r.id}>
                ({r.columns.join(", ")}) → {r.other_table_id
                  ? <a href="#" onClick={(e) => { e.preventDefault(); onSelect(r.other_table_id!); }}>{r.other_label}</a>
                  : r.other_label} <span className="muted">{r.name}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {t.indexes.length === 0 ? (
        <EmptyState>{t.kind.includes("view") && t.kind !== "materialized view" ? "Views have no indexes." : "This table has no indexes."}</EmptyState>
      ) : (
        <div className="table-scroll"><table className="compact">
          <thead><tr><th>Index</th><th>Columns (in key order)</th><th>Type</th><th>Notes</th></tr></thead>
          <tbody>
            {t.indexes.map((ix) => (
              <tr key={ix.name}>
                <td className="mono">
                  {ix.name}{" "}
                  {ix.primary ? <span className="key key-pk">PK</span> : ix.unique && <span className="key">UNIQUE</span>}
                </td>
                <td className="mono small">
                  {ix.columns.join(", ")}
                  {ix.include?.length > 0 && <div className="muted">INCLUDE ({ix.include.join(", ")})</div>}
                </td>
                <td className="small">{ix.method}{ix.partitioned ? ", partitioned" : ""}</td>
                <td className="small">
                  {ix.predicate && <div title="Partial index: only rows matching this condition are indexed">WHERE <span className="mono">{ix.predicate}</span></div>}
                  {ix.status && ix.status !== "VALID" && <span className="badge badge-bad">{ix.status}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </div>
  );
}

type CodeFilter = "all" | "writes" | "reads" | "dynamic";

function CodeTab({ t, onSelect, onOpenCode }: { t: CatalogTableDetail; onSelect: (id: string) => void; onOpenCode: OpenCode }) {
  const [open, setOpen] = useState<string | null>(null);
  const [filter, setFilter] = useState<CodeFilter>("all");
  const rows = t.used_by.filter((u) =>
    filter === "writes" ? u.operations.some((o) => WRITES.has(o))
      : filter === "reads" ? !u.operations.some((o) => WRITES.has(o))
        : filter === "dynamic" ? u.found_by.includes("dynamic_sql") : true,
  );
  if (!t.used_by.length) return <EmptyState>No procedure, function, package or trigger uses this {t.kind}.</EmptyState>;
  const count = (f: CodeFilter) => t.used_by.filter((u) =>
    f === "writes" ? u.operations.some((o) => WRITES.has(o)) : f === "reads" ? !u.operations.some((o) => WRITES.has(o)) : f === "dynamic" ? u.found_by.includes("dynamic_sql") : true).length;
  return (
    <div className="tab-body">
      <div className="chips">
        {(["all", "writes", "reads", "dynamic"] as CodeFilter[]).map((f) => (
          <button key={f} className={`chip chip-btn ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
            {{ all: "All", writes: "Writes to it", reads: "Only reads it", dynamic: "Via dynamic SQL" }[f]} ({count(f)})
          </button>
        ))}
      </div>
      <div className="table-scroll"><table className="compact">
        <thead>
          <tr><th>Code</th><th>Kind</th><th>How it uses this {t.kind.includes("view") ? "view" : "table"}</th><th>Found by</th></tr>
        </thead>
        <tbody>
          {rows.map((u) => (
            <Fragment key={u.code.id}>
              <tr className={`clickable ${open === u.code.id ? "selected" : ""}`} onClick={() => setOpen(open === u.code.id ? null : u.code.id)}>
                <td>
                  <span className="mono"><span className="muted">{u.code.schema_name}.</span>{u.code.name}</span>
                  {u.subprograms.length > 0 && (
                    <div className="muted small">in {u.subprograms.join(", ")}</div>
                  )}
                </td>
                <td><CodeKind code={u.code} /></td>
                <td><Operations ops={u.operations} /></td>
                <td><FoundBy usage={u} /></td>
              </tr>
              {open === u.code.id && (
                <tr className="expanded">
                  <td colSpan={4}><CodePanel codeId={u.code.id} tableId={t.id} onSelectTable={onSelect} onOpenCode={onOpenCode} /></td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table></div>
    </div>
  );
}

function TriggersTab({ t, onSelect, onOpenCode }: { t: CatalogTableDetail; onSelect: (id: string) => void; onOpenCode: OpenCode }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!t.triggers.length) return <EmptyState>No triggers fire on this {t.kind}.</EmptyState>;
  return (
    <div className="table-scroll"><table className="compact">
      <thead><tr><th>Trigger</th><th>Fires</th><th>Enabled</th><th>Runs</th></tr></thead>
      <tbody>
        {t.triggers.map((tr) => (
          <Fragment key={tr.id}>
            <tr className={`clickable ${open === tr.id ? "selected" : ""}`} onClick={() => setOpen(open === tr.id ? null : tr.id)}>
              <td className="mono">{tr.name}{tr.extra.partition && <div className="muted small">on partition {tr.extra.partition}</div>}</td>
              <td className="small">{[tr.extra.timing, tr.extra.events, tr.extra.level && `FOR EACH ${tr.extra.level}`].filter(Boolean).join(" ")}</td>
              <td>{tr.extra.enabled === false ? <span className="badge badge-warn">disabled</span> : <span className="badge badge-ok">enabled</span>}</td>
              <td className="mono small">{tr.extra.function ?? (tr.status && tr.status !== "VALID" ? <span className="badge badge-bad">{tr.status}</span> : "trigger body")}</td>
            </tr>
            {open === tr.id && (
              <tr className="expanded"><td colSpan={4}><CodePanel codeId={tr.id} tableId={t.id} onSelectTable={onSelect} onOpenCode={onOpenCode} /></td></tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table></div>
  );
}

type Tab = "relationships" | "columns" | "indexes" | "code" | "triggers" | "performance";

function TableDetail({ tableId, tab, onTab, onSelect, onOpenCode }: {
  tableId: string; tab: Tab; onTab: (t: Tab) => void; onSelect: (id: string) => void; onOpenCode: OpenCode;
}) {
  const table = useQuery({ queryKey: ["catalog-table", tableId], queryFn: () => catalogApi.table(tableId) });
  if (table.error) return <ErrorBanner error={table.error} />;
  if (!table.data) return <div className="card muted">Loading…</div>;
  const t = table.data;
  const tabs: [Tab, string][] = [
    ["relationships", `Relationships (${t.references.length + t.referenced_by.length + t.reads_from.length + t.read_by_views.length})`],
    ["columns", `Columns (${t.columns.length})`],
    ["indexes", `Indexes (${t.indexes.length})${t.unindexed_fk_count ? " ⚠" : ""}`],
    ["code", `Procedures & functions (${t.used_by.length})`],
    ["triggers", `Triggers (${t.triggers.length})`],
    ["performance", "Performance review (AI)"],
  ];
  return (
    <section className="card explorer-detail">
      <div className="explorer-title">
        <h2 className="mono"><span className="muted">{t.schema_name}.</span>{t.name}</h2>
        <span className={`tag tag-code`}>{t.kind}</span>
      </div>
      <div className="object-meta small muted">
        {t.row_estimate !== null && <span title="From optimizer statistics; may be out of date">≈ {t.row_estimate.toLocaleString()} rows</span>}
        {t.partitions > 0 && <span>{t.partitions} partitions</span>}
        {t.primary_key ? <span>PK ({t.primary_key.columns.join(", ")})</span> : !t.kind.includes("view") && <span className="warn-text">no primary key</span>}
        {!t.kind.includes("view") && <span>{t.indexes.length} index{t.indexes.length === 1 ? "" : "es"}</span>}
        {t.unindexed_fk_count > 0 && (
          <a href="#" className="warn-text" onClick={(e) => { e.preventDefault(); onTab("indexes"); }}>
            ⚠ {t.unindexed_fk_count} foreign key(s) without an index
          </a>
        )}
      </div>
      {t.comment && <p className="small">{t.comment}</p>}
      <div className="tabs">
        {tabs.map(([key, label]) => (
          <button key={key} className={`tab ${tab === key ? "active" : ""}`} onClick={() => onTab(key)}>{label}</button>
        ))}
      </div>
      {tab === "relationships" && <RelationshipsTab t={t} onSelect={onSelect} />}
      {tab === "columns" && <ColumnsTab key={t.id} t={t} />}
      {tab === "indexes" && <IndexesTab t={t} onSelect={onSelect} />}
      {tab === "code" && <CodeTab key={t.id} t={t} onSelect={onSelect} onOpenCode={onOpenCode} />}
      {tab === "triggers" && <TriggersTab key={t.id} t={t} onSelect={onSelect} onOpenCode={onOpenCode} />}
      {tab === "performance" && <PerformanceTab key={t.id} t={t} onOpenCode={onOpenCode} />}
    </section>
  );
}

// --- table list -----------------------------------------------------------------------

function TableList({ snapshot, params, setParams }: { snapshot: CatalogSnapshot; params: URLSearchParams; setParams: (p: URLSearchParams) => void }) {
  const schema = params.get("schema") ?? "";
  const kind = params.get("kind") ?? "";
  const linked = params.get("linked") === "1";
  const indexFilter = params.get("indexes") ?? "";
  const selected = params.get("table");
  const [q, setQ] = useState(params.get("q") ?? "");
  const [debounced, setDebounced] = useState(q);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(q), 250);
    return () => clearTimeout(timer);
  }, [q]);

  const query = new URLSearchParams();
  if (schema) query.set("schema", schema);
  if (kind) query.set("kind", kind);
  if (debounced) query.set("q", debounced);
  if (linked) query.set("linked", "true");
  if (indexFilter) query.set("indexes", indexFilter);
  const tables = useQuery({
    queryKey: ["catalog-tables", snapshot.id, query.toString()],
    queryFn: () => catalogApi.tables(snapshot.id, query),
    placeholderData: (prev) => prev,
  });
  const schemas = (snapshot.summary.schemas ?? []).filter((s) => (s.tables ?? 0) + (s.views ?? 0) > 0);

  return (
    <>
      <div className="explorer-filters">
        <input type="search" placeholder="Search tables…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search tables" />
        <select value={schema} onChange={(e) => setParams(setParam(params, "schema", e.target.value || null))} aria-label="Schema">
          <option value="">All schemas ({schemas.length})</option>
          {schemas.map((s) => (
            <option key={s.schema} value={s.schema}>{s.schema} ({(s.tables ?? 0) + (s.views ?? 0)})</option>
          ))}
        </select>
        <select value={kind} onChange={(e) => setParams(setParam(params, "kind", e.target.value || null))} aria-label="Kind">
          <option value="">Tables & views</option>
          <option value="table">Tables</option>
          <option value="view">Views</option>
        </select>
        <select value={indexFilter} onChange={(e) => setParams(setParam(params, "indexes", e.target.value || null))} aria-label="Indexes">
          <option value="">Any indexes</option>
          <option value="unindexed_fk">Has a foreign key without an index</option>
          <option value="none">Has no index at all</option>
        </select>
        <label className="checkbox inline small">
          <input type="checkbox" checked={linked} onChange={(e) => setParams(setParam(params, "linked", e.target.checked ? "1" : null))} />
          Only with relationships or code
        </label>
      </div>
      <div className="explorer-count muted small">
        {tables.data ? `${tables.data.total.toLocaleString()} ${tables.data.total === 1 ? "match" : "matches"}${tables.data.total > tables.data.items.length ? `, showing ${tables.data.items.length}; refine the search` : ""}` : "Loading…"}
      </div>
      <ErrorBanner error={tables.error} />
      <div className="explorer-items">
        {tables.data?.items.map((t: CatalogTableSummary) => (
          <button
            key={t.id}
            className={`explorer-item ${selected === t.id ? "active" : ""}`}
            onClick={() => setParams(setParam(params, "table", t.id))}
          >
            <span className="mono explorer-name">
              {!schema && <span className="muted">{t.schema_name}.</span>}{t.name}
            </span>
            <span className="explorer-counts">
              {t.kind !== "table" && <span className="muted">{t.kind.replace("materialized view", "mview").replace("partitioned table", "partitioned").replace("temporary table", "temp")}</span>}
              {t.references_count > 0 && <span title={`References ${t.references_count} table(s)`}>→{t.references_count}</span>}
              {t.referenced_by_count > 0 && <span title={`Referenced by ${t.referenced_by_count} foreign key(s)`}>←{t.referenced_by_count}</span>}
              {t.code_count > 0 && <span title={`Used by ${t.code_count} procedure(s)/function(s)`}>ƒ{t.code_count}</span>}
              {t.trigger_count > 0 && <span title={`${t.trigger_count} trigger(s)`}>⚡{t.trigger_count}</span>}
              {t.unindexed_fk_count > 0 && <span className="warn-text" title={`${t.unindexed_fk_count} foreign key(s) without an index`}>⚠</span>}
            </span>
          </button>
        ))}
      </div>
    </>
  );
}

type Mode = "tables" | "code";

function ListPane({ snapshot, mode, params, setParams }: {
  snapshot: CatalogSnapshot; mode: Mode; params: URLSearchParams; setParams: (p: URLSearchParams) => void;
}) {
  const s = snapshot.summary;
  const code = Object.values(s.code ?? {}).reduce((a, b) => a + b, 0);
  return (
    <aside className="card explorer-list">
      <div className="tabs explorer-modes">
        <button className={`tab ${mode === "tables" ? "active" : ""}`} onClick={() => setParams(setParam(params, "mode", null))}>
          Tables & views ({((s.tables ?? 0) + (s.views ?? 0)).toLocaleString()})
        </button>
        <button className={`tab ${mode === "code" ? "active" : ""}`} onClick={() => setParams(setParam(params, "mode", "code"))}>
          Code ({code.toLocaleString()})
        </button>
      </div>
      {mode === "tables"
        ? <TableList snapshot={snapshot} params={params} setParams={setParams} />
        : <CodeList snapshot={snapshot} params={params} setParams={setParams} />}
    </aside>
  );
}

// --- page -------------------------------------------------------------------------------

function SnapshotBar({ connectionId, current, active, failed }: {
  connectionId: string; current?: CatalogSnapshot; active?: CatalogSnapshot; failed?: CatalogSnapshot;
}) {
  const queryClient = useQueryClient();
  const [schemas, setSchemas] = useState((current?.requested_schemas ?? []).join(", "));
  useEffect(() => setSchemas((current?.requested_schemas ?? []).join(", ")), [current?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const start = useMutation({
    mutationFn: () => catalogApi.start(connectionId, schemas.split(/[\s,]+/).filter(Boolean)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["catalog-snapshots", connectionId] }),
  });
  const s = current?.summary;
  const code = Object.values(s?.code ?? {}).reduce((a, b) => a + b, 0);
  return (
    <section className="card explorer-bar">
      <div className="explorer-bar-row">
        <label className="explorer-schemas small">
          Schemas to read
          <input value={schemas} onChange={(e) => setSchemas(e.target.value)} placeholder="All schemas (or e.g. sales, billing)" disabled={!!active} />
        </label>
        <button className="btn btn-primary" onClick={() => start.mutate()} disabled={!!active || start.isPending}>
          {active ? "Reading…" : current ? "Refresh catalog" : "Read catalog"}
        </button>
      </div>
      <ErrorBanner error={start.error} />
      {active?.job_id && (
        <JobProgress key={active.job_id} jobId={active.job_id} label="Reading the catalog"
          onDone={() => queryClient.invalidateQueries({ queryKey: ["catalog-snapshots", connectionId] })} />
      )}
      {failed && !active && <div className="banner banner-error">Last read failed: {failed.error} {failed.job_id && <Link to={`/jobs/${failed.job_id}`}>Log</Link>}</div>}
      {current && s && (
        <div className="explorer-stats small">
          <span><strong>{s.tables?.toLocaleString()}</strong> tables</span>
          <span><strong>{s.views?.toLocaleString()}</strong> views</span>
          <span><strong>{s.foreign_keys?.toLocaleString()}</strong> foreign keys</span>
          {s.indexes !== undefined ? (
            <span><strong>{s.indexes.toLocaleString()}</strong> indexes{s.unindexed_foreign_keys ? <> · <span className="warn-text">{s.unindexed_foreign_keys} foreign keys without an index</span></> : null}</span>
          ) : (
            <span className="warn-text">Indexes not read yet: refresh the catalog</span>
          )}
          <span title={Object.entries(s.code ?? {}).map(([k, v]) => `${v} ${k}`).join(", ")}><strong>{code.toLocaleString()}</strong> procedures, functions, packages & triggers</span>
          <span className="muted">in {current.schemas.length} schema(s) · read {formatTime(current.finished_at)}{current.dictionary ? ` · ${current.dictionary}_* views` : ""}</span>
        </div>
      )}
      {current && current.warnings.length > 0 && (
        <div className="banner banner-warn small">{current.warnings.map((w) => <div key={w}>{w}</div>)}</div>
      )}
    </section>
  );
}

export default function ExplorerPage() {
  const { connectionId = "" } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const snapshots = useQuery({
    queryKey: ["catalog-snapshots", connectionId],
    queryFn: () => catalogApi.snapshots(connectionId),
    enabled: !!connectionId,
    refetchInterval: (q) => (q.state.data?.some((s) => s.status === "queued" || s.status === "running") ? 3000 : false),
  });

  useEffect(() => {
    if (!connectionId && connections.data?.length === 1) navigate(`/explorer/${connections.data[0].id}`, { replace: true });
  }, [connectionId, connections.data, navigate]);

  const list = snapshots.data ?? [];
  const current = list.find((s) => s.status === "succeeded");
  const active = list.find((s) => s.status === "queued" || s.status === "running");
  const failed = list[0]?.status === "failed" ? list[0] : undefined;
  const conn = connections.data?.find((c) => c.id === connectionId);
  const tableId = params.get("table");
  const codeId = params.get("code");
  const mode: Mode = params.get("mode") === "code" ? "code" : "tables";
  const tab = (params.get("tab") as Tab) || "relationships";
  const selectTable = (id: string) => setParams(setParam(setParam(params, "mode", null), "table", id));
  const openCode = (id: string) => setParams(setParam(setParam(params, "mode", "code"), "code", id));

  // A refresh replaces the snapshot, and with it every table id.
  const shownSnapshot = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (shownSnapshot.current && current && shownSnapshot.current !== current.id && (tableId || codeId)) {
      setParams(setParam(setParam(params, "table", null), "code", null), { replace: true });
    }
    shownSnapshot.current = current?.id;
  }, [current?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <PageHeader
        title="Schema explorer"
        subtitle="Tables, their relationships, and the procedures, functions and triggers that use them. Read-only."
        actions={
          <select
            className="explorer-conn"
            value={connectionId}
            onChange={(e) => navigate(e.target.value ? `/explorer/${e.target.value}` : "/explorer")}
            aria-label="Connection"
          >
            <option value="">Choose a connection…</option>
            {connections.data?.map((c) => (
              <option key={c.id} value={c.id}>{c.name} ({c.kind === "oracle" ? "Oracle" : "PostgreSQL"})</option>
            ))}
          </select>
        }
      />
      <ErrorBanner error={connections.error ?? snapshots.error} />
      {!connectionId && (
        <EmptyState>
          {connections.data?.length === 0
            ? <>Add a connection on the <Link to="/connections">Connections</Link> page first.</>
            : "Choose a connection to explore its tables."}
        </EmptyState>
      )}
      {connectionId && conn && (
        <>
          <div className="explorer-conn-line small muted">
            <KindTag kind={conn.kind} /> {conn.host}:{conn.port}/{conn.database} as {conn.username}
          </div>
          <SnapshotBar key={connectionId} connectionId={connectionId} current={current} active={active} failed={failed} />
          {snapshots.isSuccess && !current && !active && !failed && (
            <EmptyState>This connection has not been read yet. Choose schemas (or leave empty for all) and click <strong>Read catalog</strong>.</EmptyState>
          )}
          {current && (
            <div className="explorer-layout">
              <ListPane key={current.id} snapshot={current} mode={mode} params={params} setParams={setParams} />
              {mode === "code" ? (
                codeId ? (
                  <CodeDetail codeId={codeId} search={params.get("src") === "1" ? params.get("cq") ?? "" : ""} onSelectTable={selectTable} />
                ) : (
                  <EmptyState>Search or filter procedures, functions, packages and triggers, then select one to see its source and the tables it uses.</EmptyState>
                )
              ) : tableId ? (
                <TableDetail tableId={tableId} tab={tab} onTab={(t) => setParams(setParam(params, "tab", t === "relationships" ? null : t))} onSelect={selectTable} onOpenCode={openCode} />
              ) : (
                <EmptyState>Select a table to see its relationships, columns, indexes, code and triggers.</EmptyState>
              )}
            </div>
          )}
        </>
      )}
    </>
  );
}
