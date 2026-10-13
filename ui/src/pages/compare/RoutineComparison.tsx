import { useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import {
  catalogApi,
  type RoutineDifference,
  type RoutineParam,
  type RoutineRow,
  type RoutineSide,
} from "../../api";
import SourceViewer, { buildMarks, type SourceViewerHandle } from "../../components/SourceViewer";
import { ErrorBanner } from "../../components/ui";

type Status = RoutineRow["status"];
const STATUS: Record<Status, { label: string; cls: string; icon: string }> = {
  error: { label: "Problem", cls: "diff-error", icon: "✕" },
  warning: { label: "Differs", cls: "diff-warning", icon: "!" },
  info: { label: "Notes", cls: "diff-info", icon: "i" },
  same: { label: "Same", cls: "diff-same", icon: "=" },
};
const SEVERITY_STATUS = { high: "error", medium: "warning", low: "info" } as const;
const CATEGORY: Record<RoutineDifference["category"], string> = {
  signature: "Parameters", tables: "Tables used", code: "Code checks",
};

function routineLabel(r: RoutineSide | null) {
  if (!r) return "—";
  return `${r.schema}.${r.name}`;
}

function StatusBadge({ status }: { status: Status }) {
  const s = STATUS[status];
  return <span className={`diff-badge ${s.cls}`}>{s.icon} {s.label}</span>;
}

function SeverityBadge({ severity }: { severity: RoutineDifference["severity"] }) {
  return <StatusBadge status={SEVERITY_STATUS[severity]} />;
}

// --- one routine pair in detail ---------------------------------------------------------------

function ParamTable({ row, names }: { row: RoutineRow; names: [string, string] }) {
  const l = row.left?.signature?.arguments ?? [];
  const r = row.right?.signature?.arguments ?? [];
  const n = Math.max(l.length, r.length);
  const lr = row.left?.signature?.return_type ?? row.left?.signature?.result;
  const rr = row.right?.signature?.return_type ?? row.right?.signature?.result;
  if (!n && !lr && !rr) return <p className="muted small">No parameters on either side.</p>;
  const cell = (a: RoutineParam | undefined) => a
    ? <><span className="mono">{a.name ?? "(unnamed)"}</span> <span className="muted small">{a.mode}</span> <span className="mono small">{a.type}</span>{a.defaulted ? <span className="muted small"> DEFAULT</span> : null}</>
    : <span className="muted">—</span>;
  const differs = (a?: RoutineParam, b?: RoutineParam) =>
    !a || !b || (a.name ?? "").toLowerCase() !== (b.name ?? "").toLowerCase() || a.mode !== b.mode;
  return (
    <div className="table-scroll">
      <table className="compact cmp-table">
        <thead>
          <tr><th className="num">#</th><th className="cmp-left">{names[0]}</th><th className="cmp-right">{names[1]}</th></tr>
        </thead>
        <tbody>
          {Array.from({ length: n }, (_, i) => (
            <tr key={i} className={differs(l[i], r[i]) ? "diff-row diff-row-warning" : ""}>
              <td className="num muted small">{i + 1}</td>
              <td className="cmp-left">{cell(l[i])}</td>
              <td className="cmp-right">{cell(r[i])}</td>
            </tr>
          ))}
          {(lr || rr) && (
            <tr>
              <td className="num muted small">↩</td>
              <td className="cmp-left"><span className="muted small">returns</span> <span className="mono small">{lr ?? "—"}</span></td>
              <td className="cmp-right"><span className="muted small">returns</span> <span className="mono small">{rr ?? "—"}</span></td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function SourcePane({ side, lines }: { side: RoutineSide | null; lines: { lines: number[]; severity: RoutineDifference["severity"]; title: string }[] }) {
  const code = useQuery({ queryKey: ["catalog-code", side?.code_id], queryFn: () => catalogApi.code(side!.code_id), enabled: !!side });
  const viewer = useRef<SourceViewerHandle>(null);
  const marks = useMemo(() => buildMarks(lines), [lines]);
  const first = side?.range?.[0] ?? lines.flatMap((l) => l.lines)[0];
  useEffect(() => {
    if (code.data && first) setTimeout(() => viewer.current?.scrollToLine(first), 50);
  }, [code.data, first]);
  if (!side) return <div className="cmp-source-missing muted small">Not present on this side</div>;
  if (code.error) return <ErrorBanner error={code.error} />;
  if (!code.data) return <div className="muted small">Loading source…</div>;
  if (!code.data.source) return <div className="muted small">Source not visible to this account</div>;
  return (
    <div className="routine-source">
      <div className="muted small mono">{routineLabel(side)}{side.range ? ` (package member, lines ${side.range[0]}–${side.range[1]})` : ""}</div>
      <SourceViewer ref={viewer} source={code.data.source} marks={marks} range={side.range ?? null} maxHeight="460px" />
    </div>
  );
}

function RoutineDetail({ row, names }: { row: RoutineRow; names: [string, string] }) {
  const grouped = (["signature", "tables", "code"] as const)
    .map((c) => ({ c, items: row.differences.filter((d) => d.category === c) }))
    .filter((g) => g.items.length);
  const sev = { high: "high", medium: "medium", low: "low" } as const;
  const leftLines = row.differences.filter((d) => d.left_lines.length).map((d) => ({ lines: d.left_lines, severity: sev[d.severity], title: d.title }));
  const rightLines = row.differences.filter((d) => d.right_lines.length).map((d) => ({ lines: d.right_lines, severity: sev[d.severity], title: d.title }));
  return (
    <div className="routine-detail">
      <div className="routine-detail-grid">
        <div>
          <h4>Differences ({row.differences.length})</h4>
          {grouped.length === 0 && <p className="muted small">No differences found.</p>}
          {grouped.map((g) => (
            <div key={g.c} className="routine-diff-group">
              <div className="small"><strong>{CATEGORY[g.c]}</strong></div>
              {g.items.map((d, i) => (
                <div key={i} className="finding">
                  <div className="finding-head"><SeverityBadge severity={d.severity} /><strong className="small">{d.title}</strong></div>
                  <div className="small">{d.detail}</div>
                  {(d.left_lines.length > 0 || d.right_lines.length > 0) && (
                    <div className="muted small">
                      {d.left_lines.length > 0 && <>{names[0]} lines {d.left_lines.slice(0, 12).join(", ")}</>}
                      {d.left_lines.length > 0 && d.right_lines.length > 0 && " · "}
                      {d.right_lines.length > 0 && <>{names[1]} lines {d.right_lines.slice(0, 12).join(", ")}</>}
                    </div>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
        <div>
          <h4>Parameters</h4>
          <ParamTable row={row} names={names} />
        </div>
      </div>
      <div className="cmp-sources">
        <SourcePane side={row.left} lines={leftLines} />
        <SourcePane side={row.right} lines={rightLines} />
      </div>
    </div>
  );
}

// --- the section ----------------------------------------------------------------------------------

type View = "routines" | "differences";
type Filter = "all" | "problems" | "differences";

function csv(rows: (string | number)[][]) {
  return rows.map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(",")).join("\n");
}

export default function RoutineComparison({ leftTableId, rightTableId, tableLabel }: {
  leftTableId: string; rightTableId: string; tableLabel: string;
}) {
  const data = useQuery({
    queryKey: ["routine-compare", leftTableId, rightTableId],
    queryFn: () => catalogApi.compareRoutines(leftTableId, rightTableId),
    staleTime: Infinity,
  });
  const [view, setView] = useState<View>("routines");
  const [filter, setFilter] = useState<Filter>("problems");
  const [category, setCategory] = useState<"" | RoutineDifference["category"]>("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const result = data.data;
  const names = result?.names ?? ["Left", "Right"];
  const needle = q.trim().toLowerCase();
  const rows = (result?.routines ?? []).map((r, i) => ({ r, i })).filter(({ r }) =>
    (filter === "all" || (filter === "problems" ? r.status === "error" : r.status === "error" || r.status === "warning"))
    && (!category || r.differences.some((d) => d.category === category))
    && (!needle || `${routineLabel(r.left)} ${routineLabel(r.right)}`.toLowerCase().includes(needle)));
  const flat = rows.flatMap(({ r, i }) => r.differences
    .filter((d) => !category || d.category === category)
    .filter((d) => filter === "all" || (filter === "problems" ? d.severity === "high" : d.severity !== "low"))
    .map((d) => ({ r, i, d })));

  const download = () => {
    const lines = [["Table", names[0], names[1], "Routine status", "Category", "Severity", "Difference", "Detail", `${names[0]} lines`, `${names[1]} lines`]];
    for (const { r, d } of flat) {
      lines.push([tableLabel, routineLabel(r.left), routineLabel(r.right), STATUS[r.status].label, CATEGORY[d.category], d.severity,
        d.title, d.detail, d.left_lines.join(" "), d.right_lines.join(" ")]);
    }
    const blob = new Blob([csv(lines)], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `routine_differences_${tableLabel.replace(/[^\w.]+/g, "_")}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const c = result?.counts;
  return (
    <details className="card table-card cmp-section" open>
      <summary className="cmp-section-head">
        <strong>Stored procedure comparison</strong>
        <span className="muted small">
          {result ? `${result.routines.length} routine(s) using this table, compared one to one` : "parameters, tables used and code checks"}
        </span>
        {c && c.error > 0 && <span className="diff-badge diff-error">✕ {c.error}</span>}
        {c && c.warning > 0 && <span className="diff-badge diff-warning">! {c.warning}</span>}
        {c && c.info > 0 && <span className="diff-badge diff-info">i {c.info}</span>}
        {c && c.same > 0 && <span className="diff-badge diff-same">= {c.same}</span>}
      </summary>
      <ErrorBanner error={data.error} />
      {data.isLoading && <p className="muted small card-pad">Comparing every procedure and function…</p>}
      {result && result.routines.length === 0 && <p className="muted small card-pad">No procedure or function uses this table on either side.</p>}
      {result && result.routines.length > 0 && (
        <>
          <div className="filters">
            <div className="chips">
              <button className={`chip chip-btn ${view === "routines" ? "active" : ""}`} onClick={() => setView("routines")}>By routine</button>
              <button className={`chip chip-btn ${view === "differences" ? "active" : ""}`} onClick={() => setView("differences")}>
                List differences
              </button>
            </div>
            <div className="chips">
              {([["problems", `With problems (${c?.error ?? 0})`], ["differences", `With differences (${(c?.error ?? 0) + (c?.warning ?? 0)})`], ["all", `All (${result.routines.length})`]] as [Filter, string][]).map(([k, label]) => (
                <button key={k} className={`chip chip-btn ${filter === k ? "active" : ""}`} onClick={() => setFilter(k)}>{label}</button>
              ))}
            </div>
            <select value={category} onChange={(e) => setCategory(e.target.value as typeof category)} aria-label="Category">
              <option value="">All kinds of difference</option>
              {Object.entries(CATEGORY).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
            </select>
            <input type="search" placeholder="Filter by routine name…" value={q} onChange={(e) => setQ(e.target.value)} />
            {view === "differences" && (
              <button className="btn btn-sm" onClick={download} disabled={!flat.length}>Download CSV ({flat.length})</button>
            )}
          </div>

          {view === "routines" ? (
            rows.length === 0 ? <p className="muted small card-pad">No routines in this filter.</p> : (
              <div className="table-scroll routine-list">
                <table className="compact cmp-table">
                  <thead>
                    <tr><th className="cmp-left">{names[0]}</th><th className="cmp-status-col">Status</th><th className="cmp-right">{names[1]}</th><th>Differences</th></tr>
                  </thead>
                  <tbody>
                    {rows.map(({ r, i }) => (
                      <Fragment key={i}>
                        <tr className={`clickable diff-row diff-row-${r.status} ${open === i ? "selected" : ""}`} onClick={() => setOpen(open === i ? null : i)}>
                          <td className="cmp-left mono">{r.left ? <><span className="muted">{r.left.schema}.</span>{r.left.name}</> : <span className="cmp-absent">— missing —</span>}</td>
                          <td className="cmp-status-col"><StatusBadge status={r.status} /></td>
                          <td className="cmp-right mono">{r.right ? <><span className="muted">{r.right.schema}.</span>{r.right.name}</> : <span className="cmp-absent">— missing —</span>}</td>
                          <td className="small">
                            {r.differences.length === 0 ? <span className="muted">none</span> : (
                              <>
                                <strong>{r.differences.length}</strong>{" "}
                                <span className="muted">{r.differences[0].title}{r.differences.length > 1 ? ` + ${r.differences.length - 1} more` : ""}</span>
                              </>
                            )}
                          </td>
                        </tr>
                        {open === i && (
                          <tr className="expanded"><td colSpan={4}><RoutineDetail row={r} names={names} /></td></tr>
                        )}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : (
            flat.length === 0 ? <p className="muted small card-pad">No differences in this filter.</p> : (
              <div className="table-scroll routine-list">
                <table className="compact">
                  <thead>
                    <tr><th>Routine</th><th>Kind</th><th>Severity</th><th>Difference</th><th>Lines</th></tr>
                  </thead>
                  <tbody>
                    {flat.slice(0, 1000).map(({ r, i, d }, k) => (
                      <tr key={k} className={`clickable diff-row diff-row-${SEVERITY_STATUS[d.severity]}`}
                        onClick={() => { setView("routines"); setOpen(i); }} title="Open this routine side by side">
                        <td className="mono small">{routineLabel(r.left)}{r.right && r.left ? <span className="muted"> → {routineLabel(r.right)}</span> : r.right ? routineLabel(r.right) : null}</td>
                        <td className="small">{CATEGORY[d.category]}</td>
                        <td><SeverityBadge severity={d.severity} /></td>
                        <td className="small"><strong>{d.title}</strong><div>{d.detail}</div></td>
                        <td className="small muted">
                          {d.left_lines.length > 0 && <div>{names[0]}: {d.left_lines.slice(0, 8).join(", ")}</div>}
                          {d.right_lines.length > 0 && <div>{names[1]}: {d.right_lines.slice(0, 8).join(", ")}</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {flat.length > 1000 && <p className="muted small card-pad">Showing 1,000 of {flat.length.toLocaleString()}: download the CSV for all of them.</p>}
              </div>
            )
          )}
        </>
      )}
    </details>
  );
}
