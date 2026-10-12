import { useQuery } from "@tanstack/react-query";
import { Fragment } from "react";
import { Link, useNavigate } from "react-router-dom";

import { inventoryApi } from "../../api";
import {
  BarList,
  CATEGORY_HELP,
  CATEGORY_LABEL,
  CATEGORY_ORDER,
  SeverityChip,
  StatTile,
  TIER_LABEL,
  TierChip,
  UnsafeBadge,
} from "../../components/inventory";
import { ErrorBanner, formatTime } from "../../components/ui";

const fmt = (n: number) => n.toLocaleString();

export default function ReportTab({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const report = useQuery({ queryKey: ["report", projectId], queryFn: () => inventoryApi.report(projectId) });

  const inventory = (filters: Record<string, string>) =>
    navigate(`/projects/${projectId}?${new URLSearchParams({ tab: "inventory", ...filters })}`);

  if (report.error) return <ErrorBanner error={report.error} />;
  if (!report.data) return <div className="card muted">Loading report…</div>;
  const r = report.data;
  const scan = r.last_successful_scan;
  const blockers = r.totals.wrapped + r.totals.invalid;
  const findingsByCategory = CATEGORY_ORDER.map((c) => ({ category: c, rows: r.findings.filter((f) => f.category === c) }));

  return (
    <div className="report">
      {scan?.warnings.map((w) => <div key={w} className="banner banner-warn">{w}</div>)}

      <section className="cards">
        <StatTile
          label="Objects"
          value={fmt(r.totals.objects)}
          sub={`${fmt(r.totals.lines)} lines of source · ${r.by_type.length} object types`}
        />
        <StatTile
          label="Callable routines"
          value={fmt(r.totals.routines.standalone + r.totals.routines.package_members)}
          sub={`${fmt(r.totals.routines.standalone)} standalone · ${fmt(r.totals.routines.package_members)} in packages`}
        />
        <StatTile
          label="Estimated effort"
          value={`${fmt(r.effort.estimated_hours)} h`}
          sub={
            r.effort.ora2pg_human_days != null
              ? `Ora2Pg: ${r.effort.ora2pg_human_days} person-day(s) · ${fmt(r.effort.total_units)} units`
              : `${fmt(r.effort.total_units)} units (heuristic)`
          }
        />
        <StatTile
          label="Unsafe to auto-run"
          value={fmt(r.totals.unsafe_to_execute)}
          sub="Commit, autonomous txn, external I/O, or calls code that does"
          tone={r.totals.unsafe_to_execute ? "warn" : undefined}
        />
        <StatTile
          label="Blockers"
          value={fmt(blockers)}
          sub={`${r.totals.wrapped} wrapped · ${r.totals.invalid} invalid in Oracle`}
          tone={blockers ? "bad" : undefined}
        />
      </section>

      <div className="two-col">
        <section className="card">
          <h2>Complexity</h2>
          <p className="muted small">
            Effort per object: Ora2Pg's cost estimate where available
            {r.effort.objects_costed_by_heuristic > 0 &&
              ` (${r.effort.objects_costed_by_heuristic} ${r.effort.objects_costed_by_heuristic === 1 ? "object uses" : "objects use"} a size-and-findings heuristic)`}.
            One unit ≈ {r.effort.unit_minutes} minutes.
          </p>
          <BarList
            data={r.complexity.map((t) => ({
              key: t.tier, label: TIER_LABEL[t.tier], value: t.count, hint: `${fmt(t.effort)} effort units`,
            }))}
            onSelect={(tier) => inventory({ complexity: tier })}
          />
        </section>

        <section className="card table-card">
          <h2 className="card-pad">Objects by type</h2>
          <table>
            <thead>
              <tr><th>Type</th><th className="num">Objects</th><th className="num">Lines</th><th className="num">Invalid</th><th className="num">Effort</th></tr>
            </thead>
            <tbody>
              {r.by_type.map((t) => (
                <tr key={t.object_type} className="clickable" onClick={() => inventory({ type: t.object_type })}>
                  <td className="cell-title">{t.object_type}</td>
                  <td className="num">{fmt(t.count)}</td>
                  <td className="num">{fmt(t.lines)}</td>
                  <td className="num">{t.invalid || "—"}</td>
                  <td className="num">{fmt(t.effort)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      <section className="card table-card">
        <div className="card-pad">
          <h2>Findings</h2>
          <p className="muted small">Constructs detected in the Oracle source. Click a row to list the affected objects.</p>
        </div>
        <table className="findings-table">
          <thead>
            <tr><th>Severity</th><th>Finding</th><th className="num">Objects</th><th className="num">Occurrences</th></tr>
          </thead>
          <tbody>
            {findingsByCategory.map(({ category, rows }) => (
              <Fragment key={category}>
                <tr className="group-row">
                  <td colSpan={4}>
                    <strong>{CATEGORY_LABEL[category]}</strong>{" "}
                    <span className="muted small">— {CATEGORY_HELP[category]}</span>
                    {rows.length === 0 && <span className="muted small"> None found.</span>}
                  </td>
                </tr>
                {rows.map((f) => (
                  <tr key={f.code} className="clickable" onClick={() => inventory({ finding: f.code })}>
                    <td><SeverityChip severity={f.severity} /></td>
                    <td>
                      <div className="cell-title">{f.title} {f.unsafe && <UnsafeBadge compact />}</div>
                      <div className="muted small advice">{f.advice}</div>
                    </td>
                    <td className="num">{fmt(f.objects)}</td>
                    <td className="num">{fmt(f.occurrences)}</td>
                  </tr>
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </section>

      <div className="two-col">
        <section className="card table-card">
          <h2 className="card-pad">Highest effort</h2>
          <table>
            <tbody>
              {r.top_effort.map((o) => (
                <tr key={o.id}>
                  <td>
                    <Link to={`/projects/${projectId}/objects/${o.id}`} className="obj-link">{o.owner}.{o.name}</Link>
                    <div className="muted small">{o.object_type}</div>
                  </td>
                  <td><TierChip tier={o.complexity} /> {o.unsafe_to_execute && <UnsafeBadge compact />}</td>
                  <td className="num">{fmt(o.effort)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <h2>Oracle-supplied packages used</h2>
          {r.sys_packages.length === 0 ? (
            <p className="muted small">None beyond STANDARD.</p>
          ) : (
            <div className="chips">
              {r.sys_packages.map((p) => (
                <span key={p.name} className="chip mono">{p.name} <span className="muted">×{p.objects}</span></span>
              ))}
            </div>
          )}
          {r.by_owner.length > 1 && (
            <>
              <h3>By schema</h3>
              <BarList
                data={r.by_owner.map((o) => ({ key: o.owner, label: o.owner, value: o.count, hint: `${fmt(o.lines)} lines` }))}
                onSelect={(owner) => inventory({ owner })}
              />
            </>
          )}
          {scan && (
            <>
              <h3>Scan</h3>
              <dl className="meta">
                <dt>Finished</dt><dd>{formatTime(scan.finished_at)}</dd>
                <dt>Oracle</dt><dd>{scan.oracle_version} via {scan.dictionary}_* views</dd>
                <dt>Schemas</dt><dd className="mono">{scan.schemas.join(", ")}</dd>
                {scan.ora2pg_reports.map((o) => (
                  <Fragment key={o.schema}>
                    <dt>Ora2Pg {o.schema}</dt>
                    <dd>{o.ok ? `cost ${o.total_cost}, level ${o.migration_level}` : <span className="error-text">failed</span>}</dd>
                  </Fragment>
                ))}
              </dl>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
