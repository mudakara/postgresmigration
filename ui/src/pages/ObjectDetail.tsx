import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { inventoryApi, type Dependency, type ObjectDetail } from "../api";
import SourceViewer, { buildMarks, type SourceViewerHandle } from "../components/SourceViewer";
import { CATEGORY_LABEL, CATEGORY_ORDER, SeverityChip, TierChip, UnsafeBadge } from "../components/inventory";
import { ErrorBanner, formatTime } from "../components/ui";

function DependencyList({ items, projectId, empty }: { items: Dependency[]; projectId: string; empty: string }) {
  if (!items.length) return <p className="muted small">{empty}</p>;
  return (
    <ul className="dep-list">
      {items.map((d) => (
        <li key={`${d.owner}.${d.name}.${d.object_type}.${d.link}`}>
          {d.object_id ? (
            <Link to={`/projects/${projectId}/objects/${d.object_id}`}>{d.owner}.{d.name}</Link>
          ) : (
            <span>{d.owner}.{d.name}</span>
          )}
          <span className="muted small"> {d.object_type?.toLowerCase()}{d.link ? ` @${d.link}` : ""}</span>
        </li>
      ))}
    </ul>
  );
}

function Metadata({ obj }: { obj: ObjectDetail }) {
  const trigger = obj.metadata.trigger;
  const state: { name: string; kind: string; line: number }[] = obj.metadata.package_state ?? [];
  if (!trigger && !state.length) return null;
  return (
    <section className="card">
      {trigger && (
        <>
          <h2>Trigger</h2>
          <dl className="meta">
            <dt>On</dt><dd className="mono">{trigger.table ?? trigger.base_object_type}</dd>
            <dt>Timing</dt><dd>{trigger.timing}</dd>
            <dt>Events</dt><dd>{trigger.events}</dd>
            <dt>Enabled</dt><dd>{trigger.enabled ? "Yes" : "No"}</dd>
          </dl>
        </>
      )}
      {state.length > 0 && (
        <>
          <h2>Package-level declarations</h2>
          <ul className="dep-list">
            {state.map((s) => (
              <li key={s.name}><span className="mono">{s.name}</span> <span className="muted small">{s.kind}, line {s.line}</span></li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export default function ObjectDetailPage() {
  const { projectId = "", objectId = "" } = useParams();
  const obj = useQuery({ queryKey: ["object", objectId], queryFn: () => inventoryApi.object(objectId) });
  const project = useQuery({ queryKey: ["project", projectId], queryFn: () => inventoryApi.project(projectId) });
  const rules = useQuery({ queryKey: ["rules"], queryFn: inventoryApi.rules, staleTime: Infinity });
  const [selected, setSelected] = useState<number | null>(null);
  const sourceRef = useRef<SourceViewerHandle>(null);

  const jumpTo = (line: number) => {
    setSelected(line);
    sourceRef.current?.scrollToLine(line);
  };

  if (obj.error) return <ErrorBanner error={obj.error} />;
  if (!obj.data) return <div className="card muted">Loading…</div>;
  const o = obj.data;
  const advice = new Map(rules.data?.map((r) => [r.code, r.advice]));
  const grouped = CATEGORY_ORDER.map((c) => ({ category: c, items: o.findings.filter((f) => f.category === c) })).filter((g) => g.items.length);
  const internalDeps = o.depends_on.filter((d) => d.object_id);
  const externalDeps = o.depends_on.filter((d) => !d.object_id);

  return (
    <>
      <div className="breadcrumb small">
        <Link to="/projects">Projects</Link> › <Link to={`/projects/${projectId}`}>{project.data?.name ?? "…"}</Link> ›{" "}
        <Link to={`/projects/${projectId}?tab=inventory`}>Inventory</Link> › {o.name}
      </div>
      <header className="page-header">
        <div>
          <h1><span className="muted">{o.owner}.</span>{o.name}</h1>
          <p className="muted object-meta">
            <span className="tag">{o.object_type}</span>
            <TierChip tier={o.complexity} />
            {o.unsafe_to_execute && <UnsafeBadge />}
            {o.status !== "VALID" && <span className="badge badge-bad">✗ Invalid in Oracle</span>}
            {o.wrapped && <span className="badge badge-bad">✗ Wrapped</span>}
            {o.removed_at && <span className="badge badge-neutral">Removed from Oracle {formatTime(o.removed_at)}</span>}
            <span className="small">
              {o.line_count.toLocaleString()} lines · effort {o.effort}
              {o.ora2pg_cost != null ? " (Ora2Pg)" : " (heuristic)"} · last DDL {formatTime(o.oracle_last_ddl)}
            </span>
          </p>
        </div>
      </header>

      <div className="object-layout">
        <aside className="object-side">
          <section className="card">
            <h2>Findings</h2>
            {grouped.length === 0 && <p className="muted small">Nothing flagged.</p>}
            {grouped.map((g) => (
              <div key={g.category} className="finding-group">
                <h3>{CATEGORY_LABEL[g.category]}</h3>
                {g.items.map((f) => (
                  <div key={f.code} className="finding">
                    <div className="finding-head"><SeverityChip severity={f.severity} /> <strong>{f.title}</strong></div>
                    {f.detail && <div className="small">{f.detail}</div>}
                    <div className="muted small">{advice.get(f.code)}</div>
                    {f.lines.length > 0 && (
                      <div className="line-chips">
                        {f.lines.map((line) => (
                          <button key={line} className={`chip chip-btn ${selected === line ? "active" : ""}`} onClick={() => jumpTo(line)}>
                            L{line}
                          </button>
                        ))}
                        {f.count > f.lines.length && <span className="muted small">+{f.count - f.lines.length} more</span>}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </section>

          <section className="card">
            <h2>Depends on</h2>
            <DependencyList items={internalDeps} projectId={projectId} empty="No objects from the inventory." />
            {externalDeps.length > 0 && (
              <>
                <h3>Outside the inventory</h3>
                <DependencyList items={externalDeps} projectId={projectId} empty="" />
              </>
            )}
            <h2 className="spaced">Used by</h2>
            <DependencyList items={o.used_by} projectId={projectId} empty="Nothing in the inventory references it." />
          </section>
          <Metadata obj={o} />
        </aside>

        <div className="object-main">
          {o.routines.length > 0 && (
            <section className="card table-card">
              <h2 className="card-pad">Routines</h2>
              <table>
                <thead><tr><th>Signature</th><th>Kind</th><th className="num">Ora2Pg cost</th></tr></thead>
                <tbody>
                  {o.routines.map((r) => (
                    <tr key={r.id}>
                      <td className="mono small">{r.signature}</td>
                      <td className="small">{r.kind.toLowerCase()}{r.overload ? ` (overload ${r.overload})` : ""}</td>
                      <td className="num">{r.ora2pg_cost ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
          <section className="card table-card">
            <div className="card-pad source-head">
              <h2>Oracle source</h2>
              <span className="muted small">Flagged lines are marked; click a line reference to jump to it.</span>
            </div>
            {o.source ? (
              <SourceViewer
                ref={sourceRef}
                source={o.source}
                marks={buildMarks(o.findings.map((f) => ({ lines: f.lines, severity: f.severity, title: f.title })))}
                selected={selected}
                onSelect={setSelected}
              />
            ) : (
              <p className="muted card-pad">No source available.</p>
            )}
          </section>
        </div>
      </div>
    </>
  );
}
