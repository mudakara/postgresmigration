import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { catalogApi, type Connection, type InventoryArea, type InventoryDiff } from "../../api";
import { EmptyState, ErrorBanner, KindTag } from "../../components/ui";

const AREAS: [InventoryArea, string][] = [
  ["tables", "Tables"], ["views", "Views"], ["columns", "Columns"], ["indexes", "Indexes"],
  ["foreign_keys", "Foreign keys"], ["procedures", "Procedures"], ["functions", "Functions"], ["triggers", "Triggers"],
];
type ListKind = "only_left" | "only_right" | "kind_changed";

const fmt = (n: number) => n.toLocaleString();

function useSnapshot(connId: string) {
  const snaps = useQuery({ queryKey: ["catalog-snapshots", connId], queryFn: () => catalogApi.snapshots(connId), enabled: !!connId });
  return snaps.data?.find((s) => s.status === "succeeded");
}

function ItemLink({ item, connId }: { item: Record<string, any>; connId: string }) {
  if (item.code_id) return <Link className="mono" to={`/explorer/${connId}?mode=code&code=${item.code_id}`}>{item.name}</Link>;
  if (item.table_id) return <Link className="mono" to={`/explorer/${connId}?table=${item.table_id}`}>{item.name}</Link>;
  return <span className="mono">{item.name}</span>;
}

function detail(area: InventoryArea, item: Record<string, any>) {
  switch (area) {
    case "tables": case "views": return item.kind;
    case "columns": return <span className="mono small">{item.type}</span>;
    case "indexes": return <span className="mono small">({item.columns}){item.unique ? " unique" : ""}</span>;
    case "foreign_keys": return <span className="mono small">{item.columns}</span>;
    case "triggers": return <span className="small">on {item.table}</span>;
    default: return <span className="small">{item.package ? `${item.kind} in package ${item.package}` : item.kind}</span>;
  }
}

function csv(rows: (string | number)[][]) {
  return rows.map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
}

function ItemList({ data, area, list, onClose }: { data: InventoryDiff; area: InventoryArea; list: ListKind; onClose: () => void }) {
  const [q, setQ] = useState("");
  const a = data.areas[area];
  const items = (list === "kind_changed" ? a.kind_changed ?? [] : a[list]);
  const side = list === "only_right" ? 1 : 0;
  const connId = data.connections[side];
  const needle = q.trim().toLowerCase();
  const rows = needle ? items.filter((i) => `${i.name} ${i.table ?? ""}`.toLowerCase().includes(needle)) : items;
  const label = AREAS.find(([k]) => k === area)![1];
  const title = list === "kind_changed" ? `${label} that changed kind` : `${label} only in ${data.names[side]}`;
  const subtitle = list === "kind_changed" ? "" : `(missing in ${data.names[1 - side]})`;
  const tableCol = ["columns", "indexes", "foreign_keys"].includes(area);

  const download = () => {
    const head = ["Name", "Detail", ...(tableCol ? ["Table"] : [])];
    const body = rows.map((i) => [i.name, i.type ?? i.columns ?? i.kind ?? i.table ?? "", ...(tableCol ? [i.table] : [])]);
    const blob = new Blob([csv([head, ...body])], { type: "text/csv" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${area}_${list === "only_left" ? "only_in_" + data.names[0] : list === "only_right" ? "only_in_" + data.names[1] : "kind_changed"}.csv`.replace(/\s+/g, "_");
    link.click();
    URL.revokeObjectURL(link.href);
  };

  return (
    <div className="diff-list">
      <div className="stats-drill-head">
        <h2>{title} <span className="muted">{subtitle}</span></h2>
        <span className="muted small">{fmt(rows.length)} item(s)</span>
        <button className="btn btn-sm btn-ghost" onClick={onClose}>Close ✕</button>
      </div>
      <div className="filters">
        <input type="search" placeholder="Filter by name…" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn btn-sm" onClick={download} disabled={!rows.length}>Download CSV</button>
      </div>
      {rows.length === 0 ? <EmptyState>Nothing here.</EmptyState> : (
        <div className="table-scroll stats-drill-list">
          <table className="compact">
            <thead>
              <tr>
                <th>{list === "kind_changed" ? data.names[0] : "Name"}</th>
                <th>{list === "kind_changed" ? "Change" : "Detail"}</th>
                {tableCol && <th>Compare the table</th>}
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 2000).map((i, n) => (
                <tr key={n}>
                  <td><ItemLink item={i} connId={connId} /></td>
                  <td>{list === "kind_changed" ? <span className="small">{i.kind}</span> : detail(area, i)}</td>
                  {tableCol && (
                    <td className="small">
                      <Link to={`/compare?l=${data.connections[0]}&r=${data.connections[1]}&lt=${encodeURIComponent(i.table)}`}>{i.table} side by side</Link>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length > 2000 && <p className="muted small card-pad">Showing 2,000 of {fmt(rows.length)}: download the CSV for all of them.</p>}
        </div>
      )}
    </div>
  );
}

export default function DifferenceReport({ connections }: { connections: Connection[] }) {
  const oracles = connections.filter((c) => c.kind === "oracle");
  const pgs = connections.filter((c) => c.kind === "postgres");
  const [leftId, setLeftId] = useState("");
  const [rightId, setRightId] = useState("");
  const left = leftId || oracles[0]?.id || "";
  const right = rightId || pgs[0]?.id || "";
  const ls = useSnapshot(left);
  const rs = useSnapshot(right);
  const report = useQuery({
    queryKey: ["inventory-diff", ls?.id, rs?.id],
    queryFn: () => catalogApi.inventoryDiff(ls!.id, rs!.id),
    enabled: !!ls && !!rs,
    staleTime: Infinity,
  });
  const [open, setOpen] = useState<{ area: InventoryArea; list: ListKind } | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const show = (area: InventoryArea, list: ListKind) => {
    setOpen({ area, list });
    setTimeout(() => listRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
  };
  const data = report.data;
  const notes = useMemo(() => (data ? AREAS.flatMap(([k]) => data.areas[k].notes) : []), [data]);

  if (!oracles.length || !pgs.length) return null;
  return (
    <section className="card table-card diff-report">
      <div className="stats-drill-head">
        <h2>Oracle vs PostgreSQL differences</h2>
        <span className="muted small">What exists on only one side, from each connection's last catalog read.</span>
      </div>
      <div className="filters">
        <select value={left} onChange={(e) => { setLeftId(e.target.value); setOpen(null); }} aria-label="Oracle connection">
          {oracles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <span className="muted">vs</span>
        <select value={right} onChange={(e) => { setRightId(e.target.value); setOpen(null); }} aria-label="PostgreSQL connection">
          {pgs.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </div>
      {(!ls || !rs) && <p className="muted small card-pad">Read both catalogs first (Refresh / Read catalog on their cards above).</p>}
      <ErrorBanner error={report.error} />
      {report.isLoading && ls && rs && <p className="muted small card-pad">Comparing…</p>}
      {data && (
        <>
          <div className="table-scroll">
            <table className="compact diff-report-table">
              <thead>
                <tr>
                  <th>Area</th>
                  <th className="num"><KindTag kind={data.kinds[0]} /> {data.names[0]}</th>
                  <th className="num"><KindTag kind={data.kinds[1]} /> {data.names[1]}</th>
                  <th className="num">Matched</th>
                  <th className="num">Only in {data.names[0]}<div className="muted small">missing in {data.names[1]}</div></th>
                  <th className="num">Only in {data.names[1]}<div className="muted small">missing in {data.names[0]}</div></th>
                </tr>
              </thead>
              <tbody>
                {AREAS.map(([key, label]) => {
                  const a = data.areas[key];
                  const cell = (list: ListKind, n: number, tone: string) => n === 0
                    ? <span className="sev-ok">0</span>
                    : <button className={`link-btn ${tone} ${open?.area === key && open.list === list ? "active" : ""}`} onClick={() => show(key, list)}>{fmt(n)}</button>;
                  return (
                    <tr key={key} className={a.only_left.length || a.only_right.length ? "" : "diff-row-same"}>
                      <td>
                        <strong>{label}</strong>
                        {a.kind_changed && a.kind_changed.length > 0 && (
                          <div><button className="link-btn small" onClick={() => show(key, "kind_changed")}>{a.kind_changed.length} changed kind</button></div>
                        )}
                      </td>
                      <td className="num">{fmt(a.left)}</td>
                      <td className="num">{fmt(a.right)}</td>
                      <td className="num">{fmt(a.matched)}</td>
                      <td className="num">{cell("only_left", a.only_left.length, "error-text")}</td>
                      <td className="num">{cell("only_right", a.only_right.length, "warn-text")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <ul className="muted small diff-notes">
            {[...new Set(notes)].map((n) => <li key={n}>{n}</li>)}
            <li>Tables are matched by schema and name ignoring case (else by a unique name). PostgreSQL partitions are counted with their parent table.</li>
          </ul>
          <div ref={listRef} />
          {open && <ItemList data={data} area={open.area} list={open.list} onClose={() => setOpen(null)} />}
        </>
      )}
    </section>
  );
}
